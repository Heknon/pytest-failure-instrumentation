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
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

from pytest_failure_instrumentation import stack_server
from pytest_failure_instrumentation.incidents import stack_server as stack_server_incident
from pytest_failure_instrumentation.probes import pyspy, stacks
from pytest_failure_instrumentation.probes.platform_flags import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
)

needs_pyspy = pytest.mark.skipif(
    not pyspy.available(), reason="py-spy is not installed in this environment"
)


#: A process parked in a known frame, readable by its parent's descendants.
VICTIM_THAT_PERMITS_TRACING = """
import ctypes, sys, time

if sys.platform == "linux":
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            0x59616D61, ctypes.c_ulong(__import__("os").getppid()), 0, 0, 0
        )
    except Exception:
        pass


def inner():
    time.sleep(60)


inner()
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((stack_server.LOOPBACK, 0))
        return int(probe.getsockname()[1])


def get(
    port: int, path: str, token: Optional[str] = None, timeout: float = 30.0
) -> tuple[int, Any]:
    """A request with proxies off, which is how the plugin itself asks.

    CI sets ``http_proxy`` constantly, and a request for 127.0.0.1 that goes
    through a proxy tests the proxy.

    ``token`` is what every endpoint but ``/identity`` requires. Omitting it is
    how the tests below ask what an unauthenticated caller gets.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {stack_server.AUTH_HEADER: f"{stack_server.AUTH_SCHEME} {token}"} if token else {}
    request = urllib.request.Request(
        f"http://{stack_server.LOOPBACK}:{port}{path}", headers=headers
    )
    try:
        with opener.open(request, timeout=timeout) as response:
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
    stranger.bind((stack_server.LOOPBACK, port))
    stranger.listen(1)
    try:
        service = serving(port, reclaim_seconds=0.2)
        assert wait_for(lambda: "not a stack server" in service.status), service.status
        assert not service.serving
        assert "--callstack-port" in service.status
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

    status, body = get(port, f"/stack?pid={os.getpid()}", service.token)
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

    # The victim nominates its parent as a permitted tracer, which is what a
    # real worker now does at startup - see probes.tracing. Without it this
    # test is refused wherever Yama enforces ptrace_scope=1, because py-spy is
    # spawned by *this* process and is therefore the victim's sibling rather
    # than its ancestor. That is the configuration most Linux boxes ship.
    victim = subprocess.Popen([sys.executable, "-c", VICTIM_THAT_PERMITS_TRACING])
    try:
        # Waiting for the frame itself, not merely for an answer: an
        # interpreter that has not finished starting reads back a perfectly
        # valid stack that is still inside the import machinery.
        found = wait_for(
            lambda: "inner" in (_named_frames(get(port, f"/stack?pid={victim.pid}", service.token)) or [])
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

    status, body = get(port, "/stack?pid=notapid", service.token)
    assert status == 400
    assert "notapid" in body["error"]


def test_a_pid_no_process_could_have_is_refused_without_spending_a_reader(serving):
    """The reader is a separate program with its own idea of an integer.

    Handed 10^20 py-spy panics and its Rust backtrace became the API's
    "error"; handed a negative number it reads it as a flag and prints its own
    usage. Both cost a subprocess and one of the concurrency slots to produce a
    reply that says nothing about any process. A pid that cannot exist is
    refused where the reply can say why.
    """
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"
    port = service.bound_port

    for impossible in (-1, 0, stack_server.MAX_PID + 1, 99999999999999999999):
        status, body = get(port, f"/stack?pid={impossible}", service.token)
        assert status == 400, f"pid={impossible} reached the reader"
        assert "pid must be between" in body["error"]
        # Nothing py-spy said about itself leaks out as the explanation.
        assert "py-spy" not in body["error"] and "panicked" not in body["error"]

    # A pid that could exist still goes to the reader, whatever it finds there.
    status, _ = get(port, f"/stack?pid={stack_server.MAX_PID}", service.token)
    assert status == 502


def test_an_unreadable_process_answers_with_why(serving):
    """A UI that is told nothing shows an empty pane; one that is told why can
    say whether this is a dead process or a missing permission."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/stack?pid=999999", service.token)
    assert status == 502
    assert body["error"]


def test_an_unknown_endpoint_lists_the_ones_that_exist(serving):
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/nothing", service.token)
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


def test_two_reads_of_one_target_do_not_race_into_a_permissions_lecture(monkeypatch):
    """A process can only be suspended by one reader at a time.

    The loser's errno is EPERM, which is exactly what a permission problem
    gives - so the collision was reported as "ptrace is not permitted, check
    /proc/sys/kernel/yama/ptrace_scope", on machines with no Yama at all.
    Measured: four concurrent reads of one process, three answered and the
    fourth was sent to change a kernel setting that had nothing to do with it.

    And the collision was usually self-inflicted: the sampler reads every stuck
    worker on a cadence while the server answers a UI polling /stack for the
    same one.
    """
    import threading

    started = threading.Event()
    release = threading.Event()

    class Finished:
        returncode = 0
        stdout = b"[]"
        stderr = b""

    def slow(*args: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(10)
        return Finished()

    monkeypatch.setattr(pyspy, "executable", lambda: "/nonexistent/py-spy")
    monkeypatch.setattr(pyspy.subprocess, "run", slow)

    holder = threading.Thread(target=lambda: pyspy.dump(4321), daemon=True)
    holder.start()
    try:
        assert started.wait(10), "the first read never began"
        threads, error = pyspy.dump(4321)
    finally:
        release.set()
        holder.join(timeout=10)

    assert threads is None
    assert error and "already in flight" in error
    assert "ptrace_scope" not in error, "a collision was reported as a policy"

    # A different target is unaffected - the bound is per process, not global.
    release.set()
    assert pyspy.dump(9999) == ([], None)


def test_the_permission_hint_names_a_tracer_that_is_already_attached():
    """Somebody else's debugger gives the same errno, and that possibility has
    to be in the hint or the only advice on offer is to change a kernel
    setting that was never the problem."""
    for platform_key in ("linux", "darwin"):
        assert "already be attached" in pyspy.PERMISSION_HINTS[platform_key]


def test_a_reader_answering_an_unexpected_shape_explains_rather_than_raising(
    monkeypatch,
):
    """py-spy is a separate program on a floating version.

    The dependency is ``py-spy>=0.3`` with no ceiling, so a release that
    wrapped its threads in an object - or answered ``null`` - lands here.
    Iterating it raised out of a function whose whole contract is that it
    returns a reason instead of a stack, and the server does not wrap this
    call: the request got no reply at all rather than a 502 saying why. A
    dict was quieter and worse, iterating its keys to "zero threads and no
    error", which reads as a process with no Python in it.
    """

    class Finished:
        returncode = 0
        stderr = b""

        def __init__(self, stdout: bytes) -> None:
            self.stdout = stdout

    def answering(payload: bytes):
        monkeypatch.setattr(pyspy, "executable", lambda: "/nonexistent/py-spy")
        monkeypatch.setattr(pyspy.subprocess, "run", lambda *a, **k: Finished(payload))
        return pyspy.dump(4321)

    for payload in (b"{}", b'{"threads": []}', b"[1, 2]", b'"a string"', b"null"):
        threads, error = answering(payload)
        assert threads is None, f"{payload!r} was read as a stack"
        assert error and "does not understand" in error

    # The shapes it does understand are untouched.
    assert answering(b"[]") == ([], None)
    threads, error = answering(
        b'[{"thread_id": 1, "frames": [{"name": "f", "filename": "a.py", "line": 2}]}]'
    )
    assert error is None
    assert threads and threads[0]["frames"][0]["function"] == "f"


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
        import glob, json, os, time, urllib.request


        def ask(path, token=None):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            headers = {{"Authorization": "Bearer " + token}} if token else {{}}
            request = urllib.request.Request("http://127.0.0.1:{port}" + path, headers=headers)
            with opener.open(request, timeout=30) as answer:
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

            published = glob.glob(".pytest-failures/*/callstack-*.json")
            assert published, "the server published no address to take a token from"
            token = json.loads(open(published[0]).read())["token"]

            stack = ask("/stack?pid=%d" % os.getpid(), token)
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


# -- drawing a port rather than claiming one ------------------------------


def test_a_drawn_port_binds_immediately_and_contends_with_nobody(serving, tmp_path):
    """The default. Nothing is shared, so nothing can be lost to another
    session - and two sessions in one directory both get a server."""
    first = serving(0, directory=tmp_path)
    second = serving(0, directory=tmp_path)
    assert wait_for(lambda: first.serving), first.status
    assert wait_for(lambda: second.serving), second.status

    assert first.bound_port and second.bound_port
    assert first.bound_port != second.bound_port
    assert first.drawn and second.drawn
    assert str(first.bound_port) in first.url


def test_a_drawn_port_is_written_down_where_a_ui_will_look(serving, tmp_path):
    """A port nobody chose is a port nobody can guess, so the address goes in
    the evidence directory - beside the state files that say which pid is
    running which test."""
    service = serving(0, directory=tmp_path)
    assert wait_for(lambda: service.serving), service.status

    published = wait_for(lambda: list(tmp_path.glob("callstack-*.json")))
    assert published and len(published) == 1
    record = json.loads(published[0].read_text())
    assert record["service"] == stack_server.SERVICE
    assert record["port"] == service.bound_port
    assert record["drawn"] is True
    assert record["pid"] == os.getpid()

    # And the address it published is one that actually answers.
    assert stack_server.identify(record["port"], record["host"]) is not None


def test_the_address_is_retracted_when_the_session_stops(tmp_path):
    """A file left behind points a UI at a port nobody is listening to, and it
    spends its timeout finding that out."""
    service = stack_server.StackService(0, directory=tmp_path)
    service.start()
    assert wait_for(lambda: service.serving), service.status
    assert list(tmp_path.glob("callstack-*.json"))

    service.stop()
    assert not list(tmp_path.glob("callstack-*.json"))


def test_two_sessions_in_one_directory_do_not_overwrite_each_others_address(
    serving, tmp_path
):
    """The filename carries the pid for exactly this reason."""
    serving(0, directory=tmp_path)
    assert wait_for(lambda: len(list(tmp_path.glob("callstack-*.json"))) == 1)
    # A second session's file, written by hand because both of ours share a pid.
    (tmp_path / "callstack-999999.json").write_text('{"service": "x", "port": 1}')
    assert len(list(tmp_path.glob("callstack-*.json"))) == 2


def test_only_dead_sessions_addresses_are_swept(tmp_path):
    """Cleaning up a live session's address is how a cleanup becomes an
    outage, so the pid is checked rather than the age."""
    alive = tmp_path / f"callstack-{os.getpid()}.json"
    dead = tmp_path / "callstack-999999.json"
    unrelated = tmp_path / "callstack-notapid.json"
    for path in (alive, dead, unrelated):
        path.write_text("{}")

    stack_server.sweep_dead_servers(tmp_path)

    assert alive.exists()
    assert unrelated.exists()  # not ours to interpret, so not ours to delete
    assert not dead.exists()


def test_a_wildcard_bind_is_advertised_on_an_address_that_can_be_connected_to():
    """0.0.0.0 is a thing to listen on, not a thing to connect to - Windows
    refuses it outright."""
    assert stack_server.reachable("0.0.0.0") == stack_server.LOOPBACK
    # Not the IPv4 loopback: a socket bound to :: without dual-stack does not
    # answer on an IPv4 address at all.
    assert stack_server.reachable("::") == stack_server.LOOPBACK6
    assert stack_server.reachable("10.1.2.3") == "10.1.2.3"


def test_a_host_that_cannot_be_bound_gives_up_rather_than_retrying(serving):
    """A drawn port that fails to bind failed for a reason no later attempt
    would find changed - a bad interface, or a sandbox that forbids listening."""
    service = serving(0, host="203.0.113.1", reclaim_seconds=0.2)
    assert wait_for(lambda: "could not bind" in service.status), service.status
    assert not service.serving


def test_a_real_run_draws_a_port_and_a_ui_finds_it_on_disk(pytester):
    """The whole default path, end to end and from the outside: no port named
    anywhere, and a UI still locates the server - because the address is
    written beside the state files it already reads."""
    pytester.makeini(
        """
        [pytest]
        failure_stack_server = true
        failure_directory = .evidence
        """
    )
    pytester.makepyfile(
        """
        import glob, json, os, time, urllib.request


        def test_a_ui_can_find_this_server_without_being_told_the_port():
            deadline = time.monotonic() + 20
            while not glob.glob(".evidence/*/callstack-*.json"):
                assert time.monotonic() < deadline, "no address was ever published"
                time.sleep(0.1)

            published = glob.glob(".evidence/*/callstack-*.json")
            assert len(published) == 1
            address = json.loads(open(published[0]).read())
            assert address["drawn"] is True
            assert address["port"] > 0
            assert address["pid"] == os.getpid()

            # The token rides in the same file as the port, because a UI that
            # can read one can read the other and nothing else can read either.
            assert address["token"]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(
                address["url"] + "/stack?pid=%d" % os.getpid(),
                headers={"Authorization": "Bearer " + address["token"]},
            )
            with opener.open(request, timeout=30) as answer:
                stack = json.loads(answer.read())
            functions = [
                frame["function"]
                for thread in stack["threads"]
                for frame in thread["frames"]
            ]
            assert "test_a_ui_can_find_this_server_without_being_told_the_port" in functions
        """
    )
    result = pytester.runpytest_subprocess("-p", "failure_instrumentation")
    result.assert_outcomes(passed=1)
    # And the address does not outlive the session that published it.
    assert not list((pytester.path / ".evidence").glob("*/callstack-*.json"))


def test_naming_a_port_on_the_command_line_is_enough_to_start_it(pytester):
    """No ini at all. An option that parsed and then did nothing because a
    separate flag was left off would be the worst of the choices here."""
    port = free_port()
    pytester.makepyfile(
        f"""
        import json, os, time, urllib.request


        def test_the_named_port_is_serving():
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            deadline = time.monotonic() + 20
            while True:
                try:
                    with opener.open("http://127.0.0.1:{port}/identity", timeout=20) as answer:
                        identity = json.loads(answer.read())
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the server never came up"
                    time.sleep(0.1)
            assert identity["pid"] == os.getpid()
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "failure_instrumentation", "--callstack-port", str(port)
    )
    result.assert_outcomes(passed=1)


