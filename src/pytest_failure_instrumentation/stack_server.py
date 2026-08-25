"""One HTTP endpoint per host that answers "what is process N doing right now".

The evidence this plugin writes is for reading *afterwards*. A UI watching a
run wants the opposite: the stack of a test that is still running, on demand,
without waiting for it to fail. That is a pull, and a pull needs something
listening.

**Why one server per host rather than one per run.** The port has to be fixed -
a port assigned at random is a port a firewall has not been told about and a UI
cannot guess - and a fixed port cannot be bound twice. Several pytest sessions
on one machine is the ordinary case (a developer's laptop, a CI runner with
parallel jobs), so a server per session would mean every session after the
first failing to start, or worse, silently stealing the port from the one
already serving.

So the first session to start claims the port and serves; the rest find it
already claimed and leave it alone. Any of them can be asked about any pid,
because the answer does not come from the session's own memory - it comes from
reading the target process, which needs no relationship to the reader.

**How "already claimed" is decided.** By asking, not by looking. The obvious
check is whether the process holding the port looks like ours, and it cannot
work: the process is a Python interpreter, so its name is ``python`` on every
platform, and its command line is whatever pytest was invoked as. What
identifies a server is that it answers ``/identity`` with a name only this
package uses. Anything else on the port - a dev server, a proxy, Jenkins - is a
stranger, and the run continues without live stacks rather than fighting it for
the port.

**Handover.** The session that claimed the port will usually finish before the
others; the port then goes free while sessions that wanted it are still
running. So a session that lost the claim keeps trying, quietly, and takes over
whenever the holder exits. Sessions do not coordinate beyond the port itself,
which is the only thing all of them can see.

The listener is loopback-only. It reports what local processes are executing,
which is exactly the kind of thing that must not be reachable from off the
machine.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .probes import pyspy, stacks
from .probes.platform_flags import IS_WINDOWS

#: What ``/identity`` answers with. The whole singleton scheme rests on this
#: string being unique to this package, so it is the distribution name.
SERVICE = "pytest-failure-instrumentation-stacks"

#: Loopback only, and not configurable. See the module docstring.
HOST = "127.0.0.1"

DEFAULT_PORT = 8080

#: How long to wait for a stranger to identify itself. Long enough for a busy
#: local server to answer, short enough not to stall session start.
IDENTIFY_TIMEOUT = 2.0

#: How often a session that lost the claim tries again, so that whoever is
#: still running picks the port up when the holder exits.
RECLAIM_SECONDS = 5.0

#: How many external reads may be in flight at once. Each is a subprocess, and
#: each takes its full timeout when the target is wedged - which is exactly
#: what a UI polls hardest. Without a bound, a dashboard refreshing sixteen
#: stuck workers every second puts hundreds of readers on a machine that is
#: already in trouble, and this plugin's one rule is that it must never be what
#: makes a run worse. Refused rather than queued: a caller polling on a timer
#: wants to be told to back off, not held until its own deadline passes.
MAX_CONCURRENT_READS = 8

_readers = threading.BoundedSemaphore(MAX_CONCURRENT_READS)


class _Server(ThreadingHTTPServer):
    """Threaded, because reading a stack can take seconds.

    A single-threaded server would let one request against a wedged process
    hold up every other request for the length of py-spy's timeout - and a
    wedged process is what a UI asks about most.
    """

    daemon_threads = True

    #: **Not** the inherited default on Windows, where SO_REUSEADDR means
    #: something else entirely: there it permits binding an address another
    #: socket is *actively listening on*, and the two then split incoming
    #: connections unpredictably. That would defeat this whole module - two
    #: sessions would both believe they had claimed the port. On POSIX the
    #: flag only bypasses TIME_WAIT, which is wanted: the next session must
    #: not be locked out for a minute because the last one just exited.
    allow_reuse_address = not IS_WINDOWS


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0, so every response closes its connection. Keep-alive would
    # require getting Content-Length right on every path including the error
    # ones, in exchange for saving a local TCP handshake.
    protocol_version = "HTTP/1.0"

    server_version = SERVICE

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        route = urlparse(self.path)
        if route.path == "/identity":
            self._reply(200, identity())
        elif route.path == "/stack":
            self._stack(parse_qs(route.query))
        else:
            self._reply(404, {"error": "no such endpoint", "endpoints": ENDPOINTS})

    def _stack(self, query: dict[str, list[str]]) -> None:
        raw = (query.get("pid") or [""])[0]
        try:
            pid = int(raw)
        except ValueError:
            self._reply(400, {"error": f"pid must be a number, not {raw!r}"})
            return

        threads, error, source = read_stack(pid)
        if error is not None:
            # 502: this server is a gateway to a reader that could not answer,
            # and the distinction from "this server is broken" is the whole
            # content of the reply.
            self._reply(502, {"pid": pid, "source": source, "error": error})
            return
        self._reply(
            200,
            {
                "pid": pid,
                "source": source,
                "captured_at": round(time.time(), 3),
                "threads": threads,
            },
        )

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the client hung up; nothing here is worth a traceback

    def log_message(self, format: str, *args: Any) -> None:
        """Silence. The default writes a line per request to stderr, which is
        the report a human is reading pytest's output from."""


