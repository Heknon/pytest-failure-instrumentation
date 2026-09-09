"""A hermetic fix: seal the whole process against repr blow-ups, in one call.

    import hermetic; hermetic.seal()

No model changes, no migration, no hunting for the duplicate reference. Covers
every pydantic model in the process, yours and your dependencies'.

The rule it enforces: **one top-level repr may spend at most `limit` characters.**
Depth, sharing, payload size and object count stop mattering, because the budget
is on the work rather than on the shape of the data.

Deliberately NOT sealed: model_dump_json(). Truncating a serialiser silently
corrupts real data - so that path gets a loud guard instead, via guard_dumps().
"""
from __future__ import annotations

import threading
import warnings

from pydantic import BaseModel

__all__ = ["seal", "unseal", "guard_dumps", "ReprBudgetExceeded"]

_fuel = threading.local()
_originals: dict[str, object] = {}


class ReprBudgetExceeded(RuntimeError):
    """Raised by guard_dumps() when a model graph would explode on serialisation."""


def _spend(n: int) -> None:
    _fuel.left -= n


def _placeholder(value) -> str:
    return "%s(...)" % type(value).__name__


def _bounded(value) -> str:
    if isinstance(value, BaseModel):
        return repr(value)                       # recurses; spends from the budget
    if isinstance(value, (str, bytes)):
        room = max(_fuel.left, 0)
        piece = repr(value[: room + 1])          # slice BEFORE repr, never after
    elif isinstance(value, (list, tuple, set, frozenset, dict)):
        piece = "<%s of %d>" % (type(value).__name__, len(value))
    else:
        try:
            piece = repr(value)
        except Exception:
            return "<unprintable %s>" % type(value).__name__
    if len(piece) > _fuel.left:
        piece = piece[: max(_fuel.left, 0)] + "..."
    _spend(len(piece))
    return piece


def seal(limit: int = 4096, max_depth: int = 12) -> None:
    """Install the budget. Idempotent.

    `limit` bounds the characters one top-level repr may spend. `max_depth`
    bounds how far it may descend, and is NOT optional: fuel alone bounds width
    but not descent, so a model with a cheap per-level cost (short class name,
    one field) keeps recursing until Python's own limit and raises
    RecursionError inside your exception handler. Raising `limit` makes that
    worse, not better, because more fuel buys a deeper descent.
    """
    if _originals:
        return
    _originals["__repr__"] = BaseModel.__repr__
    _originals["__str__"] = BaseModel.__str__
    _originals["__rich_repr__"] = BaseModel.__rich_repr__

    def __repr__(self) -> str:
        top = not getattr(_fuel, "active", False)
        if top:
            _fuel.active, _fuel.left, _fuel.depth = True, limit, 0
        try:
            if _fuel.depth >= max_depth:
                placeholder = _placeholder(self)
                _spend(len(placeholder))
                return placeholder
            if _fuel.left <= 0:
                placeholder = _placeholder(self)
                _spend(len(placeholder))
                return placeholder
            name = type(self).__name__
            _spend(len(name) + 2)                      # "Cls(" and ")"
            parts = []
            for key, value in BaseModel.__repr_args__(self):
                if _fuel.left <= 0:
                    _spend(3)
                    parts.append("...")
                    break
                if parts:
                    _spend(2)                          # ", "
                if key:
                    _spend(len(key) + 1)               # "key="
                _fuel.depth += 1
                try:
                    piece = _bounded(value)
                finally:
                    _fuel.depth -= 1
                parts.append("%s=%s" % (key, piece) if key else piece)
            out = "%s(%s)" % (name, ", ".join(parts))
            # the fuel gauge bounds the WORK; this bounds the OUTPUT exactly,
            # mopping up the few characters a level can overshoot by on unwind.
            if top and len(out) > limit:
                out = out[: limit - 3] + "..."
            return out
        finally:
            if top:
                _fuel.active = False

    def __rich_repr__(self):
        # rich drives its own recursion through this protocol and never calls
        # __repr__, so hand it already-flattened values or it walks the graph.
        for name, value in BaseModel.__repr_args__(self):
            if isinstance(value, BaseModel):
                value = _placeholder(value)
            elif isinstance(value, (list, tuple, set, frozenset, dict)):
                value = "<%s of %d>" % (type(value).__name__, len(value))
            yield (name, value) if name is not None else value

    BaseModel.__repr__ = __repr__
    BaseModel.__str__ = __repr__
    BaseModel.__rich_repr__ = __rich_repr__


def unseal() -> None:
    if not _originals:
        return
    BaseModel.__repr__ = _originals.pop("__repr__")
    BaseModel.__str__ = _originals.pop("__str__")
    BaseModel.__rich_repr__ = _originals.pop("__rich_repr__")
    _originals.clear()


def guard_dumps(model, limit: int = 5_000_000, raise_on_exceed: bool = True):
    """Serialise, but refuse to hang doing it.

    The serialiser is NOT budgeted - silently truncating real output is worse
    than the hang. Instead the cost is predicted first (O(V+E), microseconds)
    and reported loudly.
    """
    from fixes import predicted_repr_size, shared_nodes

    predicted = predicted_repr_size(model)
    if predicted > limit:
        worst = shared_nodes(model)[:3]
        detail = ", ".join("%s x%s" % (name, "{:,}".format(n)) for n, name, _ in worst)
        message = ("serialising this %s would expand to ~%s bytes; shared nodes: %s"
                   % (type(model).__name__, "{:,}".format(predicted), detail or "none"))
        if raise_on_exceed:
            raise ReprBudgetExceeded(message)
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    return model.model_dump_json()
