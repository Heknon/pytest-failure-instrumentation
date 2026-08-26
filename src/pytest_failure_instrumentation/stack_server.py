"""One HTTP endpoint per host that answers "what is process N doing right now".

The evidence this plugin writes is for reading *afterwards*. A UI watching a
run wants the opposite: the stack of a test that is still running, on demand,
without waiting for it to fail. That is a pull, and a pull needs something
listening.

**Two modes, and the port number picks between them.**

*Drawn* (``port = 0``, the default). The session binds whatever the kernel
hands it, serves its own run, and writes the address into the evidence
directory it is already writing to. Nothing is shared and nothing is contended,
so nothing can be lost to another session. This is right whenever the UI can
read that directory - which it must anyway, since that is where it learns which
pid is running which test.

*Named* (any other port). The session claims that exact port and shares it with
every other session on the machine, because a fixed port cannot be bound twice.
The first to start claims it and serves; the rest find it claimed and wait.
Naming a port is worth it when something outside has to be told the address
once and for all - a firewall rule, a UI with it compiled in, a published
container port.

Both work because the answer does not come from the serving session's memory -
it comes from reading the target process, which needs no relationship to the
reader. That is also the one thing a *named* port cannot promise everywhere:
under Linux's Yama LSM at ``ptrace_scope=1``, a process may only read its own
descendants, so a shared server reads its own workers and not another session's.
A drawn port has no such gap, because every session reads only its own.

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

**What it binds.** Loopback by default, because it reports what local
processes are executing and asks nobody who they are. A container is the
exception that makes it configurable: its UI lives outside it, and 127.0.0.1
inside a container is unreachable from there. Binding anything else warns, once,
at settings time.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .probes import process, pyspy, stacks
from .probes.platform_flags import IS_WINDOWS

#: What ``/identity`` answers with. The whole singleton scheme rests on this
#: string being unique to this package, so it is the distribution name.
SERVICE = "pytest-failure-instrumentation-stacks"

#: The default bind, and what a drawn address is advertised as. A server
#: bound to 0.0.0.0 is *reached* on a routable address, never on 0.0.0.0
#: itself - Windows refuses to connect to it outright - so what gets written
#: down for a UI is never the wildcard.
LOOPBACK = "127.0.0.1"

#: Its IPv6 spelling, for a server that was asked to bind the IPv6 wildcard.
LOOPBACK6 = "::1"

#: What the wildcard binds mean, so an advertised URL can avoid them.
IPV6_WILDCARD = "::"
WILDCARDS = frozenset({"0.0.0.0", IPV6_WILDCARD, ""})

#: Files a UI reads to find the servers running on this machine, one per
#: serving session, named for the pid that wrote it so that two sessions in one
#: evidence directory cannot overwrite each other.
DISCOVERY_PREFIX = "callstack-"
DISCOVERY_SUFFIX = ".json"

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


def address_family(host: str) -> int:
    """IPv4 unless the host is written as an IPv6 address.

    ``HTTPServer`` is AF_INET and nothing about it notices otherwise, so a
    server asked for ``::1`` opened an IPv4 socket and failed to bind it -
    while the settings said ``::1`` was a supported loopback and warned about
    nothing. Resolved from the literal rather than guessed: a name is left to
    IPv4, which is what the stack of every other default here assumes.
    """
    try:
        socket.inet_pton(socket.AF_INET6, host)
    except (OSError, ValueError, AttributeError):
        return socket.AF_INET
    return socket.AF_INET6


def authority(host: str, port: Optional[int]) -> str:
    """``host:port`` for a URL, with an IPv6 literal bracketed.

    ``http://::1:8080/`` is not a URL anybody can parse - the colons are
    ambiguous - and every client rejects it. RFC 3986 wants brackets.
    """
    reached = reachable(host)
    if address_family(reached) == socket.AF_INET6:
        return f"[{reached}]:{port}"
    return f"{reached}:{port}"


class _Server(ThreadingHTTPServer):
    """Threaded, because reading a stack can take seconds.

    A single-threaded server would let one request against a wedged process
    hold up every other request for the length of py-spy's timeout - and a
    wedged process is what a UI asks about most.
    """

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], handler: Any, evidence_root: Optional[Path] = None
    ) -> None:
        # Set before the base class opens the socket, which reads it off the
        # instance. A class attribute cannot answer this: the family depends
        # on what this particular server was asked to bind.
        self.address_family = address_family(address[0])
        #: The base directory every run writes under - the parent of this
        #: session's own, since /workers describes the machine rather than
        #: whichever run happens to be hosting.
        self.evidence_root = evidence_root
        super().__init__(address, handler)

    def server_bind(self) -> None:
        """Bind without asking DNS who we are.

        ``HTTPServer.server_bind`` fills in ``server_name`` with
        ``socket.getfqdn(host)``, which is a *reverse DNS lookup performed
        while the server is being constructed*. On a Linux desktop that costs
        three milliseconds; on macOS it goes through mDNS and can take tens of
        seconds, during which this thread is inside the constructor and the
        session has no server at all.

        That is not a hypothesis about macOS. It failed there, on every job:
        every test that binds and serves timed out with the status still at
        its initial value, because nothing past the constructor had run.

        ``server_name`` is only ever used to fill in CGI variables and a
        default ``Host`` header, and nothing here serves CGI or originates
        requests. So the lookup buys nothing and is skipped: the address the
        caller asked for is a better answer than whatever DNS says about it,
        and it is available immediately.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

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
        elif route.path == "/workers":
            self._workers(parse_qs(route.query))
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

    def _workers(self, query: dict[str, list[str]]) -> None:
        """Every run on this machine, and what each worker is doing.

        One request rather than one per worker, and no ptrace: this is
        assembled from files the run was writing anyway. A UI polls this to
        know *where* to look, and ``/stack`` to look.

        ``?worker=`` narrows it. Both spellings are accepted and both shapes -
        repeated parameters and one comma-separated list - because a caller
        will write whichever occurs to them and being strict about it would
        only ever produce a wrong answer rather than a corrected one.
        """
        from . import topology

        base = getattr(self.server, "evidence_root", None)
        if base is None:
            self._reply(
                503,
                {
                    "error": "this server was not given an evidence directory, so "
                    "it cannot say what is running; only /stack is available"
                },
            )
            return
        self._reply(200, topology.snapshot(base, served_by=identity(), only=_named(query)))

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
    "/workers": "every run on this machine, and what each worker is doing",
    "/workers?worker=gw0,gw3": "only those workers; repeat the parameter or comma-separate",
    "/stack?pid=N": "the current stack of every thread in process N",
}


