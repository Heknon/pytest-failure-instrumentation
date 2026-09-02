"""The async client, against a server that is actually serving.

Nothing here mocks a transport. The whole value of this client is the mapping
from what the server does to something a caller can act on, and a fake that
answers the way the client expects tests the expectation rather than the
mapping - the statuses this file asserts on are the ones the real server
chose, and several of them are deliberate: 403 rather than 404 for a pid that
is not this run's, 502 rather than 500 when the reader is the thing that
failed.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from pytest_failure_instrumentation import stack_server
from pytest_failure_instrumentation.live_view import LiveStackServer

httpx = pytest.importorskip("httpx", reason="the client extra is not installed")

from pytest_failure_instrumentation.client import (  # noqa: E402 - after the skip
    AccessRefused,
    AuthenticationRequired,
    BadRequest,
    EvidenceUnavailable,
    FailureServerClient,
    Fleet,
    NotFound,
    PublishedServer,
    ReaderFailed,
    ServerRefused,
    ServerUnreachable,
    discover_servers,
    read_fleet,
)


def free_port() -> int:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        return int(holder.getsockname()[1])


def wait_for(condition, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.05)
    return None


@pytest.fixture
def serving():
    """Servers started by a test, stopped however the test ends."""
    started: list[stack_server.StackService] = []

    def start(**kwargs: Any) -> stack_server.StackService:
        service = stack_server.StackService(free_port(), **kwargs)
        service.start()
        started.append(service)
        assert wait_for(lambda: service.serving and service.bound_port), service.status
        return service

    yield start
    for service in started:
        service.stop()


def run_directory(root: Path, name: str = "run-a") -> Path:
    """This session's own run directory, whose *parent* the server serves.

    `StackService(directory=...)` is told where this run writes, and reports
    on the directory above it - `/workers` describes the machine rather than
    whichever run happens to be hosting the server. Handing it a tmp_path
    directly would serve tmp_path's parent, which is every other test's
    directory too.
    """
    made = root / name
    made.mkdir(parents=True, exist_ok=True)
    return made


def connected(service: stack_server.StackService, token: str = "") -> FailureServerClient:
    """A client built the way a product builds one: from the run's own payload."""
    return FailureServerClient(
        LiveStackServer(
            service=stack_server.SERVICE,
            url=service.url,
            host=service.host,
            port=service.bound_port or 0,
            token=token,
        )
    )


def run(coroutine):
    return asyncio.run(coroutine)


# -- the three calls ------------------------------------------------------


def test_identity_says_who_is_serving(serving):
    service = serving()

    async def ask():
        async with connected(service) as client:
            return await client.identity()

    identity = run(ask())
    assert identity.service == stack_server.SERVICE
    assert identity.version
    assert identity.pid > 0


def test_identity_needs_no_token_where_everything_else_does(serving):
    # The endpoint a caller uses to find out whether it is talking to one of
    # ours at all, which it cannot do if it has to be let in first.
    service = serving(token="s3cret")

    async def ask():
        async with FailureServerClient(url=service.url) as client:
            return await client.identity()

    assert run(ask()).service == stack_server.SERVICE


def test_workers_reports_the_runs_under_the_evidence_directory(serving, tmp_path: Path):
    service = serving(directory=run_directory(tmp_path))

    async def ask():
        async with connected(service) as client:
            return await client.workers()

    snapshot = run(ask())
    assert snapshot.observed_at > 0
    assert snapshot.served_by.pid == os.getpid()
    # No run has written state here, so the fleet is empty rather than absent.
    assert snapshot.workers == []


def test_a_name_that_matched_nothing_is_reported_rather_than_dropped(serving, tmp_path: Path):
    # Otherwise a caller cannot tell "not running" from "misspelt".
    service = serving(directory=run_directory(tmp_path))

    async def ask():
        async with connected(service) as client:
            return await client.workers(only=["gw0", "gw9"])

    snapshot = run(ask())
    assert snapshot.filter is not None
    assert snapshot.filter.workers == ["gw0", "gw9"]
    assert snapshot.filter.unmatched == ["gw0", "gw9"]