# -- IPv6 -----------------------------------------------------------------


def _has_ipv6() -> bool:
    """Whether this machine has IPv6 at all. Plenty of containers do not, and
    a skip that says so beats a failure that looks like our bug."""
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    with probe:
        try:
            probe.bind(("::1", 0))
        except OSError:
            return False
    return True


needs_ipv6 = pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this machine")


def test_an_ipv6_host_gets_an_ipv6_socket():
    """http.server is AF_INET and notices nothing otherwise, so a server asked
    for ``::1`` opened an IPv4 socket and could not bind it - while the
    settings called ``::1`` a supported loopback and warned about nothing."""
    assert stack_server.address_family("::1") == socket.AF_INET6
    assert stack_server.address_family("::") == socket.AF_INET6
    assert stack_server.address_family("127.0.0.1") == socket.AF_INET
    assert stack_server.address_family("0.0.0.0") == socket.AF_INET
    # A name is left to IPv4, which is what every other default here assumes.
    assert stack_server.address_family("localhost") == socket.AF_INET


def test_an_ipv6_literal_is_bracketed_in_a_url():
    """``http://::1:8080/`` is not a URL anybody can parse, and every client
    rejects it."""
    assert stack_server.authority("::1", 8080) == "[::1]:8080"
    assert stack_server.authority("::", 8080) == "[::1]:8080"
    assert stack_server.authority("127.0.0.1", 8080) == "127.0.0.1:8080"
    assert stack_server.authority("0.0.0.0", 8080) == "127.0.0.1:8080"