ENDPOINTS = {
    "/identity": "who is serving this port",
    "/stack?pid=N": "the current stack of every thread in process N",
}


def identity() -> dict[str, Any]:
    from . import __version__

    return {"service": SERVICE, "version": __version__, "pid": os.getpid()}


def read_stack(pid: int) -> tuple[Optional[list[dict[str, Any]]], Optional[str], str]:
    """``(threads, error, source)`` for a live process.

    This process answers for itself out of its own frames, which costs a dict
    lookup and needs no permission from anybody. Every other pid needs the
    external reader, because there is no way to walk another process's frames
    from Python.
    """
    if pid == os.getpid():
        try:
            return stacks.own_threads(), None, "in-process"
        except Exception as failure:  # noqa: BLE001 - a served error beats a 500
            return None, f"could not read own frames: {failure!r}", "in-process"

    if not _readers.acquire(blocking=False):
        return (
            None,
            f"{MAX_CONCURRENT_READS} stack reads are already in flight; retry shortly",
            "py-spy",
        )
    try:
        threads, error = pyspy.dump(pid)
    finally:
        _readers.release()
    return threads, error, "py-spy"


def identify(port: int, timeout: float = IDENTIFY_TIMEOUT) -> Optional[dict[str, Any]]:
    """Whoever holds ``port``, if it is one of ours; None for anything else.

    Proxies are explicitly disabled. ``urlopen`` honours ``http_proxy`` from
    the environment, which CI sets constantly - and a request for 127.0.0.1
    sent to a proxy either fails or, much worse, reaches something else
    entirely and gets believed.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{HOST}:{port}/identity", timeout=timeout) as response:
            payload = json.loads(response.read(4096))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if isinstance(payload, dict) and payload.get("service") == SERVICE:
        return payload
    return None


class StackService:
    """This session's part in keeping one server alive on the host.

    Either it is serving, or it is waiting for whoever is. Nothing here ever
    raises into a run: a session that cannot serve and cannot find a server
    simply has no live stacks, which is the same position every session was in
    before this module existed.
    """

    def __init__(self, port: int = DEFAULT_PORT, reclaim_seconds: float = RECLAIM_SECONDS) -> None:
        self.port = port
        self.reclaim_seconds = reclaim_seconds
        #: What happened, in words, for whoever asks why there are no stacks.
        self.status = "not started"
        self.serving = False
        self._httpd: Optional[_Server] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._supervise, name="failure-instrumentation-stacks", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        httpd = self._httpd
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        thread = self._thread
        if thread is not None:
            # Bounded, and the thread is a daemon: a server that will not come
            # down must not be what keeps a finished run from exiting.
            thread.join(timeout=5.0)

    # -- the claim ------------------------------------------------------

    def _supervise(self) -> None:
        """Claim the port, or wait for it to come free, until told to stop."""
        while True:
            served = self._claim_and_serve()
            if served or self._stop.is_set():
                return
            if self._stop.wait(self.reclaim_seconds):
                return

    def _claim_and_serve(self) -> bool:
        """True once this session has served and been shut down.

        False means the port was not ours to take this time round - either
        somebody else is serving it, or something that is not a server at all
        is sitting on it.
        """
        try:
            httpd = _Server((HOST, self.port), _Handler)
        except OSError as failure:
            self._note_who_has_it(failure)
            return False

        self._httpd = httpd
        # Checked *after* publishing the handle, which is what makes the two
        # orderings exhaustive: either this sees the flag and closes, or
        # ``stop`` set the flag afterwards and therefore reads a handle it can
        # shut down. Checking first instead leaves a window where neither
        # happens and the port stays held until the interpreter exits.
        if self._stop.is_set():
            self._httpd = None
            httpd.server_close()
            return True

        self.serving = True
        self.status = f"serving on {self.url}"
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception as failure:  # noqa: BLE001
            self.status = f"stopped serving after {failure!r}"
        finally:
            self.serving = False
            self._httpd = None
            try:
                httpd.server_close()
            except OSError:
                pass
        return True

    def _note_who_has_it(self, failure: OSError) -> None:
        """Say which of the two reasons this is, since only one is a problem.

        A port held by another session is the design working. A port held by
        something else is a collision somebody has to resolve, and it is worth
        saying so in terms that name the fix.
        """
        holder = identify(self.port)
        if holder is not None:
            self.status = (
                f"another session is serving {self.url} (pid {holder.get('pid')}); "
                "waiting to take over when it exits"
            )
            return
        self.status = (
            f"port {self.port} is held by something that is not a stack server "
            f"({failure.strerror or failure}); set failure_stack_server_port to "
            "an unused port, or turn failure_stack_server off"
        )


def start(port: int = DEFAULT_PORT) -> Optional[StackService]:
    """Begin serving, or begin waiting to. None if it could not even start.

    Never raises. This is called from session start, and a run that fails to
    begin because a diagnostic port could not be opened has been made worse by
    the thing that came to make it better.
    """
    try:
        service = StackService(port)
        service.start()
        return service
    except Exception:  # noqa: BLE001 - see the docstring
        return None
