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
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Optional

import pytest

from pytest_failure_instrumentation import stack_server
from pytest_failure_instrumentation.incidents import stack_server as stack_server_incident
from pytest_failure_instrumentation.probes import pyspy
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


#: The band of numbers ``free_port`` hands out, and how much of it one xdist
#: worker owns. It stops below the lowest ephemeral range any of the three
#: platforms draws from - Linux takes 32768-60999, macOS and Windows
#: 49152-65535 - so that nothing on the machine is ever *assigned* one of
#: these. Something can still bind one by naming it, and in this suite only
#: this file does.
PORT_BAND_START = 21000
PORTS_PER_WORKER = 500
EPHEMERAL_FLOOR = 32768


def _ports_this_worker_owns() -> list[int]:
    """The numbers this process may hand out, in the order it will.

    xdist's identity is read at import rather than when a test asks for a
    port, because ``tests/conftest.py`` deletes ``PYTEST_XDIST_WORKER`` and
    ``PYTEST_XDIST_TESTRUNUID`` for the duration of every test - the inner
    pytest runs must not inherit the outer run's identity. Collection is when
    this module is imported, it happens in the worker that will run these
    tests, and it happens before any fixture.

    The worker id picks the slice, which is what makes it impossible for two
    workers of one run to be handed the same number. The run id only picks
    where in that slice this run starts, so that two copies of this suite on
    one machine do not walk the same numbers in step; that part is a
    probability rather than a guarantee, because nothing here reserves a band
    against a process that is not part of this run.
    """
    slices = (EPHEMERAL_FLOOR - PORT_BAND_START) // PORTS_PER_WORKER
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    numbered = "".join(character for character in worker if character.isdigit())
    # Modulo the slices that fit: a run with more workers than the band has
    # room for shares numbers, rather than allocating up into the ephemeral
    # range where the kernel would be handing the same ones out behind us.
    mine = (int(numbered) if numbered else 0) % slices
    run = os.environ.get("PYTEST_XDIST_TESTRUNUID", "") or str(os.getpid())
    first = zlib.crc32(run.encode()) % PORTS_PER_WORKER
    start = PORT_BAND_START + mine * PORTS_PER_WORKER
    return [start + (first + step) % PORTS_PER_WORKER for step in range(PORTS_PER_WORKER)]


_BAND = _ports_this_worker_owns()
_UNISSUED = iter(_BAND)


def free_port() -> int:
    """A number nothing else will be handed while a test is using it.

    This drew one from the kernel: bind port 0, read back what it chose, close
    the socket, return the number. That answers "what was free a moment ago",
    not "what will still be free when the caller binds it" - and every caller
    here hands the number on to something that binds it several statements
    later, one of them to a whole pytest subprocess that has yet to start.
    Anything drawing an ephemeral port in that window can be given the same
    number, and since CI runs this suite under ``-n 4`` the likeliest such
    thing is this very function in the three sibling workers. Losing the race
    does not even raise: the second binder reports "a stranger is on the port"
    and the run carries on, so it would arrive as an assertion about a status
    string rather than as an address already in use.

    So the number is not drawn from the kernel at all. It comes out of the
    band above, which the kernel never allocates from and no sibling worker
    shares, walked forwards so that a number is never handed out twice within
    a worker - where the tests are serial anyway. The bind below only checks
    that nothing got there first; the guarantee is the band and not the probe,
    which is the whole difference from what this replaces.
    """
    for candidate in _UNISSUED:
        with socket.socket() as probe:
            try:
                probe.bind((stack_server.LOOPBACK, candidate))
            except OSError:
                continue
        return candidate
    raise RuntimeError(
        f"this worker has handed out all {PORTS_PER_WORKER} ports of its band "
        f"from {_BAND[0]}: either something is sitting on them, or this file "
        "now wants more ports than one band holds"
    )


