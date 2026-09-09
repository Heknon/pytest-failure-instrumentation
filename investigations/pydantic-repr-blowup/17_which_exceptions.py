"""Which exception types put the payload in repr()? And what reaches 8 GB?"""
import json, pickle, subprocess, sys
MB = 2**20
PAYLOAD = 50 * MB

def probe(label, fn):
    try:
        fn()
        print("  %-38s (did not raise)" % label); return
    except BaseException as exc:
        s, r = len(str(exc)), len(repr(exc))
        held = 0
        for attr in ("doc", "object", "output", "stdout", "stderr", "response", "content"):
            v = getattr(exc, attr, None)
            if isinstance(v, (str, bytes)): held = max(held, len(v))
        flag = "  <-- repr dumps the payload" if r > 10_000 else ""
        print("  %-38s str=%9s repr=%12s holds=%12s%s"
              % (label, "{:,}".format(s), "{:,}".format(r), "{:,}".format(held), flag))

print("== stdlib exceptions carrying a %d MB payload ==" % (PAYLOAD // MB))
blob_b = b"x" * PAYLOAD + b"\xff\xfe"
blob_s = "x" * PAYLOAD + "\ud800"
probe("UnicodeDecodeError", lambda: blob_b.decode("utf-8"))
probe("UnicodeEncodeError", lambda: blob_s.encode("utf-8"))
probe("json.JSONDecodeError", lambda: json.loads('{"a":1' + " " * PAYLOAD + "x"))
probe("ValueError(big_string)", lambda: (_ for _ in ()).throw(ValueError("y" * PAYLOAD)))
probe("AssertionError(big_string)", lambda: (_ for _ in ()).throw(AssertionError("y" * PAYLOAD)))
probe("pickle.UnpicklingError", lambda: pickle.loads(b"\x80\x05" + b"q" * 100))
def called_process():
    raise subprocess.CalledProcessError(1, "cmd", output=b"z" * PAYLOAD, stderr=b"w" * PAYLOAD)
probe("subprocess.CalledProcessError", called_process)
del blob_b, blob_s

print()
print("== what actually reaches 8 GB ==")
EIGHT = 8 * 2**30
FRAMES = 13          # a typical httpx/anyio timeout stack
print("  route                                            needed input for 8 GB")
print("  %-46s %s" % ("linear: body repr'd once per frame",
                       "%.0f MB body" % (EIGHT / FRAMES / MB)))
print("  %-46s %s" % ("linear: UnicodeDecodeError repr per frame",
                       "%.0f MB payload" % (EIGHT / FRAMES / MB)))
print("  %-46s %s" % ("ValidationError (5,300 B each)",
                       "%s errors" % "{:,}".format(EIGHT // 5300)))
for payload_kb in (4, 400):
    import math
    levels = math.log2(EIGHT / FRAMES / (payload_kb * 1024))
    print("  %-46s %s" % ("exponential: %d KB payload, shared per level" % payload_kb,
                          "%.0f levels of double-reference" % levels))
