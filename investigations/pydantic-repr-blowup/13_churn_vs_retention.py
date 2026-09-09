import ctypes, ctypes.util, gc, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from pydantic import BaseModel
from rss import Trace

PAGE = 4096
def rss():
    with open("/proc/self/statm", "rb") as f:
        return int(f.read().split()[1]) * PAGE
def gb(n): return "%.2f GB" % (n / 2**30)
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None
def chain(d):
    t = Turn(idx=0)
    for i in range(1, d+1): t = Turn(idx=i, prev=t, echo=t)
    return t

DEPTH, ROUNDS = int(os.environ.get("DEPTH", "24")), int(os.environ.get("ROUNDS", "3"))
guard = Trace(interval=0.25, ceiling_gb=9.0).__enter__()
state = chain(DEPTH)

print("== A. churn: build a ~1.4GB repr %d times, drop each ==" % ROUNDS)
print("   baseline                      %s" % gb(rss()))
peak = 0
for _ in range(ROUNDS):
    s = repr(state); peak = max(peak, rss()); del s
print("   peak                          %s" % gb(peak))
print("   after dropping the strings    %s" % gb(rss()))
gc.collect()
print("   after gc.collect()            %s" % gb(rss()))
leaked = [o for o in gc.get_objects() if isinstance(o, str) and len(o) > 10_000_000]
print("   reachable strings > 10MB      %d  <- a real leak would show here" % len(leaked))
del leaked
libc.malloc_trim(ctypes.c_size_t(0))
print("   after malloc_trim(0)          %s" % gb(rss()))

print()
print("== B. retention: anything holding a FRAME holds every local in it ==")
def _send(state, body): raise TimeoutError("read timeout")
def serialize_and_send(state):
    body = "X" * (400 * 2**20)         # 400MB serialised payload, a frame local
    return _send(state, body)

before = rss()
held = None
try:
    serialize_and_send(state)
except TimeoutError as exc:
    held = exc
print("   holding the exception         %s  (+%s)" % (gb(rss()), gb(rss() - before)))
held = None
gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))
print("   after dropping it             %s  <- comes back" % gb(rss()))

before = rss()
pinned = []
try:
    serialize_and_send(state)
except TimeoutError as exc:
    tb = exc.__traceback__
    while tb:
        pinned.append(tb.tb_frame); tb = tb.tb_next
print("   holding its FRAMES            %s  (+%s)" % (gb(rss()), gb(rss() - before)))
gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))
print("   gc + trim while still held    %s  <- cannot come back" % gb(rss()))
del pinned
gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))
print("   after dropping the frames     %s" % gb(rss()))
guard.__exit__()