@needs_ipv6
def test_a_server_on_ipv6_binds_and_answers(serving):
    service = serving(0, host="::1")
    assert wait_for(lambda: service.serving), service.status
    assert service.url.startswith("http://[::1]:")
    assert stack_server.identify(service.bound_port, "::1") is not None


def test_the_claim_is_settled_between_real_processes(serving, tmp_path):
    """Every other election test runs both sides in one interpreter, where the
    kernel is the only thing actually being tested. This is the arrangement the
    named mode is *for*: a separate pytest session already holding the port,
    identified over HTTP because there is no other way to ask it."""
    port = free_port()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from pytest_failure_instrumentation import stack_server\n"
            f"service = stack_server.StackService({port})\n"
            "service.start()\n"
            "time.sleep(120)\n",
        ]
    )
    try:
        assert wait_for(lambda: stack_server.identify(port) is not None), "holder never served"
        held_by = stack_server.identify(port)
        assert held_by["pid"] == holder.pid  # a different process, really

        waiting = serving(port, reclaim_seconds=0.2)
        assert wait_for(lambda: "another session is serving" in waiting.status), waiting.status
        assert not waiting.serving
        assert str(holder.pid) in waiting.status

        # And the port is handed over when that process goes away.
        holder.kill()
        holder.wait(timeout=10)
        assert wait_for(lambda: waiting.serving), waiting.status
        assert stack_server.identify(port)["pid"] == os.getpid()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_the_workers_endpoint_describes_the_run_it_is_serving(serving, tmp_path):
    """One request for the whole machine, assembled from files the run wrote
    anyway - so a UI learns *where* to look here, and looks with /stack."""
    run = tmp_path / "run-abc123"
    run.mkdir()
    (run / "owner.json").write_text(json.dumps({"pid": os.getpid()}))
    (run / "gw0.state").write_bytes(
        json.dumps(
            {
                "pid": os.getpid(),
                "nodeid": "test_pool.py::test_writes",
                "phase": "call",
                "time": time.time(),
                "tests_started": 3,
                "tests_finished": 2,
            }
        ).encode()
        + b"\n"
    )

    # The service is given its own run directory; /workers describes the
    # machine, so it looks at the parent.
    service = serving(0, directory=run)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(service.bound_port, "/workers", service.token)
    assert status == 200
    assert body["served_by"]["service"] == stack_server.SERVICE
    described = [entry for entry in body["runs"] if entry["session"] == "run-abc123"]
    assert len(described) == 1
    worker = described[0]["workers"][0]
    assert worker["worker"] == "gw0"
    assert worker["nodeid"] == "test_pool.py::test_writes"
    assert worker["process_exists"] is True


