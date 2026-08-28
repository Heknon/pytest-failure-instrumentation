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
under Linux's Yama LSM at ``ptrace_scope=1`` a tracer must be an ancestor of
its target, and workers nominate their own controller (see
:mod:`.probes.tracing`) - so a shared server reads its own workers and not
another session's, which nominated a different controller.
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

**Who may ask.** The bind, and a token if this run was started with one.

The token is *supplied*, never minted - ``--callstack-token`` or
``PYTEST_CALLSTACK_TOKEN`` - and this package never writes it anywhere. That
distinction is the whole design, and it comes from the two halves of the
problem being opposites:

*The port has to be published.* A port drawn at random is unguessable by
construction, which is the point of drawing it, so the run must write it down
for anything outside to find it.

*The token does not.* It is the one value both ends can agree on in advance,
because whoever starts the run picks it. Minting one here made it discoverable
instead - which meant writing it into the address file, which turned every
question about where a run may write its evidence into a question about where
a *secret* may live. That is a guarantee POSIX keeps with an ``0o600`` and
Windows does not: a mode there is not an ACL, so the file inherits the evidence
directory's and the promise quietly stops holding on a supported platform.

So the address file is ordinary data - a host, a port and a pid - and goes
wherever evidence goes, on every platform. The secret arrives the way secrets
already reach a container, a CI job and a shell, and leaves nothing behind.

**No token is the default, and it is the right one on loopback**, where the
reachable set is processes on this machine. On a box you share with people you
would not hand a debugger to, supply one or leave the server off: "only local"
and "only you" are different statements, and without a token only the first is
being made.

``/identity`` is open even when a token is set: it is what one session asks
another before standing down from a contested port, and two sessions that
minted nothing have no way to share a credential. It answers with a service
name, a version and a pid.

**What may be asked about is bounded too**, and separately, because on the
default nothing bounds who is asking. ``/stack`` answers for the serving
process and for the workers this run wrote ``.state`` files for, and refuses
every other pid with a 403 - see :func:`serves_pid`. The reader behind it does
not care whose process it is pointed at, so without that the endpoint was a way
to walk the pids of the machine and read the frames of anything on it.

**Which addresses it answers to is the other half of that**, because "bound to
loopback" is not the same as "only reachable by things on this machine that
mean to reach it". A page in the developer's browser can have its own name
re-resolved to 127.0.0.1 and then read this server same-origin - the
same-origin policy is satisfied rather than bypassed, so no CORS header is
involved in either the attack or the fix. What the page cannot choose is the
``Host`` header it sends, so a request that names anything but a loopback
spelling of this bind is refused - see :meth:`_Handler._addressed_to_this_server`.

