import os, sys, time, gc, traceback
sys.path.insert(0, os.path.dirname(__file__))
import better_exceptions, httpx
from pydantic import BaseModel
from rss import Trace, gb

DEPTH = int(os.environ.get("DEPTH", "22"))

class Turn(BaseModel):
    idx: int; note: str
    prev: "Turn | None" = None
    echo: "Turn | None" = None

def build(d):
    t = Turn(idx=0, note="seed")
    for i in range(1, d+1): t = Turn(idx=i, note="t%d" % i, prev=t, echo=t)
    return t

def _post(client, url, state, body):
    return client.post(url, content=body)

def serialize_and_send(client, url, state):
    body = state.model_dump_json()
    return _post(client, url, state, body)

def blow_up(state):
    transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timed out", request=r)))
    with httpx.Client(transport=transport) as client:
        return serialize_and_send(client, "https://api.internal/v1/generate", state)

def bounded_repr(v, max_depth=2, budget=2000, _depth=0):
    if isinstance(v, BaseModel):
        if _depth >= max_depth: return "%s(...)" % type(v).__name__
        parts = []
        for k, sub in v.__repr_args__():
            parts.append("%s=%s" % (k, bounded_repr(sub, max_depth, budget, _depth + 1)))
            if sum(map(len, parts)) > budget: parts.append("..."); break
        return "%s(%s)" % (type(v).__name__, ", ".join(parts))
    try: s = repr(v[:budget]) if isinstance(v, (str, bytes)) else repr(v)
    except Exception: return "<unprintable %s>" % type(v).__name__
    return s if len(s) <= budget else s[:budget] + "..."

class SafeFormatter(better_exceptions.ExceptionFormatter):
    def format_value(self, v):
        s = bounded_repr(v, budget=self._max_length or 128)
        m = self._max_length
        return s if m is None or len(s) <= m else s[:m] + "..."

state = build(DEPTH)
for label, factory in (("better_exceptions (as shipped)", better_exceptions.ExceptionFormatter),
                       ("with bounded format_value    ", SafeFormatter)):
    try:
        blow_up(state)
    except httpx.ReadTimeout:
        gc.collect()
        with Trace() as tr:
            out = "".join(factory(colored=False).format_exception(*sys.exc_info()))
        print("%s : %8.2fs   peak %s (+%s)   -> %d bytes rendered"
              % (label, tr.wall, gb(tr.peak), gb(tr.peak - tr.base), len(out)))
try:
    blow_up(state)
except httpx.ReadTimeout:
    t0 = time.monotonic(); traceback.format_exc()
    print("traceback.format_exc()        : %8.4fs   (stdlib never reads locals)" % (time.monotonic() - t0))
