"""Where does the seal actually break? Push depth until something gives."""
import sys
sys.path.insert(0, "/home/user/pytest-failure-instrumentation/investigations/pydantic-repr-blowup")
from pydantic import BaseModel
import hermetic

class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None

class A(BaseModel):                      # minimal cost per level: short name, one field
    n: "A | None" = None

def chain(cls, d):
    if cls is A:
        t = A()
        for _ in range(d): t = A(n=t)
        return t
    t = Turn(idx=0)
    for i in range(1, d+1): t = Turn(idx=i, prev=t, echo=t)
    return t

print("python recursion limit:", sys.getrecursionlimit())

for limit in (4096, 65536, 1_000_000):
    hermetic.unseal(); hermetic.seal(limit=limit)
    print("\n== budget %s ==" % "{:,}".format(limit))
    for cls, name in ((Turn, "Turn (3 fields, ~20 chars/level)"), (A, "A (1 field, ~5 chars/level)")):
        for depth in (200, 500, 1000, 5000, 20000):
            try:
                obj = chain(cls, depth)
            except RecursionError:
                print("   %-34s depth %6d  build: RecursionError" % (name, depth)); break
            try:
                n = len(repr(obj))
                print("   %-34s depth %6d  len(repr)=%s" % (name, depth, "{:,}".format(n)))
            except RecursionError:
                print("   %-34s depth %6d  *** RecursionError ***" % (name, depth)); break

print("\n== a real CYCLE, sealed ==")
hermetic.unseal(); hermetic.seal(limit=4096)
a = Turn(idx=1); b = Turn(idx=2, prev=a); a.prev = b
try:
    print("   cycle repr: %d chars -> %s" % (len(repr(a)), repr(a)[:70]))
except RecursionError:
    print("   cycle -> RecursionError (seal did not save it)")
