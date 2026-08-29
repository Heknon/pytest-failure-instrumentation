"""The platform probes, called directly.

Most of these are shadowed in normal use: psutil answers before psapi does,
and execnet's Popen object answers before waitid does. Calling them directly is
the only way the fallback paths are executed at all - and the fallbacks are
precisely what a customer's machine will be running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pytest_failure_instrumentation import probes
from pytest_failure_instrumentation.probes import memory, process

# macOS does not expose os.waitid at all, so the plugin falls back to the
# Popen object there - which is why the capability record reports the
# mechanism rather than assuming one.
has_waitid = pytest.mark.skipif(
    not hasattr(os, "waitid"), reason="no os.waitid on this platform"
)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def child(*code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "; ".join(code)])


def wait_for(read, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if result is not None:
            return result
        time.sleep(0.05)
    return None


# -- what this machine can measure ---------------------------------------


def test_resident_memory_is_measurable_on_every_supported_platform():
    """A missing figure is reported as unmeasurable rather than as fine, so an
    always-unavailable probe would be honest and useless at the same time."""
    value, source = memory.resident_megabytes()
    assert value is not None, source
    assert value > 0
    assert source != "unavailable"


def test_capabilities_names_the_mechanism_rather_than_claiming_a_capability():
    capabilities = probes.capabilities()
    assert capabilities["system"] and capabilities["python"]
    assert capabilities["resident_memory"] != "unavailable"
    assert capabilities["exit_status"] in {"waitid", "windows", "popen-only"}


@windows_only
def test_psapi_answers_without_psutil():
    """The psapi fallback is what every Windows machine without psutil uses,
    and psutil answering first is what kept it from ever being exercised."""
    assert (memory._windows_working_set() or 0) > 0


# -- exit status ---------------------------------------------------------


@has_waitid
def test_waitid_reads_a_status_without_consuming_it():
    """The whole reason for WNOWAIT: whoever owns the process still gets to
    reap it, so looking at a worker cannot break execnet's own cleanup."""
    popen = child("import sys", "sys.exit(7)")
    status = wait_for(lambda: process._waitid_status(popen.pid, 1.0))
    assert status == (7, "exited", "waitid")
    # Still reapable afterwards, and with the same answer.
    assert popen.wait(timeout=10) == 7


@has_waitid
def test_waitid_reports_a_kill_as_a_negative_status():
    popen = child("import time", "time.sleep(30)")
    popen.send_signal(signal.SIGKILL)
    status = wait_for(lambda: process._waitid_status(popen.pid, 1.0))
    assert status == (-int(signal.SIGKILL), "killed", "waitid")
    assert popen.wait(timeout=10) == -int(signal.SIGKILL)


@windows_only
def test_getexitcodeprocess_answers_from_a_handle(monkeypatch):
    """Windows lets any handle you can open answer, which is what makes the
    status readable without being the parent. psutil answers first in normal
    use, so it is made to decline here to reach the call underneath."""

    def declines(pid):
        raise OSError("psutil is not answering for this test")

    monkeypatch.setattr(process, "psutil", SimpleNamespace(Process=declines))
    popen = child("import sys", "sys.exit(7)")
    popen.wait(timeout=10)
    assert process._windows_exit_status(popen.pid) == (7, "exited", "GetExitCodeProcess")


def test_the_windows_path_survives_psutil_declining_to_answer(monkeypatch):
    """Runs everywhere, on purpose. The test above only proves the fallback
    on Windows, so a rename of the psutil import went unnoticed here until a
    Windows runner reached it. This one pins the same patch point on every
    platform: off Windows the ctypes call cannot load, and the contract is
    that it yields nothing rather than raising or claiming psutil answered."""

    def declines(pid):
        raise OSError("psutil is not answering for this test")

    monkeypatch.setattr(process, "psutil", SimpleNamespace(Process=declines))
    status = process._windows_exit_status(os.getpid())
    assert status is None or status[2] == "GetExitCodeProcess"


def test_a_process_that_was_never_ours_yields_no_status_rather_than_a_guess():
    assert probes.exit_status(None, None) == (None, None, "unavailable")


@windows_only
def test_an_ntstatus_is_normalised_to_the_unsigned_form_it_is_documented_as():
    """0xC000013A arrives signed or unsigned depending on who answered, and a
    negative status means "killed by signal N" everywhere downstream."""
    assert process.unsigned_on_windows(-1073741510) == 0xC000013A
    assert process.unsigned_on_windows(-1073741819) == 0xC0000005
    assert process.unsigned_on_windows(3) == 3


def test_a_posix_signal_status_is_left_alone():
    if sys.platform == "win32":
        pytest.skip("negative statuses are signals only on POSIX")
    assert process.unsigned_on_windows(-9) == -9


# -- the package as a dependency -----------------------------------------


def test_the_package_ships_its_types():
    """PEP 561. The typed payload is the product, and without this marker a
    consumer writing `incident: WorkerDeathIncident` against registry.parse()
    gets Any - which is the opposite of what a discriminated union is for."""
    import pytest_failure_instrumentation

    root = Path(pytest_failure_instrumentation.__file__).parent
    assert (root / "py.typed").is_file()


# -- liveness, which is a different mechanism per platform ----------------


