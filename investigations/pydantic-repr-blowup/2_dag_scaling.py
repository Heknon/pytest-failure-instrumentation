"""Mechanism B: a *small* pydantic graph whose repr is exponential.

Shape mirrors an agent/RAG turn state that keeps a pointer to the previous turn
in more than one place (`parent` AND inside the message it produced).  The object
graph is a DAG with sharing; repr() is a tree walk, so every shared node is
re-expanded once per path that reaches it.
"""
import sys, time, gc
sys.setrecursionlimit(100000)
from pydantic import BaseModel
sys.path.insert(0, __import__("os").path.dirname(__file__))
from rss import Trace, gb

class Turn(BaseModel):
    idx: int
    note: str
    prev: "Turn | None" = None      # path 1 to the previous turn
    echo: "Turn | None" = None      # path 2 to the SAME previous turn

def chain(depth: int) -> Turn:
    t = Turn(idx=0, note="seed")
    for i in range(1, depth + 1):
        t = Turn(idx=i, note="turn-%d" % i, prev=t, echo=t)   # same object, twice
    return t

print(" depth  objects   len(repr)        wall      bytes/obj")
for depth in range(4, 25):
    root = chain(depth)
    n_objects = depth + 1
    gc.collect()
    t0 = time.monotonic()
    r = repr(root)
    wall = time.monotonic() - t0
    print("%6d %8d %11s %11.3fs %13s" % (depth, n_objects, "{:,}".format(len(r)), wall, "{:,}".format(len(r)//n_objects)))
    del r
    if wall > 8:
        print("\n-> stopped: each further turn in the conversation DOUBLES the work.")
        break