def test_the_workers_endpoint_is_listed_and_says_when_it_cannot_answer(serving):
    """A server started without an evidence directory can still serve stacks;
    it just has nothing to enumerate, and says which of the two it is."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/workers", service.token)
    assert status == 503
    assert "evidence directory" in body["error"]

    status, body = get(port, "/nothing", service.token)
    assert "/workers" in body["endpoints"]


def test_the_workers_endpoint_takes_a_worker_filter(serving, tmp_path):
    """Both spellings and both shapes, because a caller writes whichever
    occurs to them and being strict would only produce a wrong answer."""
    run = tmp_path / "run-abc123"
    run.mkdir()
    (run / "owner.json").write_text(json.dumps({"pid": os.getpid()}))
    for name in ("gw0", "gw1", "gw2"):
        (run / f"{name}.state").write_bytes(
            json.dumps(
                {"pid": os.getpid(), "nodeid": f"test_{name}.py::test_one", "time": time.time()}
            ).encode()
            + b"\n"
        )

    service = serving(0, directory=run)
    assert wait_for(lambda: service.serving), service.status

    def named(query: str) -> list[str]:
        status, body = get(service.bound_port, "/workers" + query, service.token)
        assert status == 200
        return [entry["worker"] for entry in body["runs"][0]["workers"]]

    assert named("") == ["gw0", "gw1", "gw2"]
    assert named("?worker=gw1") == ["gw1"]
    assert named("?worker=gw0,gw2") == ["gw0", "gw2"]
    assert named("?worker=gw0&worker=gw2") == ["gw0", "gw2"]
    assert named("?workers=gw1") == ["gw1"]
    assert named("?worker=") == ["gw0", "gw1", "gw2"]

    status, body = get(service.bound_port, "/workers?worker=gw9", service.token)
    assert body["runs"] == []
    assert body["filter"]["unmatched"] == ["gw9"]


# -- who may ask ----------------------------------------------------------


def test_every_endpoint_but_identity_wants_the_token(serving, tmp_path):
    """This server reports what local processes are executing, so it asks who
    is asking. Loopback is not that boundary: it bounds the reachable set to
    this machine, and every user on this machine is inside it."""
    run = tmp_path / "run-abc123"
    run.mkdir()
    (run / "owner.json").write_text(json.dumps({"pid": os.getpid()}))
    service = serving(0, directory=run)
    assert wait_for(lambda: service.serving), service.status
    port = service.bound_port

    assert get(port, f"/stack?pid={os.getpid()}")[0] == 401
    assert get(port, "/workers")[0] == 401
    assert get(port, f"/stack?pid={os.getpid()}", "not-the-token")[0] == 401

    assert get(port, f"/stack?pid={os.getpid()}", service.token)[0] == 200
    assert get(port, "/workers", service.token)[0] == 200


def test_a_token_that_is_not_ascii_is_refused_rather_than_crashing(serving, capfd):
    """``compare_digest`` raises on two non-ASCII ``str`` instead of saying no.

    The offered token is whatever the caller sent, so that TypeError came out
    of the handler *before* authentication: the request got no reply at all
    where a 401 belonged, and socketserver printed the traceback to the stderr
    a human is reading pytest's output from - the one thing ``log_message`` is
    overridden to prevent. One URL-encoded character, no credentials needed.

    Both ways of offering a token are exercised, because they decode
    differently: a query parameter arrives as UTF-8 and a header as latin-1.
    """
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"
    port = service.bound_port

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status_for(path: str, headers: Optional[dict] = None) -> int:
        request = urllib.request.Request(
            f"http://{stack_server.LOOPBACK}:{port}{path}", headers=headers or {}
        )
        try:
            with opener.open(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as refusal:
            return refusal.code

    # In the query string, percent-encoded as any client would send it.
    assert status_for("/workers?token=caf%C3%A9") == 401
    assert status_for("/workers?token=%F0%9F%92%A9") == 401
    # And in the header, where latin-1 is what the wire carries.
    assert status_for(
        "/workers", {stack_server.AUTH_HEADER: f"{stack_server.AUTH_SCHEME} caf\xe9"}
    ) == 401
    # The real one still works, so the fix did not just refuse everything.
    assert status_for(
        "/workers?token=" + service.token
    ) in (200, 503)  # 503 only because this service was given no directory

    assert "Traceback" not in capfd.readouterr().err


def test_a_handler_failure_never_reaches_the_report_as_a_traceback(serving, capfd):
    """A backstop, not a licence. socketserver's default prints the whole
    traceback to stderr, so one malformed request could bury the report it was
    meant to leave alone. The failure is kept on the server for whoever is
    debugging it; only its route to the terminal is removed."""
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"

    httpd = service._httpd
    assert httpd is not None
    assert httpd.last_error is None

    try:
        raise RuntimeError("a handler blew up")
    except RuntimeError:
        httpd.handle_error(None, ("127.0.0.1", 0))

    assert httpd.last_error is not None
    assert "a handler blew up" in httpd.last_error
    assert "Traceback" not in capfd.readouterr().err


def test_identity_stays_open_and_never_carries_the_token(serving):
    """It is what one session asks another before standing down from a
    contested port, and the two share no token."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/identity")
    assert status == 200
    assert "token" not in body
    # Which is also what makes the election work without one.
    assert stack_server.identify(port) is not None


