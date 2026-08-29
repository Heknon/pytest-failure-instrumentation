"""A worker that stopped reporting but is still alive.

The waits are bounded so the inner run always terminates; the stall is detected
long before they expire.
"""

from __future__ import annotations

import sys

from .conftest import needs_xdist

pytestmark = needs_xdist

# Detected around 13s in; the tests release at 25s so the run ends on its own.
# The slow-test watchdog is dropped to 4s as well, because that is the path
# that has to produce a stack on Windows - where no signal can ask for one.
#
# The margin between the two is what these numbers are really about, and it
# used to be one second. The watchdog measures from when the *worker* started
# the test and the stall from when the *controller* last heard anything, and
# on a loaded runner the gap between those is seconds - so a stall assessed at
# six read a file the watchdog had not written yet, and the tests below saw an
# empty stack where the point was that there is one.
STALL_ARGUMENTS = (
    "-o", "failure_stall_seconds=10",
    "-o", "failure_heartbeat_interval=2",
    "-o", "failure_slow_test_seconds=4",
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


def test_a_worker_that_stopped_running_python_still_leaves_a_stack(distributed):
    """The one stack nothing else in this plugin can take.

    Native code holding the GIL means no Python thread runs, so the slow-test
    watchdog - which is a Python thread - writes nothing. On POSIX the
    controller can still signal for one; on Windows it cannot, and this is the
    only path there is. The probe is switched off here so that path is what is
    being measured, on every platform rather than only the one that needs it.
    """
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
    incidents = distributed.run(
        "-n", "2",
        *STALL_ARGUMENTS,
        "-o", "failure_stack_probe=false",
        "test_frozen.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.verdict == "STALLED_FROZEN"
    assert stall.stack, "a worker frozen inside native code left no stack"
    assert stall.stack_source == "frozen-fallback"
    # Which is worth saying out loud: the frames look exactly like a watchdog
    # dump, and they mean something much stronger.
    assert any("stopped running Python" in line for line in stall.details())
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "test_freezes"


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


def test_a_stall_with_the_watchdog_off_says_so_rather_than_guessing(distributed):
    """No heartbeat was ever written, so there is no passive evidence either
    way. The worker is silent and that is the whole finding - it must not be
    dressed up as a diagnosis, so the confidence says low."""
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
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_watchdog=false",
        "-o", "failure_stall_seconds=6",
        "test_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.verdict == "STALLED_SILENT"
    assert stall.state == "SILENT"
    assert stall.confidence == "low"
    # Nothing was measured, so nothing is reported as measured.
    assert stall.cpu_rate is None
    assert stall.heartbeat_age_seconds is None
    assert any("never wrote a heartbeat" in line for line in stall.evidence)
    # The state file is written regardless of the watchdog, so the test in
    # flight is still known.
    assert stall.test_in_flight == "test_hang.py::test_deadlocks"


def test_the_stack_reason_names_the_setting_not_the_platform(distributed):
    """Three different reasons produced one sentence.

    "this platform cannot ask a live process for one" is true on Windows and
    false everywhere else, and it was printed just as readily to somebody who
    had turned the probe off themselves.
    """
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
    incidents = distributed.run(
        "-n", "2", *STALL_ARGUMENTS,
        "-o", "failure_stack_probe=false",
        "test_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.stack_probed is False
    assert stall.stack_unavailable_reason is not None
    assert "failure_stack_probe is off" in stall.stack_unavailable_reason
    assert "platform" not in stall.stack_unavailable_reason
    # The watchdog dump is still there to read, so a stack is reported anyway
    # and the reason never has to be printed.
    assert stall.stack
    assert "no stack:" not in str(stall)


def test_a_sub_second_heartbeat_setting_does_not_manufacture_a_frozen_worker(distributed):
    """The worker clamped the interval to a floor and the controller did not,
    so below the floor the controller's staleness window was shorter than the
    worker's actual cadence - and a plainly blocked worker confirmed as FROZEN,
    reported as native code holding the GIL."""
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
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_stall_seconds=6",
        "-o", "failure_heartbeat_interval=0.05",
        "-o", "failure_slow_test_seconds=5",
        "test_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.verdict == "STALLED_BLOCKED"
    assert not any("holding the GIL" in line for line in stall.evidence)


def test_the_test_that_is_wedged_now_is_blamed_and_not_an_earlier_slow_one(distributed):
    """The watchdog repeats, so a wedged worker's file fills with dumps.

    Reading the file from the top returned the *first* one - the stack of a
    test that was merely slow, finished, and passed. The incident then named
    the wedged test in test_in_flight and blamed a different, passing test in
    the field a reader takes for the finding.
    """
    distributed.pytester.makepyfile(
        test_two_slow="""
        import threading
        import time

        never_set = threading.Event()


        def test_aaa_slow_but_passes():
            time.sleep(4)


        def test_zzz_wedges():
            never_set.wait(90)
        """
    )
    incidents = distributed.run(
        "-n", "1",
        # One worker, so both tests write to the same dump file - and no
        # replacement, or the fresh worker wedges in its turn and a second
        # stall is correct but not what is being measured.
        "--max-worker-restart=0",
        "-o", "failure_slow_test_seconds=2",
        # Comfortably past anything a cold runner can spend getting to the
        # first report: at eight seconds - and again at twenty - a slow macOS
        # worker was still in the *first* test when the stall was assessed, and
        # the assertion below then read as the bug it exists to catch. The
        # wedge lasts ninety seconds, so the margin is free to be this wide.
        "-o", "failure_stall_seconds=45",
        "-o", "failure_heartbeat_interval=2",
        "-o", "failure_stack_probe=false",
        "test_two_slow.py",
        timeout=240,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.test_in_flight == "test_two_slow.py::test_zzz_wedges"
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "test_zzz_wedges"
    assert not any("test_aaa_slow_but_passes" in line for line in stall.stack)


def test_a_stack_nobody_asked_for_says_when_it_was_taken(distributed):
    """A probed stack is current; a watchdog dump can be most of
    failure_slow_test_seconds old. The frames look identical either way, so
    the report has to be the thing that distinguishes them."""
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
    incidents = distributed.run(
        "-n", "2", *STALL_ARGUMENTS,
        "-o", "failure_stack_probe=false",
        "test_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.stack and stall.stack_probed is False
    assert stall.stack_age_seconds is not None
    assert "by the slow-test watchdog, not taken just now" in str(stall)


def test_a_worker_wedged_longer_than_the_cadence_has_a_stack(distributed):
    """The old 120s default left nothing at all for a shorter wedge.

    The cadence is a staleness bound, not just a "this test is slow" threshold:
    on Windows the watchdog is the only stack a stalled worker will ever have,
    so a default that never fires means no stack for the whole class of hangs
    that resolve, or get killed, inside two minutes.
    """
    distributed.pytester.makepyfile(
        test_hang="""
        import threading

        never_set = threading.Event()


        def test_filler():
            assert True


        def test_deadlocks():
            never_set.wait(40)
        """
    )
    incidents = distributed.run(
        "-n", "2",
        # The cadence has to be below the stall threshold or the worker is
        # judged before the watchdog has written anything - see
        # test_the_shipped_defaults_leave_a_stall_something_to_read.
        "-o", "failure_slow_test_seconds=4",
        # Twelve was inside what a loaded macOS runner spends getting to the
        # first report, so the stall was assessed against a worker that had not
        # reached the hang yet. Twenty still leaves twenty-odd seconds of hang
        # to be caught in.
        "-o", "failure_stall_seconds=20",
        "-o", "failure_heartbeat_interval=2",
        "-o", "failure_stack_probe=false",
        "test_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.stack, "the cadence left no stack for a wedge outlasting it"
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "test_deadlocks"
    # And it is recent, because the cadence keeps refreshing it.
    assert stall.stack_age_seconds is not None
    assert stall.stack_age_seconds <= 10


def test_a_fixture_that_blocks_in_setup_is_named(distributed):
    """A fixture blocking on a container, a connection or a service is one of
    the commonest real hangs there is, and the watchdog used to cover only the
    call phase - so this class of hang produced no stack at all."""
    distributed.pytester.makepyfile(
        test_setup_hang="""
        import threading

        import pytest

        never_set = threading.Event()


        @pytest.fixture
        def slow_container():
            never_set.wait(40)
            yield


        def test_filler():
            assert True


        def test_needs_container(slow_container):
            assert True
        """
    )
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_slow_test_seconds=4",
        # Twelve was inside what a loaded macOS runner spends getting to the
        # first report, so the stall was assessed against a worker that had not
        # reached the hang yet. Twenty still leaves twenty-odd seconds of hang
        # to be caught in.
        "-o", "failure_stall_seconds=20",
        "-o", "failure_heartbeat_interval=2",
        "-o", "failure_stack_probe=false",
        "test_setup_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.phase == "setup"
    assert stall.stack, "a hang in setup left no stack"
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "slow_container"


def test_a_finalizer_that_blocks_in_teardown_is_named(distributed):
    """The state slot has always told "died in teardown" from "died mid-call",
    so a stack that stops at the call was the odd one out."""
    distributed.pytester.makepyfile(
        test_teardown_hang="""
        import threading

        import pytest

        never_set = threading.Event()


        @pytest.fixture
        def leaky_client():
            yield
            never_set.wait(45)


        def test_filler():
            assert True


        def test_uses_client(leaky_client):
            assert True
        """
    )
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_slow_test_seconds=4",
        # Twelve was inside what a loaded macOS runner spends getting to the
        # first report, so the stall was assessed against a worker that had not
        # reached the hang yet. Twenty still leaves twenty-odd seconds of hang
        # to be caught in.
        "-o", "failure_stall_seconds=20",
        "-o", "failure_heartbeat_interval=2",
        "-o", "failure_stack_probe=false",
        "test_teardown_hang.py",
        timeout=180,
    )

    stall = distributed.only(incidents, "worker_stall")
    assert stall.phase == "teardown"
    assert stall.stack, "a hang in teardown left no stack"
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "leaky_client"


def test_a_crashed_worker_is_not_also_reported_as_stalled(distributed):
    """A corpse is not a stalled process, and it used to be reported as one.

    When a worker crashes, xdist writes up the test it abandoned as a failure
    and attributes that report to the *dead* node - so the last report a worker
    ever produces arrives after ``pytest_testnodedown`` has said it is gone.
    Read as a sign of life it put the dead worker back among those being
    watched, where nothing could remove it again, and one
    ``failure_stall_seconds`` later it was reported a second time as
    STALLED_FROZEN: "the process is stopped", which was true, useless, and
    counted as another run-ending incident against the run.

    The other workers here burn CPU rather than sleeping, so the run outlasts
    the stall threshold several times over without any of them being a stall in
    its own right.
    """
    busy = """

def test_busy_{index}():
    deadline = time.time() + 8
    total = 0
    while time.time() < deadline:
        total += sum(range(10000))
    assert total
"""
    distributed.pytester.makepyfile(
        test_crash_then_work="import time\n\nimport victim\n\n\n"
        "def test_dies_early():\n    victim.hard_exit(9)\n"
        + "".join(busy.format(index=index) for index in range(4))
    )
    incidents = distributed.run(
        "-n", "2", *STALL_ARGUMENTS, "test_crash_then_work.py", timeout=180
    )

    death = distributed.only(incidents, "worker_death")
    assert death.exit_status == 9
    assert distributed.of_kind(incidents, "worker_stall") == []
    summary = distributed.only(incidents, "run_summary")
    assert summary.run_ending_incidents == 0
