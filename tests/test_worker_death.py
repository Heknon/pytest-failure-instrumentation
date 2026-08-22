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


def test_a_native_crash_is_attributed_to_the_package_that_made_the_call(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.native_call(1)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "NATIVE_CRASH"
    assert death.owner == "product"
    assert death.severity == "critical"
    # The deepest frame is ctypes.string_at; the useful one is the caller.
    assert death.blamed_frame is not None
    assert death.blamed_frame.module == "victim"
    assert death.test_in_flight == "test_crash.py::test_crashes"
    assert death.phase == "call"
    assert death.run_ending is False


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