def test_a_stack_read_answers_or_says_why_it_could_not(serving):
    """The server's own pid is the one process it may always read.

    Whether py-spy is installed decides which of the two answers arrives, and
    both are correct - so this asserts the shape of each rather than requiring
    the reader to be present in the environment running the suite.
    """
    service = serving()

    async def ask():
        async with connected(service) as client:
            return await client.callstack(pid=os.getpid(), locals=True)

    try:
        stack = run(ask())
    except ReaderFailed as failed:
        # 502: the gateway reached a reader that could not answer. The body
        # carries what a success would, so a caller can still say which
        # process failed and under what flags.
        assert failed.status == 502
        assert failed.pid == os.getpid()
        assert failed.options.locals is True
        assert failed.message
    else:
        assert stack.pid == os.getpid()
        assert stack.source
        assert stack.captured_at > 0
        assert stack.options.locals is True


# -- naming the process ---------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"pid": 1, "worker": "gw0"}, id="both"),
        pytest.param({}, id="neither"),
    ],
)
def test_a_read_names_one_process_or_the_call_is_refused(serving, arguments):
    # Refused here rather than at the server: the two can disagree and there
    # is no right one to prefer, so a request that names both is a mistake
    # worth reporting without spending a round trip on it.
    service = serving()

    async def ask():
        async with connected(service) as client:
            await client.callstack(**arguments)

    with pytest.raises(ValueError):
        run(ask())


def test_an_unknown_worker_name_is_not_found(serving, tmp_path: Path):
    service = serving(directory=run_directory(tmp_path))

    async def ask():
        async with connected(service) as client:
            await client.callstack(worker="gw404")

    with pytest.raises(NotFound) as refused:
        run(ask())
    assert refused.value.status == 404
    assert "gw404" in refused.value.message


def test_a_pid_this_server_does_not_serve_is_refused_rather_than_missing(serving):
    """403 and not 404, which the server is deliberate about.

    The pid may well name a running process, and answering "no such process"
    about one that exists sends a caller looking for the wrong fault.
    """
    service = serving()

    async def ask():
        async with connected(service) as client:
            await client.callstack(pid=999999)

    with pytest.raises(AccessRefused) as refused:
        run(ask())
    assert refused.value.status == 403


def test_a_pid_no_process_could_have_is_a_bad_request(serving):
    service = serving()

    async def ask():
        async with connected(service) as client:
            await client.callstack(pid=0)

    with pytest.raises(BadRequest) as refused:
        run(ask())
    assert refused.value.status == 400


# -- refusals -------------------------------------------------------------


def test_a_run_with_a_token_refuses_a_caller_without_one(serving, tmp_path: Path):
    service = serving(directory=run_directory(tmp_path), token="s3cret")

    async def ask():
        async with FailureServerClient(url=service.url) as client:
            await client.workers()

    with pytest.raises(AuthenticationRequired) as refused:
        run(ask())
    assert refused.value.status == 401
    # The server's own sentence, which names where the token comes from.
    assert refused.value.message


def test_a_server_with_no_evidence_directory_says_so(serving):
    # It can still serve /stack for the process it runs in; it knows of no
    # workers at all.
    service = serving()

    async def ask():
        async with connected(service) as client:
            await client.workers()

    with pytest.raises(EvidenceUnavailable) as refused:
        run(ask())
    assert refused.value.status == 503


def test_an_address_nobody_is_serving_is_unreachable_rather_than_refused(serving):
    """The one failure that says nothing about the run.

    A stale address, a host that is gone, a session that ended - none of them
    are the server refusing, and a caller retries or re-discovers rather than
    reading the message for a fix.
    """

    async def ask():
        async with FailureServerClient(url=f"http://127.0.0.1:{free_port()}") as client:
            await client.identity()

    with pytest.raises(ServerUnreachable) as gone:
        run(ask())
    assert not isinstance(gone.value, ServerRefused)


# -- the transport --------------------------------------------------------


def test_a_borrowed_client_is_left_open_for_its_owner(serving):
    service = serving()

    async def ask():
        async with httpx.AsyncClient() as borrowed:
            async with FailureServerClient(url=service.url, client=borrowed) as client:
                await client.identity()
            # Closing ours must not close theirs - the caller may be pooling
            # one client across every server in a fleet.
            assert not borrowed.is_closed
            return True

    assert run(ask())


