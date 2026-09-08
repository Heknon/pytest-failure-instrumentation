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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from pytest_failure_instrumentation import stack_server
from pytest_failure_instrumentation.live_view import LiveStackServer

httpx = pytest.importorskip("httpx", reason="the client extra is not installed")

from pytest_failure_instrumentation.client import (  # noqa: E402 - after the skip
    CONNECT_TIMEOUT,
    DEFAULT_TIMEOUT,
    STACK_TIMEOUT,
    AccessRefused,
    AuthenticationRequired,
    BadRequest,
    EvidenceUnavailable,
    FailureServerClient,
    Fleet,
    NotFound,
    ReaderFailed,
    ServerRefused,
    ServerUnreachable,
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


def test_the_processes_under_a_run_arrive_where_they_can_be_asked_about(
    serving, tmp_path: Path
):
    """The other half of the same call: a worker parked waiting for a
    subprocess is not the stall, it is the wait for one, and the stack that
    says why is in the child. Nothing wrote the child down, so a caller with
    only the workers has no pid to ask about - and its pid is one this server
    answers for.
    """
    directory = run_directory(tmp_path)
    service = serving(directory=directory)
    spawned = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
    )
    try:
        (directory / "gw0.state").write_bytes(
            json.dumps({"pid": os.getpid(), "nodeid": "test_pool.py::test_writes",
                        "phase": "call", "time": time.time()}).encode() + b"\n"
        )
        (directory / "owner.json").write_text(json.dumps({"pid": os.getpid()}))

        async def ask():
            async with connected(service) as client:
                plain = await client.workers()
                return plain, await client.workers(children=True)

        plain, described = run(ask())
        # Off unless asked for: it is the one part of this answer the server
        # cannot assemble from the run's own files.
        assert plain.workers[0].children == []

        under = {child.pid: child for child in described.workers[0].children}
        assert spawned.pid in under
        assert under[spawned.pid].ppid == os.getpid()
        assert under[spawned.pid].name
        assert described.runs[0].children_truncated is False

        async def read():
            async with connected(service) as client:
                return await client.callstack(pid=spawned.pid)

        try:
            assert run(read()).pid == spawned.pid
        except ReaderFailed as failed:
            # Whether py-spy can read this machine's processes is a different
            # question from whether the server will let it be asked - and it
            # is 403 that this test is about not seeing.
            assert failed.status == 502, failed.status
    finally:
        spawned.kill()
        spawned.wait(timeout=10)


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


def reachable(service: stack_server.StackService, token: str = "") -> LiveStackServer:
    """The payload a run reports, which is all a fleet read needs.

    Built here the way the hook builds it: the bound port rather than the
    requested one, and the run's own token.
    """
    return LiveStackServer(
        service=stack_server.SERVICE,
        url=service.url,
        host=service.host,
        port=service.bound_port or 0,
        token=token,
    )


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


def test_repeated_concurrent_resources_workers_and_stacks(serving, tmp_path, monkeypatch):
    """Real HTTP and history writes while a stack reader is deliberately held.

    The hold proves overlap without relying on a fast machine or py-spy attach
    permissions. The independent real-reader test below exercises that boundary.
    """
    from pytest_failure_instrumentation.probes.pyspy import Reading

    from .test_resources import batch, history

    directory = evidence_with_worker(tmp_path, "load-run", "gw0")
    store = history(directory)
    service = serving(directory=directory, token="load-secret")
    entered, release = threading.Event(), threading.Event()

    def held_read(pid, options):
        entered.set()
        assert release.wait(15), "the other endpoints stalled behind a stack read"
        return Reading([], None, options)

    monkeypatch.setattr(stack_server.stacks, "live_reading", held_read)

    async def exercise():
        async with connected(service, "load-secret") as client:
            for sequence in range(1, 31):
                entered.clear()
                release.clear()
                store.append(batch(sequence))
                stack = asyncio.create_task(client.callstack(pid=os.getpid()))
                try:
                    assert await asyncio.to_thread(entered.wait, 10)
                    resources, workers = await asyncio.wait_for(asyncio.gather(
                        client.resources("load-run", latest=True), client.workers(),
                    ), timeout=10)
                    assert not stack.done(), "stack request was not held during other reads"
                    assert resources.batches[0].sequence == sequence
                    assert any(w.worker == "gw0" for w in workers.workers)
                finally:
                    release.set()
                    result = await asyncio.wait_for(stack, timeout=10)
                assert result.pid == os.getpid()
            assert (await client.identity()).pid == os.getpid()
            assert (await client.resources("load-run", latest=True)).latest_sequence == 30

    try:
        run(exercise())
        assert service.serving
    finally:
        release.set()
        store.close()


