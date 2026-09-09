"""How many times does better_exceptions repr() the payload for ONE exception?"""
import better_exceptions, httpx
from pydantic import BaseModel

CALLS = {"n": 0}

class Payload(BaseModel):
    tenant: str
    blob: str
    def __repr__(self):                      # stand-in for pydantic's Representation.__repr__
        CALLS["n"] += 1
        return "Payload(tenant=%r, blob=<%d chars>)" % (self.tenant, len(self.blob))

def _send(client: httpx.Client, url: str, payload: Payload, body: str) -> httpx.Response:
    return client.post(url, content=body, headers={"x-tenant": payload.tenant})

def serialize_and_send(client: httpx.Client, url: str, payload: Payload) -> httpx.Response:
    body = payload.model_dump_json()
    return _send(client, url, payload, body)

def call_upstream(client: httpx.Client, payload: Payload) -> httpx.Response:
    return serialize_and_send(client, "http://10.255.255.1:9/v1/generate", payload)

payload = Payload(tenant="acme", blob="x" * 1000)
transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=req)))
with httpx.Client(transport=transport) as client:
    try:
        call_upstream(client, payload)
    except httpx.ReadTimeout:
        fmt = better_exceptions.ExceptionFormatter(colored=False)
        text = fmt.format_exception(*__import__("sys").exc_info())
        out = "".join(text)

print("repr() calls on the payload object for ONE exception:", CALLS["n"])
print("frames in rendered traceback:", out.count("File "))
print("---- rendered ----")
print(out[:1400])