def test_the_refusal_says_where_the_token_is(serving):
    """A 401 that does not say how to satisfy it teaches people to turn the
    whole thing off."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/workers")
    assert status == 401
    assert "callstack-" in body["error"]
    assert "Authorization" in body["error"]


def test_the_token_is_accepted_either_way_it_is_sent(serving):
    """A header is the right place for a credential; the query parameter is
    there because a person debugging with curl will reach for it."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    assert get(port, f"/stack?pid={os.getpid()}&token={service.token}")[0] == 200
    assert get(port, f"/stack?pid={os.getpid()}", service.token)[0] == 200


@pytest.mark.skipif(IS_WINDOWS, reason="a mode is not an ACL; see the docstring")
def test_the_address_file_is_owner_only_on_posix(serving, tmp_path):
    """The token is only as private as the file holding it, so that file is
    created owner-only rather than created and then narrowed - the second
    leaves a window, and a window is all anybody needs.

    Named for the platform it holds on. On Windows ``os.open``'s mode only
    decides the read-only attribute, so there is no owner-only guarantee for a
    test to make there and the file inherits the directory's ACL instead -
    which the module docstring says rather than leaving the skip to imply the
    check merely could not run."""
    run = tmp_path / "run-abc123"
    service = serving(0, directory=run)
    assert wait_for(lambda: service.serving), service.status

    published = wait_for(lambda: list(run.glob("callstack-*.json")))
    assert published
    assert stat.S_IMODE(published[0].stat().st_mode) == 0o600
    assert json.loads(published[0].read_text())["token"] == service.token