def get(
    port: int, path: str, token: Optional[str] = None, timeout: float = 30.0
) -> tuple[int, Any]:
    """A request with proxies off, which is how the plugin itself asks.

    CI sets ``http_proxy`` constantly, and a request for 127.0.0.1 that goes
    through a proxy tests the proxy.

    ``token`` is only needed against a server this run supplied one to. Most
    tests below start a server with none, because that is the default and the
    right one on loopback; omitting it there is not "asking anonymously", it
    is the whole interface.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = (
        {stack_server.AUTH_HEADER: f"{stack_server.AUTH_SCHEME} {token}"}
        if token
        else {}
    )
    request = urllib.request.Request(
        f"http://{stack_server.LOOPBACK}:{port}{path}", headers=headers
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refusal:
        return refusal.code, json.loads(refusal.read())


def raw(port: int, request: bytes, timeout: float = 30.0) -> tuple[int, dict, bytes]:
    """``(status, body, head)`` for a request written straight onto the socket.

    ``urllib`` cannot send most of what is worth testing here: it will not put
    a path that is not a URL on the wire, and it fills in the ``Host`` header
    from the address it connected to. Both are exactly what a hostile caller
    chooses for itself.

    A status of 0 means the server wrote nothing at all before closing, which
    is the failure several of the tests below are about - and it is the reason
    this reads to EOF rather than parsing with a client library, which would
    raise its own error over the top of the evidence.
    """
    with socket.create_connection((stack_server.LOOPBACK, port), timeout=timeout) as client:
        client.sendall(request)
        answer = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            answer += chunk
    head, _, body = answer.partition(b"\r\n\r\n")
    if not head:
        return 0, {}, b""
    return int(head.split()[1]), (json.loads(body) if body else {}), head


def a_run_of(tmp_path: Path, **workers: int) -> Path:
    """A run directory as the recorder leaves one, with ``name=pid`` workers.

    The state files are the only place this machine records which processes
    belong to a run, so they are what a server reads to decide whose stack it
    has any business reporting - see ``stack_server.serves_pid``.
    """
    run = tmp_path / "run-abc123"
    run.mkdir(exist_ok=True)
    (run / "owner.json").write_text(json.dumps({"pid": os.getpid()}))
    for name, pid in workers.items():
        (run / f"{name}.state").write_bytes(
            json.dumps(
                {"pid": pid, "nodeid": f"test_{name}.py::test_one", "time": time.time()}
            ).encode()
            + b"\n"
        )
    return run


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


def test_stopping_a_session_that_never_reached_its_accept_loop_returns(monkeypatch):
    """The interleaving that hung a run after it had finished.

    The claim publishes its handle and *then* reads the stop flag. A ``stop``
    that lands between those two statements takes the handle and calls
    ``shutdown`` on it, while the claim - now seeing the flag - stands the
    server down without ever entering ``serve_forever``. ``shutdown`` waits on
    an event that only that loop's own ``finally`` sets, so the wait never
    ends, and the bounded join that was supposed to make ``stop`` total sits on
    the next line, never reached. A pytest process that had run every test it
    was asked to hung there until somebody killed it.

    Driven by hand rather than by racing two threads and hoping: the state the
    service is put in here is exactly the state the claim leaves it in between
    those two statements - bound, published on the service, never served.
    """
    monkeypatch.setattr(stack_server, "ACCEPT_LOOP_TIMEOUT", 1.0)
    service = stack_server.StackService(0)
    httpd = stack_server._Server((stack_server.LOOPBACK, 0), stack_server._Handler)
    service._httpd = httpd
    try:
        returned = threading.Event()

        def stop_it() -> None:
            service.stop()
            returned.set()

        threading.Thread(target=stop_it, daemon=True).start()
        assert returned.wait(20), (
            "stop() never returned: it is waiting inside shutdown() for a loop "
            "that was never entered"
        )
    finally:
        httpd.server_close()


def test_a_session_that_is_serving_is_still_actually_shut_down(serving, tmp_path):
    """The other half of the fix, which is the half that could be lost to it:
    a server that *did* reach its accept loop must still be brought down by
    ``stop`` rather than left holding the port for the rest of the session."""
    port = free_port()
    service = serving(port, directory=tmp_path / "run")
    assert wait_for(lambda: service.serving), service.status
    service.stop()

    assert not service.serving
    assert stack_server.identify(port, timeout=0.5) is None
    # And the port is free for the next session, which is what shutting down
    # is for: the socket is closed rather than merely abandoned.
    with socket.socket() as probe:
        probe.bind((stack_server.LOOPBACK, port))


# -- answering ------------------------------------------------------------


@needs_pyspy
def test_the_serving_process_is_read_the_same_way_as_any_other(serving):
    """Its own frames are directly to hand and are deliberately not used.

    Answering for ourselves out of ``sys._current_frames`` costs a dict lookup
    and needs no permission from anybody, and it was a second reader with a
    second ``source`` for the one process that could avoid the first. A caller
    that has to know which mechanism answered has been handed two APIs.
    """
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, f"/stack?pid={os.getpid()}")
    assert status == 200
    assert body["source"] == "py-spy"
    assert body["pid"] == os.getpid()
    assert body["captured_at"] > 0

    functions = [frame["function"] for thread in body["threads"] for frame in thread["frames"]]
    assert "do_GET" in functions  # the request being served is on one of them


@needs_pyspy
def test_another_process_is_read_from_outside_it(serving, tmp_path):
    """The case the whole external reader exists for: a stack out of a process
    that was never asked for one and cannot be made to cooperate."""
    service = serving(free_port(), directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), service.status
    port = service.bound_port

    # The victim nominates its parent as a permitted tracer, which is what a
    # real worker now does at startup - see probes.tracing. Without it this
    # test is refused wherever Yama enforces ptrace_scope=1, because py-spy is
    # spawned by *this* process and is therefore the victim's sibling rather
    # than its ancestor. That is the configuration most Linux boxes ship.
    victim = subprocess.Popen([sys.executable, "-c", VICTIM_THAT_PERMITS_TRACING])
    try:
        # Named as one of this run's workers, because that is what makes it
        # this server's business at all - see the refusal test below.
        a_run_of(tmp_path, gw0=victim.pid)
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


def test_a_pid_no_process_could_have_is_refused_without_spending_a_reader(serving, tmp_path):
    """The reader is a separate program with its own idea of an integer.

    Handed 10^20 py-spy panics and its Rust backtrace became the API's
    "error"; handed a negative number it reads it as a flag and prints its own
    usage. Both cost a subprocess and one of the concurrency slots to produce a
    reply that says nothing about any process. A pid that cannot exist is
    refused where the reply can say why.
    """
    a_run_of(tmp_path, gw0=stack_server.MAX_PID)
    service = serving(free_port(), directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), "never served"
    port = service.bound_port

    for impossible in (-1, 0, stack_server.MAX_PID + 1, 99999999999999999999):
        status, body = get(port, f"/stack?pid={impossible}")
        assert status == 400, f"pid={impossible} reached the reader"
        assert "pid must be between" in body["error"]
        # Nothing py-spy said about itself leaks out as the explanation.
        assert "py-spy" not in body["error"] and "panicked" not in body["error"]

    # A pid this run claims still goes to the reader, whatever it finds there.
    status, _ = get(port, f"/stack?pid={stack_server.MAX_PID}")
    assert status == 502


def test_an_unreadable_process_answers_with_why(serving, tmp_path):
    """A UI that is told nothing shows an empty pane; one that is told why can
    say whether this is a dead process or a missing permission."""
    a_run_of(tmp_path, gw0=999999)
    service = serving(free_port(), directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), service.status

    status, body = get(service.bound_port, "/stack?pid=999999")
    assert status == 502
    assert body["error"]


def test_a_target_that_is_not_a_url_is_answered_rather_than_dropped(serving, capfd):
    """An unclosed bracket in the request target is ``Invalid IPv6 URL``, and
    ``urlparse`` was the first statement of the request.

    It ran before authentication and outside any guard, so the ValueError went
    to ``handle_error`` - which keeps tracebacks off the terminal by design -
    and the connection was then closed with nothing written to it. The caller
    got a socket that hung up: no status, no body, and no way to tell a broken
    server from a wrong address.

    Sent down a raw socket because no client library will put this on the wire,
    and in the absolute form because that is the one that reaches the parse: a
    request line may carry a whole URL, and ``BaseHTTPRequestHandler`` passes
    that through untouched while collapsing the leading ``//`` of the shorter
    spelling.
    """
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"

    status, body, _ = raw(
        service.bound_port, b"GET http://[oops HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert status == 400, "the malformed target got no reply at all"
    assert "not a URL" in body["error"]
    assert "Traceback" not in capfd.readouterr().err

    # The shorter spelling is answered too, and which answer it gets is not
    # this server's to decide. Collapsing a leading "//" in the request target
    # arrived in a CPython security release, so whether that reaches the parse
    # at all varies with the interpreter underneath: where it is collapsed this
    # is a path like any other and there is no such endpoint; where it is not,
    # it is the malformed target above. Windows on 3.9 answers 400 here and
    # Linux on 3.9 answers 404, which is how this assertion was found asserting
    # a property of CPython rather than one of this handler.
    #
    # What is being tested either way is the thing the fix is for: a reply
    # rather than a socket that hangs up.
    assert raw(
        service.bound_port, b"GET //[oops HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
    )[0] in (400, 404)

    # And the server is still answering afterwards, which is the other thing a
    # dropped connection leaves a caller unsure of.
    assert get(service.bound_port, "/identity")[0] == 200


def test_a_handler_that_raises_answers_500_rather_than_nothing(serving, capfd, monkeypatch):
    """The backstop behind the parse guard, for the defects nobody has found
    yet: whatever goes wrong in here, the caller is told that something did.

    What went wrong stays on the server. These replies are assembled out of a
    run's own state - paths, node ids, the frames of a test - and an exception
    from the middle of that is not a thing to hand to whoever asked.
    """
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"

    def explode() -> dict:
        raise RuntimeError("a run's private detail")

    monkeypatch.setattr(stack_server, "identity", explode)

    status, body = get(service.bound_port, "/identity")
    assert status == 500
    assert "a run's private detail" not in json.dumps(body)
    # Still recorded where somebody debugging this server can read it.
    assert "a run's private detail" in (service._httpd.last_error or "")
    assert "Traceback" not in capfd.readouterr().err


def test_only_the_processes_of_this_run_can_be_asked_about(serving, tmp_path):
    """The port is opened to watch *this* run, and that is what it answers on.

    The reader behind /stack does not care whose process it is pointed at, and
    the default supplies no token because loopback is taken to be the bound on
    who can ask. Between the two, ``/stack?pid=N`` counted from 1 was every
    Python process on the machine: another session, a service, an interpreter
    with a credential in a local.
    """
    run = a_run_of(tmp_path)
    service = serving(0, directory=run)
    assert wait_for(lambda: service.serving), service.status
    port = service.bound_port

    # The serving process is one this server has a reason to answer about: it
    # is the stack of whoever is asking. Whether a reader is installed to do
    # the reading is a different question from whether it is allowed, and 403
    # is the only answer this test is about.
    assert get(port, f"/stack?pid={os.getpid()}")[0] != 403

    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        status, body = get(port, f"/stack?pid={stranger.pid}")
        assert status == 403
        assert str(stranger.pid) in body["error"]
        assert "is not part of any run this server is serving" in body["error"]

        # Until the run says it is one of its workers, which it may do at any
        # point: the pids are read per request, because xdist replaces a dead
        # worker mid-run and a set taken when the port was claimed would refuse
        # exactly the worker somebody is asking about.
        a_run_of(tmp_path, gw0=stranger.pid)
        assert get(port, f"/stack?pid={stranger.pid}")[0] != 403
    finally:
        stranger.kill()
        stranger.wait(timeout=10)


def test_a_server_with_no_evidence_directory_answers_only_for_itself(serving):
    """It has nowhere to learn a run's workers from, so it claims none. The
    same position /workers is in, and it says so the same way."""
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"
    port = service.bound_port

    assert get(port, f"/stack?pid={os.getpid()}")[0] != 403

    status, body = get(port, "/stack?pid=999999")
    assert status == 403
    assert "no evidence directory" in body["error"]


def test_the_pids_a_server_will_answer_for_come_from_the_run(tmp_path):
    """The rule itself, without a socket in the way."""
    run = a_run_of(tmp_path, gw0=4242)

    assert stack_server.serves_pid(os.getpid(), tmp_path)
    assert stack_server.serves_pid(4242, tmp_path)
    assert not stack_server.serves_pid(4243, tmp_path)

    # Nothing but its own pid when there is no directory to read, and nothing
    # at all from a directory that has no runs in it.
    assert stack_server.serves_pid(os.getpid(), None)
    assert not stack_server.serves_pid(4242, None)
    assert not stack_server.serves_pid(4242, run)


def test_an_unknown_endpoint_lists_the_ones_that_exist(serving):
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/nothing")
    assert status == 404
    assert "/stack?pid=N" in body["endpoints"]


def test_two_reads_of_one_target_do_not_race_into_a_permissions_lecture(monkeypatch):
    """A process can only be suspended by one reader at a time.

    The loser's errno is EPERM, which is exactly what a permission problem
    gives - so the collision was reported as "ptrace is not permitted, check
    /proc/sys/kernel/yama/ptrace_scope", on machines with no Yama at all.
    Measured: four concurrent reads of one process, three answered and the
    fourth was sent to change a kernel setting that had nothing to do with it.

    And the collision is easy to cause: a UI polling every stuck
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


