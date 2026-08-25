"""The live-stack server: claiming the port, sharing it, and answering.

The port is the only thing the sessions on a host share, so nearly everything
worth testing here is about what happens when two of them want it at once.
Every test binds an ephemeral port rather than the real default: a suite that
takes 8080 would fight whatever the developer running it already has there.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import pytest

from pytest_failure_instrumentation import stack_server
from pytest_failure_instrumentation.probes import pyspy, stacks
from pytest_failure_instrumentation.probes.platform_flags import IS_WINDOWS

needs_pyspy = pytest.mark.skipif(
    not pyspy.available(), reason="py-spy is not installed in this environment"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((stack_server.HOST, 0))
        return int(probe.getsockname()[1])


def get(port: int, path: str, timeout: float = 30.0) -> tuple[int, Any]:
    """A request with proxies off, which is how the plugin itself asks.

    CI sets ``http_proxy`` constantly, and a request for 127.0.0.1 that goes
    through a proxy tests the proxy.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{stack_server.HOST}:{port}{path}", timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refusal:
        return refusal.code, json.loads(refusal.read())


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
    """Services started by a test, stopped however the test ends.

    A leaked server holds its port for the rest of the session, and the next
    test to ask for one would be testing this one's leftovers.
    """
    started: list[stack_server.StackService] = []

    def start(port: int, **kwargs: Any) -> stack_server.StackService:
        service = stack_server.StackService(port, **kwargs)
        service.start()
        started.append(service)
        return service

    yield start
    for service in started:
        service.stop()


# -- claiming the port ----------------------------------------------------


def test_the_first_session_serves_and_says_where():
    port = free_port()
    service = stack_server.StackService(port)
    service.start()
    try:
        assert wait_for(lambda: service.serving), service.status
        assert service.url in service.status
        assert stack_server.identify(port) is not None
    finally:
        service.stop()
    assert not service.serving


def test_a_second_session_leaves_the_port_alone_and_says_who_has_it(serving):
    """The whole point of the singleton. Two sessions, one server, and the one
    that lost says something a human can act on rather than failing."""
    port = free_port()
    holder = serving(port)
    assert wait_for(lambda: holder.serving), holder.status

    waiting = serving(port, reclaim_seconds=0.2)
    assert wait_for(lambda: "another session is serving" in waiting.status), waiting.status
    assert not waiting.serving
    assert str(os.getpid()) in waiting.status  # both are this process, here


def test_the_waiting_session_takes_over_when_the_holder_exits(serving):
    """The case a fixed port makes unavoidable: the session hosting the server
    is not the last one running, and the rest must not go blind when it ends."""
    port = free_port()
    holder = serving(port)
    assert wait_for(lambda: holder.serving), holder.status

    waiting = serving(port, reclaim_seconds=0.2)
    assert wait_for(lambda: not waiting.serving and "another session" in waiting.status)

    holder.stop()
    assert wait_for(lambda: waiting.serving), waiting.status
    assert stack_server.identify(port) is not None


def test_a_stranger_on_the_port_is_left_alone_and_named(serving):
    """A dev server on 8080 is the likeliest thing to be found there, and
    taking the port from it - or failing the run over it - are both worse than
    running without live stacks and saying so."""
    port = free_port()
    stranger = socket.socket()
    stranger.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    stranger.bind((stack_server.HOST, port))
    stranger.listen(1)
    try:
        service = serving(port, reclaim_seconds=0.2)
        assert wait_for(lambda: "not a stack server" in service.status), service.status
        assert not service.serving
        assert "failure_stack_server_port" in service.status
    finally:
        stranger.close()


def test_identify_does_not_mistake_someone_elses_json_for_ours(serving):
    """The check is a name only this package uses, not "something answered"."""
    assert stack_server.identify(free_port(), timeout=0.5) is None


def test_windows_does_not_get_the_address_reuse_flag():
    """On Windows SO_REUSEADDR permits binding a port another socket is
    actively listening on, so inheriting http.server's default would let two
    sessions both believe they had claimed it."""
    assert stack_server._Server.allow_reuse_address == (not IS_WINDOWS)


# -- answering ------------------------------------------------------------


def test_a_request_for_the_serving_process_is_answered_from_its_own_frames(serving):
    """No ptrace, no subprocess, no permission: this process already has them."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, f"/stack?pid={os.getpid()}")
    assert status == 200
    assert body["source"] == "in-process"
    assert body["pid"] == os.getpid()
    assert body["captured_at"] > 0

    functions = [frame["function"] for thread in body["threads"] for frame in thread["frames"]]
    assert "do_GET" in functions  # the request being served is on one of them


@needs_pyspy
def test_another_process_is_read_from_outside_it(serving):
    """The case the whole external reader exists for: a stack out of a process
    that was never asked for one and cannot be made to cooperate."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time\ndef inner():\n time.sleep(60)\ninner()"]
    )
    try:
        # Waiting for the frame itself, not merely for an answer: an
        # interpreter that has not finished starting reads back a perfectly
        # valid stack that is still inside the import machinery.
        found = wait_for(
            lambda: "inner" in (_named_frames(get(port, f"/stack?pid={victim.pid}")) or [])
        )
        assert found, "py-spy never reported the frame the victim is parked in"
    finally:
        victim.kill()
        victim.wait(timeout=10)


