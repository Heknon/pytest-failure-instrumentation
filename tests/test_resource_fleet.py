"""Fleet resource reads preserve scope, credentials, cursors and partial failures."""
from __future__ import annotations

import asyncio
import time

import pytest

httpx = pytest.importorskip("httpx")

from pytest_failure_instrumentation.client import read_resources_fleet  # noqa: E402
from pytest_failure_instrumentation.live_view import LiveStackServer  # noqa: E402

from .test_client import serving as serving  # noqa: E402
from .test_resources import batch, history  # noqa: E402


def test_real_resource_fleet_has_independent_tokens_pages_and_failures(serving, tmp_path):
    stores, servers = [], []
    try:
        for name in ("first", "second"):
            directory = tmp_path / name / "run"
            directory.mkdir(parents=True)
            store = history(directory)
            store.append(batch(time.time()))
            store.append(batch(time.time() + 1))
            stores.append(store)
            service = serving(directory=directory, token=f"{name}-secret")
            servers.append(LiveStackServer(url=service.url, session_id="run", token=f"{name}-secret"))
        # Same worker/session names on different servers must remain distinct.
        wrong = servers[0].model_copy(update={"token": "wrong"})
        async def ask():
            async with httpx.AsyncClient() as transport:
                first = await read_resources_fleet([*servers, wrong], limit=1, worker="gw0", client=transport)
                assert len(first.answered) == 2 and len(first.silent) == 1
                assert first.silent[0].status == 401
                assert len(first.cursors) == 2
                assert all(len(member.history.batches) == 1 for member in first.answered)
                assert all(member.history.has_more for member in first.answered)
                assert "secret" not in first.model_dump_json()
                second = await read_resources_fleet(servers, after=first.cursors, limit=1, client=transport)
                assert all(member.history.next_after > first.cursors[(member.url, member.session)]
                           for member in second.answered)
                assert not any(member.history.has_more for member in second.answered)
                stores[0].close()
                third = await read_resources_fleet(servers, latest=True, client=transport)
                assert third.members[0].status == 404
                assert third.members[1].answered
                assert not transport.is_closed
        asyncio.run(ask())
    finally:
        for store in stores:
            store.close()


def payload(session="run"):
    return {"session": session, "controller_pid": 1, "controller_created_at": 1,
            "started_at": 1, "sample_seconds": 5, "max_bytes": 1000}


def test_resource_fleet_bounds_concurrency_and_isolates_bad_answers():
    async def ask():
        active = peak = 0
        async def respond(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.01)
                assert request.url.params["from"] == "1.0"
                assert request.url.params["to"] == "2.0"
                if request.url.host == "timeout":
                    raise httpx.ReadTimeout("slow host", request=request)
                if request.url.host == "invalid":
                    return httpx.Response(200, json={"bad": "schema"})
                if request.url.host == "wrong-session":
                    return httpx.Response(200, json=payload("other"))
                return httpx.Response(200, json=payload())
            finally:
                active -= 1
        hosts = ["good", "timeout", "invalid", "wrong-session", "also-good"]
        servers = [LiveStackServer(url=f"http://{host}", session_id="run") for host in hosts]
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            fleet = await read_resources_fleet(servers, concurrency=2, start=1.0, end=2.0, client=transport)
        assert peak == 2
        assert [member.url for member in fleet.answered] == ["http://good", "http://also-good"]
        assert len(fleet.silent) == 3
        assert all(member.error for member in fleet.silent)
    asyncio.run(ask())


def test_resource_fleet_cancellation_cancels_active_reads():
    async def ask():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def respond(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            task = asyncio.create_task(read_resources_fleet(
                [LiveStackServer(url="http://host", session_id="run")], client=transport))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cancelled.wait(), 1)
    asyncio.run(ask())


def test_empty_missing_session_and_invalid_concurrency():
    async def ask():
        assert (await read_resources_fleet([])).members == []
        fleet = await read_resources_fleet([LiveStackServer(url="http://unused")])
        assert not fleet.answered
        assert "session_id" in fleet.silent[0].error
        with pytest.raises(ValueError, match="concurrency"):
            await read_resources_fleet([], concurrency=0)
    asyncio.run(ask())