**What it binds.** Loopback by default. A container is the exception that makes
it configurable: its UI lives outside it, and 127.0.0.1 inside a container is
unreachable from there. Binding anything else warns - and *without a token is
refused outright*, before the socket is opened. Serving every local process's
stack to whatever can route to the host is not a thing anybody configures on
purpose, and a warning is the wrong instrument for it: by the time one is read
the port has been open for the length of the run.
"""

from __future__ import annotations

import errno
import hmac
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

#: Binds that cannot be reached from another machine. Spelled here as well as
#: in :mod:`.config` because the check they gate is different: settings decide
#: what to warn about, and this decides what to refuse to bind.
LOCAL_ONLY = frozenset({LOOPBACK, LOOPBACK6, "localhost"})

#: How the token is presented, when there is one. A header is the right place
#: for a credential - query strings reach logs and shell history - and the
#: query parameter exists anyway because a person debugging with curl will
#: reach for it, and refusing would only teach them to turn the whole thing
#: off.
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"
TOKEN_PARAM = "token"

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

#: The largest number that could be a process id. No operating system this
#: runs on issues one outside it: Linux caps ``pid_max`` at 2^22 and Windows
#: process ids are DWORDs well inside this. The bound is here because the
#: reader is a *separate program* with its own idea of what an integer is -
#: handed 10^20 py-spy panics and reports a Rust backtrace, and handed a
#: negative number it reads it as a flag and reports its own usage. Neither is
#: an answer about a process, and both cost a subprocess and one of the slots
#: below to produce. A pid that cannot exist is refused where the reply can say
#: why instead.
MAX_PID = 2**31 - 1

#: How many external reads may be in flight at once. Each is a subprocess, and
#: each takes its full timeout when the target is wedged - which is exactly
#: what a UI polls hardest. Without a bound, a dashboard refreshing sixteen
#: stuck workers every second puts hundreds of readers on a machine that is
#: already in trouble, and this plugin's one rule is that it must never be what
#: makes a run worse. Refused rather than queued: a caller polling on a timer
#: wants to be told to back off, not held until its own deadline passes.
MAX_CONCURRENT_READS = 8

_readers = threading.BoundedSemaphore(MAX_CONCURRENT_READS)

#: What a worker's state file is called, which is the only thing under the
#: evidence root that says which processes belong to a run - see
#: :func:`serves_pid`. Spelled here as well as in :mod:`.topology` because the
#: two read it for different reasons: that module describes workers, and this
#: one only wants to know whether a pid is one.
STATE_SUFFIX = ".state"

#: How long :meth:`StackService.stop` waits for a session that has just
#: claimed the port to reach its accept loop, before giving up on shutting it
#: down cleanly. Bounded because a run that is over must not be held up by
#: this; short because the only thing between the claim and the loop is
#: writing one small file - see :meth:`StackService._reached_the_accept_loop`.
ACCEPT_LOOP_TIMEOUT = 5.0

#: How often that wait looks at the handle as well as the flag, so that the
#: interleaving it exists for costs a poll rather than the whole timeout.
ACCEPT_LOOP_POLL = 0.05


def _is_contention(failure: OSError) -> bool:
    """Whether this bind failed because somebody already has the address.

    The only error worth waiting on. Everything else says the address itself
    is unusable here, which no amount of retrying changes - and telling those
    apart is what decides between "stand down and take over later" and "say so
    and stop".
    """
    return failure.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE))


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

    For *reaching* the server, so a wildcard is resolved to something a client
    can connect to. Never for describing a bind - see :func:`bind_address`.
    """
    return _bracketed(reachable(host), port)


def bind_address(host: str, port: Optional[int]) -> str:
    """``host:port`` as it was *asked for*, for a message about the bind.

    :func:`authority` answers what to connect to, which rewrites a wildcard to
    loopback because nobody connects to 0.0.0.0 - Windows refuses outright. In
    an error message that rewrite is a lie: refusing ``--callstack-host
    0.0.0.0`` reported "refusing to bind 127.0.0.1:0", naming the one address
    that was not the problem and that the reader never typed.
    """
    return _bracketed(host, port)


def _bracketed(host: str, port: Optional[int]) -> str:
    if address_family(host) == socket.AF_INET6:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