@needs_pyspy
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


        def ask(path):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            request = urllib.request.Request("http://127.0.0.1:{port}" + path)
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
            assert published, "the server published no address"
            assert "token" not in json.loads(open(published[0]).read())

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

    def slow_read(pid, options=None):
        held.set()
        released.wait(10)
        return pyspy.Reading([], None, options or pyspy.StackOptions())

    monkeypatch.setattr(stack_server.stacks, "live_reading", slow_read)
    hog = __import__("threading").Thread(
        target=stack_server.read_stack, args=(os.getpid() + 1,), daemon=True
    )
    hog.start()
    try:
        assert held.wait(10)
        reading = stack_server.read_stack(os.getpid() + 1)
        assert reading.threads is None
        assert "already in flight" in reading.error
    finally:
        released.set()
        hog.join(timeout=10)

    # And the slot comes back once the reader in flight is done.
    assert stack_server.read_stack(os.getpid() + 1).error is None


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


@pytest.mark.skipif(IS_WINDOWS, reason="a mode there is not an ACL, and this asserts one")
def test_the_address_is_written_to_a_file_this_process_created(tmp_path):
    """The temporary is opened, not assumed.

    ``write_text`` asked for O_CREAT and nothing else, and at a well-known
    name in a directory other things can write to that settles neither
    question. A symlink already at ``callstack-<pid>.json.part`` is
    *followed*, so a name anybody can predict aims this write wherever they
    point it - and it is renamed into place afterwards, so what a UI then
    reads is the attacker's file. An ordinary file already at that name keeps
    the mode it was created with, because O_CREAT does not change the mode of
    a file that exists, so a 0666 leftover stays 0666 however this asks for it.

    Called directly rather than served, because what is being tested is one
    write and the state of the directory before it.
    """
    run = tmp_path / "run-abc123"
    run.mkdir()
    elsewhere = tmp_path / "somebody-elses.json"
    elsewhere.write_text("untouched")

    service = stack_server.StackService(0, directory=run)
    service.bound_port = 8080
    temporary = run / f"callstack-{os.getpid()}.json.part"
    temporary.symlink_to(elsewhere)

    service._publish()

    assert elsewhere.read_text() == "untouched", "the write followed the symlink"
    published = run / f"callstack-{os.getpid()}.json"
    assert not published.is_symlink()
    assert json.loads(published.read_text())["port"] == 8080

    # And a leftover .part of somebody else's making does not decide the mode
    # of what gets renamed over the address file. Asserted as "not the mode
    # somebody else chose" rather than as one exact number: what this file
    # holds is a host, a port and a pid, and it is meant to be readable - a UI
    # runs as another uid often enough that locking it to 0o600 would break
    # the case a published address exists for. The flags above are about who
    # may substitute the target, not who may read it.
    published.unlink()
    temporary.touch()
    temporary.chmod(0o666)
    service._publish()
    mode = stat.S_IMODE(published.stat().st_mode)
    assert mode != 0o666, "the leftover's mode survived into the address file"
    assert not mode & stat.S_IWGRP and not mode & stat.S_IWOTH, (
        f"the address file is writable by somebody else: {mode:#o}"
    )


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
    service = serving(0, host="203.0.113.1", reclaim_seconds=0.2, token="s3cret")
    assert wait_for(lambda: "could not bind" in service.status), service.status
    assert not service.serving