def test_three_concurrent_real_stack_requests_leave_server_responsive(serving, tmp_path):
    service = serving(directory=run_directory(tmp_path))

    async def exercise():
        async with connected(service) as client:
            results = await asyncio.gather(
                *(client.callstack(pid=os.getpid()) for _ in range(3)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, ReaderFailed):
                    # Missing reader / attach denial is a supported, structured
                    # reply. Transport failures and server crashes are not.
                    assert result.status == 502 and result.message
                else:
                    assert not isinstance(result, BaseException), repr(result)
                    assert result.pid == os.getpid()
            assert (await client.identity()).pid == os.getpid()
            assert (await client.workers()).served_by.pid == os.getpid()

    run(exercise())
    assert service.serving


def test_resource_overload_refuses_excess_work_and_recovers(serving, tmp_path, monkeypatch):
    from pytest_failure_instrumentation.capture import resource_history

    from .test_resources import batch, history

    directory = evidence_with_worker(tmp_path, "load-run", "gw0")
    store = history(directory)
    store.append(batch())
    service = serving(directory=directory, token="load-secret")
    barrier = threading.Barrier(3)
    release = threading.Event()
    original = resource_history.read_history

    def held_history(*args, **kwargs):
        barrier.wait(timeout=15)
        assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(resource_history, "read_history", held_history)

    async def exercise():
        async with connected(service, "load-secret") as client:
            held = [asyncio.create_task(client.resources("load-run")) for _ in range(2)]
            try:
                await asyncio.to_thread(barrier.wait, 10)
                with pytest.raises(ServerRefused) as refused:
                    await asyncio.wait_for(client.resources("load-run"), 5)
                assert refused.value.status == 503
                assert (await client.workers()).served_by.pid == os.getpid()
                assert (await client.identity()).pid == os.getpid()
            finally:
                release.set()
                await asyncio.gather(*held)
            monkeypatch.setattr(resource_history, "read_history", original)
            assert (await client.resources("load-run")).latest_sequence == 1

    try:
        run(exercise())
        assert service.serving
    finally:
        release.set()
        store.close()


def test_the_fleet_reads_every_server(serving, tmp_path: Path):
    first = serving(directory=run_directory(tmp_path))
    second = serving(directory=run_directory(tmp_path))

    fleet = run(read_fleet([reachable(first), reachable(second)]))

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

    fleet = run(read_fleet([reachable(here), reachable(there)]))

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

    fleet = run(read_fleet([reachable(alive), LiveStackServer(url=gone)]))

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

    fleet = run(read_fleet([reachable(answering), reachable(bare)]))

    refused = [member for member in fleet.members if member.url == bare.url][0]
    assert refused.status == 503
    assert refused.error and "evidence directory" in refused.error
    assert len(fleet.answered) == 1


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

    fleet = run(read_fleet([reachable(first, "first-secret"), reachable(second, "second-secret")]))
    assert len(fleet.answered) == 2, [member.error for member in fleet.members]

    # And swapped, to show the tokens were doing the work rather than the
    # servers being open: each is refused the other's.
    swapped = run(
        read_fleet([reachable(first, "second-secret"), reachable(second, "first-secret")])
    )
    assert len(swapped.silent) == 2
    # Refused on the credential, and saying so: not the same as a host that is
    # gone, and not something restarting a machine would fix.
    assert [member.status for member in swapped.silent] == [401, 401]
    assert all("token" in (member.error or "") for member in swapped.silent)


# -- what a call is allowed to cost ---------------------------------------


def test_the_cheap_calls_and_the_expensive_one_get_different_budgets(serving):
    """`/workers` reads files; `/stack` stops a process to be read.

    One number for both would either cut a stack read off or let a host that
    is not answering hold a fleet poll for as long as a stack is worth
    waiting for.
    """
    assert DEFAULT_TIMEOUT < STACK_TIMEOUT
    # And connecting is bounded well inside either: a host that is gone
    # refuses at once, and one behind a dropped route never answers at all.
    assert CONNECT_TIMEOUT < DEFAULT_TIMEOUT

    service = serving()
    client = connected(service)
    assert client._timeout == DEFAULT_TIMEOUT
    assert client._stack_timeout == STACK_TIMEOUT
    run(client.aclose())


def test_a_host_that_never_answers_costs_its_timeout_and_no_more(serving, tmp_path: Path):
    """A dropped route, rather than a refused connection.

    The socket that nothing is listening on is refused immediately and tests
    nothing about the budget; this one is opened and then never spoken to, so
    the client has to give up on its own.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)  # accepted by the OS backlog, answered by nobody
    silent = f"http://127.0.0.1:{listener.getsockname()[1]}"
    alive = serving(directory=run_directory(tmp_path))

    started = time.monotonic()
    try:
        fleet = run(
            read_fleet([reachable(alive), LiveStackServer(url=silent)], timeout=1.0)
        )
    finally:
        listener.close()
    elapsed = time.monotonic() - started

    # The one that answered still did, and the silent one cost its budget
    # rather than the fleet.
    assert [member.url for member in fleet.answered] == [alive.url]
    assert fleet.silent[0].url == silent
    assert elapsed < 10, f"the fleet took {elapsed:.1f}s for a 1s timeout"


def test_one_server_answering_nonsense_does_not_lose_the_fleet(serving, tmp_path: Path):
    """The promise is that a host costs only itself.

    A payload malformed enough to fail validation is still that host's
    problem, and a fleet lost to one server's bad JSON would break the
    promise at exactly the moment something was already wrong.
    """
    alive = serving(directory=run_directory(tmp_path))

    class Nonsense(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            body = b'{"runs": "not a list at all"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_unused):
            pass

    liar = HTTPServer(("127.0.0.1", 0), Nonsense)
    threading.Thread(target=liar.serve_forever, daemon=True).start()
    try:
        fleet = run(
            read_fleet([
                reachable(alive),
                LiveStackServer(url=f"http://127.0.0.1:{liar.server_port}"),
            ])
        )
    finally:
        liar.shutdown()

    assert [member.url for member in fleet.answered] == [alive.url]
    broken = fleet.silent[0]
    # Recorded by type, so it stays diagnosable rather than swallowed.
    assert broken.error and "ValidationError" in broken.error