#: Both spellings of the worker filter. A caller writes whichever occurs to
#: them, and refusing the other only produces a wrong answer.
WORKER_PARAMS = ("worker", "workers")


def _named(query: dict[str, list[str]]) -> Optional[list[str]]:
    """The workers a request asked about, or None for all of them.

    Repeated parameters and comma-separated values both work, and mixing them
    works too. Nothing here becomes a path: these names are compared against a
    directory listing, never joined onto one.
    """
    values = [value for key in WORKER_PARAMS for value in query.get(key, [])]
    if not values:
        return None
    return [name for value in values for name in value.split(",")]


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


def reachable(host: str) -> str:
    """An address a client can actually connect to.

    A wildcard bind means "every interface", which is a thing to listen on and
    not a thing to connect to: Windows refuses ``connect()`` to 0.0.0.0
    outright, and on POSIX it only works by accident. Loopback always reaches a
    wildcard-bound server from the same machine, and a client on another
    machine was never going to be told the address by this process anyway.
    """
    if host == IPV6_WILDCARD:
        # A socket bound to :: without dual-stack does not answer on an IPv4
        # address at all, so the IPv4 loopback would be a wrong answer here.
        return LOOPBACK6
    return LOOPBACK if host in WILDCARDS else host


def identify(
    port: int, host: str = LOOPBACK, timeout: float = IDENTIFY_TIMEOUT
) -> Optional[dict[str, Any]]:
    """Whoever holds ``port``, if it is one of ours; None for anything else.

    Proxies are explicitly disabled. ``urlopen`` honours ``http_proxy`` from
    the environment, which CI sets constantly - and a request for 127.0.0.1
    sent to a proxy either fails or, much worse, reaches something else
    entirely and gets believed.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
            f"http://{authority(host, port)}/identity", timeout=timeout
        ) as response:
            payload = json.loads(response.read(4096))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if isinstance(payload, dict) and payload.get("service") == SERVICE:
        return payload
    return None


class StackService:
    """This session's part in serving live stacks.

    With a drawn port that is the whole of it: bind, serve, write down where.
    With a named one it is a claim that may be lost, and then it is either
    serving or waiting for whoever is.

    Nothing here ever raises into a run: a session that cannot serve and cannot
    find a server simply has no live stacks, which is the same position every
    session was in before this module existed.
    """

    def __init__(
        self,
        port: int = 0,
        host: str = LOOPBACK,
        directory: Optional[Path] = None,
        reclaim_seconds: float = RECLAIM_SECONDS,
    ) -> None:
        #: What was asked for. 0 means "draw one", and is not what got bound.
        self.port = port
        self.host = host
        #: Where to write the address down, so a UI can find a drawn port. The
        #: evidence directory, because that is where the same UI already reads
        #: which pid is running which test.
        self.directory = directory
        self.reclaim_seconds = reclaim_seconds
        #: What is actually bound, which is the only number worth publishing.
        self.bound_port: Optional[int] = None
        #: What happened, in words, for whoever asks why there are no stacks.
        self.status = "not started"
        self.serving = False
        self._httpd: Optional[_Server] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def drawn(self) -> bool:
        """Whether this session drew its own port rather than naming one.

        The difference is not cosmetic: a drawn port is nobody else's, so there
        is no claim to lose, nothing to wait for and nothing to hand over.
        """
        return self.port == 0

    @property
    def url(self) -> str:
        return f"http://{authority(self.host, self.bound_port or self.port)}"

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
            httpd = _Server(
                (self.host, self.port),
                _Handler,
                self.directory.parent if self.directory is not None else None,
            )
        except OSError as failure:
            if self.drawn:
                # Nobody is holding "any free port". This is a bad interface, a
                # sandbox that forbids listening, or a machine with nothing
                # free - none of which a later attempt would find changed.
                self.status = f"could not bind {self.host}: {failure.strerror or failure}"
                return True
            self._note_who_has_it(failure)
            return False

        self.bound_port = int(httpd.server_address[1])

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

        # Published *before* anything is told the server is up. The socket is
        # bound and listening from the constructor, so the order costs nothing -
        # and the other order is a window in which a reader sees a serving
        # session whose address is not written down yet. macOS found it.
        self._publish()
        self.serving = True
        self.status = f"serving on {self.url}"
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception as failure:  # noqa: BLE001
            self.status = f"stopped serving after {failure!r}"
        finally:
            self.serving = False
            self._httpd = None
            self._retract()
            self.bound_port = None
            try:
                httpd.server_close()
            except OSError:
                pass
        return True

    # -- saying where this is -------------------------------------------

    @property
    def _discovery_path(self) -> Optional[Path]:
        """Named for the pid, not the run.

        Two sessions in one repository share an evidence directory, and a
        single well-known filename would mean the second silently overwriting
        the first's address - leaving a UI reading one server and believing it
        had found them all.
        """
        if self.directory is None:
            return None
        return self.directory / f"{DISCOVERY_PREFIX}{os.getpid()}{DISCOVERY_SUFFIX}"

    def _publish(self) -> None:
        """Write the address down, then sweep away the dead.

        Written whole and renamed into place, because a UI reading a file that
        is being written gets half an address and no way to know it.
        """
        path = self._discovery_path
        if path is None:
            return
        payload = dict(
            identity(),
            host=self.host,
            port=self.bound_port,
            url=self.url,
            drawn=self.drawn,
            started_at=round(time.time(), 3),
        )
        temporary = path.with_name(path.name + ".part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            return  # bookkeeping must never break a run
        sweep_dead_servers(self.directory)

    def _retract(self) -> None:
        path = self._discovery_path
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass  # a stale file is swept by whoever publishes next

    def _note_who_has_it(self, failure: OSError) -> None:
        """Say which of the two reasons this is, since only one is a problem.

        A port held by another session is the design working. A port held by
        something else is a collision somebody has to resolve, and it is worth
        saying so in terms that name the fix.
        """
        holder = identify(self.port, self.host)
        if holder is not None:
            self.status = (
                f"another session is serving {self.url} (pid {holder.get('pid')}); "
                "waiting to take over when it exits"
            )
            return
        self.status = (
            f"port {self.port} is held by something that is not a stack server "
            f"({failure.strerror or failure}); pass --callstack-port with an "
            "unused port, or leave it off entirely and let one be drawn"
        )


def sweep_dead_servers(directory: Optional[Path]) -> None:
    """Remove the address files of sessions that are no longer running.

    A session killed hard never retracts its own, and a UI that trusts a stale
    file spends its timeout on a port nobody is listening to. The pid is in the
    filename precisely so this costs no read: a signal 0 answers whether that
    process still exists.

    Only *dead* ones. Every other file here belongs to a session that is very
    much alive, and deleting those is how a cleanup becomes an outage.
    """
    if directory is None:
        return
    try:
        candidates = list(directory.glob(f"{DISCOVERY_PREFIX}*{DISCOVERY_SUFFIX}"))
    except OSError:
        return
    for path in candidates:
        pid = path.stem[len(DISCOVERY_PREFIX):]
        if not pid.isdigit() or process.is_running(int(pid)):
            continue
        try:
            path.unlink()
        except OSError:
            pass


def start(
    port: int = 0, host: str = LOOPBACK, directory: Optional[Path] = None
) -> Optional[StackService]:
    """Begin serving, or begin waiting to. None if it could not even start.

    Never raises. This is called from session start, and a run that fails to
    begin because a diagnostic port could not be opened has been made worse by
    the thing that came to make it better.
    """
    try:
        service = StackService(port, host, directory)
        service.start()
        return service
    except Exception:  # noqa: BLE001 - see the docstring
        return None
