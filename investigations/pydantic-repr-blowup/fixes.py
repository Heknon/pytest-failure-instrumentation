"""Drop-in tooling for the repr-blowup class of bug.

Everything here is O(V+E) in the object graph, so it stays cheap even when the
repr it is measuring would be exponential.

    from fixes import predicted_repr_size, shared_nodes, BoundedReprModel

Diagnosing a live incident:      shared_nodes(state)
Guarding against a regression:   assert predicted_repr_size(state) < 100_000
"""
from __future__ import annotations

from pydantic import BaseModel

__all__ = ["predicted_repr_size", "shared_nodes", "assert_repr_bounded",
           "BoundedReprModel", "bounded_repr", "install_safe_formatter"]


def _children(obj):
    if isinstance(obj, BaseModel):
        return [v for _, v in obj.__repr_args__()]
    if isinstance(obj, (list, tuple, set, frozenset)):
        return list(obj)
    if isinstance(obj, dict):
        return list(obj.keys()) + list(obj.values())
    return []


def _walk(root):
    """Visit every node once. Returns (objects_by_id, children_by_id, dfs_order)."""
    objs, kids, order, seen = {}, {}, [], set()
    stack = [root]
    while stack:
        obj = stack.pop()
        key = id(obj)
        if key in seen:
            continue
        seen.add(key)
        objs[key] = obj
        children = _children(obj)
        kids[key] = [id(c) for c in children]
        order.append(key)
        stack.extend(children)
    return objs, kids, order


def predicted_repr_size(root) -> int:
    """Exactly how many bytes repr(root) would produce, without building it.

    Memoised on id(), so a graph whose repr is 64 TB is measured in microseconds.
    """
    memo = {}

    def size(obj):
        key = id(obj)
        if key in memo:
            return memo[key]
        memo[key] = 0  # cycle guard: a cycle would be infinite, report it as 0
        if isinstance(obj, BaseModel):
            total = len(type(obj).__name__) + 2
            for i, (name, value) in enumerate(obj.__repr_args__()):
                if name:
                    total += len(name) + 1
                total += size(value)
                if i:
                    total += 2
        elif isinstance(obj, (list, tuple)):
            total = 2 + sum(size(v) for v in obj) + 2 * max(len(obj) - 1, 0)
        elif isinstance(obj, dict):
            total = 2 + sum(size(k) + size(v) + 2 for k, v in obj.items())
        else:
            total = len(repr(obj))
        memo[key] = total
        return total

    return size(root)


def shared_nodes(root):
    """Nodes reachable by more than one path, worst first.

    Returns [(expansions, type_name, repr_prefix), ...]. `expansions` is how many
    times that single object gets written out by repr()/model_dump_json().
    """
    objs, kids, order = _walk(root)

    # topological order by DFS finishing time, then propagate path counts
    counts = {key: 0 for key in objs}
    counts[id(root)] = 1
    indeg = {key: 0 for key in objs}
    for key in objs:
        for child in kids[key]:
            indeg[child] += 1
    ready = [k for k in objs if indeg[k] == 0]
    topo, seen = [], set()
    while ready:
        key = ready.pop()
        if key in seen:
            continue
        seen.add(key)
        topo.append(key)
        for child in kids[key]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    for key in topo:
        for child in kids[key]:
            counts[child] += counts[key]

    out = []
    for key, n in counts.items():
        if n > 1 and isinstance(objs[key], BaseModel):
            obj = objs[key]
            label = "%s(%s)" % (type(obj).__name__,
                                ", ".join(str(k) for k, _ in obj.__repr_args__())[:60])
            out.append((n, type(obj).__name__, label))
    out.sort(reverse=True)
    return out


def assert_repr_bounded(root, limit=100_000):
    """Fail loudly, and cheaply, if a model graph has become a repr bomb."""
    size = predicted_repr_size(root)
    if size <= limit:
        return
    worst = shared_nodes(root)[:5]
    detail = "\n".join("    %s expansions  %s" % ("{:,}".format(n), label)
                       for n, _, label in worst)
    raise AssertionError(
        "repr() of this %s would be %s bytes (limit %s).\n"
        "  Shared nodes causing it (each written out once per path to it):\n%s\n"
        "  Fix the duplicate reference, or mark the redundant field "
        "Field(exclude=True, repr=False)."
        % (type(root).__name__, "{:,}".format(size), "{:,}".format(limit), detail))


class BoundedReprModel(BaseModel):
    """Base class whose repr never recurses into sub-models.

    Covers repr(), logging, better_exceptions and sentry in one edit. It does
    NOT cover model_dump_json() (pydantic-core, separate walk) or rich's
    show_locals (rich introspects attributes and ignores __repr__).
    """

    def __repr__(self) -> str:
        parts = []
        for name, value in self.__repr_args__():
            if isinstance(value, BaseModel):
                shown = "%s(...)" % type(value).__name__
            elif isinstance(value, (list, tuple, set, dict)):
                shown = "<%s of %d>" % (type(value).__name__, len(value))
            else:
                shown = repr(value)
                if len(shown) > 200:
                    shown = shown[:200] + "..."
            parts.append("%s=%s" % (name, shown) if name else shown)
        return "%s(%s)" % (type(self).__name__, ", ".join(parts))

    __str__ = __repr__


def bounded_repr(value, max_depth=2, budget=2000, _depth=0):
    """A repr that bounds the WORK, not just the output."""
    if isinstance(value, BaseModel):
        if _depth >= max_depth:
            return "%s(...)" % type(value).__name__
        parts = []
        for name, sub in value.__repr_args__():
            parts.append("%s=%s" % (name, bounded_repr(sub, max_depth, budget, _depth + 1)))
            if sum(map(len, parts)) > budget:
                parts.append("...")
                break
        return "%s(%s)" % (type(value).__name__, ", ".join(parts))
    try:
        text = repr(value[:budget]) if isinstance(value, (str, bytes)) else repr(value)
    except Exception:
        return "<unprintable %s>" % type(value).__name__
    return text if len(text) <= budget else text[:budget] + "..."


def install_safe_formatter():
    """Last resort if better_exceptions must stay on: bound its format_value."""
    import better_exceptions

    def format_value(self, v):
        text = bounded_repr(v, budget=self._max_length or 128)
        limit = self._max_length
        return text if limit is None or len(text) <= limit else text[:limit] + "..."

    better_exceptions.ExceptionFormatter.format_value = format_value
