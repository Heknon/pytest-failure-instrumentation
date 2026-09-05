"""Every byte a killed worker wrote to stderr, kept for the incident.

The line that explains a native death - ``pthread_create failed``, a malloc
abort - is on stderr and in no stack, and the process is gone before Python
runs. pytest hands its own capture to a report only for a phase that completed,
so this reads fd 2 directly, into a file whose synchronous writes survive an
abort. The tests that matter prove it catches a message printed in the very
phase that crashes, and never disturbs pytest's own capture or the run.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import tempfile

import pytest

from pytest_failure_instrumentation.analysis import classify
from pytest_failure_instrumentation.capture.output import StderrTee, read_tail
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident

from .conftest import needs_xdist

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX-only for now")


# -- the tee, in isolation ---------------------------------------------------


@posix_only
def test_a_synchronous_write_is_on_disk_before_the_process_could_abort(tmp_path):
    """A real file, not a pipe: os.write(2) is a synchronous write(2) the
    kernel has persisted before an abort on the next line could run. A pipe
    drained by a thread of the same dying process could not promise that."""
    tee = StderrTee(tmp_path / "gw0.output")
    assert tee.start(), tee.reason
    # pytest owns fd 2 by pointing it at its own file; the tee takes it after.
    pytest_file = tempfile.TemporaryFile()
    saved = os.dup(2)
    os.dup2(pytest_file.fileno(), 2)
    try:
        tee.take()
        assert stat.S_ISREG(os.fstat(2).st_mode)
        os.write(2, b"OpenBLAS blas_thread_init: pthread_create failed\n")
        # On disk now - before any hand_back, which is the abort case.
        assert read_tail(tmp_path / "gw0.output") == ["OpenBLAS blas_thread_init: pthread_create failed"]
        tee.hand_back()
        # Handed back: fd 2 is pytest's file again, and it received the bytes.
        assert stat.S_ISREG(os.fstat(2).st_mode)
        pytest_file.seek(0)
        assert b"pthread_create failed" in pytest_file.read()
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        pytest_file.close()
        tee.close()


@posix_only
def test_the_file_is_trimmed_between_phases_never_within_one(tmp_path):
    # A throwaway stands in for pytest's capture file, so the passthrough does
    # not reach the real terminal.
    sink = tempfile.TemporaryFile()
    saved = os.dup(2)
    os.dup2(sink.fileno(), 2)
    tee = StderrTee(tmp_path / "gw0.output", limit=2048)
    assert tee.start()
    try:
        for _ in range(50):
            os.dup2(sink.fileno(), 2)   # pytest re-takes fd 2 each phase
            tee.take()
            os.write(2, b"x" * 200 + b"\n")
            tee.hand_back()
        # Bounded: trimmed to the tail each phase, so far below the 10 KB that
        # fifty untrimmed phases would be - and never mid-line.
        data = (tmp_path / "gw0.output").read_bytes()
        assert len(data) < 2048 + 400, len(data)
        assert data.startswith(b"x")
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        tee.close()
        sink.close()
    # A single phase may exceed the limit while it runs - the crash-before-trim
    # case - and its whole output is kept, not trimmed underneath it.
    sink = tempfile.TemporaryFile()
    saved = os.dup(2)
    os.dup2(sink.fileno(), 2)
    tee2 = StderrTee(tmp_path / "gw1.output", limit=512)
    assert tee2.start()
    try:
        tee2.take()
        os.write(2, b"y" * 4000 + b"\n")
        assert len(read_tail(tmp_path / "gw1.output", limit=1 << 20)[0]) >= 4000
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        tee2.close()
        sink.close()


# -- the verdict surfaces the line -------------------------------------------


def test_the_verdict_surfaces_the_last_stderr_line_on_a_crash():
    incident = WorkerDeathIncident(
        worker="gw1", exit_status=-signal.SIGABRT if hasattr(signal, "SIGABRT") else -6,
        test_in_flight="test_x.py::test_x", phase="call",
        recent_output=["loading libfoo", "OpenBLAS blas_thread_init: pthread_create failed"],
    )
    _v, _c, evidence = classify.of(incident)
    assert any("Last stderr: OpenBLAS blas_thread_init: pthread_create failed" in line for line in evidence)


def test_an_absent_tail_is_not_read_as_silence():
    incident = WorkerDeathIncident(worker="gw1", exit_status=-6, test_in_flight="t.py::t", phase="call")
    _v, _c, evidence = classify.of(incident)
    assert not any(line.startswith("Last stderr:") for line in evidence)


# -- for real ----------------------------------------------------------------


def _abort_suite(message: str, where: str) -> str:
    write = f'os.write(2, {message!r}.encode() + b"\\n")'
    if where == "import":
        return f"""
