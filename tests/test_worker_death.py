"""A worker process that ended when it should not have.

Cross-platform by construction: an access violation is reachable everywhere
through ctypes, and the POSIX-only signals are marked as such rather than
skipped silently.
"""

from __future__ import annotations

import signal
import sys

import pytest

from .conftest import needs_xdist

pytestmark = needs_xdist

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def crashing_test(body: str) -> str:
    return f"""
        import ctypes
        import os
        import signal

        import victim


        def test_filler():
            assert True


        def test_crashes():
            {body}
        """


def test_a_native_crash_is_reported_with_the_status_the_platform_gives(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.native_call(1)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "NATIVE_CRASH"
    assert death.test_in_flight == "test_crash.py::test_crashes"
    assert death.phase == "call"
    assert death.run_ending is False

    if sys.platform != "win32":
        assert death.exit_status == -signal.SIGSEGV
    else:
        # abort() exits with 3, the same as a deliberate os._exit(3). What
        # separates them is the dump asserted below, and nothing else.
        assert death.exit_status == 3

    # The deepest frame is ctypes or the CRT; the useful one is whoever
    # called it.
    assert death.crash_stack
    assert death.blamed_frame is not None
    assert death.blamed_frame.module == "victim"
    assert death.owner == "product"
    assert death.severity == "critical"


def test_a_deliberate_exit_is_not_reported_as_a_crash(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.hard_exit(3)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "SELF_EXIT"
    assert death.exit_status == 3


@posix_only
def test_sigkill_does_not_claim_an_oom_it_cannot_prove(distributed):
    distributed.pytester.makepyfile(
        test_crash=crashing_test("os.kill(os.getpid(), signal.SIGKILL)")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    # OOM_KILLED needs the cgroup counter to have moved. Without it the honest
    # answer is that something killed it.
    assert death.verdict == "SIGKILLED"
    assert death.exit_status == -signal.SIGKILL
    assert any("no cgroup OOM event" in line for line in death.evidence)


@posix_only
def test_a_stop_signal_is_not_a_defect(distributed):
    distributed.pytester.makepyfile(
        test_crash=crashing_test("os.kill(os.getpid(), signal.SIGTERM)")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == f"SIGNAL_{int(signal.SIGTERM)}"
    # Nobody is paged for a run somebody asked to stop.
    assert death.severity == "informational"


@pytest.mark.skipif(sys.platform != "win32", reason="NTSTATUS is a Windows exit code")
def test_a_windows_ntstatus_is_decoded_as_the_fault_it_stands_for(distributed):
    """The decode table is unit-tested everywhere; this is the only place a
    real process actually exits with one of those codes."""
    distributed.pytester.makepyfile(
        test_crash=crashing_test("victim.exit_with_ntstatus()")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.exit_status == 0xC0000005
    assert death.verdict == "NATIVE_CRASH"
    assert "access violation" in death.exit_status_meaning
    # TerminateProcess leaves no dump, so there is no frame to blame at all.
    # What is left is the test that was in flight, offered as a lead: it names
    # whoever owns the test module, which is not the same claim as knowing
    # whose code failed.
    assert death.owner == "unknown"
    assert death.suspect_owner == "customer-code"
    assert "test_crash.py" in (death.suspect_basis or "")


def test_the_phase_is_recorded_because_pytest_cannot_tell_you(distributed):
    distributed.pytester.makepyfile(
        test_crash="""
        import victim


        def test_dies_in_teardown(request):
            request.addfinalizer(lambda: victim.native_call(1))
        """
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.phase == "teardown"
    assert death.test_in_flight == "test_crash.py::test_dies_in_teardown"


def test_the_capabilities_of_the_machine_are_recorded(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.native_call(1)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.capabilities is not None
    assert death.capabilities.system
    # A figure that is absent must be distinguishable from a healthy one.
    assert death.capabilities.resident_memory
    assert death.product_version == "1.2.3"
