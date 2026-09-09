import io, os, sys, time
sys.path.insert(0, "/home/user/pytest-failure-instrumentation/investigations/pydantic-repr-blowup")
sys.path.insert(0, os.path.dirname(__file__))
from pydantic import BaseModel
import hermetic

DEPTH, NSUB = int(os.environ.get("DEPTH", "10")), int(os.environ.get("NSUB", "6"))

class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None

def chain(d):
    t = Turn(idx=0)
    for i in range(1, d+1): t = Turn(idx=i, prev=t, echo=t)
    return t
state = chain(DEPTH)

def _route(s, e): raise TimeoutError("read timeout on %s" % e)
def call_endpoint(s, e): return _route(s, e)
def group_exc():
    errors = []
    for i in range(NSUB):
        try: call_endpoint(state, "/v1/r/%d" % i)
        except TimeoutError as ex: errors.append(ex)
    try: raise ExceptionGroup("mock api: %d failed" % NSUB, errors)
    except ExceptionGroup: return sys.exc_info()

def c_repr():  return repr(state)
def c_fstr():  return f"{state!r}"
def c_log():   return "%r" % (state,)
def c_better():
    import better_exceptions
    return "".join(better_exceptions.format_exception(*group_exc()))
def c_sentry():
    import sentry_sdk
    from sentry_sdk.utils import event_from_exception
    cl = sentry_sdk.Client(dsn="https://k@o0.ingest.sentry.io/1", include_local_variables=True,
                           auto_enabling_integrations=False)
    ev, _ = event_from_exception(group_exc(), client_options=cl.options,
                                 mechanism={"type": "generic", "handled": True})
    return "x" * sum(len(v) for e in ev["exception"]["values"]
                     for f in (e.get("stacktrace") or {}).get("frames", [])
                     for v in (f.get("vars") or {}).values() if isinstance(v, str))
def c_rich():
    from rich.console import Console
    from rich.traceback import Traceback
    c = Console(file=io.StringIO(), width=100)
    c.print(Traceback.from_exception(*group_exc(), show_locals=True))
    return c.file.getvalue()
def c_json(): return state.model_dump_json()

CONSUMERS = [("repr()", c_repr), ("f'{model!r}'", c_fstr), ("logging '%r'", c_log),
             ("better_exceptions", c_better), ("sentry + ExceptionGroup", c_sentry),
             ("rich show_locals", c_rich), ("model_dump_json()", c_json)]

for phase in ("BEFORE", "AFTER hermetic.seal()"):
    if phase.startswith("AFTER"): hermetic.seal(limit=4096)
    print("== %s ==" % phase, flush=True)
    for label, fn in CONSUMERS:
        t0 = time.monotonic(); out = fn(); el = time.monotonic()-t0
        print("   %-26s %8.2fs %14s bytes%s" % (label, el, "{:,}".format(len(out)),
              "  <-- blows up" if el > 1.0 else ""), flush=True)
    print(flush=True)

print("guard_dumps on the bomb:")
try:
    hermetic.guard_dumps(chain(40))
except hermetic.ReprBudgetExceeded as e:
    print("   ReprBudgetExceeded:", str(e)[:150])
print("guard_dumps on a healthy model:", len(hermetic.guard_dumps(Turn(idx=1))), "bytes")
print()
print("sample sealed repr:", repr(state)[:110])
