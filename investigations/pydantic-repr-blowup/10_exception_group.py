"""ExceptionGroup: the exception tree multiplies the model tree.

A mock-API / task-group failure collects N sub-exceptions. Each carries its own
traceback, each traceback frame holds the same model. Consumers that walk both
trees pay N x (the already-exponential model repr).
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from pydantic import BaseModel
from rss import Trace, gb

DEPTH = int(os.environ.get("DEPTH", "12"))
NSUB  = int(os.environ.get("NSUB", "12"))

class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None

def chain(d):
    t = Turn(idx=0)
    for i in range(1, d+1): t = Turn(idx=i, prev=t, echo=t)
    return t

state = chain(DEPTH)

def _route(state, endpoint):
    raise TimeoutError("read timeout on %s" % endpoint)

def call_endpoint(state, endpoint):
    return _route(state, endpoint)

def fan_out(state, n):
    errors = []
    for i in range(n):
        try:
            call_endpoint(state, "/v1/resource/%d" % i)
        except TimeoutError as exc:
            errors.append(exc)
    raise ExceptionGroup("mock api: %d endpoints failed" % n, errors)

try:
    fan_out(state, NSUB)
except ExceptionGroup:
    exc_info = sys.exc_info()

print("model depth %d (%d objects), exception group with %d sub-exceptions\n" % (DEPTH, DEPTH+1, NSUB))

def bench(label, fn):
    print("  %-42s ... " % label, end="", flush=True)
    t0 = time.monotonic()
    out = fn()
    el = time.monotonic() - t0
    print("%8.2fs   %14s bytes%s" % (el, "{:,}".format(len(out)),
                                     "   <-- blows up" if el > 1.0 else ""), flush=True)
    return el

import traceback as tb_mod
bench("traceback.format_exception (group-aware)", lambda: "".join(tb_mod.format_exception(*exc_info)))

def better():
    import better_exceptions
    return "".join(better_exceptions.format_exception(*exc_info))
bench("better_exceptions", better)

def sentry():
    import sentry_sdk
    from sentry_sdk.utils import event_from_exception
    cl = sentry_sdk.Client(dsn="https://k@o0.ingest.sentry.io/1", include_local_variables=True,
                           auto_enabling_integrations=False)
    ev, _ = event_from_exception(exc_info, client_options=cl.options,
                                 mechanism={"type": "generic", "handled": True})
    total = sum(len(v) for e in ev["exception"]["values"]
                for f in (e.get("stacktrace") or {}).get("frames", [])
                for v in (f.get("vars") or {}).values() if isinstance(v, str))
    return "x" * total
bench("sentry (walks every sub-exception)", sentry)

print()
print("  for reference, ONE repr of that model: %s bytes" % "{:,}".format(len(repr(state))))