def test_naming_no_server_at_all_is_refused_before_any_request():
    with pytest.raises(ValueError):
        FailureServerClient()


# -- the fleet ------------------------------------------------------------


def test_discovery_finds_the_servers_that_published_an_address(serving, tmp_path: Path):
    """The address files are the only place the port appears.

    ``/workers`` describes the machine and never states its own address, so a
    consumer that was not handed a LiveStackServer finds one this way.
    """
    published_into = run_directory(tmp_path)
    service = serving(directory=published_into)

    published = wait_for(lambda: discover_servers(published_into))
    assert published, "the serving session published no address"
    assert published[0].port == service.bound_port
    assert published[0].url == service.url
    assert published[0].service == stack_server.SERVICE


def test_a_stale_address_is_dropped_unless_it_is_asked_for(tmp_path: Path):
    """A session killed hard never retracts its file.

    Trusting one costs a caller its whole timeout on a port nobody is
    listening to - but the check reads the kernel, which only answers for this
    machine, so a shared directory has to be able to opt out of it.
    """
    dead = tmp_path / "callstack-999999.json"
    dead.write_text('{"service": "x", "host": "127.0.0.1", "port": 1, "url": "http://127.0.0.1:1"}')

    assert discover_servers(tmp_path) == []
    assert len(discover_servers(tmp_path, include_dead=True)) == 1


def test_a_half_written_address_is_skipped_rather_than_raised_on(tmp_path: Path):
    # This runs against a directory a live run is writing into.
    (tmp_path / f"callstack-{os.getpid()}.json").write_text("{not json at all")
    assert discover_servers(tmp_path) == []


def evidence_with_worker(root: Path, session: str, name: str) -> Path:
    """One run directory holding one worker, enough for topology to report it.

    The pid is this process because it has to be one that exists: a dead pid
    is reported ``gone``, which would be testing that rule instead.
    """
    directory = root / session
    directory.mkdir(parents=True, exist_ok=True)
    moment = time.time()
    (directory / "owner.json").write_text(json.dumps({"pid": os.getpid(), "started_at": moment}))
    state = json.dumps({
        "pid": os.getpid(), "nodeid": f"test_x.py::{name}", "phase": "call",
        "time": moment, "tests_started": 1, "tests_finished": 0,
    }).encode()
    (directory / f"{name}.state").write_bytes(state + b"\x00" * (5120 - len(state)))
    # The run directory itself: the server serves the directory above it.
    return directory


def test_the_fleet_reads_every_server_and_says_where_each_worker_is(serving, tmp_path: Path):
    first = serving(directory=run_directory(tmp_path))
    second = serving(directory=run_directory(tmp_path))

    fleet = run(read_fleet([first.url, second.url]))

    assert len(fleet.answered) == 2
    assert fleet.silent == []
    assert {member.url for member in fleet.members} == {first.url, second.url}
    assert fleet.observed_at > 0


def test_two_machines_can_hold_the_same_worker_name_and_the_same_pid(serving, tmp_path: Path):
    """Which is why a flattened fleet says where every row came from.

    `gw0` is a name each machine hands out for itself and a pid is unique on
    one machine and nowhere else, so these two rows are indistinguishable by
    everything except the server they were read from.
    """
    here = serving(directory=evidence_with_worker(tmp_path / "here", "run-a", "gw0"))
    there = serving(directory=evidence_with_worker(tmp_path / "there", "run-b", "gw0"))

    fleet = run(read_fleet([here.url, there.url]))

    rows = fleet.workers
    assert len(rows) == 2
    # Same name, same pid - and still telling apart, by address and by run.
    assert {row.worker.worker for row in rows} == {"gw0"}
    assert {row.worker.pid for row in rows} == {os.getpid()}
    assert {row.url for row in rows} == {here.url, there.url}
    assert {row.session for row in rows} == {"run-a", "run-b"}


