"""Which serialisers survive sharing, and which detect cycles? Same graph to all."""
import json, pickle, pprint, sys, time, reprlib
import yaml
from pydantic import BaseModel

class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None

def chain(d):
    t = Turn(idx=0)
    for i in range(1, d+1): t = Turn(idx=i, prev=t, echo=t)
    return t

def as_dict(d):
    node = {"idx": 0, "prev": None, "echo": None}
    for i in range(1, d+1):
        node = {"idx": i, "prev": node, "echo": node}
    return node

D = 16
print("=" * 74)
print("SHARING: same DAG (%d nodes), through every serialiser" % (D+1))
print("=" * 74)
model, plain = chain(D), as_dict(D)
for label, fn in [
    ("repr(model)",            lambda: repr(model)),
    ("model_dump_json()",      lambda: model.model_dump_json()),
    ("json.dumps(dict)",       lambda: json.dumps(plain)),
    ("pprint.pformat(dict)",   lambda: pprint.pformat(plain)),
    ("pickle.dumps(model)",    lambda: pickle.dumps(model)),
    ("yaml.dump(dict)",        lambda: yaml.dump(plain)),
]:
    t0 = time.monotonic(); out = fn(); el = time.monotonic()-t0
    print("  %-22s %8.3fs  %14s bytes%s" % (label, el, "{:,}".format(len(out)),
          "   <-- expands" if len(out) > 100000 else "   <-- keeps the sharing"))

print()
print("=" * 74)
print("CYCLES: the case everybody DID handle")
print("=" * 74)
a = Turn(idx=1); b = Turn(idx=2, prev=a); a.prev = b
da = {"idx": 1}; db = {"idx": 2, "prev": da}; da["prev"] = db
for label, fn in [
    ("repr(model)",           lambda: repr(a)),
    ("model_dump_json()",     lambda: a.model_dump_json()),
    ("json.dumps(dict)",      lambda: json.dumps(da)),
    ("pprint.pformat(dict)",  lambda: pprint.pformat(da)),
    ("pickle.dumps(model)",   lambda: pickle.dumps(a)),
    ("yaml.dump(dict)",       lambda: yaml.dump(da)),
]:
    try:
        out = fn()
        print("  %-22s ok, %s bytes" % (label, "{:,}".format(len(out))))
    except Exception as e:
        print("  %-22s %s: %s" % (label, type(e).__name__, str(e)[:52]))

print()
print("=" * 74)
print("WHY yaml survives: it has syntax for 'the same object again'")
print("=" * 74)
print(yaml.dump(as_dict(3)))
print("=" * 74)
print("stdlib's only repr guard, and what it actually guards:")
print("=" * 74)
print(reprlib.recursive_repr.__doc__)