def _named_frames(answer: tuple[int, Any]) -> Optional[list[str]]:
    status, body = answer
    if status != 200:
        return None
    names = [frame["function"] for thread in body["threads"] for frame in thread["frames"]]
    return names or None


def test_a_pid_that_is_not_a_number_is_refused_rather_than_guessed(serving):
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/stack?pid=notapid")
    assert status == 400
    assert "notapid" in body["error"]


def test_an_unreadable_process_answers_with_why(serving):
    """A UI that is told nothing shows an empty pane; one that is told why can
    say whether this is a dead process or a missing permission."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/stack?pid=999999")
    assert status == 502
    assert body["error"]


def test_an_unknown_endpoint_lists_the_ones_that_exist(serving):
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/nothing")
    assert status == 404
    assert "/stack?pid=N" in body["endpoints"]


# -- the two readers agree on a shape -------------------------------------


def test_both_readers_describe_a_thread_the_same_way():
    """A caller that has to switch on which mechanism answered has been handed
    two APIs, and the UI is where that would end up being encoded."""
    from_inside = stacks.own_threads()[0]
    from_outside = pyspy._thread(
        {
            "thread_id": 1,
            "thread_name": "MainThread",
            "owns_gil": True,
            "active": True,
            "frames": [{"name": "f", "filename": "x.py", "line": 2}],
        }
    )
    assert set(from_inside) == set(from_outside)
    assert set(from_inside["frames"][0]) == set(from_outside["frames"][0])


def test_own_threads_reports_the_innermost_frame_first():
    """py-spy's order, and this package's order everywhere else."""

    def inner():
        return stacks.own_threads()

    threads = {thread["thread_id"]: thread for thread in inner()}
    mine = threads[__import__("threading").get_ident()]
    assert mine["frames"][0]["function"] == "own_threads"
    assert mine["frames"][1]["function"] == "inner"


def test_a_missing_reader_explains_itself_rather_than_raising(monkeypatch):
    monkeypatch.setattr(pyspy, "executable", lambda: None)
    threads, error = pyspy.dump(os.getpid())
    assert threads is None
    assert "py-spy" in error and "pip install" in error


def test_a_refused_attach_says_what_to_do_about_it():
    """The three causes are platform-specific and none of them is guessable
    from "Operation not permitted"."""
    explained = pyspy._explained(b"Error: Operation not permitted (os error 1)", 42)
    assert "not permitted" in explained.lower()
    assert len(explained) > len("Operation not permitted (os error 1)")


def test_a_dead_process_is_named_as_such():
    assert "not running" in pyspy._explained(b"Error: No such process (os error 3)", 42)


# -- wired into a real run ------------------------------------------------


def test_a_real_pytest_session_serves_its_own_stack(pytester):
    """The wiring, end to end: ini switches it on, session start claims the
    port, and a test that is *still running* can be asked what it is doing.

    A subprocess rather than an in-process run, because the thing being tested
    is what an ordinary ``pytest`` invocation does - entry point, ini and all.
    """
    port = free_port()
    pytester.makeini(
        f"""
        [pytest]
        failure_stack_server = true
        failure_stack_server_port = {port}
        """
    )
    pytester.makepyfile(
        f"""
        import json, os, time, urllib.request


        def ask(path):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            with opener.open("http://127.0.0.1:{port}" + path, timeout=30) as answer:
                return json.loads(answer.read())


        def test_the_running_test_can_be_asked_what_it_is_doing():
            deadline = time.monotonic() + 20
            while True:
                try:
                    identity = ask("/identity")
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the server never came up"
                    time.sleep(0.1)

            assert identity["service"] == "pytest-failure-instrumentation-stacks"
            assert identity["pid"] == os.getpid()

            stack = ask("/stack?pid=%d" % os.getpid())
            functions = [
                frame["function"]
                for thread in stack["threads"]
                for frame in thread["frames"]
            ]
            assert "test_the_running_test_can_be_asked_what_it_is_doing" in functions
        """
    )
    result = pytester.runpytest_subprocess("-p", "failure_instrumentation")
    result.assert_outcomes(passed=1)


def test_reads_of_other_processes_are_bounded(monkeypatch):
    """A dashboard polling stuck workers must not put an unbounded number of
    subprocesses on a machine that is already in trouble."""
    monkeypatch.setattr(
        stack_server, "_readers", __import__("threading").BoundedSemaphore(1)
    )
    held = __import__("threading").Event()
    released = __import__("threading").Event()

    def slow_dump(pid, timeout=None):
        held.set()
        released.wait(10)
        return [], None

    monkeypatch.setattr(stack_server.pyspy, "dump", slow_dump)
    hog = __import__("threading").Thread(
        target=stack_server.read_stack, args=(os.getpid() + 1,), daemon=True
    )
    hog.start()
    try:
        assert held.wait(10)
        threads, error, source = stack_server.read_stack(os.getpid() + 1)
        assert threads is None
        assert "already in flight" in error
        assert source == "py-spy"
    finally:
        released.set()
        hog.join(timeout=10)

    # And the slot comes back once the reader in flight is done.
    threads, error, _ = stack_server.read_stack(os.getpid() + 1)
    assert error is None
