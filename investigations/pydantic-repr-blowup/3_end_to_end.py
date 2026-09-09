"""End-to-end: read/write timeout while POSTing a serialized pydantic model,
then better_exceptions renders the traceback.  Traces RSS the whole time."""
import os, sys, time, gc
sys.setrecursionlimit(100000)
sys.path.insert(0, os.path.dirname(__file__))
import better_exceptions, httpx
from pydantic import BaseModel
from rss import Trace, gb

DEPTH = int(os.environ.get("DEPTH", "24"))

class Turn(BaseModel):
    idx: int
    note: str
    prev: "Turn | None" = None
    echo: "Turn | None" = None      # second path to the same object -> DAG

class Request(BaseModel):
    tenant: str
    state: Turn

def build(depth):
    t = Turn(idx=0, note="seed")
    for i in range(1, depth + 1):
        t = Turn(idx=i, note="turn-%d" % i, prev=t, echo=t)
    return Request(tenant="acme", state=t)

def _post(client, url, req, body):
    return client.post(url, content=body)

def serialize_and_send(client, url, req):
    body = req.model_dump_json()
    return _post(client, url, req, body)

def call_upstream(client, req):
    return serialize_and_send(client, "https://api.internal/v1/generate", req)

req = build(DEPTH)
n_objects = DEPTH + 2
size = sum(sys.getsizeof(o) for o in gc.get_objects() if isinstance(o, Turn))
print("model graph: %d objects, ~%d bytes of Turn instances -- nothing looks wrong" % (n_objects, size))

transport = httpx.MockTransport(
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timed out", request=r)))

t0 = time.monotonic()
with httpx.Client(transport=transport) as client:
    try:
        call_upstream(client, req)
    except httpx.ReadTimeout:
        print("timeout raised after %.3fs (serialization itself was fine)" % (time.monotonic() - t0))
        gc.collect()
        fmt = better_exceptions.ExceptionFormatter(colored=False, max_length=128)
        with Trace() as tr:
            rendered = "".join(fmt.format_exception(*sys.exc_info()))

print()
print("better_exceptions formatting:")
print("  wall clock      : %.1fs   (single core, pure python)" % tr.wall)
print("  RSS before      : %s" % gb(tr.base))
print("  RSS peak        : %s" % gb(tr.peak))
print("  RSS swing       : %s" % gb(tr.peak - min(r for _, r in tr.samples)))
print("  rendered output : %d bytes  <-- what all that work produced" % len(rendered))
print("  RSS shape       : %s" % tr.sparkline())
print()
print("the line it was building:")
for ln in rendered.splitlines():
    if "state=" in ln or "req " in ln.lower():
        print("   ", ln.strip()[:150]); break
