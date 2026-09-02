"""An async client for the live-stack server, and typed answers from it.

The server is an HTTP API and a product could always have called it with
whatever it had to hand. What that cost, every time, was the same handful of
mistakes: assembling a URL out of a host and a port that were right until the
port was drawn, hard-coding an ``Authorization`` scheme this package reserves
the right to change, and - most of all - reading a failure as a failure of the
request rather than of the thing the request asked about.

That last one is what most of this module is. The server distinguishes, on
purpose, between *it* refusing to answer and the *reader* not managing to: a
py-spy that could not attach comes back 502 with a full body, because this
server is a gateway and the difference from "this server is broken" is the
whole content of the reply. A client that flattened both into "HTTP error"
would throw away the only part a caller can act on, so every failure here
arrives as its own exception carrying the server's own sentence, verbatim.

**httpx is an optional dependency.** The plugin does not import this module,
and a run that never talks to a live view never pays for it - see the
``client`` extra.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from .live_view import LiveStackServer
from .stack_server import (
    AUTH_HEADER,
    AUTH_SCHEME,
    DISCOVERY_PREFIX,
    DISCOVERY_SUFFIX,
)

try:  # pragma: no cover - exercised by the import test
    import httpx
except ModuleNotFoundError as missing:  # pragma: no cover - same
    raise ModuleNotFoundError(
        "the async client needs httpx, which is not a dependency of the plugin "
        "itself: install pytest-failure-instrumentation[client]"
    ) from missing


#: Long enough that a stack read is not cut off by the client that asked for
#: it. Reading a process stops it while py-spy walks its memory, and the server
#: queues past eight in flight, so a busy fleet answers in seconds rather than
#: milliseconds. `/workers` and `/identity` touch no process and return at once.
DEFAULT_TIMEOUT = 30.0


# --- what comes back ---------------------------------------------------
#
# Every model here ignores fields it does not know, which is the opposite of
# the rule everywhere else in this package. The others describe payloads this
# version produces; these describe payloads *another* version served, and a
# client that refused to parse a server one field newer than itself would break
# on the upgrade it was meant to survive. `version` on the identity is what
# dates the wire format when that matters.


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Identity(_Wire):
    """Who is serving this port, from ``/identity``."""

    service: str = ""
    version: str = ""
    #: The process serving. Under xdist this is the controller, and it is not
    #: any of the pids ``/workers`` reports on.
    pid: int = 0


class ReaderOptions(_Wire):
    """py-spy's flags **as applied**, which is not always as asked for."""

    native: bool = False
    locals: bool = False
    nonblocking: bool = False


class Local(_Wire):
    name: Optional[str] = None
    #: Rendered inside py-spy while the target was stopped. Text, never a live
    #: object, so reading it cannot execute anything.
    repr: str = ""
    #: Whether this was an argument to the frame rather than a local bound in
    #: it - the arguments are what the call was *asked* to do.
    argument: bool = False


class Frame(_Wire):
    function: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    #: The binary a native frame is in; None for a Python frame.
    module: Optional[str] = None
    native: bool = False
    #: None when locals were not requested, which is not the same as none.
    locals: Optional[list[Local]] = None


class Thread(_Wire):
    thread_id: Optional[int] = None
    thread_name: Optional[str] = None
    os_thread_id: Optional[int] = None
    #: Only a reader that paused the process can say, so None under
    #: ``nonblocking``.
    owns_gil: Optional[bool] = None
    active: Optional[bool] = None
    #: Innermost first, which is the order py-spy reports.
    frames: list[Frame] = []


class Callstack(_Wire):
    """One process's stack, from ``/stack``."""

    pid: int = 0
    source: str = ""
    #: Epoch seconds.
    captured_at: float = 0.0
    options: ReaderOptions = ReaderOptions()
    #: Present only when the read was asked for by name.
    worker: Optional[str] = None
    #: Anything asked for that could not be applied, in words. Absent rather
    #: than empty when there is nothing to say.
    notes: Optional[list[str]] = None
    threads: list[Thread] = []


