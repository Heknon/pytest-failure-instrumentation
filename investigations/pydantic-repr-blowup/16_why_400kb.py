import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from pydantic import BaseModel
from rss import Trace, gb

class L(BaseModel):
    v: int
    a: "L | None" = None
    b: "L | None" = None

print("=" * 70)
print("STEP 1: watch the text double. Same object stored in 'a' and in 'b'.")
print("=" * 70)
leaf = L(v=0)
lvl1 = L(v=1, a=leaf, b=leaf)          # ONE leaf object, referenced twice
lvl2 = L(v=2, a=lvl1, b=lvl1)          # ONE lvl1 object, referenced twice
lvl3 = L(v=3, a=lvl2, b=lvl2)
for name, obj in (("leaf ", leaf), ("lvl1 ", lvl1), ("lvl2 ", lvl2), ("lvl3 ", lvl3)):
    r = repr(obj)
    print("%s objects in RAM: %d   text: %3d chars" % (name, 1, len(r)))
    print("       %s" % r)
print()
print("lvl1 holds ONE leaf. Its text contains the leaf's text TWICE.")
print("lvl2 holds ONE lvl1. Its text contains lvl1's text TWICE. So the leaf 4x.")
print("Each new object adds ~30 bytes of RAM and DOUBLES the text.")

print()
print("=" * 70)
print("STEP 2: your numbers. A 400 KB payload, stored twice per level.")
print("=" * 70)

class P(BaseModel):
    blob: str = ""
    a: "P | None" = None
    b: "P | None" = None

PAYLOAD = 400 * 1024
print("%6s %14s %16s %12s" % ("levels", "RAM (bytes)", "repr (bytes)", "ratio"))
node = P(blob="x" * PAYLOAD)
ram = sys.getsizeof(node) + sys.getsizeof(node.__dict__) + PAYLOAD
for level in range(0, 14):
    if level:
        node = P(a=node, b=node)                       # same object, two slots
        ram += sys.getsizeof(node) + sys.getsizeof(node.__dict__)
    if level in (0, 1, 2, 5, 10, 12, 13):
        with Trace(interval=0.05, ceiling_gb=9.0) as tr:
            size = len(repr(node))
        print("%6d %14s %16s %11.0fx" % (level, "{:,}".format(ram),
                                         "{:,}".format(size), size / ram))
print()
print("13 levels of 'stored twice' turns 400 KB of data into 3.2 GB of text.")
print("The data never grew. Only the number of PLACES it gets written to.")