def test_a_pyspy_failure_reports_its_message_and_not_its_backtrace():
    """py-spy writes a message, then a "Caused by" section carrying the errno,
    then a Rust backtrace of its own frames. Taking the last line - the obvious
    thing - reported "10: main" as the reason a pid could not be read, which
    describes py-spy's stack and not the target at all."""
    stderr = (
        b"Error: Failed to get process executable name. "
        b"Check that the process is running.\n"
        b"\nCaused by:\n"
        b"    0: No such file or directory (os error 2)\n"
        b"\nStack backtrace:\n"
        b"   0: anyhow::error::<impl anyhow::Error>::msg\n"
        b"  10: main\n"
    )
    explained = pyspy._explained(stderr, 4242)

    assert "process 4242 is not running" in explained
    assert "Failed to get process executable name" in explained
    # The part that is about py-spy rather than about the process is gone.
    assert "main" not in explained.split(" - ")[-1] or "anyhow" not in explained
    assert "anyhow" not in explained
    assert "Stack backtrace" not in explained


def test_a_refusal_keeps_the_errno_that_says_which_refusal_it_is():
    """"Permission denied" and the errno under it are different facts, and the
    hint is chosen from the pair.

    The hint asserted is *this platform's*. An earlier version of this test
    asserted the Linux one everywhere and went red on macOS, where the right
    answer is SIP and root rather than ptrace_scope - the code had picked
    correctly and the test had not.
    """
    explained = pyspy._explained(
        b"Error: Failed to open process\n\nCaused by:\n"
        b"    0: Operation not permitted (os error 1)\n"
        b"\nStack backtrace:\n   9: std::rt\n  10: main\n",
        4242,
    )
    assert "Operation not permitted" in explained

    expected = "linux" if IS_LINUX else ("darwin" if IS_MACOS else "win32")
    assert pyspy.PERMISSION_HINTS[expected] in explained
    # And nobody else's hint, so a platform lookup that silently fell through
    # to a default would still fail here.
    for platform_key, hint in pyspy.PERMISSION_HINTS.items():
        if platform_key != expected:
            assert hint not in explained

    assert "main" not in explained


# -- saying so when there is no live view ---------------------------------


def test_a_stranger_on_the_port_is_reported_once(serving):
    """Somebody switched the live view on. Without an incident the run
    continues perfectly well and their UI shows nothing forever, with no error
    anywhere - from the outside "no server" and "no tests running" are
    identical."""
    port = free_port()
    stranger = socket.socket()
    stranger.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    stranger.bind((stack_server.LOOPBACK, port))
    stranger.listen(1)
    reported = []
    try:
        service = serving(
            port, reclaim_seconds=0.1, on_giving_up=lambda *args: reported.append(args)
        )
        assert wait_for(lambda: reported), service.status
        verdict, detail = reported[0]
        assert verdict == "PORT_TAKEN"
        assert "--callstack-port" in detail

        # Re-probed every interval for the life of the run; reported once.
        time.sleep(0.5)
        assert len(reported) == 1
    finally:
        stranger.close()


def test_an_address_that_cannot_be_bound_is_reported(serving):
    """Naming another port does not help here, so it is a separate verdict
    rather than one message with a different string in it."""
    reported = []
    service = serving(
        0, host="203.0.113.1", reclaim_seconds=0.1,
        on_giving_up=lambda *args: reported.append(args),
    )
    assert wait_for(lambda: reported), service.status
    assert reported[0][0] == "BIND_REFUSED"


def test_our_own_session_holding_the_port_is_not_an_incident(serving):
    """The named mode working as designed. Reporting it would turn the
    ordinary case into an alert and teach a reader to filter the kind out."""
    port = free_port()
    holder = serving(port)
    assert wait_for(lambda: holder.serving), holder.status

    reported = []
    waiting = serving(
        port, reclaim_seconds=0.1, on_giving_up=lambda *args: reported.append(args)
    )
    assert wait_for(lambda: "another session is serving" in waiting.status)
    time.sleep(0.5)
    assert reported == []


def test_the_incident_is_owned_by_the_runtime_and_stays_quiet():
    """No test is at fault and the run is unaffected. Left to attribution this
    would be "unknown", which means "we could not tell" and is scored
    needs-triage - and here it was known before the incident was built."""
    from pytest_failure_instrumentation.analysis import severity
    from pytest_failure_instrumentation.incidents import stack_server as kind

    incident = kind.build("PORT_TAKEN", "127.0.0.1", 8080, "something else is there")
    assert incident.owner_when_unattributable() == "runtime"
    assert severity.of(incident.kind, "runtime", incident.verdict, "high", False)[0] == (
        "informational"
    )
    assert "8080" in str(incident)
    assert "the run itself is unaffected" in str(incident)
    # The detail is printed once. The alert text is the product, and a fact
    # printed twice reads as two findings.
    assert str(incident).count("something else is there") == 1
    # And no "blamed on" or suspect line: nothing of anybody's ran.
    assert "blamed on" not in str(incident)
    assert "suspect" not in str(incident)


