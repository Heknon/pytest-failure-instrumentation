"""A bounded copy of what a worker printed, kept for a death that leaves no word.

The line that explains a native abort - ``pthread_create failed``, a malloc
abort - is written to stderr and captured by pytest, which keeps it only for a
completed phase and throws it away when the worker is killed. Copied into
``<worker>.output`` it reaches the incident. This reads pytest's own capture
rather than a file descriptor, so the tests that matter prove it survives a
kill and never disturbs the run.
"""

from __future__ import annotations

import json
import signal
import sys

import pytest

from pytest_failure_instrumentation.analysis import classify
from pytest_failure_instrumentation.capture.output import OutputLog, read_tail
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident

from .conftest import needs_xdist

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="native abort via ctypes is POSIX here")


def test_a_phase_is_persisted_the_moment_it_is_added(tmp_path):
    """On disk immediately: a SIGKILL gives no chance to flush."""
    log = OutputLog(tmp_path / "gw0.output")
    log.add(stderr="OpenBLAS blas_thread_init: pthread_create failed")
    # Read before close - the SIGKILL case.
    assert read_tail(tmp_path / "gw0.output") == ["OpenBLAS blas_thread_init: pthread_create failed"]
    log.close()


def test_the_ring_is_bounded_and_never_starts_mid_line(tmp_path):
    log = OutputLog(tmp_path / "gw0.output", limit=2048)
    for i in range(2000):
        log.add(stderr=f"line {i:06d} " + "x" * 40)
    log.close()
    data = (tmp_path / "gw0.output").read_bytes()
    assert len(data) <= 2048
    assert data.startswith(b"line "), data[:20]
    assert read_tail(tmp_path / "gw0.output")[-1].startswith("line 001999")


def test_stdout_is_kept_but_tagged_and_after_stderr(tmp_path):
    log = OutputLog(tmp_path / "gw0.output")
    log.add(stderr="the real reason", stdout="chatty progress\nmore chatter")
    lines = read_tail(tmp_path / "gw0.output")
    assert lines[0] == "the real reason"
    assert "[stdout] chatty progress" in lines and "[stdout] more chatter" in lines


def test_empty_capture_writes_nothing(tmp_path):
    log = OutputLog(tmp_path / "gw0.output")
    log.add(stderr="", stdout="")
    log.close()
    assert not (tmp_path / "gw0.output").exists()


def test_the_verdict_surfaces_the_last_stderr_line_on_a_crash():
    incident = WorkerDeathIncident(
        worker="gw1", exit_status=-signal.SIGABRT if hasattr(signal, "SIGABRT") else -6,
        test_in_flight="test_x.py::test_x", phase="call",
        recent_output=["loading libfoo", "OpenBLAS blas_thread_init: pthread_create failed"],
    )
    _v, _c, evidence = classify.of(incident)
    assert any("last stderr: OpenBLAS blas_thread_init: pthread_create failed" in line for line in evidence)


def test_an_absent_tail_is_not_read_as_silence():
    incident = WorkerDeathIncident(worker="gw1", exit_status=-6, test_in_flight="t.py::t", phase="call")
    _v, _c, evidence = classify.of(incident)
    assert not any(line.startswith("last stderr:") for line in evidence)


# -- for real ----------------------------------------------------------------


@posix_only
@needs_xdist
def test_a_native_message_from_a_completed_phase_reaches_a_crash(distributed):
    """A test whose setup prints a native line and whose body then crashes:
    the setup completed, so pytest captured it, so it is on the incident even
    though the crashing phase itself produced no report."""
    distributed.pytester.makepyfile(
        test_abort="""
        import ctypes, os, sys
        import pytest


        @pytest.fixture
        def noisy():
            os.write(2, b"libfoo: fatal: the widget pool is exhausted\\n")
            sys.stderr.flush()
            yield


        def test_filler():
            assert True


        def test_aborts(noisy):
            ctypes.CDLL(None).abort()
        """
    )
    incidents = distributed.run(
        "-n", "2", "-o", "failure_capture_output=true", "test_abort.py", timeout=180
    )
    death = distributed.only(incidents, "worker_death")
    assert death.recent_output, "capture was on and setup completed, so a tail is owed"
    assert any("widget pool is exhausted" in line for line in death.recent_output)
    assert any(line.startswith("last stderr:") for line in death.evidence)


@needs_xdist
def test_capture_on_does_not_disturb_a_healthy_run_or_pytests_own_output(distributed):
    """The run passes, pytest's captured-on-failure output is intact, and the
    setting is recorded as on."""
    distributed.pytester.makepyfile(
        test_suite="""
        import sys


        def test_prints_then_fails():
            print("visible stdout"); sys.stderr.write("visible stderr\\n")
            assert False


        def test_two():
            assert True
        """
    )
    result = distributed.pytester.runpytest_subprocess(
        "--failure-instrumentation", "-n", "2", "-o", "failure_capture_output=true",
        "test_suite.py", timeout=180,
    )
    result.assert_outcomes(passed=1, failed=1)
    # pytest's own captured output still reaches the report of the failing test.
    result.stdout.fnmatch_lines(["*visible stdout*", "*visible stderr*"])
    statuses = [
        json.loads(line).get("status")
        for path in (distributed.pytester.path / ".pytest-failures").glob("*/gw*.events")
        for line in path.read_text().splitlines()
        if '"output_capture"' in line
    ]
    assert statuses and all(status == "on" for status in statuses), statuses


@needs_xdist
def test_capture_off_by_default_keeps_no_output(distributed):
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
