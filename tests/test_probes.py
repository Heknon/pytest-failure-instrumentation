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
    use, so it is removed here to reach the call underneath."""
    monkeypatch.setattr(process, "optional_psutil", lambda: None)
    popen = child("import sys", "sys.exit(7)")
    popen.wait(timeout=10)
    assert process._windows_exit_status(popen.pid) == (7, "exited", "GetExitCodeProcess")


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