def test_windows_liveness_never_goes_through_os_kill(monkeypatch):
    """``os.kill(pid, 0)`` is a POSIX question and a Windows *action*.

    There, ``os.kill`` sends a console event for CTRL_C_EVENT and
    CTRL_BREAK_EVENT and calls TerminateProcess for every other value -
    including zero. A liveness check written the POSIX way would kill each
    worker it inspected, and the live view inspects every worker on every
    request. This test runs on POSIX too, because that is where the mistake
    gets written.
    """
    from pytest_failure_instrumentation.probes import process as process_probe

    killed = []
    asked = []

    class FakePsutil:
        """Stands in for psutil so that *its* POSIX implementation - which
        legitimately uses os.kill on this machine - cannot be mistaken for
        ours. On Windows psutil takes an entirely different path."""

        @staticmethod
        def pid_exists(pid):
            asked.append(pid)
            return True

    monkeypatch.setattr(process_probe, "IS_WINDOWS", True)
    monkeypatch.setattr(process_probe, "psutil", FakePsutil)
    monkeypatch.setattr(process_probe.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    process_probe.is_running(os.getpid())

    assert killed == [], "the Windows path reached os.kill, which terminates there"
    assert asked == [os.getpid()], "the Windows path asked nothing at all"


def test_windows_liveness_asks_psutil(monkeypatch):
    from pytest_failure_instrumentation.probes import process as process_probe

    monkeypatch.setattr(process_probe, "IS_WINDOWS", True)
    asked = []

    class FakePsutil:
        @staticmethod
        def pid_exists(pid):
            asked.append(pid)
            return False

    monkeypatch.setattr(process_probe, "psutil", FakePsutil)
    assert process_probe.is_running(4321) is False
    assert asked == [4321]


def test_liveness_errs_towards_alive_when_it_cannot_tell(monkeypatch):
    """A wrong "it died" deletes evidence and reports a working worker as
    gone. A wrong "still there" costs a stale row, so that is the way to be
    wrong when psutil itself refuses to answer."""
    from pytest_failure_instrumentation.probes import process as process_probe

    class Broken:
        @staticmethod
        def pid_exists(pid):
            raise RuntimeError("psutil is unhappy")

    monkeypatch.setattr(process_probe, "IS_WINDOWS", True)
    monkeypatch.setattr(process_probe, "psutil", Broken)
    assert process_probe.is_running(4321) is True


def test_a_permission_error_means_the_process_exists(monkeypatch):
    """EPERM is the kernel saying there is something there that is not ours to
    signal, which is an answer rather than a failure."""
    from pytest_failure_instrumentation.probes import process as process_probe

    monkeypatch.setattr(process_probe, "IS_WINDOWS", False)

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(process_probe.os, "kill", denied)
    assert process_probe.is_running(4321) is True


# -- letting the reader read us -------------------------------------------


def test_asking_to_be_traceable_never_raises_and_answers_a_bool():
    """The call is a no-op wherever it is not needed - a kernel without Yama
    answers EINVAL, and every non-Linux platform never makes it. What must not
    happen is an exception on a worker's startup path, which would cost the run
    rather than a stack."""
    from pytest_failure_instrumentation.probes import tracing

    granted = tracing.permit_tracing("parent")
    assert isinstance(granted, bool)
    if sys.platform != "linux":
        assert granted is False, "nothing outside Linux has a tracer exception to grant"


def test_the_prctl_option_is_yamas_own_number():
    """0x59616d61 spells "Yama". Written down rather than imported because
    Python exposes no constant for it, and a wrong number here would fail
    silently - prctl returns EINVAL and the worker simply stays unreadable."""
    from pytest_failure_instrumentation.probes import tracing

    assert tracing.PR_SET_PTRACER == 0x59616D61
    assert bytes.fromhex(f"{tracing.PR_SET_PTRACER:x}") == b"Yama"
# -- whose process is that pid ---------------------------------------------
#
# is_own_child errs the opposite way from is_running above, and the tests hold
# that: a wrong "yes" signals a stranger's process, a wrong "no" only costs a
# stalled worker its stack. psutil is a hard dependency here, so there is no
# machine that cannot answer and no third value to handle.


def test_a_live_child_is_recognised_as_one():
    """What licenses the stack probe. SIGUSR1's default disposition is to
    terminate, and the pid it is aimed at was read back out of a file - so the
    question "is that still our worker" is the difference between a bad report
    and somebody else's process being killed."""
    process_handle = child("import time", "time.sleep(30)")
    try:
        assert probes.is_own_child(process_handle.pid) is True
    finally:
        process_handle.kill()
        process_handle.wait()


def test_a_process_that_is_not_ours_is_not_mistaken_for_a_worker():
    """This process is alive and is certainly not its own child.

    Not pid 1, which is init on POSIX and *nothing at all* on Windows - there
    the ids start at 0 for Idle and 4 for System, so a test written around pid
    1 asks about a process that does not exist and passes for the wrong
    reason.
    """
    assert probes.is_own_child(os.getpid()) is False


def test_a_pid_that_cannot_exist_is_not_ours():
    assert probes.is_own_child(0) is False
    assert probes.is_own_child(-1) is False


def test_a_child_that_has_gone_is_no_longer_ours():
    """The case the whole check exists for: the pid outlives the process, and
    the number is handed on to somebody else."""
    process_handle = child("import sys", "sys.exit(0)")
    process_handle.wait()  # reaped, so this asks about a pid with no process
    assert probes.is_own_child(process_handle.pid) is False


def test_the_two_liveness_questions_disagree_about_a_stranger():
    """is_running says this process is there; is_own_child says it is not ours.

    Both are right, and the live view depends on them being different: one
    decides whether to keep reporting a worker, the other whether it is safe to
    signal it. Asked about *this* process rather than pid 1, which does not
    exist on Windows - is_running answered False there and the test failed
    having proved nothing about either question.
    """
    assert probes.is_running(os.getpid()) is True
    assert probes.is_own_child(os.getpid()) is False