class ScheduleSummary(_Wire):
    """How big the run is, and whether that answer can still move."""

    dist: Optional[str] = None
    collected: Optional[int] = None
    #: Collected and in nobody's queue yet, so somebody's total will grow.
    unassigned: Optional[int] = None
    #: Whether any worker's total can still change. A percentage drawn while
    #: this is false is a bar whose end moves - see :mod:`.schedule`.
    settled: Optional[bool] = None
    updated_at: Optional[float] = None


class Controller(_Wire):
    pid: Optional[int] = None
    #: A controller gone while its workers are not is a run nobody is
    #: collecting.
    alive: Optional[bool] = None


class Worker(_Wire):
    """One worker at one instant, as the run's own files describe it."""

    worker: str = ""
    pid: Optional[int] = None
    nodeid: Optional[str] = None
    #: A node id too long for its slot is trimmed from both ends and says so,
    #: which a consumer matching it against a collection has to know.
    nodeid_elided: bool = False
    phase: Optional[str] = None
    tests_started: Optional[int] = None
    tests_finished: Optional[int] = None
    #: The denominator, and the only figure here not out of this worker's own
    #: files: no worker knows how much it has been given.
    tests_assigned: Optional[int] = None
    tests_running: Optional[int] = None
    tests_queued: Optional[int] = None
    state_age_s: Optional[float] = None
    rss_mb: Optional[float] = None
    #: ``working`` / ``blocked`` / ``frozen`` / ``gone`` / ``unmeasured`` /
    #: ``finished``. A plain string rather than an enum, so a status added by a
    #: newer server arrives rather than failing to parse.
    status: str = ""
    #: The verdict in a sentence, naming the evidence behind it.
    why: str = ""
    process_exists: Optional[bool] = None
    heartbeat_age_s: Optional[float] = None
    #: None is not zero: "burned nothing" and "could not measure" differ.
    cpu_rate: Optional[float] = None


class Run(_Wire):
    session: str = ""
    run_id: Optional[str] = None
    directory: Optional[str] = None
    controller: Controller = Controller()
    started_at: Optional[float] = None
    schedule: Optional[ScheduleSummary] = None
    workers: list[Worker] = []


class WorkerFilter(_Wire):
    """Which names were asked for, and which of them matched nothing."""

    workers: list[str] = []
    #: Reported rather than dropped: a caller cannot otherwise tell "not
    #: running" from "misspelt".
    unmatched: list[str] = []


class WorkersSnapshot(_Wire):
    """Every run on one machine, from ``/workers``."""

    served_by: Identity = Identity()
    #: Epoch seconds. Every age in the workers is measured from this instant.
    observed_at: float = 0.0
    runs: list[Run] = []
    #: Present only when the request named workers.
    filter: Optional[WorkerFilter] = None

    @property
    def workers(self) -> list[Worker]:
        """Every worker across every run, for a caller that wants the fleet."""
        return [record for run in self.runs for record in run.workers]


# --- what goes wrong ---------------------------------------------------


class FailureServerError(Exception):
    """Anything that stopped a call from producing an answer."""


class ServerUnreachable(FailureServerError):
    """The request never got an answer: refused, timed out, DNS, TLS.

    Separate from every refusal below because it is the one failure that says
    nothing about the run - the address is stale, the host is gone, or the
    session that was serving has ended.
    """

    def __init__(self, url: str, cause: Exception) -> None:
        super().__init__(f"could not reach the stack server at {url}: {cause}")
        self.url = url
        self.cause = cause


