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
    NotFound,
    ReaderFailed,
    ServerRefused,
    ServerUnreachable,
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
    service = serving(directory=tmp_path)

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
    service = serving(directory=tmp_path)

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
    service = serving(directory=tmp_path)

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
    service = serving(directory=tmp_path, token="s3cret")

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