def test_one_host_that_did_not_answer_costs_only_that_host(serving, tmp_path: Path):
    """The case the whole thing exists for.

    A reader that raised on the first refusal would report nothing at exactly
    the moment there was something to see.
    """
    alive = serving(directory=run_directory(tmp_path))
    gone = f"http://127.0.0.1:{free_port()}"

    fleet = run(read_fleet([alive.url, gone]))

    assert [member.url for member in fleet.answered] == [alive.url]
    silent = fleet.silent
    assert [member.url for member in silent] == [gone]
    # Verbatim, and per member: the reader is told which address and why.
    assert silent[0].error and gone in silent[0].error
    assert silent[0].snapshot is None
    # Nothing answered, so there is no status to report - which is what tells
    # this apart from a server that refused.
    assert silent[0].status is None


def test_a_refusal_is_kept_beside_the_servers_that_answered(serving, tmp_path: Path):
    # A server with no evidence directory refuses /workers with its own
    # sentence; that is not a transport failure and must not read as one.
    answering = serving(directory=run_directory(tmp_path))
    bare = serving()

    fleet = run(read_fleet([answering.url, bare.url]))

    refused = [member for member in fleet.members if member.url == bare.url][0]
    assert refused.status == 503
    assert refused.error and "evidence directory" in refused.error
    assert len(fleet.answered) == 1


def test_a_published_server_carries_its_address_into_the_fleet(serving, tmp_path: Path):
    published_into = run_directory(tmp_path)
    service = serving(directory=published_into)
    published = wait_for(lambda: discover_servers(published_into))

    fleet = run(read_fleet(published))

    assert len(fleet.answered) == 1
    member = fleet.members[0]
    assert member.server is not None
    assert member.server.port == service.bound_port


def test_a_live_stack_server_brings_its_own_token(serving, tmp_path: Path):
    """A fleet can span runs, and two runs need not share a token."""
    guarded = serving(directory=run_directory(tmp_path), token="s3cret")

    named = LiveStackServer(
        service=stack_server.SERVICE,
        url=guarded.url,
        host=guarded.host,
        port=guarded.bound_port or 0,
        token="s3cret",
    )
    assert run(read_fleet([named])).answered

    # And without it, the same server refuses - so the token above was doing
    # the work rather than the run being open.
    assert run(read_fleet([guarded.url])).silent


def test_an_entry_that_is_not_a_server_is_refused_by_type(serving):
    with pytest.raises(TypeError):
        run(read_fleet([object()]))


def test_an_empty_fleet_is_a_fleet(tmp_path: Path):
    fleet = run(read_fleet([]))
    assert isinstance(fleet, Fleet)
    assert fleet.members == []
    assert fleet.workers == []


def test_every_server_is_reached_with_its_own_token(serving, tmp_path: Path):
    """A token belongs to a run, not to a fleet.

    Two sessions on one machine can have been started with different ones, and
    two hosts almost certainly were. The headers go out per request rather
    than on the transport, so these two share a connection pool without ever
    being sent each other's credential.
    """
    first = serving(directory=run_directory(tmp_path / "a"), token="first-secret")
    second = serving(directory=run_directory(tmp_path / "b"), token="second-secret")
    tokens = {first.url: "first-secret", second.url: "second-secret"}

    published = [
        PublishedServer(url=service.url, host=service.host, port=service.bound_port or 0)
        for service in (first, second)
    ]
    fleet = run(read_fleet([entry.with_token(tokens[entry.url]) for entry in published]))

    assert len(fleet.answered) == 2, [member.error for member in fleet.members]

    # And swapped, to show the tokens were doing the work rather than the
    # servers being open: each is refused the other's.
    swapped = run(
        read_fleet([
            published[0].with_token(tokens[second.url]),
            published[1].with_token(tokens[first.url]),
        ])
    )
    assert len(swapped.silent) == 2
    # Refused on the credential, and saying so: not the same as a host that is
    # gone, and not something restarting a machine would fix.
    assert [member.status for member in swapped.silent] == [401, 401]
    assert all("token" in (member.error or "") for member in swapped.silent)


def test_one_token_still_covers_a_fleet_that_shares_one(serving, tmp_path: Path):
    # The fallback, for the entries that cannot carry their own: an address
    # file holds no credential, and a bare URL is only a string.
    first = serving(directory=run_directory(tmp_path / "a"), token="shared")
    second = serving(directory=run_directory(tmp_path / "b"), token="shared")

    fleet = run(read_fleet([first.url, second.url], token="shared"))
    assert len(fleet.answered) == 2