def test_the_same_address_failing_the_same_way_is_one_incident():
    from pytest_failure_instrumentation.incidents import stack_server as kind

    first = kind.build("PORT_TAKEN", "127.0.0.1", 8080, "a")
    again = kind.build("PORT_TAKEN", "127.0.0.1", 8080, "b")
    elsewhere = kind.build("PORT_TAKEN", "127.0.0.1", 9090, "a")

    assert first.fingerprint_parts() == again.fingerprint_parts()
    assert first.fingerprint_parts() != elsewhere.fingerprint_parts()


def test_the_kind_round_trips_through_the_registry():
    """A stored row parses back into its own model, like every other kind."""
    from pytest_failure_instrumentation.incidents import registry
    from pytest_failure_instrumentation.incidents import stack_server as kind

    incident = kind.build("BIND_REFUSED", "203.0.113.1", 0, "no such interface")
    parsed = registry.parse(json.loads(incident.model_dump_json()))
    assert parsed.kind == "stack_server_unavailable"
    assert parsed.verdict == "BIND_REFUSED"
    assert parsed.drawn is True
    assert "stack_server_unavailable" in json.dumps(registry.json_schema())


def test_a_real_run_reports_that_it_has_no_live_view(runner):
    """End to end, through the hook a product actually implements.

    The port is held for the whole inner run by this process, which is the
    situation a developer hits when 8080 is already something else's.
    """
    port = free_port()
    stranger = socket.socket()
    stranger.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    stranger.bind((stack_server.LOOPBACK, port))
    stranger.listen(1)
    try:
        runner.pytester.makepyfile(test_suite="def test_one():\n    assert True\n")
        incidents = runner.run(
            "-o", "failure_stack_server=true",
            "-o", f"failure_stack_server_port={port}",
            "test_suite.py",
            timeout=180,
        )
    finally:
        stranger.close()

    reported = runner.only(incidents, "stack_server_unavailable")
    assert reported.verdict == "PORT_TAKEN"
    assert reported.requested_port == port
    assert reported.owner == "runtime"
    assert reported.severity == "informational"
    assert reported.drawn is False
    assert "--callstack-port" in reported.detail
    # The run itself was fine, which the summary alongside it confirms.
    assert runner.only(incidents, "run_summary").exitstatus == 0


@pytest.mark.parametrize(
    ("linux", "macos", "expected"),
    [(True, False, "linux"), (False, True, "darwin"), (False, False, "win32")],
)
def test_every_platform_gets_its_own_refusal_hint(monkeypatch, linux, macos, expected):
    """The remedy for a refused read is different on each platform, and only
    one of the three can be checked by running here.

    The previous test asserts the hint for whichever platform it runs on, which
    means two of the three branches are only ever exercised by CI - and that is
    how a test asserting the Linux hint everywhere reached macOS and failed
    there while the code was right. This one drives all three anywhere.
    """
    monkeypatch.setattr(pyspy, "IS_LINUX", linux)
    monkeypatch.setattr(pyspy, "IS_MACOS", macos)

    explained = pyspy._explained(
        b"Error: Failed to open process\n\nCaused by:\n"
        b"    0: Operation not permitted (os error 1)\n",
        4242,
    )
    assert pyspy.PERMISSION_HINTS[expected] in explained
    assert "Operation not permitted" in explained


def test_the_hints_say_something_different_on_each_platform():
    """Three identical hints would pass the selection test above and help
    nobody: the whole point is that the remedy differs."""
    assert len(set(pyspy.PERMISSION_HINTS.values())) == len(pyspy.PERMISSION_HINTS)
    assert "ptrace_scope" in pyspy.PERMISSION_HINTS["linux"]
    assert "root" in pyspy.PERMISSION_HINTS["darwin"]


# -- announcing that it is up ---------------------------------------------


def test_a_serving_session_hands_out_everything_needed_to_reach_it(serving, tmp_path):
    """The payload is the whole interface for a drawn port. Anything missing
    from it sends a product back to parsing the discovery file, which is a
    private file that then cannot be changed."""
    announced: list[Any] = []
    service = serving(
        0,
        directory=tmp_path,
        on_ready=announced.append,
        session_id="run-under-test",
    )
    assert wait_for(lambda: announced), service.status
    server = announced[0]

    assert server.service == stack_server.SERVICE
    assert server.version
    assert server.pid == os.getpid()
    assert server.session_id == "run-under-test"
    assert server.directory == str(tmp_path)
    # The bound port, never the requested one: this asked for 0.
    assert server.port == service.bound_port
    assert server.port > 0
    assert str(server.port) in server.url
    assert server.token == service.token

    # And it actually opens the door it describes.
    status, payload = get(server.port, "/workers", token=server.token)
    assert status == 200, payload