@needs_pyspy
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

            # Nothing in here is a secret: it is a host, a port and a pid,
            # which is the address of a server anyone who can reach it may
            # query anyway. That is what lets this file live wherever the
            # rest of a run's evidence lives.
            assert "token" not in address
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(
                address["url"] + "/stack?pid=%d" % os.getpid()
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

    status, body = get(service.bound_port, "/workers")
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

    status, body = get(port, "/workers")
    assert status == 503
    assert "evidence directory" in body["error"]

    status, body = get(port, "/nothing")
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
        status, body = get(service.bound_port, "/workers" + query)
        assert status == 200
        return [entry["worker"] for entry in body["runs"][0]["workers"]]

    assert named("") == ["gw0", "gw1", "gw2"]
    assert named("?worker=gw1") == ["gw1"]
    assert named("?worker=gw0,gw2") == ["gw0", "gw2"]
    assert named("?worker=gw0&worker=gw2") == ["gw0", "gw2"]
    assert named("?workers=gw1") == ["gw1"]
    assert named("?worker=") == ["gw0", "gw1", "gw2"]

    status, body = get(service.bound_port, "/workers?worker=gw9")
    assert body["runs"] == []
    assert body["filter"]["unmatched"] == ["gw9"]


# -- who may ask ----------------------------------------------------------


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


def test_a_request_naming_a_host_this_server_never_bound_is_refused(serving, tmp_path):
    """DNS rebinding, which "it only listens on loopback" does not answer.

    A page the developer visits controls a name, serves it with a one-second
    TTL and then re-resolves it to 127.0.0.1. The browser sees the same scheme,
    name and port, so as far as the same-origin policy is concerned nothing has
    changed and the page's own script may read what comes back - which is every
    node id in the run and every frame in every worker. The policy is not
    bypassed here, it is satisfied, which is why the answer is not a CORS
    header but a check on the one field the page cannot choose.
    """
    service = serving(0, directory=a_run_of(tmp_path))
    assert wait_for(lambda: service.serving), service.status
    port = service.bound_port

    for endpoint in (b"/workers", b"/identity", b"/stack?pid=1"):
        status, body, _ = raw(
            port, b"GET " + endpoint + b" HTTP/1.0\r\nHost: rebound.example\r\n\r\n"
        )
        assert status == 403, endpoint
        assert "rebound.example" in body["error"]

    # Every spelling a real client can have connected by is still served. The
    # port is not part of the comparison: a browser leaves it out when it is
    # the scheme's default, so comparing it would refuse a correct caller
    # without refusing a single rebound one, whose name is wrong either way.
    for host in (b"127.0.0.1", b"127.0.0.1:%d" % port, b"localhost", b"localhost:%d" % port):
        status, _, _ = raw(port, b"GET /identity HTTP/1.0\r\nHost: " + host + b"\r\n\r\n")
        assert status == 200, host

    # And so is a request with no Host at all: HTTP/1.0 does not require one,
    # and a browser - the only thing that can be made to rebind - always sends
    # it. Refusing here would break curl and stop nothing.
    assert raw(port, b"GET /identity HTTP/1.0\r\n\r\n")[0] == 200


def test_an_ipv6_loopback_host_is_recognised_in_both_spellings():
    """A Host header brackets an IPv6 literal and a bind does not, so the two
    are compared with the brackets off - and ``::1`` must not be taken apart by
    a rightmost-colon split looking for a port."""
    assert stack_server._hostname("[::1]:8080") == "::1"
    assert stack_server._hostname("[::1]") == "::1"
    assert stack_server._hostname("::1") == "::1"
    assert stack_server._hostname("127.0.0.1:8080") == "127.0.0.1"
    assert stack_server._hostname(" LocalHost ") == "localhost"


def test_a_reply_is_not_left_for_a_browser_to_interpret(serving):
    """These bodies carry a run's own strings - a node id, a frame, a rejected
    Host echoed back - and content sniffing is how a body a server called data
    becomes a document the browser runs."""
    service = serving(free_port(), directory=None)
    assert wait_for(lambda: service.serving), "never served"

    _, _, head = raw(
        service.bound_port, b"GET /identity HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert b"X-Content-Type-Options: nosniff" in head


def test_a_run_that_supplied_no_token_asks_for_none(serving):
    """The default, and the right one on loopback, where the bind already
    bounds the reachable set to this machine."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    assert get(port, "/workers")[0] in (200, 503)  # 503: no directory, not 401
    # Not 200: whether a reader is installed to do the reading is a
    # different question from whether this request was authorised, and the
    # second is the only one this test is about.
    assert get(port, f"/stack?pid={os.getpid()}")[0] != 401


def test_a_supplied_token_is_demanded_on_every_endpoint_but_identity(serving):
    """Supplied, never minted. Whoever started the run picked the value, so
    both ends already have it and nothing has to be published for them to
    agree - which is what keeps it off disk entirely."""
    port = free_port()
    service = serving(port, token="s3cret")
    assert wait_for(lambda: service.serving), service.status

    assert get(port, f"/stack?pid={os.getpid()}")[0] == 401
    assert get(port, "/workers")[0] == 401
    assert get(port, f"/stack?pid={os.getpid()}", "not-the-token")[0] == 401

    # The right token is not 401, whatever the reader then makes of the
    # pid: this is about who is let in, not about what they are handed.
    assert get(port, f"/stack?pid={os.getpid()}", "s3cret")[0] != 401
    # Either way it is sent: a header is the right place for a credential, and
    # the query parameter is there because a person with curl reaches for it.
    assert get(port, f"/stack?pid={os.getpid()}&token=s3cret")[0] != 401

    # And still open, because two sessions that minted nothing cannot share a
    # credential, and this is what one asks the other before standing down.
    assert get(port, "/identity")[0] == 200


def test_the_token_is_never_written_down(serving, tmp_path):
    """The whole reason it is supplied rather than minted. A published secret
    makes the address file a credential store, and makes where a run may write
    its evidence a question about where a secret may live - which POSIX
    answers with an 0o600 and Windows does not answer at all."""
    run = tmp_path / "run-abc123"
    service = serving(0, directory=run, token="s3cret")
    assert wait_for(lambda: service.serving), service.status

    published = wait_for(lambda: list(run.glob("callstack-*.json")))
    assert published
    written = published[0].read_text()
    assert "s3cret" not in written
    assert "token" not in json.loads(written)


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
    service = serving(free_port(), directory=None, token="s3cret")
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

    assert status_for("/workers?token=caf%C3%A9") == 401
    assert status_for("/workers?token=%F0%9F%92%A9") == 401
    assert status_for(
        "/workers", {stack_server.AUTH_HEADER: f"{stack_server.AUTH_SCHEME} caf\xe9"}
    ) == 401
    # The real one still works, so the fix did not just refuse everything.
    assert status_for("/workers?token=s3cret") in (200, 503)

    assert "Traceback" not in capfd.readouterr().err


def test_the_refusal_says_where_the_token_comes_from(serving):
    """A 401 that does not say how to satisfy it teaches people to turn the
    whole thing off - and here the answer is not a file to go and read, which
    is what a reader who used an earlier version would go looking for."""
    port = free_port()
    service = serving(port, token="s3cret")
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/workers")
    assert status == 401
    assert "PYTEST_CALLSTACK_TOKEN" in body["error"]
    assert "Authorization" in body["error"]


def test_binding_off_loopback_without_a_token_is_refused_before_the_socket(serving):
    """The one combination nobody configures on purpose: every local process's
    stack, served to whatever can route to this host. A warning is the wrong
    instrument - by the time it is read the port has been open for the length
    of the run - so it is refused, and reported the way a taken port is."""
    reported: list[Any] = []
    service = serving(
        0, host="0.0.0.0", on_giving_up=lambda *args: reported.append(args)
    )
    assert wait_for(lambda: reported), service.status
    assert reported[0][0] == "BIND_REFUSED"
    assert "no token was supplied" in reported[0][1]
    assert not service.serving

    # And it names the address that was actually asked for. `authority` maps a
    # wildcard to loopback so a client has something to connect to; in a
    # refusal that rewrite named 127.0.0.1 - the one address that was not the
    # problem, and one the reader never typed.
    assert "0.0.0.0" in service.status
    assert "127.0.0.1" not in service.status


def test_a_token_makes_binding_off_loopback_a_decision_rather_than_a_refusal(serving):
    """Which is the container case: the UI is outside, 127.0.0.1 in there is
    unreachable from it, and the exposure is deliberate."""
    service = serving(0, host="0.0.0.0", token="s3cret")
    assert wait_for(lambda: service.serving), service.status
    assert get(service.bound_port, f"/stack?pid={os.getpid()}", "s3cret")[0] != 401
    assert get(service.bound_port, f"/stack?pid={os.getpid()}")[0] == 401


def test_identity_is_what_one_session_asks_another(serving):
    """The election runs on it: a session that lost a contested port asks
    whoever holds it whether they are one of ours before standing down."""
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/identity")
    assert status == 200
    assert body["service"] == stack_server.SERVICE
    assert stack_server.identify(port) is not None


def test_the_address_file_carries_no_credential(serving, tmp_path):
    """It used to, and that is what turned "where may a run write its
    evidence" into a question about where a *secret* may live - one this
    package could answer on POSIX with an 0o600 and could not answer on
    Windows at all, where a mode is not an ACL and the file inherits the
    directory's.

    What is in it now is a host, a port and a pid: the address of a server
    anyone who can reach it may query anyway. So it goes wherever the rest of
    a run's evidence goes, on every platform, and the guarantee that held on
    only one of them is no longer load-bearing on any."""
    run = tmp_path / "run-abc123"
    service = serving(0, directory=run)
    assert wait_for(lambda: service.serving), service.status

    published = wait_for(lambda: list(run.glob("callstack-*.json")))
    assert published
    address = json.loads(published[0].read_text())
    assert "token" not in address
    assert address["port"] == service.bound_port
    assert address["pid"] == os.getpid()


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
        0, host="203.0.113.1", reclaim_seconds=0.1, token="s3cret",
        on_giving_up=lambda *args: reported.append(args),
    )
    assert wait_for(lambda: reported), service.status
    assert reported[0][0] == "BIND_REFUSED"


def test_a_port_outside_the_range_is_a_bind_refusal_not_a_thread_traceback(serving):
    """The bind answers an OverflowError here, not the OSError every other
    unbindable address gives, and only OSError was being caught - so the
    supervisor thread died with a raw traceback, no incident was raised, and
    the server silently never served. Under ``filterwarnings = error`` pytest
    turned that thread exception into a failure, so a suite where every test
    passed exited non-zero over a typo in a port number.
    """
    reported = []
    service = serving(
        99999, reclaim_seconds=0.1, on_giving_up=lambda *args: reported.append(args)
    )
    assert wait_for(lambda: reported), service.status
    assert reported[0][0] == "BIND_REFUSED", reported
    assert "0-65535" in reported[0][1] or "port must be" in reported[0][1], reported


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

    # And the address it describes actually answers.
    status, payload = get(server.port, "/workers")
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
    assert announced["token"] == ""  # this run supplied none
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
        18080, host="203.0.113.1", directory=tmp_path, token="s3cret",
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

# -- what the reader was asked for, and what it did -----------------------


def _reading(monkeypatch, payload: bytes, returncode: int = 0, stderr: bytes = b""):
    """py-spy replaced by a fixed answer, so option handling is what is tested.

    The mapping from py-spy's JSON to this package's shape is the thing under
    test in several of these, and pinning the JSON is what makes them say
    something on a machine with no py-spy and no process worth reading.
    """

    class Finished:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = payload
            self.stderr = stderr

    recorded: list[list[str]] = []

    def run(command, **kwargs):
        recorded.append(list(command))
        return Finished()

    monkeypatch.setattr(pyspy, "executable", lambda: "/nonexistent/py-spy")
    monkeypatch.setattr(pyspy.subprocess, "run", run)
    return recorded


#: One Python frame and one native frame, in py-spy's own shape - a native
#: frame carries the binary in ``module`` and has no line, which is what tells
#: the two apart.
TWO_KINDS_OF_FRAME = json.dumps(
    [
        {
            "thread_id": 7,
            "thread_name": "MainThread",
            "os_thread_id": 4242,
            "owns_gil": True,
            "active": True,
            "frames": [
                {
                    "name": "sqlite3_step",
                    "filename": "/usr/lib/_sqlite3.so",
                    "module": "/usr/lib/_sqlite3.so",
                    "line": 0,
                    "locals": None,
                },
                {
                    "name": "waiting",
                    "filename": "/src/app.py",
                    "module": None,
                    "line": 41,
                    "locals": [
                        {"name": "lease", "addr": 94, "arg": True, "repr": '"l-77"'},
                        {"name": "waited", "addr": 95, "arg": False, "repr": "27.4"},
                    ],
                },
            ],
        }
    ]
).encode()


def test_each_option_reaches_py_spy_as_its_own_flag(monkeypatch):
    """The three are independent and each has to actually be passed on."""
    for options, expected in [
        (pyspy.StackOptions(), []),
        (pyspy.StackOptions(native=True), ["--native"]),
        (pyspy.StackOptions(locals=True), ["--locals"]),
        (pyspy.StackOptions(nonblocking=True), ["--nonblocking"]),
        (pyspy.StackOptions(native=True, locals=True), ["--native", "--locals"]),
    ]:
        recorded = _reading(monkeypatch, b"[]")
        pyspy.read(4321, options)
        # --pid is how the target is named rather than an option, so it is
        # not part of what this is checking.
        chosen = [
            flag
            for flag in recorded[0]
            if flag.startswith("--") and flag not in ("--pid",)
        ]
        assert chosen == ["--json", *expected]


def test_native_gives_way_to_nonblocking_and_says_so(monkeypatch):
    """py-spy refuses the pair outright, so passing both through would turn a
    request that can be answered into a failed read.

    Native is the half that goes: --nonblocking is a promise about the target,
    and honouring native instead would pause a process somebody asked not to
    have paused."""
    recorded = _reading(monkeypatch, b"[]")
    reading = pyspy.read(4321, pyspy.StackOptions(native=True, nonblocking=True))

    assert "--native" not in recorded[0]
    assert "--nonblocking" in recorded[0]
    # What comes back is what was done, not what was asked for.
    assert reading.options.native is False
    assert reading.options.nonblocking is True
    assert reading.notes and "refuses that pair" in reading.notes[0]


def test_a_py_spy_that_cannot_unwind_still_answers_with_python_frames(monkeypatch):
    """A build without native support is a reason to return fewer frames, not
    a reason to return none - and the caller has to be told which it got."""
    attempts: list[list[str]] = []

    class Finished:
        def __init__(self, command) -> None:
            native = "--native" in command
            self.returncode = 1 if native else 0
            self.stdout = b"" if native else b"[]"
            self.stderr = b"error: unexpected argument '--native'" if native else b""

    def run(command, **kwargs):
        attempts.append(list(command))
        return Finished(command)

    monkeypatch.setattr(pyspy, "executable", lambda: "/nonexistent/py-spy")
    monkeypatch.setattr(pyspy.subprocess, "run", run)

    reading = pyspy.read(4321, pyspy.StackOptions(native=True))

    assert len(attempts) == 2, "the retry without --native never happened"
    assert "--native" in attempts[0] and "--native" not in attempts[1]
    assert reading.error is None and reading.threads == []
    assert reading.options.native is False
    assert reading.notes and "native frames" in reading.notes[0]


def test_a_refused_attach_is_not_retried_as_a_native_problem(monkeypatch):
    """The retry above must not swallow the failures that are about the
    process rather than the flag - a refused ptrace re-read without --native
    fails identically, and would be reported as a py-spy build feature.

    The hint appended to it is platform-specific - ptrace_scope on Linux, SIP
    on macOS, opening the process on Windows - so this asserts that one was
    added rather than which, the way ``test_a_refused_attach_says_what_to_do
    _about_it`` does. Asserting the Linux wording passed on Linux and failed
    on every other job in the matrix.
    """
    attempts = _reading(
        monkeypatch,
        b"",
        returncode=1,
        stderr=b"Failed to suspend process - Operation not permitted (os error 1)",
    )
    reading = pyspy.read(4321, pyspy.StackOptions(native=True))

    assert len(attempts) == 1, "a permission failure was retried"
    # Reported as itself: py-spy's own words, plus this platform's advice.
    assert reading.error and "Operation not permitted" in reading.error
    assert any(hint in reading.error for hint in pyspy.PERMISSION_HINTS.values())
    # And not as a native problem: nothing downgraded, nothing explained away.
    assert reading.notes == ()
    assert reading.options.native is True


def test_locals_are_absent_rather_than_empty_when_nobody_asked(monkeypatch):
    """None and [] are different answers: one says the caller did not ask, the
    other says the frame holds nothing. A UI showing "no variables" for a
    frame nobody requested variables for is reporting a fact it does not have.
    """
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    without = pyspy.read(4321, pyspy.StackOptions())
    assert [frame["locals"] for frame in without.threads[0]["frames"]] == [None, None]

    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    with_them = pyspy.read(4321, pyspy.StackOptions(locals=True))
    native, python = with_them.threads[0]["frames"]
    # The native frame holds no Python variables, which is [] - not None, which
    # would read as "you did not ask".
    assert native["locals"] == []
    assert python["locals"] == [
        {"name": "lease", "repr": '"l-77"', "argument": True},
        {"name": "waited", "repr": "27.4", "argument": False},
    ]


def test_a_locals_address_is_not_published(monkeypatch):
    """py-spy offers the object's address, which says nothing about the value
    and undoes what ASLR was buying."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    reading = pyspy.read(4321, pyspy.StackOptions(locals=True))
    for frame in reading.threads[0]["frames"]:
        for variable in frame["locals"] or []:
            assert "addr" not in variable


def test_a_native_frame_is_marked_by_its_module_not_its_line(monkeypatch):
    """A Python frame in a generated file can have no useful line either, so
    blaming the extension for that would be a wrong answer, not a missing
    one."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    native, python = pyspy.read(4321).threads[0]["frames"]

    assert native["native"] is True
    assert native["module"] == "/usr/lib/_sqlite3.so"
    assert native["line"] == 0
    assert python["native"] is False
    assert python["module"] is None


def test_reading_without_a_pause_cannot_claim_to_know_who_holds_the_gil(
    monkeypatch,
):
    """py-spy still reports both under --nonblocking, read at some instant
    other than the one the frames came from. A UI captioning a thread from
    that is being told something nobody measured."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    paused = pyspy.read(4321).threads[0]
    assert paused["owns_gil"] is True and paused["active"] is True

    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    unpaused = pyspy.read(4321, pyspy.StackOptions(nonblocking=True)).threads[0]
    assert unpaused["owns_gil"] is None and unpaused["active"] is None
    # The frames themselves are still the frames.
    assert unpaused["frames"][0]["function"] == "sqlite3_step"
    assert unpaused["os_thread_id"] == 4242


# -- the options over HTTP ------------------------------------------------


def test_the_stack_endpoint_reports_the_options_it_applied(monkeypatch, serving):
    """A caller that showed its own request back to a user would caption
    frames with a setting that did not produce them."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, f"/stack?pid={os.getpid()}&native&nonblocking")
    assert status == 200
    assert body["options"] == {
        "native": False,
        "locals": False,
        "nonblocking": True,
    }
    assert any("refuses that pair" in note for note in body["notes"])


def test_a_bare_flag_switches_an_option_on(monkeypatch, serving):
    """``?locals`` is how a URL carries a boolean, and reading it as false
    would make the shortest spelling the one that does nothing. Only an
    explicit falsehood is a no."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    for query, expected in [
        ("locals", True),
        ("locals=1", True),
        ("locals=true", True),
        ("locals=0", False),
        ("locals=false", False),
        ("", False),
    ]:
        _, body = get(port, f"/stack?pid={os.getpid()}&{query}")
        assert body["options"]["locals"] is expected, query


def test_notes_are_absent_when_nothing_was_downgraded(monkeypatch, serving):
    """Present means something was given up, so an always-empty list would
    make the field say nothing."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    port = free_port()
    service = serving(port)
    assert wait_for(lambda: service.serving), service.status

    _, body = get(port, f"/stack?pid={os.getpid()}&locals")
    assert "notes" not in body


def test_a_run_can_refuse_to_serve_locals_at_all(monkeypatch, serving):
    """The variables are the one thing here that is the data a test works on
    rather than the shape of the code, and no filter could tell a password
    from a lease id - so the switch is honest and total."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    port = free_port()
    service = serving(port, serves_locals=False)
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, f"/stack?pid={os.getpid()}&locals")
    assert status == 403
    assert "failure_stack_server_locals" in body["error"]

    # The frames themselves are still served.
    status, body = get(port, f"/stack?pid={os.getpid()}")
    assert status == 200
    assert body["threads"]


