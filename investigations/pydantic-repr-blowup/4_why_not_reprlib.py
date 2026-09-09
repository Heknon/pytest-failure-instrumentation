"""The two fixes that look obvious and are not.

1. ``reprlib`` does not bound the work.  Its depth/length limits only apply to
   the container types it knows about; for anything else ``repr_instance`` calls
   ``builtins.repr(x)`` in full and truncates the result afterwards - which is
   the exact thing we are trying to avoid.
2. A genuine reference *cycle* is not the problem.  Cycles blow the stack in
   milliseconds and you notice.  It is *sharing* - a DAG - that is silent.
"""
import inspect
import reprlib
import sys
import time

import better_exceptions
from pydantic import BaseModel


class Turn(BaseModel):
    idx: int
    note: str
    prev: "Turn | None" = None
    echo: "Turn | None" = None


def chain(depth):
    turn = Turn(idx=0, note="seed")
    for i in range(1, depth + 1):
        turn = Turn(idx=i, note="t%d" % i, prev=turn, echo=turn)
    return turn


print("== the serialized body is repr'd in full too, then cut to 128 chars ==")
body = "x" * (200 * 2**20)  # stand-in for model_dump_json() output
t0 = time.monotonic()
kept = better_exceptions.ExceptionFormatter(colored=False).format_value(body)
print("   repr of a 200MB str: %.2fs, kept %d chars" % (time.monotonic() - t0, len(kept)))
del body, kept

print()
print("== a cycle fails fast; sharing hangs ==")
a = Turn(idx=1, note="a")
b = Turn(idx=2, note="b", prev=a)
a.prev = b
t0 = time.monotonic()
try:
    repr(a)
    print("   cycle: no error?!")
except RecursionError:
    print("   cycle -> RecursionError in %.3fs (pydantic has no cycle guard, but you find out "
          "immediately)" % (time.monotonic() - t0))
    print("           NB: with sys.setrecursionlimit() raised, this segfaults instead.")
t0 = time.monotonic()
size = len(repr(chain(18)))
print("   DAG   -> no error, {:,} bytes in {:.2f}s (silent, and doubles per turn)".format(
    size, time.monotonic() - t0))

print()
print("== why reprlib is not the fix ==")
src = inspect.getsource(reprlib.Repr.repr_instance)
print("   reprlib.Repr.repr_instance does:  %s"
      % next(ln.strip() for ln in src.splitlines() if "builtins.repr" in ln))
capped = reprlib.Repr()
capped.maxlevel = 2
capped.maxother = 100
root = chain(20)
t0 = time.monotonic()
capped.repr(root)
print("   reprlib.Repr(maxlevel=2).repr(model): %.2fs - the limits arrive too late"
      % (time.monotonic() - t0))
sys.stdout.flush()