import ctypes, os
{write}
def test_filler():
    assert True
def test_crashes():
    ctypes.CDLL(None).abort()
"""
    if where == "fixture":
        return f"""
import ctypes, os
import pytest
@pytest.fixture
def noisy():
    {write}
    yield
def test_filler():
    assert True
def test_crashes(noisy):
    ctypes.CDLL(None).abort()
"""
    return f"""
import ctypes, os
def test_filler():
    assert True
def test_crashes():
    {write}
    ctypes.CDLL(None).abort()
"""


@posix_only
@needs_xdist
@pytest.mark.parametrize("where", ["call", "fixture", "import"])
def test_a_native_message_reaches_the_incident_wherever_it_was_printed(distributed, where):
    """The message survives whether it was printed in the crashing call, in a
    fixture, or at import - each is a different point pytest owns fd 2, and the
    tee takes it back at each."""
    message = "OpenBLAS blas_thread_init: pthread_create failed for nth=64"
    distributed.pytester.makepyfile(test_abort=_abort_suite(message, where))
    incidents = distributed.run(
        "-n", "2", "-o", "failure_capture_output=true", "test_abort.py", timeout=180
    )
    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "NATIVE_CRASH"
    assert any("pthread_create failed" in line for line in death.recent_output), death.recent_output
    assert any(line.startswith("Last stderr:") for line in death.evidence)


@posix_only
@needs_xdist
def test_capture_does_not_disturb_pytests_own_captured_output(distributed):
    """The one thing this must never do: cost pytest its captured-on-failure
    output. A failing test's stdout and stderr still reach pytest's report."""
    distributed.pytester.makepyfile(
        test_suite="""
        import os, sys
        def test_prints_then_fails():
            print("VISIBLE-STDOUT")
            sys.stderr.write("VISIBLE-STDERR\\n")
            os.write(2, b"VISIBLE-NATIVE\\n")
            assert False
        def test_two():
            assert True
        """
    )
    result = distributed.pytester.runpytest_subprocess(
        "--failure-instrumentation", "-n", "2", "-o", "failure_capture_output=true",
        "test_suite.py", "-rA", timeout=180,
    )
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*VISIBLE-STDOUT*"])
    result.stdout.fnmatch_lines(["*VISIBLE-STDERR*"])
    result.stdout.fnmatch_lines(["*VISIBLE-NATIVE*"])


@needs_xdist
def test_a_collection_error_is_still_reported(distributed):
    """The collection wrapper must not swallow an import that raises."""
    distributed.pytester.makepyfile(
        test_broken="raise RuntimeError('boom at import')\n\ndef test_never():\n    pass\n"
    )
    result = distributed.pytester.runpytest_subprocess(
        "--failure-instrumentation", "-n", "2", "-o", "failure_capture_output=true",
        "test_broken.py", timeout=180,
    )
    result.stdout.fnmatch_lines(["*boom at import*"])


@posix_only
@needs_xdist
def test_the_tee_stands_down_for_a_test_that_captures_fd_output_itself(distributed):
    """capfd and capfdbinary take fd 1/2 over for the test to read them back.
    The tee must not take fd 2 out from under them - that would make their
    readouterr() miss what the test wrote, which is a change to a passing
    test. It stands down for such a test, and still captures a crash in a
    plain test in the same run: the stand-down is per test, not per run.
    """
    (distributed.pytester.path / "test_fdfix.py").write_text(
        "import ctypes, os, sys\n"
        "def test_capfd_still_reads_fd_level(capfd):\n"
        "    os.write(1, b'native-out\\n')\n"
        "    os.write(2, b'native-err\\n')\n"
        "    sys.stderr.write('py-err\\n')\n"
        "    out, err = capfd.readouterr()\n"
        "    assert 'native-out' in out, repr(out)\n"
        "    assert 'native-err' in err and 'py-err' in err, repr(err)\n"
        "def test_capfdbinary_reads_bytes(capfdbinary):\n"
        "    os.write(2, b'raw-native-bytes\\n')\n"
        "    _out, err = capfdbinary.readouterr()\n"
        "    assert b'raw-native-bytes' in err, err\n"
        "def test_a_plain_test_still_crashes_captured():\n"
        "    os.write(2, b'OpenBLAS pthread_create failed\\n')\n"
        "    ctypes.CDLL(None).abort()\n",
        encoding="utf-8",
    )
    incidents = distributed.run(
        "-n", "2", "-o", "failure_capture_output=true", "test_fdfix.py", timeout=180
    )
    # The two fd-fixture tests passed (their asserts held because the tee stood
    # down), and the plain test's crash was still captured.
    death = distributed.only(incidents, "worker_death")
    assert any("pthread_create failed" in line for line in death.recent_output), death.recent_output


@posix_only
@needs_xdist
def test_capsys_is_untouched_being_sys_level(distributed):
    (distributed.pytester.path / "test_capsys.py").write_text(
        "import sys\n"
        "def test_capsys(capsys):\n"
        "    print('hello'); sys.stderr.write('world\\n')\n"
        "    out, err = capsys.readouterr()\n"
        "    assert out == 'hello\\n' and err == 'world\\n', (repr(out), repr(err))\n",
        encoding="utf-8",
    )
    result = distributed.pytester.runpytest_subprocess(
        "--failure-instrumentation", "-n", "2", "-o", "failure_capture_output=true",
        "test_capsys.py", timeout=180,
    )
    result.assert_outcomes(passed=1)


@needs_xdist
def test_capture_off_by_default_keeps_no_file(distributed):
    distributed.pytester.makepyfile(
        test_crash="""
        import ctypes
        def test_filler():
            assert True
        def test_crashes():
            ctypes.CDLL(None).abort()
        """
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)
    death = distributed.only(incidents, "worker_death")
    assert death.recent_output == []
    assert not list((distributed.pytester.path / ".pytest-failures").glob("*/gw*.output"))


@posix_only
@needs_xdist
def test_capture_on_is_recorded_on_the_worker_log(distributed):
    distributed.pytester.makepyfile(test_suite="def test_one():\\n    assert True\\n")
    distributed.run("-n", "2", "-o", "failure_capture_output=true", "test_suite.py", timeout=180)
    statuses = [
        json.loads(line).get("status")
        for path in (distributed.pytester.path / ".pytest-failures").glob("*/gw*.events")
        for line in path.read_text().splitlines()
        if '"output_capture"' in line
    ]
    assert statuses and all(status == "on" for status in statuses), statuses


def test_the_tail_is_read_by_seeking_not_by_reading_the_file_in(tmp_path):
    """The ring is trimmed between phases and never during one, so a phase
    that logs heavily leaves a file of any size at all - and this runs on the
    controller, once per dead worker, to keep the last few KB of it.

    Reading the whole thing in to slice the end off puts a runaway test's
    entire output through the controller's memory at the moment it is already
    dealing with a death.
    """
    path = tmp_path / "gw0.output"
    with path.open("wb") as handle:
        for index in range(200_000):
            handle.write(b"line %d: %s\n" % (index, b"z" * 40))
    size = path.stat().st_size
    assert size > 8 * 1024 * 1024, "a file far larger than the tail being asked for"

    tail = read_tail(path, limit=4096)

    assert tail and tail[-1] == "line 199999: " + "z" * 40
    assert sum(len(line) + 1 for line in tail) <= 4096
    # The seek lands mid-line, and half a line read as a whole one is a line
    # the worker never wrote.
    assert all(line.startswith("line ") for line in tail)


def test_a_file_smaller_than_the_tail_keeps_its_first_line(tmp_path):
    path = tmp_path / "gw0.output"
    path.write_bytes(b"first\nsecond\n")
    assert read_tail(path, limit=4096) == ["first", "second"]


def test_a_worker_that_kept_nothing_reads_as_nothing(tmp_path):
    assert read_tail(tmp_path / "absent.output") == []
    (tmp_path / "empty.output").write_bytes(b"")
    assert read_tail(tmp_path / "empty.output") == []