# -- naming the process by worker -----------------------------------------


def test_a_worker_can_be_named_instead_of_a_pid(monkeypatch, tmp_path, serving):
    """A person looking at a stalled gw3 is asking about that worker. Making
    them resolve it first costs a request whose answer can be stale by the
    time the second one lands."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    a_run_of(tmp_path, gw0=os.getpid())
    port = free_port()
    service = serving(port, directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/stack?worker=gw0&locals")
    assert status == 200
    assert body["pid"] == os.getpid()
    assert body["worker"] == "gw0"
    assert body["threads"][0]["frames"][1]["locals"]


def test_an_unknown_worker_is_not_found_rather_than_forbidden(
    monkeypatch, tmp_path, serving
):
    """Unlike a pid, which may name a real process this server has no business
    reading, a worker name is only ever a name in this run - so there is
    nothing to refuse, only nothing to find."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    a_run_of(tmp_path, gw0=os.getpid())
    port = free_port()
    service = serving(port, directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, "/stack?worker=gw9")
    assert status == 404
    assert "gw9" in body["error"] or body.get("worker") == "gw9"


def test_a_worker_name_never_becomes_a_path(monkeypatch, tmp_path, serving):
    """The name arrives in a URL. Joining it onto the evidence root - or
    interpolating it into a glob, which is the same mistake wearing a
    pattern's clothes - would let a caller address files outside the run."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    a_run_of(tmp_path, gw0=os.getpid())
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / "secret.state").write_bytes(
        json.dumps({"pid": os.getpid(), "nodeid": "x"}).encode()
    )
    port = free_port()
    service = serving(port, directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), service.status

    for name in ("../elsewhere/secret", "..%2Felsewhere%2Fsecret", "*", "gw?"):
        status, _ = get(port, f"/stack?worker={name}")
        assert status == 404, f"{name!r} resolved to something"


def test_a_finished_worker_does_not_resolve_to_its_last_pid(
    monkeypatch, tmp_path, serving
):
    """Pids are reused and a state file outlives the process it describes.
    Handing back a dead worker's pid means reading whatever the OS has since
    given that number to - a stranger's process, served as this run's."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    run = a_run_of(tmp_path, gw0=os.getpid())
    (run / "gw1.state").write_bytes(
        json.dumps({"pid": 999_999, "nodeid": "test_x.py::test_one"}).encode() + b"\n"
    )
    port = free_port()
    service = serving(port, directory=run)
    assert wait_for(lambda: service.serving), service.status

    status, _ = get(port, "/stack?worker=gw1")
    assert status == 404, "a dead worker's pid was served"


def test_naming_a_process_twice_or_not_at_all_is_refused(
    monkeypatch, tmp_path, serving
):
    """Two names can disagree and there is no right one to prefer; no name at
    all used to read as pid 0."""
    _reading(monkeypatch, TWO_KINDS_OF_FRAME)
    a_run_of(tmp_path, gw0=os.getpid())
    port = free_port()
    service = serving(port, directory=tmp_path / "run-abc123")
    assert wait_for(lambda: service.serving), service.status

    status, body = get(port, f"/stack?pid={os.getpid()}&worker=gw0")
    assert status == 400 and "not both" in body["error"]

    status, body = get(port, "/stack")
    assert status == 400 and "endpoints" in body
