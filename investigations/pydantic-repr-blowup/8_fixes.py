"""Validate each candidate fix against EVERY consumer that blows up."""
import io, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from pydantic import BaseModel, Field

DEPTH = int(os.environ.get("DEPTH", "14"))

# ---------------------------------------------------------------- diagnostic
def shared_nodes(root):
    """Find nodes reachable by more than one path, and the repr blow-up factor."""
    counts, order = {}, []
    def walk(o, depth=0):
        if isinstance(o, BaseModel):
            key = id(o)
            if key not in counts:
                counts[key] = [0, type(o).__name__, depth]
                order.append(key)
            counts[key][0] += 1
            if counts[key][0] > 1:
                return                      # already counted this subtree once
            for _, v in o.__repr_args__():
                walk(v, depth + 1)
        elif isinstance(o, (list, tuple, set)):
            for v in o: walk(v, depth + 1)
        elif isinstance(o, dict):
            for v in o.values(): walk(v, depth + 1)
    walk(root)
    dupes = [(c, name, d) for c, name, d in counts.values() if c > 1]
    factor = 1
    for c, _, _ in dupes: factor *= c
    return dupes, factor

# ---------------------------------------------------------------- the models
class Broken(BaseModel):
    idx: int
    prev: "Broken | None" = None
    echo: "Broken | None" = None                      # duplicate reference

class ExcludedField(BaseModel):
    idx: int
    prev: "ExcludedField | None" = None
    echo: "ExcludedField | None" = Field(default=None, exclude=True, repr=False)

class BoundedRepr(BaseModel):
    """Same data, but repr() does not recurse."""
    idx: int
    prev: "BoundedRepr | None" = None
    echo: "BoundedRepr | None" = None
    def __repr__(self):
        return "%s(idx=%r, prev=%s, echo=%s)" % (
            type(self).__name__, self.idx,
            "..." if self.prev is not None else None,
            "..." if self.echo is not None else None)
    __str__ = __repr__

class Fixed(BaseModel):
    """The real fix: the second path is a key, not an object."""
    idx: int
    prev: "Fixed | None" = None
    echo_idx: int | None = None

def build(cls, depth):
    t = cls(idx=0)
    for i in range(1, depth + 1):
        t = (cls(idx=i, prev=t, echo_idx=t.idx) if cls is Fixed
             else cls(idx=i, prev=t, echo=t))
    return t

# ---------------------------------------------------------------- consumers
def _send(state, body): raise TimeoutError("read timed out")
def serialize_and_send(state):
    body = "{}"
    return _send(state, body)
def make_exc(state):
    try: serialize_and_send(state)
    except TimeoutError: return sys.exc_info()

def c_repr(s):  return repr(s)
def c_json(s):  return s.model_dump_json()
def c_better(s):
    import better_exceptions
    return "".join(better_exceptions.format_exception(*make_exc(s)))
def c_sentry(s):
    import sentry_sdk
    from sentry_sdk.utils import event_from_exception
    cl = sentry_sdk.Client(dsn="https://k@o0.ingest.sentry.io/1", include_local_variables=True,
                           auto_enabling_integrations=False)
    ev, _ = event_from_exception(make_exc(s), client_options=cl.options,
                                 mechanism={"type": "generic", "handled": True})
    return "x" * max((len(v) for f in ev["exception"]["values"][0]["stacktrace"]["frames"]
                      for v in (f.get("vars") or {}).values() if isinstance(v, str)), default=0)
def c_rich(s):
    from rich.console import Console
    from rich.traceback import Traceback
    c = Console(file=io.StringIO(), width=100)
    c.print(Traceback.from_exception(*make_exc(s), show_locals=True))
    return c.file.getvalue()

CONSUMERS = [("repr()", c_repr), ("model_dump_json()", c_json),
             ("better_exceptions", c_better), ("sentry locals", c_sentry),
             ("rich show_locals", c_rich)]

print("depth %d\n" % DEPTH)
print("%-16s %-18s %10s %16s" % ("model", "consumer", "wall", "bytes"))
for label, cls in (("Broken (baseline)", Broken), ("Field(exclude,repr=False)", ExcludedField),
                   ("__repr__ override", BoundedRepr), ("Fixed (key not object)", Fixed)):
    state = build(cls, DEPTH)
    dupes, factor = shared_nodes(state)
    print("-- %s  | shared nodes: %d, blow-up factor: %s"
          % (label, len(dupes), "{:,}".format(factor)))
    for cname, fn in CONSUMERS:
        t0 = time.monotonic()
        try:
            out = fn(state); el = time.monotonic() - t0
            print("%-16s %-18s %9.3fs %16s%s" % ("", cname, el, "{:,}".format(len(out)),
                                                 "  <-- blows up" if el > 1.0 else ""))
        except Exception as e:
            print("%-16s %-18s %9.3fs   raised %s" % ("", cname, time.monotonic()-t0, type(e).__name__))
    print()