def test_the_announcement_is_made_once_the_server_is_already_answering(serving, tmp_path):
    """The reason it is dispatched rather than called inline. An
    implementation that calls straight back into the server - the first thing
    most of them do - must not be waiting on the accept loop that is waiting
    on it."""
    answered: list[tuple[int, Any]] = []

    def call_it_back(server: Any) -> None:
        answered.append(get(server.port, "/identity", timeout=10.0))

    serving(0, directory=tmp_path, on_ready=call_it_back)
    assert wait_for(lambda: answered, timeout=20.0), "the hook never got an answer"
    status, payload = answered[0]
    assert status == 200
    assert payload["service"] == stack_server.SERVICE


def test_a_session_that_never_serves_announces_nothing(serving, tmp_path):
    """Two sessions, one named port. The one that stands down has no address
    to give anybody - and announcing anyway would have a product storing the
    holder's address twice, under two sessions."""
    port = free_port()
    holder = serving(port, directory=tmp_path)
    assert wait_for(lambda: holder.serving), holder.status

    announced: list[Any] = []
    stood_down = serving(port, directory=tmp_path, on_ready=announced.append)
    # Long enough for a claim to have been attempted and refused.
    time.sleep(2.0)
    assert not stood_down.serving
    assert not announced


def test_an_announcement_that_raises_does_not_stop_the_server(serving, tmp_path):
    """A product's reporting is never allowed to cost the run its live view."""

    def unhelpful(server: Any) -> None:
        raise RuntimeError("the product's database was down")

    service = serving(0, directory=tmp_path, on_ready=unhelpful)
    assert wait_for(lambda: service.serving), service.status
    status, _ = get(service.bound_port, "/identity")
    assert status == 200


def test_the_headers_on_the_payload_are_the_ones_the_server_accepts(serving, tmp_path):
    """The scheme is this package's to change, so a product that uses what it
    was handed keeps working across a change that a hard-coded "Bearer" would
    not survive."""
    announced: list[Any] = []
    service = serving(0, directory=tmp_path, on_ready=announced.append)
    assert wait_for(lambda: announced), service.status
    server = announced[0]

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(server.endpoint("/workers"), headers=server.headers())
    with opener.open(request, timeout=30.0) as response:
        assert response.status == 200


def test_a_real_run_hands_the_address_to_a_product_that_implements_the_hook(pytester):
    """The whole chain, in one real pytest: a drawn port, the plugin's own
    wiring, pluggy's dispatch, and a conftest that is exactly what a product
    would write. The unit tests above all call the callback directly, so
    nothing else here would notice the hook never being registered."""
    pytester.makeconftest(
        """
        import json


        def pytest_failure_server_ready(server):
            with open("server.json", "w") as handle:
                handle.write(server.model_dump_json())
        """
    )
    pytester.makepyfile(
        """
        def test_one():
            assert True
        """
    )
    # Zero: the drawn port, which is the case a product cannot configure ahead
    # of the run and therefore the case this hook exists for.
    result = pytester.runpytest_subprocess("-p", "failure_instrumentation", "--callstack-port", "0")
    result.assert_outcomes(passed=1)

    announced = json.loads((pytester.path / "server.json").read_text())
    assert announced["service"] == stack_server.SERVICE
    assert announced["port"] > 0
    assert announced["token"]
    assert announced["session_id"]
    assert str(announced["port"]) in announced["url"]
    # The directory it names is the one the run was writing evidence into.
    assert announced["directory"] and Path(announced["directory"]).name == announced["session_id"]


def test_implementing_the_hook_costs_nothing_when_the_server_is_off(pytester):
    """A product ships the hook once; the people running its tests decide per
    run whether to switch the server on. The run where they did not must be an
    ordinary run - not an "unknown hook" at check_pending, and not a hook
    called with nothing to report."""
    pytester.makeconftest(
        """
        def pytest_failure_server_ready(server):
            with open("server.json", "w") as handle:
                handle.write("called")
        """
    )
    pytester.makepyfile(
        """
        def test_one():
            assert True
        """
    )
    result = pytester.runpytest_subprocess("-p", "failure_instrumentation")
    result.assert_outcomes(passed=1)
    assert not (pytester.path / "server.json").exists()


def test_a_named_port_on_an_unbindable_host_says_so_and_stops(tmp_path):
    """The remedy has to match the fault. An address that is not an interface
    here cannot be fixed by choosing another port, and retrying it every few
    seconds for the length of the run fixes it even less - which is what
    deciding this by whether the port was *drawn* used to do."""
    reported: list[tuple[str, str]] = []
    # A documentation-range address, which is never a local interface.
    service = stack_server.StackService(
        18080, host="203.0.113.1", directory=tmp_path,
        on_giving_up=lambda verdict, detail: reported.append((verdict, detail)),
    )
    service.start()
    try:
        assert wait_for(lambda: reported), service.status
    finally:
        service.stop()

    verdict, detail = reported[0]
    assert verdict == stack_server_incident.BIND_REFUSED, (
        f"a bad host was reported as {verdict}, whose remedy is a different port"
    )
    assert "--callstack-port" not in detail, "advised a remedy that cannot help"
    assert "203.0.113.1" in detail
    # And it stopped rather than settling into a retry loop.
    assert not service.serving