class ServerRefused(FailureServerError):
    """The server answered, and the answer was a refusal.

    ``message`` is the server's own sentence, kept verbatim: these name their
    own fix - a sysctl, a ``--cap-add``, a flag the run was started without -
    and replacing them with a house message drops the only actionable part.
    """

    def __init__(self, status: int, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


class AuthenticationRequired(ServerRefused):
    """401: this run was started with a token and the request carried none."""


class AccessRefused(ServerRefused):
    """403, which this server uses for three different refusals.

    The ``Host`` header naming an address it is not bound to (its answer to DNS
    rebinding); a pid that is not part of any run it serves - deliberately not
    404, since the pid may well be a real process and saying "no such process"
    would send a caller after the wrong fault; and ``?locals`` on a run started
    with locals switched off. The sentence says which.
    """


class NotFound(ServerRefused):
    """404: no worker by that name under this server, or no such endpoint."""


class BadRequest(ServerRefused):
    """400: the request could not be read as naming one process."""


class EvidenceUnavailable(ServerRefused):
    """503: this server was given no evidence directory.

    It can still serve ``/stack`` for the process it runs in. It knows of no
    workers at all, so ``/workers`` has nothing to answer with.
    """


class ReaderFailed(ServerRefused):
    """502: the server was reached and the *reader* could not answer.

    py-spy missing, ptrace refused, the process gone between the listing and
    the read, or more reads already in flight than the server allows. The
    distinction from a broken server is the whole content of the reply, and the
    body carries the same ``pid``, ``options`` and ``notes`` a success would -
    so a caller can say which process it failed on and under what flags.
    """

    def __init__(self, status: int, message: str, payload: dict[str, Any]) -> None:
        super().__init__(status, message, payload)
        self.pid: Optional[int] = payload.get("pid")
        self.worker: Optional[str] = payload.get("worker")
        self.notes: list[str] = list(payload.get("notes") or [])
        self.options = ReaderOptions.model_validate(payload.get("options") or {})


#: Each status this server refuses with, and what that refusal means. Anything
#: else raises the base :class:`ServerRefused`, so a status added later is
#: still an error a caller can catch rather than a parse of a body that is not
#: the shape it expected.
_REFUSALS = {
    400: BadRequest,
    401: AuthenticationRequired,
    403: AccessRefused,
    404: NotFound,
    502: ReaderFailed,
    503: EvidenceUnavailable,
}


class FailureServerClient:
    """Talks to one live-stack server.

    Built from the :class:`~.live_view.LiveStackServer` the run hands to
    :func:`~.hookspec.pytest_failure_server_ready`, because that payload exists
    precisely so nobody assembles this by hand: it carries the bound port
    rather than the requested one, a URL already bracketed if the host is an
    IPv6 literal, and the token, whose header scheme is this package's to
    change.

    An ``httpx.AsyncClient`` may be passed in, and is then the caller's to
    close; one made here is closed by :meth:`aclose` or by leaving the ``async
    with`` block.
    """

    def __init__(
        self,
        server: Optional[LiveStackServer] = None,
        *,
        url: str = "",
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if server is None and not url:
            raise ValueError(
                "name the server: pass the LiveStackServer the run reported, or "
                "url= for one discovered another way"
            )
        self._server = server
        self._url = (server.url if server is not None else url).rstrip("/")
        self._headers = server.headers() if server is not None else _bearer(token)
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def __aenter__(self) -> FailureServerClient:
        return self

    async def __aexit__(self, *_unused: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the transport, if this client opened it."""
        if self._owns_client:
            await self._client.aclose()

    async def identity(self) -> Identity:
        """Who is serving this address.

        The one endpoint that never asks for a token, because it is what a
        caller uses to find out whether it is talking to one of ours at all -
        an address can be stale, and something else may since have taken the
        port.
        """
        return Identity.model_validate(await self._get("/identity", authenticated=False))

    async def workers(
        self,
        *,
        only: Optional[Sequence[str]] = None,
        timeout: Optional[float] = None,
    ) -> WorkersSnapshot:
        """Every run on this machine, and what each worker is doing.

        Costs the run nothing: it is assembled from files the workers were
        already writing, with no ptrace and nothing signalled. Poll this to
        know *where* to look, and :meth:`callstack` to look.

        ``only`` narrows it to named workers, which is applied to the directory
        listing rather than after the read - a worker nobody asked about costs
        a name comparison instead of a state read. Names that matched nothing
        come back under ``filter.unmatched`` rather than being dropped.
        """
        params: dict[str, Any] = {}
        if only:
            # Comma-joined: the server takes either shape, and one parameter
            # keeps a long fleet's URL readable in a log.
            params["worker"] = ",".join(only)
        return WorkersSnapshot.model_validate(
            await self._get("/workers", params=params, timeout=timeout)
        )

    async def callstack(
        self,
        *,
        pid: Optional[int] = None,
        worker: Optional[str] = None,
        native: bool = False,
        locals: bool = False,
        nonblocking: bool = False,
        timeout: Optional[float] = None,
    ) -> Callstack:
        """What one process is doing, right now.

        Name it by ``pid`` - what :meth:`workers` reports and a UI already
        holds - or by ``worker``, which the server resolves against the same
        files at the moment of the read. Prefer the name where a person is
        asking about a worker: resolving it yourself takes two requests, and a
        worker xdist replaced in between is a pid that now belongs to somebody
        else, with nothing in the answer to say so.

        This is not free. py-spy stops the target while it walks its memory, so
        take a reading when somebody asks for one and not on a timer.

        Raises :class:`ReaderFailed` where the server was reached and the read
        could not be taken - that carries the pid and the applied options, so a
        caller can say which process failed and under what flags.
        """
        if (pid is None) == (worker is None):
            raise ValueError(
                "name the process by pid or by worker, not both and not neither: "
                "they can disagree, and there is no right one to prefer"
            )
        params: dict[str, Any] = {"pid": pid} if pid is not None else {"worker": worker}
        # Only what is switched on. The server reads an absent flag as off, and
        # a URL carrying three falsehoods says the same thing as one carrying
        # none while being harder to read in a log.
        for name, switched_on in (
            ("native", native),
            ("locals", locals),
            ("nonblocking", nonblocking),
        ):
            if switched_on:
                params[name] = "true"
        return Callstack.model_validate(await self._get("/stack", params=params, timeout=timeout))

    async def _get(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        url = f"{self._url}/{path.lstrip('/')}"
        try:
            response = await self._client.get(
                url,
                params=params or None,
                headers=self._headers if authenticated else None,
                timeout=self._timeout if timeout is None else timeout,
            )
        except httpx.HTTPError as unreachable:
            raise ServerUnreachable(url, unreachable) from unreachable

        payload = _payload(response)
        if response.status_code >= 400:
            raise _REFUSALS.get(response.status_code, ServerRefused)(
                response.status_code, _message(response, payload), payload
            )
        return payload


def _bearer(token: str) -> dict[str, str]:
    """The header the server expects, or none where the run minted no token.

    Built from the server's own constants rather than spelled out here: the
    scheme is this package's to change, and a client that hard-coded it would
    break quietly on the upgrade that changed it.
    """
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"} if token else {}


def _payload(response: httpx.Response) -> dict[str, Any]:
    """The body as a mapping, or an empty one.

    Every reply this server writes is a JSON object. Something in front of it -
    a proxy, a captive portal, whatever took the port after the run ended - may
    write something else, and that is a refusal to report rather than a crash
    to propagate; :func:`_message` falls back to the raw text.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _message(response: httpx.Response, payload: dict[str, Any]) -> str:
    """The server's own sentence, or the best available stand-in."""
    stated = payload.get("error")
    if isinstance(stated, str) and stated:
        return stated
    text = (response.text or "").strip()
    return text or f"the server answered {response.status_code} and said nothing"


# --- the fleet ---------------------------------------------------------
#
# One server answers for one machine. A run that fans out across hosts, or a
# repository where two sessions share an evidence directory, leaves several -
# and each knows only its own. Reading them all and putting the answers back
# together is a job every consumer would otherwise write again, and would get
# subtly wrong in the same two places: losing the whole fleet to one host that
# did not answer, and forgetting that a pid means nothing without the machine
# it is a pid on.


class PublishedServer(_Wire):
    """A server's address, as it wrote it down for anyone to find.

    This is the ``callstack-<pid>.json`` a serving session publishes, and it is
    the only place the port appears: ``/workers`` describes the machine and
    never states its own address, so a caller that did not keep the address it
    dialled cannot recover it from the answer.

    No token. The file held one once, and taking it out is what let the file be
    world-readable so a UI running as another uid can find the run at all.
    """

    service: str = ""
    version: str = ""
    #: The serving session. Under xdist the controller, and none of the pids
    #: ``/workers`` reports on. The discovery file is named for it, so that two
    #: sessions sharing a directory do not overwrite each other's address.
    pid: int = 0
    host: str = ""
    #: What got bound, never what was asked for: a drawn port is requested as 0.
    port: int = 0
    url: str = ""
    drawn: bool = False
    started_at: Optional[float] = None

    def with_token(self, token: str = "") -> LiveStackServer:
        """This address, plus the credential it takes to be let in.

        The address file carries no token - taking the credential out of it is
        what let it be world-readable, so a UI running as another uid can find
        the run at all - and the token is not one value per fleet either. It is
        one per *run*: two sessions on one machine can have been started with
        different ones, and two hosts almost certainly were.

        So this is the seam. A caller that knows which token goes with which
        address pairs them here, and hands :func:`read_fleet` a server that
        brings its own rather than one the fleet has to guess for::

            servers = [found.with_token(tokens[found.url])
                       for found in discover_servers(directory)]
        """
        return LiveStackServer(
            service=self.service,
            version=self.version,
            url=self.url,
            host=self.host,
            port=self.port,
            token=token,
            pid=self.pid,
        )


def discover_servers(directory: Path, *, include_dead: bool = False) -> list[PublishedServer]:
    """Every server that has published an address under ``directory``.

    The evidence directory is the join key a product has from the first moment
    of a run, before xdist has built a run id, so this is how a consumer that
    was not handed a :class:`~.live_view.LiveStackServer` finds one.

    A session killed hard never retracts its file, and trusting a stale one
    costs a caller its whole timeout on a port nobody is listening to - so a
    record whose process is gone is dropped. That check reads the pid out of
    the filename and asks the kernel, which only answers for *this* machine:
    pass ``include_dead`` where the directory is shared from somewhere else and
    the local answer would be meaningless.

    Unreadable and half-written files are skipped rather than raised on. This
    runs against a directory a live run is writing into, and a caller asking
    who is serving should not be handed an exception because one file was mid
    rename.
    """
    try:
        candidates = sorted(directory.glob(f"{DISCOVERY_PREFIX}*{DISCOVERY_SUFFIX}"))
    except OSError:
        return []

    found: list[PublishedServer] = []
    for path in candidates:
        if not include_dead and not _still_running(path):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            found.append(PublishedServer.model_validate(record))
    return found


def _still_running(path: Path) -> bool:
    """Whether the session that published this address is still alive."""
    from .probes.process import is_running

    pid = path.stem[len(DISCOVERY_PREFIX) :]
    return is_running(int(pid)) if pid.isdigit() else True


class FleetMember(_Wire):
    """One server's answer, or the reason there is not one.

    Both halves are kept. A host that did not answer is not a host with no
    workers, and a fleet that dropped it would read as smaller rather than as
    partly unknown - which is the difference between "the run is nearly done"
    and "we cannot see half of it".
    """

    #: The address asked, which is what identifies this member: a pid is unique
    #: on one machine and nowhere else, so a consumer flattening the fleet into
    #: rows needs this beside each one.
    url: str = ""
    server: Optional[PublishedServer] = None
    snapshot: Optional[WorkersSnapshot] = None
    #: The refusal, verbatim, or the transport failure. None where it answered.
    error: Optional[str] = None
    #: The status it refused with, or None where nothing answered at all. Worth
    #: keeping apart: a 401 is a token this caller got wrong and a dead socket
    #: is a host that is gone, and a fleet that reported both as "unreachable"
    #: would send somebody restarting a machine over a credential.
    status: Optional[int] = None

    @property
    def answered(self) -> bool:
        return self.snapshot is not None


class FleetWorker(_Wire):
    """A worker, and the two things that say which one it is."""

    #: The server it lives on. `gw0` is a name every machine hands out for
    #: itself, so the name alone does not identify a worker in a fleet - and
    #: neither does the pid.
    url: str = ""
    #: The run it belongs to, since one machine can host several at once.
    session: str = ""
    worker: Worker = Worker()


class Fleet(_Wire):
    """Every server that was asked, and everything they said."""

    #: When the fleet was assembled, which is not any one server's
    #: ``observed_at``: the reads are concurrent but not simultaneous.
    observed_at: float = 0.0
    members: list[FleetMember] = []

    @property
    def answered(self) -> list[FleetMember]:
        return [member for member in self.members if member.answered]

    @property
    def silent(self) -> list[FleetMember]:
        """The ones that did not answer. Their workers are absent, not gone."""
        return [member for member in self.members if not member.answered]

    @property
    def workers(self) -> list[FleetWorker]:
        """Every worker across every server that answered, each saying where it is."""
        return [
            FleetWorker(url=member.url, session=run.session, worker=record)
            for member in self.members
            if member.snapshot is not None
            for run in member.snapshot.runs
            for record in run.workers
        ]


async def read_fleet(
    servers: Sequence[Any],
    *,
    token: str = "",
    only: Optional[Sequence[str]] = None,
    timeout: Optional[float] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Fleet:
    """Ask every server at once, and keep what each of them said.

    ``servers`` takes whatever a caller has: the
    :class:`~.live_view.LiveStackServer` payloads a run reported, the
    :class:`PublishedServer` records :func:`discover_servers` found, or bare
    URL strings.

    **A token belongs to a run, not to a fleet.** Two sessions on one machine
    can have been started with different ones, and two hosts almost certainly
    were - so each entry carries its own where it has one, and a
    ``LiveStackServer`` always does. ``token`` is the fallback for the entries
    that cannot: an address file holds no credential by design, and a bare URL
    is just a string. Where those need one each, pair them first with
    :meth:`PublishedServer.with_token` rather than reaching for this.

    The headers go out per request rather than on the transport, so servers on
    different tokens share one connection pool without ever being sent each
    other's.

    **One host failing costs only that host.** The reads run concurrently and
    every failure is caught per member, because the case this exists for is
    precisely the one where something is wrong somewhere: a fleet reader that
    raised on the first refusal would report nothing at exactly the moment
    there was something to see.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT) as owned:
            return await read_fleet(
                servers, token=token, only=only, timeout=timeout, client=owned
            )

    async def ask(entry: Any) -> FleetMember:
        url, carried, published = _target(entry, token)
        member = FleetMember(url=url, server=published)
        reader = FailureServerClient(
            url=url, token=carried, timeout=timeout or DEFAULT_TIMEOUT, client=client
        )
        try:
            member.snapshot = await reader.workers(only=only, timeout=timeout)
        except ServerRefused as refused:
            # Verbatim, and with the status: a refusal names its own fix, and
            # which refusal it was decides whose fix it is.
            member.error = refused.message
            member.status = refused.status
        except FailureServerError as failed:
            # Nothing answered. The message names the address, which is the
            # only thing there is to say about it.
            member.error = str(failed)
        return member

    gathered = await asyncio.gather(*(ask(entry) for entry in servers))
    return Fleet(observed_at=round(time.time(), 3), members=list(gathered))


def _target(entry: Any, token: str) -> tuple[str, str, Optional[PublishedServer]]:
    """One entry of ``servers``, as an address and the token to reach it with."""
    if isinstance(entry, str):
        return entry.rstrip("/"), token, None
    if isinstance(entry, LiveStackServer):
        # Its own, which is the point of being handed the payload: a fleet can
        # span runs, and two runs need not have been started with one token.
        return entry.url.rstrip("/"), entry.token, None
    if isinstance(entry, PublishedServer):
        # The address files carry no token by design, so this one falls back to
        # the caller's.
        return entry.url.rstrip("/"), token, entry
    raise TypeError(
        "a server is a LiveStackServer, a PublishedServer or a URL string, "
        f"not {type(entry).__name__}"
    )
