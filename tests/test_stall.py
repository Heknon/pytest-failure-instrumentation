"""A worker that stopped reporting but is still alive.

The waits are bounded so the inner run always terminates; the stall is detected
long before they expire.
"""

from __future__ import annotations

import sys

import pytest

from .conftest import needs_xdist

pytestmark = needs_xdist

# Detected around 8s in; the test releases at 25s so the run ends on its own.
# The slow-test watchdog is dropped to 5s as well, because that is the path
# that has to produce a stack on Windows - where no signal can ask for one.
STALL_ARGUMENTS = (
    "-o", "failure_stall_seconds=6",
    "-o", "failure_heartbeat_interval=2",
    "-o", "failure_slow_test_seconds=5",
)


def test_a_blocked_thread_is_named_along_with_what_it_is_waiting_on(distributed):
    distributed.pytester.makepyfile(
        test_hang="""
        import threading

        never_set = threading.Event()


        def test_filler():
            assert True


        def test_deadlocks():
            never_set.wait(25)
        """
    )
    incidents = distributed.run("-n", "2", *STALL_ARGUMENTS, "test_hang.py", timeout=180)

    stall = distributed.only(incidents, "worker_stall")
    assert stall.verdict == "STALLED_BLOCKED"
    assert stall.state == "BLOCKED"
    # Not exactly zero: the heartbeat thread waking every few seconds burns a
    # little itself, which is why the busy threshold is not zero either.
    assert stall.cpu_rate is not None and stall.cpu_rate < 0.05
    assert stall.test_in_flight == "test_hang.py::test_deadlocks"
    assert stall.run_ending is True
    # The blamed frame must be the blocked test, not this plugin's own
    # heartbeat thread, which is the first one in the dump.
    assert stall.stack
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "test_deadlocks"


def test_a_slow_test_is_not_reported_at_all(distributed):
    """The case that matters most: silence alone means nothing, and a
    twenty-minute test must not page anybody."""
    distributed.pytester.makepyfile(
        test_slow="""
        import time


        def test_filler():
            assert True


        def test_is_merely_slow():
            deadline = time.time() + 20
            total = 0
            while time.time() < deadline:
                total += sum(range(10000))
            assert total
        """
    )
    incidents = distributed.run("-n", "2", *STALL_ARGUMENTS, "test_slow.py", timeout=180)

    assert distributed.of_kind(incidents, "worker_stall") == []
    summary = distributed.only(incidents, "run_summary")
    assert summary.raised == 0


def test_native_code_holding_the_gil_is_frozen_not_blocked(distributed):
    """PyDLL keeps the GIL across the call, so the worker's own heartbeat
    thread cannot run - which is what native code does in the field."""
    distributed.pytester.makepyfile(
        test_frozen="""
        import ctypes
        import sys


        def test_filler():
            assert True


        def test_freezes():
            if sys.platform == "win32":
                ctypes.PyDLL("kernel32").Sleep(20000)
            else:
                ctypes.PyDLL(None).sleep(20)
        """
    )
    incidents = distributed.run("-n", "2", *STALL_ARGUMENTS, "test_frozen.py", timeout=180)

    stall = distributed.only(incidents, "worker_stall")
    assert stall.verdict == "STALLED_FROZEN"
    assert stall.heartbeat_age_seconds is not None
    assert any("holding the GIL" in line for line in stall.evidence)
    # The stack is still obtained even though the GIL is held: faulthandler
    # dumps from a C timer thread that does not need it.
    assert stall.stack
    # Windows can be asked for a stack only in advance, never on demand.
    assert stall.stack_probed is (sys.platform != "win32")


def test_a_stall_is_reported_once_not_every_poll(distributed):
    distributed.pytester.makepyfile(
        test_hang="""
        import threading

        never_set = threading.Event()


        def test_deadlocks():
            never_set.wait(25)
        """
    )
    incidents = distributed.run("-n", "2", *STALL_ARGUMENTS, "test_hang.py", timeout=180)
    assert len(distributed.of_kind(incidents, "worker_stall")) == 1