class _Server(ThreadingHTTPServer):
    """Threaded, because reading a stack can take seconds.

    A single-threaded server would let one request against a wedged process
    hold up every other request for the length of py-spy's timeout - and a
    wedged process is what a UI asks about most.
    """

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: Any,
        evidence_root: Optional[Path] = None,
        token: str = "",
    ) -> None:
        # Set before the base class opens the socket, which reads it off the
        # instance. A class attribute cannot answer this: the family depends
        # on what this particular server was asked to bind.
        self.address_family = address_family(address[0])
        #: The base directory every run writes under - the parent of this
        #: session's own, since /workers describes the machine rather than
        #: whichever run happens to be hosting.
        self.evidence_root = evidence_root
        #: What a request must carry, or "" for a server that asks nothing.
        #: Supplied by whoever started the run and never written down - see
        #: the module docstring.
        self.token = token
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

    #: The last request that failed, for whoever is debugging this server.
    last_error: Optional[str] = None

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Never a traceback, for the same reason as ``log_message``.

        ``socketserver`` prints the whole traceback to stderr, which is where a
        human is reading pytest's output - so one malformed request could bury
        the report it was meant to leave alone. The failure is still kept, on
        the server, where something debugging this can find it; what is dropped
        is only its route to the terminal.

        A handler that raises is a defect here rather than a client's problem,
        so this is a backstop and not a way of ignoring one.
        """
        import traceback

        self.last_error = traceback.format_exc(limit=6)


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0, so every response closes its connection. Keep-alive would
    # require getting Content-Length right on every path including the error
    # ones, in exchange for saving a local TCP handshake.
    protocol_version = "HTTP/1.0"

    server_version = SERVICE

    #: Whether this request has already been answered. Read by the backstop
    #: in :meth:`do_GET`, which must not put a second status line on a
    #: connection that already carries one.
    _replied = False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        """Answer every request, including the ones that are not requests.

        Two guards wrap the dispatch, and both are here because a failure on
        this frame is not a 500 - it is *silence*. ``handle_error`` keeps the
        traceback off the terminal a human is reading pytest's output from
        (see :meth:`_Server.handle_error`), the connection is then closed with
        nothing written to it, and the caller is left holding a socket that
        hung up with no way to tell a broken server from a wrong address.

        The parse is the first of them because it was the first statement of
        the request and is reachable by anyone: ``urlparse`` raises
        ``ValueError: Invalid IPv6 URL`` on a request target with an unclosed
        bracket in its authority, before authentication and before any route is
        known. The spelling that reaches it is the absolute form, which a
        request line is allowed to carry - ``GET http://[oops HTTP/1.0``.
        (``GET //[oops`` does not, and it is worth saying why, because it is
        the shorter thing to try and it answers 404: ``BaseHTTPRequestHandler``
        collapses a leading ``//`` to one slash before this sees it, for
        reasons of its own. That is one client-facing spelling closed by
        accident, not the parse being safe.) A target that is not a URL is a
        malformed request and gets a 400 saying so.

        The blanket ``except`` after it is a backstop, not a licence. A handler
        that raises is this server's defect rather than a caller's problem, so
        the failure is still recorded on the server where whoever is debugging
        it can read it, and the reply says only that it happened: this
        server's exception text is assembled out of a run's own state, and
        that is not a thing to hand to whoever asked.
        """
        try:
            route = urlparse(self.path)
            query = parse_qs(route.query)
        except ValueError as malformed:
            self._reply(
                400,
                {"error": f"the request path is not a URL that can be parsed: {malformed}"},
            )
            return

        try:
            self._dispatch(route.path, query)
        except Exception:  # noqa: BLE001 - see the docstring
            # Recorded the way socketserver would have, since catching it here
            # is what stops it reaching socketserver at all.
            self.server.handle_error(self.request, self.client_address)
            if not self._replied:
                self._reply(500, {"error": "this server failed to handle the request"})

    def _dispatch(self, path: str, query: dict[str, list[str]]) -> None:
        if not self._addressed_to_this_server():
            self._reply(
                403,
                {
                    "error": "the Host header names "
                    f"{self.headers.get('Host', '')!r}, which is not an address "
                    "this server is bound to. A caller that reached a loopback "
                    "server under some other name was told that name resolves "
                    "here, and the browser that believed it would have read this "
                    "run's stacks as same-origin"
                },
            )
            return

        # Open, because it is what one session asks another before standing
        # down from a contested port - and the two have no way to share a
        # token, since neither minted one.
        if path == "/identity":
            self._reply(200, identity())
            return

        if not self._authorised(query):
            self._reply(
                401,
                {
                    "error": "this server reports what local processes are "
                    "executing, and this run was started with a token. Send it "
                    f"as '{AUTH_HEADER}: {AUTH_SCHEME} <token>' or "
                    f"?{TOKEN_PARAM}=<token>. It is the value in "
                    "PYTEST_CALLSTACK_TOKEN or --callstack-token; this server "
                    "did not mint it and has not written it anywhere"
                },
            )
            return

        if path == "/stack":
            self._stack(query)
        elif path == "/workers":
            self._workers(query)
        else:
            self._reply(404, {"error": "no such endpoint", "endpoints": ENDPOINTS})

    def _addressed_to_this_server(self) -> bool:
        """Whether ``Host`` names an address this server actually answers on.

        The attack this refuses is DNS rebinding, and it is worth spelling out
        because "it only listens on loopback" sounds like the answer to it
        already. A page the developer visits controls a name; the name is
        served with a one-second TTL and then re-resolved to 127.0.0.1. As far
        as the browser is concerned the origin has not changed - same scheme,
        same name, same port - so the page's own script may read the response
        it gets back. It is now reading ``/workers`` and ``/stack``: every node
        id in the run and every frame in every worker, out of a server that
        demanded nothing because loopback was taken to be the bound on who
        could ask. The same-origin policy is not bypassed here, it is
        *satisfied*, which is exactly why a CORS header would be the wrong
        instrument: CORS grants reads across origins, and this attack arranges
        for there to be only one origin.

        What the page cannot choose is ``Host``. The browser sends the name in
        the address bar, which is the attacker's, and never one of ours. So the
        names this server answers to are the check, and they are the loopback
        spellings a real client can have connected by.

        **A request with no Host at all is allowed.** HTTP/1.0 does not require
        one and this server speaks 1.0, so refusing would break a raw client
        for nothing: the only agent that can be made to rebind is a browser,
        and a browser always sends it.

        **A bind that is not loopback is not checked**, because there is
        nothing here to check against. The address a legitimate client uses
        there is the container's published one or some routable interface, and
        this process never learns which of its host's names that is. That bind
        is refused outright without a token (see
        :meth:`StackService._claim_and_serve`), and the token is what stands in
        for this check: a rebinding page has no way to come by one.
        """
        bound = str(getattr(self.server, "server_name", "") or "")
        if bound not in LOCAL_ONLY:
            return True
        offered = self.headers.get("Host", "")
        if not offered:
            return True
        return _hostname(offered) in LOCAL_ONLY

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        """Whether this request carries the token this run was started with.

        Total by construction when no token was supplied: the server asks
        nothing, so everything is authorised and the branch below is the whole
        of it. "Authorised" is only about who may *ask*, and on the default it
        is nobody in particular - which is why what may be asked *about* is
        bounded separately, by :func:`serves_pid`, rather than resting on this.

        Compared in constant time. The comparison is short and local and an
        attacker's timing signal across it is buried in HTTP jitter, but a
        credential check that leaks its progress is the kind of thing that is
        cheap to get right and awkward to explain having got wrong.

        **Compared as bytes, because the offered value is attacker-chosen.**
        ``compare_digest`` refuses two ``str`` unless both are pure ASCII, and
        raises ``TypeError`` rather than returning False when they are not - so
        a token with one non-ASCII character in it took this straight out of
        the handler: no reply at all where a 401 belonged, and a traceback into
        the stderr a human is reading pytest's output from, which is the thing
        ``log_message`` below exists to keep clean. Unauthenticated, and one
        URL-encoded character to trigger. Encoding both sides first makes the
        check total: every token gets compared rather than crashed on, and the
        comparison is still the constant-time one.

        That the *request* gets an answer whatever happens to it is
        :meth:`do_GET`'s guarantee and not this method's, which is a
        distinction this docstring used to blur by claiming it for itself. The
        same silence was reachable one statement earlier, from a path that is
        not a URL and never got as far as a token.
        """
        expected = getattr(self.server, "token", "")
        if not expected:
            return True  # this run supplied no token, so none can be demanded

        offered = (query.get(TOKEN_PARAM) or [""])[0]
        header = self.headers.get(AUTH_HEADER, "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == AUTH_SCHEME.lower():
            offered = offered or value.strip()
        if not offered:
            return False
        return hmac.compare_digest(
            offered.encode("utf-8", "surrogatepass"),
            expected.encode("utf-8", "surrogatepass"),
        )

    def _stack(self, query: dict[str, list[str]]) -> None:
        raw = (query.get("pid") or [""])[0]
        try:
            pid = int(raw)
        except ValueError:
            self._reply(400, {"error": f"pid must be a number, not {raw!r}"})
            return
        if not 0 < pid <= MAX_PID:
            # Refused here rather than by the reader, which answers a pid it
            # cannot use with its own usage text or a panic - see MAX_PID.
            self._reply(
                400,
                {
                    "error": f"pid must be between 1 and {MAX_PID}, not {pid}; "
                    "no process can have the id given"
                },
            )
            return

        evidence_root = getattr(self.server, "evidence_root", None)
        if not serves_pid(pid, evidence_root):
            # 403 rather than 404: the pid may well name a running process,
            # and saying "no such process" about one that exists would send a
            # caller looking for the wrong fault.
            serves = (
                "the process it is running in, and the workers whose state "
                "files are under the evidence directory it was given"
                if evidence_root is not None
                else "the process it is running in, and nothing else: it was "
                "given no evidence directory, so it knows of no workers"
            )
            self._reply(
                403,
                {
                    "error": f"pid {pid} is not part of any run this server is "
                    f"serving. It reports on {serves}; every other process on "
                    "this machine is somebody else's"
                },
            )
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
        self._replied = True
        body = json.dumps(payload, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            # Every body here is JSON and none of it is ever anything else, so
            # a browser that decides for itself what a body is can only decide
            # wrongly. Content sniffing is how a document a server called data
            # becomes a document the browser runs, and the strings in these
            # replies - a node id, a frame, a Host header echoed back - come
            # from outside this process.
            self.send_header("X-Content-Type-Options", "nosniff")
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


def _hostname(header: str) -> str:
    """The name out of a ``Host`` header, without its port or its brackets.

    All four spellings reach a server that binds both families and is
    connected to by either name: ``127.0.0.1``, ``127.0.0.1:8080``, ``[::1]``
    and ``[::1]:8080``. The brackets are what make the last two unambiguous -
    an IPv6 literal is all colons, so a rightmost-colon split would take
    ``::1`` apart - and they are stripped here rather than compared, so that
    the set of names this server answers to is written once, in one spelling.
    """
    value = header.strip()
    if value.startswith("["):
        return value[1:].partition("]")[0].lower()
    if value.count(":") == 1:
        return value.rpartition(":")[0].lower()
    return value.lower()


def serves_pid(pid: int, evidence_root: Optional[Path]) -> bool:
    """Whether this server has a reason to report on ``pid``.

    Every other process on the machine belongs to somebody else. The reader
    behind ``/stack`` does not care whose process it is asked about - py-spy
    reads whatever ptrace will let it read - so on the default, which supplies
    no token because loopback is the bound, ``/stack?pid=N`` walked from 1
    upwards was every Python process on the host: a developer's other session,
    a service, an interpreter holding a credential in a local. The port is
    opened to watch *this* run, and that is the set it answers about.

    Two things are in it. This process, which answers out of its own frames
    and needs no permission from anybody. And the workers, which are read out
    of the ``.state`` files the run is writing anyway - the same files
    ``/workers`` is assembled from, so a pid a UI can see here is a pid it can
    ask about, and nothing else is.

    Read on every request rather than once at startup, because the set is not
    fixed: xdist replaces a crashed worker mid-run, a second session starts
    under the same evidence root, and a snapshot taken when the port was
    claimed would refuse exactly the worker somebody is asking about because
    it is new. It costs one directory listing and one fixed-size read per
    worker, against a request that is about to spawn a subprocess.

    :mod:`.topology` reads the same files and is deliberately not called here:
    it answers "what is every worker doing", which is an event tail, a CPU
    rate and a liveness check per worker, for a question that only needs the
    one field.
    """
    if pid == os.getpid():
        return True
    if evidence_root is None:
        # Nothing to enumerate, so nothing is claimed. A server started
        # without an evidence directory serves its own stack and says so -
        # see the 503 from /workers, which is the same position.
        return False

    from .capture.state import read_state

    try:
        # One level down: the evidence root is the parent of the run
        # directories, since /workers describes the machine rather than
        # whichever run happens to be hosting.
        states = sorted(evidence_root.glob(f"*/*{STATE_SUFFIX}"))
    except OSError:
        return False
    for state in states:
        try:
            if int(read_state(state)["pid"]) == pid:
                return True
        except (KeyError, TypeError, ValueError):
            continue  # a torn or hand-written record says nothing either way
    return False


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
        on_giving_up: Optional[Any] = None,
        on_ready: Optional[Any] = None,
        session_id: str = "",
        token: str = "",
    ) -> None:
        #: What was asked for. 0 means "draw one", and is not what got bound.
        self.port = port
        self.host = host
        #: Where to write the address down, so a UI can find a drawn port. The
        #: evidence directory, because that is where the same UI already reads
        #: which pid is running which test.
        self.directory = directory
        self.reclaim_seconds = reclaim_seconds
        #: Called ``(verdict, detail)`` the first time this session concludes
        #: it cannot serve. Once, not per retry: a named port held by a
        #: stranger is re-probed for the life of the run, and an alert per
        #: probe would teach a reader to filter the whole kind out.
        self.on_giving_up = on_giving_up
        self._reported = False
        #: Called with a :class:`~.live_view.LiveStackServer` once this session
        #: is actually serving. At most once per session by construction: the
        #: supervisor stops looping the moment a claim succeeds.
        self.on_ready = on_ready
        #: Names the evidence directory, and goes in the announcement because
        #: the run id does not exist yet - see :mod:`.live_view`.
        self.session_id = session_id
        #: Supplied by whoever started the run, or "" for a server that asks
        #: nothing. Never minted here and never written to disk: the address
        #: of a drawn port has to be published, but a secret both ends can
        #: agree on in advance does not, and publishing it is what made the
        #: address file a credential store.
        self.token = token
        self._announcer: Optional[threading.Thread] = None
        #: What is actually bound, which is the only number worth publishing.
        self.bound_port: Optional[int] = None
        #: What happened, in words, for whoever asks why there are no stacks.
        self.status = "not started"
        self.serving = False
        self._httpd: Optional[_Server] = None
        self._stop = threading.Event()
        #: Set on the serving thread immediately before it enters the accept
        #: loop, and never cleared. It is what makes :meth:`stop` total: a
        #: server that has not reached that loop cannot be shut down, only
        #: closed - see :meth:`_reached_the_accept_loop`.
        self._accepting = threading.Event()
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
        """Bring the server down, or leave it alone, but always return.

        ``shutdown`` is only asked for once this session is known to be inside
        the accept loop. Asking unconditionally is what hung a pytest process
        that had finished its run: ``BaseServer.shutdown`` waits on an event
        that only ``serve_forever``'s own ``finally`` sets, so a server that
        never entered the loop is one that never comes out of that wait, and
        the bounded join below - the thing that was supposed to make this
        total - was never reached to bound anything.
        """
        self._stop.set()
        httpd = self._httpd
        if httpd is not None and self._reached_the_accept_loop():
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        thread = self._thread
        if thread is not None:
            # Bounded, and the thread is a daemon: a server that will not come
            # down must not be what keeps a finished run from exiting.
            thread.join(timeout=5.0)
        announcer = self._announcer
        if announcer is not None:
            # Briefly, and for a different reason: a product writing the
            # address down should be allowed to finish, but a slow one must
            # not hold up a run that is over.
            announcer.join(timeout=2.0)

    def _reached_the_accept_loop(self) -> bool:
        """Whether the serving thread is inside ``serve_forever`` yet.

        Bounded twice over, because the two ways of not being in the loop want
        different answers.

        The handle is the tighter bound and the reason this is a loop rather
        than one ``wait``. The interleaving this exists for is a ``stop`` that
        lands between the claim publishing its handle and the claim reading the
        stop flag: the claim then stands down, closing the server it never
        served from and clearing ``_httpd`` as it goes, and no flag will ever
        be set for this wait to see. Watching the handle as well as the flag
        costs that case one poll interval instead of the whole timeout.

        The clock is the outer bound, for a claim that is merely slow: between
        publishing the handle and entering the loop it writes one small file
        and starts one thread, which is milliseconds even on a filesystem
        having a bad day. Past that, leaving a daemon thread holding a socket
        the interpreter is about to close is the better of the two failures -
        this plugin does not get to be why a finished run has not exited.
        """
        deadline = time.monotonic() + ACCEPT_LOOP_TIMEOUT
        while not self._accepting.wait(ACCEPT_LOOP_POLL):
            if self._httpd is None or time.monotonic() >= deadline:
                return False
        return True

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
        if self.host not in LOCAL_ONLY and not self.token:
            # Not attempted at all, rather than bound and then regretted.
            # Serving every local process's stack to whatever can route here
            # is not something anybody configures on purpose, and a warning is
            # the wrong instrument for it: by the time one is read the port
            # has been open for the length of the run.
            self.status = (
                f"refusing to bind {bind_address(self.host, self.port)}: that is "
                "reachable from off this machine and no token was supplied"
            )
            self._give_up("BIND_REFUSED")
            return True

        try:
            httpd = _Server(
                (self.host, self.port),
                _Handler,
                self.directory.parent if self.directory is not None else None,
                self.token,
            )
        except OSError as failure:
            if not _is_contention(failure):
                # Not "somebody has this port" but "this address cannot be
                # bound": a host that is not an interface here, a sandbox that
                # forbids listening, a privileged port. Waiting changes none of
                # them, and it was previously decided by whether the port was
                # *drawn* - so a named port on a bad host was reported as held
                # by a stranger, advised to try another port, and then retried
                # the impossible bind every few seconds for the whole run.
                self.status = f"could not bind {bind_address(self.host, self.port)}: " + (
                    failure.strerror or str(failure)
                )
                self._give_up("BIND_REFUSED")
                return True
            if self.drawn:
                # Nobody can be holding "any free port", so contention here
                # means the machine has nothing free at all.
                self.status = f"could not draw a port on {self.host}: {failure.strerror or failure}"
                self._give_up("BIND_REFUSED")
                return True
            self._note_who_has_it(failure)
            return False
        except Exception as failure:  # noqa: BLE001 - see below
            # Not every unbindable address answers with an OSError, and the
            # ones that do not were escaping onto this thread. A port outside
            # 0-65535 raises OverflowError from ``bind`` - not an OSError, not
            # caught above - so ``--callstack-port 99999`` printed a raw
            # traceback, raised no incident, and left the server silently
            # never serving. Under a project running ``-W error`` pytest turns
            # that thread exception into a failure: a suite where every test
            # passed exited 1 because of a typo in a port number. This plugin
            # does not get to fail a run over its own settings.
            #
            # It is a bind refusal like any other - the address cannot be
            # bound and no amount of waiting changes that - so it is reported
            # as one, with the number that was asked for.
            self.status = (
                f"could not bind {bind_address(self.host, self.port)}: {failure}"
            )
            self._give_up("BIND_REFUSED")
            return True

        self.bound_port = int(httpd.server_address[1])

        self._httpd = httpd
        # Checked *after* publishing the handle, so that a ``stop`` arriving
        # now has something to shut down rather than leaving the port held
        # until the interpreter exits.
        #
        # That ordering is not, as this comment used to claim, exhaustive.
        # ``stop`` can land between the assignment above and the read below:
        # it takes the handle, and this branch then stands the server down
        # without ever having entered ``serve_forever`` - so the ``shutdown``
        # ``stop`` is about to call waits on an event that only that loop's
        # ``finally`` sets, forever, and the bounded join after it is never
        # reached. A pytest process that had finished its run hung there.
        # ``_accepting`` below is what closes the window: ``stop`` shuts down
        # only a server it has seen reach the loop.
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
        self._announce()
        # Set on the last statement before the loop, and read by ``stop``.
        # Nothing may come between the two: the flag's whole meaning is that
        # ``serve_forever`` is about to be entered and will therefore run its
        # ``finally``, which is the only thing that ever releases a caller
        # waiting in ``shutdown``.
        self._accepting.set()
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
            # An ordinary file. It held a credential once, which is what made
            # its mode load-bearing and turned "where may a run write its
            # evidence" into a question about where a *secret* may live - a
            # guarantee POSIX could keep and Windows could not. What is in it
            # now is a host, a port and a pid: the address of a server that
            # anyone who can reach it may query anyway.
            #
            # Still written through a temporary and renamed. That was never
            # about the secret: a reader polling this directory must never be
            # handed half an address.
            #
            # Created by this process or not at all. ``write_text`` opened the
            # temporary with O_CREAT and nothing else, which settles neither
            # question that matters at a well-known name in a directory other
            # things can write to. A symlink already sitting there is
            # *followed*, so the write lands wherever it points - and this
            # process is often the one with the interesting permissions. An
            # ordinary file already sitting there keeps the mode it was
            # created with, because O_CREAT does not change the mode of a file
            # that exists, so a 0666 leftover stays 0666 however this asks for
            # it. O_EXCL refuses both by refusing to open anything that is
            # already there, O_NOFOLLOW refuses the symlink even if something
            # wins the race against the unlink, and the mode is then the one
            # asked for here - 0600 not because there is a secret in it, but
            # because a file this process is about to rename into place should
            # not be one that anything else can rewrite in between.
            #
            # The stale one is removed first because O_EXCL would otherwise
            # make a crashed session's leftover ``.part`` permanent, and this
            # file is rewritten on every publish.
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                # Ordinary data, and readable as such. What this file holds is a
                # host, a port and a pid; the flags above are about who may
                # *substitute* the target, not who may read it. An 0o600 here
                # would fix the symlink and quietly break a UI running as
                # another uid, which is the case a published address exists to
                # serve - see the module docstring on why the secret was taken
                # out of this file rather than the file locked down.
                0o644,
            )
            # Bytes, so that nothing translates a newline on Windows and
            # changes the length of what a reader is about to parse.
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(json.dumps(payload).encode("utf-8"))
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
        self._give_up("PORT_TAKEN")

    def _announce(self) -> None:
        """Tell whoever asked that this is up, from a thread of its own.

        The thread is the point. This is called from the thread that is about
        to enter the accept loop, and the first thing an implementation
        naturally does is call the server it has just been told about - which
        against this thread would be a request waiting for a loop that has not
        started, waiting for the hook that is making the request. Worse than
        slow: the address is already published by now, so a hook that never
        returned would leave a written-down address that nothing ever answers,
        with the run none the wiser.

        Dispatching instead costs one short-lived thread and makes the
        obvious implementation the correct one.
        """
        callback = self.on_ready
        if callback is None:
            return
        try:
            payload = self.describe()
        except Exception:  # noqa: BLE001 - announcing must never break a run
            return
        self._announcer = threading.Thread(
            target=self._call_on_ready,
            args=(callback, payload),
            name="failure-instrumentation-stacks-ready",
            daemon=True,
        )
        try:
            self._announcer.start()
        except RuntimeError:
            # An interpreter already shutting down refuses new threads, and a
            # run that is ending has nothing to do with this announcement.
            self._announcer = None

    def _call_on_ready(self, callback: Any, payload: Any) -> None:
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 - reporting must never break a run
            pass

    def describe(self) -> Any:
        """What this server is, as the payload a product is handed.

        Built here rather than by the caller because the only correct source
        for the port is what got *bound* - a drawn port is requested as 0, and
        a caller assembling this from its own settings would publish that 0.
        """
        from . import __version__
        from .live_view import LiveStackServer

        return LiveStackServer(
            service=SERVICE,
            version=__version__,
            url=self.url,
            host=self.host,
            port=int(self.bound_port or self.port),
            token=self.token,
            pid=os.getpid(),
            directory=str(self.directory) if self.directory is not None else None,
            session_id=self.session_id,
        )

    def _give_up(self, verdict: str) -> None:
        """Say so once, to whoever asked to be told.

        Not raised when another of *our* sessions holds the port: that is the
        named mode working as designed, and reporting it would turn the
        ordinary case into an alert.
        """
        if self._reported or self.on_giving_up is None:
            return
        self._reported = True
        try:
            self.on_giving_up(verdict, self.status)
        except Exception:  # noqa: BLE001 - reporting must never break a run
            pass


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
    port: int = 0,
    host: str = LOOPBACK,
    directory: Optional[Path] = None,
    on_giving_up: Optional[Any] = None,
    on_ready: Optional[Any] = None,
    session_id: str = "",
    token: str = "",
) -> Optional[StackService]:
    """Begin serving, or begin waiting to. None if it could not even start.

    Never raises. This is called from session start, and a run that fails to
    begin because a diagnostic port could not be opened has been made worse by
    the thing that came to make it better.
    """
    try:
        service = StackService(
            port,
            host,
            directory,
            on_giving_up=on_giving_up,
            on_ready=on_ready,
            session_id=session_id,
            token=token,
        )
        service.start()
        return service
    except Exception:  # noqa: BLE001 - see the docstring
        return None
