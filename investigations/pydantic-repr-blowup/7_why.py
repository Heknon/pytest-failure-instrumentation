"""Small model or big model or infinite loop? Measure all three claims."""
import sys, time, gc
from pydantic import BaseModel

class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None

def chain(d):
    t = Turn(idx=0)
    for i in range(1, d+1): t = Turn(idx=i, prev=t, echo=t)
    return t

print("=" * 72)
print("A. A THREE-deep example you can check by eye")
print("=" * 72)
root = chain(3)
r = repr(root)
print("objects created:", 4, "  (idx=3 -> idx=2 -> idx=1 -> idx=0)")
print("repr:")
print("   ", r)
print()
print("the ONE leaf object idx=0 appears in that string %d times." % r.count("idx=0"))
print("there is one of it in memory. it got printed %d times, once per path to it." % r.count("idx=0"))

print()
print("=" * 72)
print("B. how the two numbers diverge")
print("=" * 72)
print("%6s %9s %12s %16s %14s" % ("depth", "objects", "RAM bytes", "times leaf printed", "repr bytes"))
for d in (3, 6, 10, 14, 18, 22):
    root = chain(d)
    objs = [o for o in gc.get_objects() if isinstance(o, Turn)]
    ram = sum(sys.getsizeof(o) + sys.getsizeof(o.__dict__) for o in objs)
    r = repr(root)
    print("%6d %9d %12s %16s %14s" % (d, d+1, "{:,}".format(ram),
                                      "{:,}".format(r.count("idx=0")), "{:,}".format(len(r))))
    del r, objs
    gc.collect()

print()
print("=" * 72)
print("C. ruling out the other two explanations")
print("=" * 72)
root = chain(22)
objs = [o for o in gc.get_objects() if isinstance(o, Turn)]
print("1. 'it's a big model'")
print("   the whole graph is %d objects, %s bytes of RAM."
      % (len(objs), "{:,}".format(sum(sys.getsizeof(o) + sys.getsizeof(o.__dict__) for o in objs))))
print("   there is no big data anywhere in it. it holds 23 integers.")
print()
print("2. 'it's an infinite loop'")
t0 = time.monotonic(); n = len(repr(root)); el = time.monotonic() - t0
print("   it terminates: %.1fs, %s bytes, then returns normally." % (el, "{:,}".format(n)))
print("   it is finite and makes steady progress. it is just astronomically large.")
print()
print("3. what it actually is")
print("   the graph is a COMPRESSED tree. pointers are the compression:")
print("   'prev' and 'echo' are 16 bytes that mean 'that whole subtree, twice'.")
print("   repr has no way to write a pointer, so it decompresses. output size is")
print("   the number of root-to-leaf PATHS (2^depth), not the number of objects.")
