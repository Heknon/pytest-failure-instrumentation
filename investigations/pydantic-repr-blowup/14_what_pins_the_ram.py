"""Two ways a finished exception keeps gigabytes alive after the handler ran."""
import ctypes, ctypes.util, gc, os, sys
sys.path.insert(0, os.path.dirname(__file__))
PAGE = 4096
def rss():
    with open("/proc/self/statm", "rb") as f: return int(f.read().split()[1]) * PAGE
def gb(n): return "%.2f GB" % (n / 2**30)
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
def trim(): gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))

PAYLOAD = 300 * 2**20

def _send(body): raise TimeoutError("read timeout")
def serialize_and_send(tag):
    body = "X" * PAYLOAD                   # the serialised model, a frame local
    return _send(body)

print("== 1. the reference CYCLE: exc -> traceback -> frame -> exc ==")
trim(); base = rss()
def leaky():
    try:
        serialize_and_send("a")
    except TimeoutError as exc:
        err = exc                          # a local in THIS frame, which is in exc's traceback
        return len(str(err))               # cycle: frame <-> exception
gc.disable()                               # simulate gen-2 collections being rare
leaky()
print("   after the handler returned    %s  (+%s)" % (gb(rss()), gb(rss() - base)))
libc.malloc_trim(ctypes.c_size_t(0))
print("   refcounting + malloc_trim     %s  <- refcounting alone cannot free a cycle" % gb(rss()))
gc.enable(); gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))
print("   after gc.collect()            %s  <- only the cyclic GC gets it back" % gb(rss()))

print()
print("== 2. an ExceptionGroup pins EVERY sub-exception's frames ==")
trim(); base = rss()
def fan_out(n):
    errors = []
    for i in range(n):
        try:
            serialize_and_send(i)
        except TimeoutError as exc:
            errors.append(exc)             # each one still owns its traceback + locals
    return ExceptionGroup("mock api: %d failed" % n, errors)

group = fan_out(6)
print("   6 sub-exceptions held         %s  (+%s = 6 x %dMB payload)"
      % (gb(rss()), gb(rss() - base), PAYLOAD // 2**20))
gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))
print("   gc + trim while group held    %s  <- all of it is reachable, none can go" % gb(rss()))
group = None
trim()
print("   after dropping the group      %s" % gb(rss()))

print()
print("== 3. the fix for a group you must keep ==")
trim(); base = rss()
group = fan_out(6)
for sub in group.exceptions:
    sub.__traceback__ = None               # keep the error, drop the frames
gc.collect(); libc.malloc_trim(ctypes.c_size_t(0))
print("   group kept, tracebacks cleared %s  (+%s)" % (gb(rss()), gb(rss() - base)))
print("   still have the errors:", [type(e).__name__ for e in group.exceptions][:3], "...")
