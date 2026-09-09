"""Disabling better_exceptions: does it help, and is it enough?

Every consumer below is handed the SAME model and the SAME exception. Split is
not "which library is nicer" - it is whether the walker carries a memo table.
"""
import copy
import io
import json
import logging
import os
import pickle
import sys
import time
import traceback

from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))
from rss import Trace  # noqa: E402

DEPTH = int(os.environ.get("DEPTH", "14"))


class Turn(BaseModel):
    idx: int
    note: str
    prev: "Turn | None" = None
    echo: "Turn | None" = None


def chain(depth):
    turn = Turn(idx=0, note="seed")
    for i in range(1, depth + 1):
        turn = Turn(idx=i, note="t%d" % i, prev=turn, echo=turn)
    return turn


root = chain(DEPTH)
guard = Trace(interval=0.25, ceiling_gb=8.0).__enter__()


def _send(state, body):
    raise TimeoutError("read timed out")


def serialize_and_send(state):
    body = "{}"
    return _send(state, body)


def make_exc():
    try:
        serialize_and_send(root)
    except TimeoutError:
        return sys.exc_info()


def bench(label, fn):
    print("%-46s ... " % label, end="", flush=True)
    t0 = time.monotonic()
    try:
        out = fn()
    except Exception as e:
        print("%9.3fs   raised %s" % (time.monotonic() - t0, type(e).__name__), flush=True)
        return
    elapsed = time.monotonic() - t0
    size = len(out) if isinstance(out, (str, bytes)) else -1
    print("%9.3fs   %s%s" % (elapsed, "{:,} bytes".format(size) if size >= 0 else "-",
                             "   <-- blows up" if elapsed > 1.0 else ""), flush=True)


def logger_exception(patched):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.Logger("t")
    log.addHandler(handler)
    if patched:
        import better_exceptions
        handler.formatter.formatException = (
            lambda ei: "".join(better_exceptions.format_exception(*ei)))
    try:
        serialize_and_send(root)
    except TimeoutError:
        log.exception("upstream failed")
    return buf.getvalue()


def sentry_event(include_locals):
    import sentry_sdk
    from sentry_sdk.utils import event_from_exception
    client = sentry_sdk.Client(dsn="https://k@o0.ingest.sentry.io/1",
                               include_local_variables=include_locals,
                               auto_enabling_integrations=False)
    event, _ = event_from_exception(make_exc(), client_options=client.options,
                                    mechanism={"type": "generic", "handled": True})
    biggest = max((len(v) for f in event["exception"]["values"][0]["stacktrace"]["frames"]
                   for v in (f.get("vars") or {}).values() if isinstance(v, str)), default=0)
    return "x" * biggest   # report the largest single frame-local it stored


def rich_tb(show_locals):
    from rich.console import Console
    from rich.traceback import Traceback
    console = Console(file=io.StringIO(), width=100)
    console.print(Traceback.from_exception(*make_exc(), show_locals=show_locals))
    return console.file.getvalue()


print("depth %d: %d objects, ~%d bytes of real data\n" % (DEPTH, DEPTH + 1, (DEPTH + 1) * 80))

print("-- walkers with no memo table: every shared node re-expanded per path --")
bench("pydantic model_dump_json()", lambda: root.model_dump_json())
bench("json.dumps(model_dump())", lambda: json.dumps(root.model_dump()))
bench("logger.exception (better_exceptions)", lambda: logger_exception(True))
bench("sentry (include_local_variables=True, default)", lambda: sentry_event(True))
bench("rich Traceback(show_locals=True)", lambda: rich_tb(True))

print("\n-- formatters that never read locals --")
bench("traceback.format_exc()", lambda: "".join(traceback.format_exception(*make_exc())))
bench("logger.exception (stdlib)", lambda: logger_exception(False))
bench("sentry (include_local_variables=False)", lambda: sentry_event(False))
bench("rich Traceback(show_locals=False, default)", lambda: rich_tb(False))

print("\n-- walkers that DO carry a memo table: sharing costs nothing --")
bench("pickle.dumps()", lambda: pickle.dumps(root))
bench("copy.deepcopy() then pickle", lambda: pickle.dumps(copy.deepcopy(root)))
guard.__exit__()
