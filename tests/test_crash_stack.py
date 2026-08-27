"""Picking one thread out of a dump that holds all of them.

The first section printed in a pytest worker is this plugin's own heartbeat
thread, so reporting it would blame the instrumentation for the failure it came
to explain. These are real dump shapes, written to a file the way a worker
writes them.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation.capture import crash_stack

HEARTBEAT_SECTION = """\
Thread 0x00007fb0d66146c0 (most recent call first):
  File "/usr/lib/python3.11/threading.py", line 331 in wait
  File "/srv/pytest_failure_instrumentation/capture/heartbeat.py", line 66 in _run
  File "/usr/lib/python3.11/threading.py", line 1002 in _bootstrap
"""

RECEIVER_SECTION = """\
Thread 0x00007fb0d81ff6c0 (most recent call first):
  File "/srv/site-packages/execnet/gateway_base.py", line 534 in read
  File "/srv/site-packages/execnet/gateway_base.py", line 1160 in _thread_receiver
"""

TEST_SECTION_BODY = """\
  File "/usr/lib/python3.11/threading.py", line 327 in wait
  File "/home/someone/suite/test_api.py", line 14 in test_deadlocks
  File "/srv/site-packages/_pytest/runner.py", line 139 in runtestprotocol
"""


def write(pytester, text):
    path = pytester.path / "worker.crash"
    path.write_text(text, encoding="utf-8")
    return path


def functions(lines):
    return [line.rsplit(" in ", 1)[-1] for line in lines if " in " in line]


def test_the_signalled_thread_wins(pytester):
    """A fatal fault and an on-demand SIGUSR1 both label their thread."""
    path = write(
        pytester,
        HEARTBEAT_SECTION
        + "\n"
        + RECEIVER_SECTION
        + "\nCurrent thread 0x00007fb0d8f0f080 (most recent call first):\n"
        + TEST_SECTION_BODY,
    )
    lines = crash_stack.read(path, limit=20)
    assert "test_deadlocks" in functions(lines)
    assert "_run" not in functions(lines)


def test_without_a_current_thread_the_test_thread_is_found_by_its_frames(pytester):
    """dump_traceback_later labels nothing - it dumps from a C timer thread -
    and prints the main thread last."""
    path = write(
        pytester,
        "Timeout (0:02:00)!\n"
        + HEARTBEAT_SECTION
        + "\n"
        + RECEIVER_SECTION
        + "\nThread 0x00007fb0d8f0f080 (most recent call first):\n"
        + TEST_SECTION_BODY,
    )
    lines = crash_stack.read(path, limit=20)
    assert lines[0].startswith("Timeout (")
    assert "test_deadlocks" in functions(lines)
    assert "_run" not in functions(lines)


def test_our_own_thread_is_the_last_resort_not_the_first(pytester):
    path = write(pytester, HEARTBEAT_SECTION + "\n" + RECEIVER_SECTION)
    lines = crash_stack.read(path, limit=20)
    assert "_thread_receiver" in functions(lines)


def test_a_dump_of_only_our_thread_is_still_returned(pytester):
    # Better to report something honest than nothing at all.
    path = write(pytester, HEARTBEAT_SECTION)
    assert crash_stack.read(path, limit=20)


def test_a_fatal_banner_is_kept(pytester):
    path = write(
        pytester,
        "Fatal Python error: Segmentation fault\n\n"
        "Current thread 0x00007fb0d8f0f080 (most recent call first):\n"
        + TEST_SECTION_BODY,
    )
    lines = crash_stack.read(path, limit=20)
    assert lines[0] == "Fatal Python error: Segmentation fault"


def test_offset_reads_only_what_was_added(pytester):
    path = write(pytester, HEARTBEAT_SECTION)
    before = path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\nCurrent thread 0x00007fb0d8f0f080 (most recent call first):\n"
            + TEST_SECTION_BODY
        )
    lines = crash_stack.read(path, limit=20, offset=before)
    assert "test_deadlocks" in functions(lines)


def test_a_missing_file_is_not_an_error(pytester):
    assert crash_stack.read(pytester.path / "nothing.crash") == []


def test_a_fatal_banner_is_what_marks_a_dump_as_a_death():
    """The two dumps are the same shape. On the Windows path a dump is the only
    thing separating abort() from a deliberate os._exit(3), so the banner is
    the whole distinction and not a caption on it."""
    assert crash_stack.is_fatal(["Fatal Python error: Segmentation fault", "  File..."])
    assert not crash_stack.is_fatal(["Timeout (0:00:02)!", "  File..."])
    # An on-demand SIGUSR1 dump has no banner at all, and it is not a death.
    assert not crash_stack.is_fatal(["Thread 0x00007f00 (most recent call first):"])
    assert not crash_stack.is_fatal([])


# -- which dump, when a file holds several --------------------------------

TWO_DUMPS = """Timeout (0:00:02)!
Thread 0x00007f00 (most recent call first):
  File "/app/test_a.py", line 5 in test_that_was_slow_and_passed

Timeout (0:00:02)!
Thread 0x00007f00 (most recent call first):
  File "/app/test_a.py", line 9 in test_that_is_wedged_now
"""


def test_the_latest_dump_is_the_one_that_describes_now(tmp_path):
    """dump_traceback_later(repeat=True) writes one dump per timeout for as
    long as a test runs, so a worker wedged for a minute leaves dozens.

    Reading from the top returns the oldest - the stack of whatever was slow
    *first*, which may be a test that has since finished and passed. Every
    fresher dump of the test actually stuck sits in the same file, unread.
    """
    path = tmp_path / "gw0.slow"
    path.write_text(TWO_DUMPS, encoding="utf-8")

    lines = crash_stack.read(path)
    assert any("test_that_is_wedged_now" in line for line in lines)
    assert not any("test_that_was_slow_and_passed" in line for line in lines)


def test_a_fatal_dump_wins_over_an_earlier_probe_in_the_same_file(tmp_path):
    """An on-demand stack taken while a worker was merely stalled is written
    to the crash file too, and it comes first. The dump that describes the
    death is the last one."""
    path = tmp_path / "gw0.crash"
    path.write_text(
        "Current thread 0x00007f00 (most recent call first):\n"
        '  File "/app/lib.py", line 3 in waiting_around\n'
        "\n"
        "Fatal Python error: Segmentation fault\n"
        "\n"
        "Current thread 0x00007f00 (most recent call first):\n"
        '  File "/app/lib.py", line 9 in the_frame_that_faulted\n',
        encoding="utf-8",
    )

    lines = crash_stack.read(path)
    assert lines[0].startswith("Fatal Python error")
    assert any("the_frame_that_faulted" in line for line in lines)
    assert not any("waiting_around" in line for line in lines)


def test_a_single_dump_is_unaffected(tmp_path):
    path = tmp_path / "gw0.slow"
    path.write_text(
        "Timeout (0:00:02)!\n"
        "Thread 0x00007f00 (most recent call first):\n"
        '  File "/app/test_a.py", line 5 in only_one\n',
        encoding="utf-8",
    )
    assert any("only_one" in line for line in crash_stack.read(path))


def test_when_a_dump_was_written_is_recoverable(tmp_path):
    """A stack is evidence about a moment. Without this the reader cannot tell
    one taken just now from one the watchdog left behind minutes ago."""
    path = tmp_path / "gw0.slow"
    path.write_text(TWO_DUMPS, encoding="utf-8")

    written = crash_stack.written_at(path)
    assert written is not None and time.time() - written < 60
    assert crash_stack.written_at(tmp_path / "nothing-here.slow") is None


# -- the watchdog's file, at rest and in flight ----------------------------


def test_a_test_that_outlives_the_timeout_has_its_stack_written(tmp_path):
    """The watchdog does not run itself: the heartbeat ticks it.

    A tick before the timeout writes nothing, and one after it writes a dump
    headed the way faulthandler heads its own.
    """
    path = tmp_path / "gw0.slow"
    watchdog = crash_stack.SlowTestWatchdog(path, timeout=0.2)

    watchdog.start_test()
    watchdog.tick()
    assert not path.exists(), "a test inside the timeout was dumped anyway"

    time.sleep(0.25)
    watchdog.tick()
    lines = crash_stack.read(path, limit=40)
    assert lines and lines[0].startswith("Timeout (")
    assert any("test_a_test_that_outlives_the_timeout" in line for line in lines)


def test_the_dump_is_dropped_when_the_test_that_left_it_ends(tmp_path):
    """Only the running test's stack is ever read, and a stack belonging to a
    test that finished is one somebody will read anyway."""
    path = tmp_path / "gw0.slow"
    watchdog = crash_stack.SlowTestWatchdog(path, timeout=0.05)

    watchdog.start_test()
    time.sleep(0.1)
    watchdog.tick()
    assert path.exists()

    watchdog.end_test()
    assert not path.exists()
    assert not list(tmp_path.glob("*.part")), "a half-written dump was left behind"


def test_the_cadence_is_per_test_and_not_per_tick(tmp_path, monkeypatch):
    """The heartbeat ticks far more often than the timeout, and each tick is a
    dump of every thread in the process. Writing one per tick would turn a
    twenty-second cadence into a five-second one nobody asked for.

    Counted rather than timed. This compared the file's ``st_mtime_ns`` before
    and after, which asks the filesystem a question NTFS answers imprecisely:
    its last-write time has ~15.6 ms granularity and is updated lazily, so two
    stats around a single write could differ by a tick of the system clock and
    the test failed on Windows having found no second dump at all. What it
    means to assert is that the cadence *decided* not to dump, so that is what
    is asserted.
    """
    path = tmp_path / "gw0.slow"
    watchdog = crash_stack.SlowTestWatchdog(path, timeout=0.2)

    dumps = []
    real_dump = watchdog._dump
    monkeypatch.setattr(
        watchdog, "_dump", lambda: (dumps.append(time.monotonic()), real_dump())[1]
    )

    watchdog.start_test()
    time.sleep(0.25)
    watchdog.tick()
    assert len(dumps) == 1, "the first tick past the timeout wrote nothing"
    assert path.exists(), "the dump named a file it did not write"

    for _ in range(5):
        watchdog.tick()
    assert len(dumps) == 1, "a tick inside the cadence re-dumped"


def test_a_reader_never_sees_half_a_dump(tmp_path):
    """The controller reads this file while the worker is writing it, and
    faulthandler prints one thread at a time. Half a dump holds the threads it
    had reached and not the one running the test, so the report names whichever
    was printed first - this plugin's own heartbeat. The file is therefore
    written beside itself and renamed into place."""
    path = tmp_path / "gw0.slow"
    watchdog = crash_stack.SlowTestWatchdog(path, timeout=0)
    watchdog.enabled = True  # a zero timeout is off; this test wants the write
    watchdog._dump()

    text = path.read_text(encoding="utf-8")
    assert text.startswith("Timeout (")
    assert text.rstrip().endswith(")") or "File " in text
    # Renamed rather than written in place, so the previous dump stands until
    # the new one is whole.
    assert not list(tmp_path.glob("*.part"))


def test_a_suite_of_fast_tests_never_pays_for_the_reset(tmp_path):
    """Nothing was written, so there is nothing to drop."""
    path = tmp_path / "gw0.slow"
    watchdog = crash_stack.SlowTestWatchdog(path, timeout=0)
    watchdog.start_test()
    watchdog.tick()
    watchdog.end_test()
    assert not path.exists()


def test_the_dumping_thread_is_not_reported_as_the_stalled_one(tmp_path):
    """faulthandler labels whoever asked for the dump "Current thread", and the
    thread that asks is this plugin's heartbeat.

    Believing that label reported the heartbeat as the frozen test - on macOS
    down to naming a psutil frame as the blamed function, in a run where the
    thing that was actually wedged was a fixture finalizer.
    """
    path = tmp_path / "gw0.slow"
    path.write_text(
        "Timeout (0:00:20)!\n"
        "Current thread 0x00000001 (most recent call first):\n"
        '  File "/x/pytest_failure_instrumentation/capture/heartbeat.py", line 66 in _run\n'
        '  File "/usr/lib/python3.11/threading.py", line 982 in run\n'
        "\n"
        "Thread 0x00000002 (most recent call first):\n"
        '  File "/app/conftest.py", line 9 in leaky_client\n'
        '  File "/x/_pytest/runner.py", line 113 in pytest_runtest_protocol\n',
        encoding="utf-8",
    )

    lines = crash_stack.read(path, limit=20)
    assert any("leaky_client" in line for line in lines)
    assert not any("heartbeat.py" in line for line in lines)


def _settled(path, quiet: float = 0.3, timeout: float = 10.0) -> str:
    """The dump's contents, once it has stopped being written to.

    A dump has no marker for where it ends, so a read taken while the C timer
    is still writing returns part of one - and part of a dump differs from the
    whole of it, which is indistinguishable from a second dump having been
    appended. That is what it looked like on a loaded runner: the comparison
    below failed with frames *added* to a continuing stack rather than with a
    second "Timeout (" banner, which is the shape a real repeat would have.

    So the file is left to settle first. This weakens nothing: the assertion is
    that no *further* dump arrives, and it can only be made against a dump that
    has finished arriving.
    """
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        size = path.stat().st_size
        if size and size == last:
            break
        last = size
        time.sleep(quiet)
    return path.read_text(encoding="utf-8")


def test_the_fallback_timer_never_fires_while_the_heartbeat_is_beating(tmp_path):
    """This is the whole safety argument, so it is asserted rather than argued.

    faulthandler's C timer dumps without the GIL and can segfault a worker
    that is executing Python when it fires. It is armed so that it cannot:
    every beat pushes the deadline out, so while anything Python runs at all
    the deadline is always in the future.

    The interval is a half second rather than a twentieth because what the
    first half asserts - that nothing fires while the beats arrive - is
    measured against a wall clock, and at 0.05 the deadline it had to beat
    was 0.15s. That is inside the ordinary scheduling noise of a loaded
    runner: a single hiccup between two ticks fires the timer, which is what
    happened on a macOS cell, and a run that reaches here through the release
    is deciding whether to publish. A gap of a second and a half is not
    scheduling noise. Nothing about the property moves - the beats still
    arrive many times faster than the deadline they push, as they do in a
    real worker - only the size of the stall it takes to break the test.
    """
    path = tmp_path / "gw0.frozen"
    with path.open("w", buffering=1, encoding="utf-8") as stream:
        fallback = crash_stack.FrozenInterpreterFallback(stream, interval=0.5)
        assert fallback.timeout == pytest.approx(1.5)

        deadline = time.time() + 2.0
        while time.time() < deadline:
            fallback.tick()
            time.sleep(0.1)
        assert path.stat().st_size == 0, "the timer fired while beats were arriving"

        # And when the beats stop, it fires - once, which is the whole answer
        # for a process that is no longer changing.
        # Past the deadline, so it has certainly fired - and then read only
        # once it has stopped being written to, since a partial dump differs
        # from the whole one in exactly the way a second dump would.
        time.sleep(2.5)
        first = _settled(path)
        assert first.startswith("Timeout (")
        time.sleep(2.0)
        assert path.read_text(encoding="utf-8") == first, "the timer repeated"

        fallback.stop()


def test_the_fallback_is_disarmed_before_the_thread_that_holds_it_back(tmp_path):
    """Session teardown is Python running flat out with nothing left to push
    the deadline forward - the one window the arming exists to stay out of."""
    path = tmp_path / "gw0.frozen"
    with path.open("w", buffering=1, encoding="utf-8") as stream:
        fallback = crash_stack.FrozenInterpreterFallback(stream, interval=0.05)
        fallback.tick()
        fallback.stop()
        time.sleep(0.4)
        assert path.stat().st_size == 0


def test_a_worker_with_the_watchdog_off_arms_nothing(tmp_path):
    path = tmp_path / "gw0.frozen"
    with path.open("w", buffering=1, encoding="utf-8") as stream:
        fallback = crash_stack.FrozenInterpreterFallback(stream, interval=0)
        assert not fallback.enabled
        fallback.tick()
        time.sleep(0.3)
        assert path.stat().st_size == 0


# -- which phases the watchdog covers --------------------------------------


class RecordingWatchdog:
    """Stands in for the real one, so the phases can be asserted directly."""

    def __init__(self):
        self.calls = []

    def start_test(self):
        self.calls.append("arm")

    def end_test(self):
        self.calls.append("cancel")


def drive(recorder, phase):
    """Run one phase hookwrapper end to end."""
    step = recorder._phase(phase, "test_x.py::test_y")
    next(step)
    list(step)


def test_the_watchdog_covers_the_whole_test_and_is_armed_once(tmp_path):
    """Started at setup and stopped at teardown.

    Not per phase: the clock is what the timeout is measured against, so
    restarting it at each phase would mean a test that spent most of the
    interval in setup and the rest in the call never reached it - the clock
    would keep starting over and no stack would ever be written.
    """
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    from pytest_failure_instrumentation.config import Settings

    recorder = WorkerRecorder(
        tmp_path, "gw0", Settings(watchdog=False, slow_test_seconds=5)
    )
    watchdog = RecordingWatchdog()
    recorder.slow_test = watchdog
    try:
        for phase in ("setup", "call", "teardown"):
            drive(recorder, phase)
    finally:
        recorder.close()

    assert watchdog.calls == ["arm", "cancel"]


def test_a_test_that_never_reaches_teardown_still_re_arms_next_time(tmp_path):
    """A missed stop self-heals: the next setup restarts the clock rather than
    leaving the previous test's running."""
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    from pytest_failure_instrumentation.config import Settings

    recorder = WorkerRecorder(
        tmp_path, "gw0", Settings(watchdog=False, slow_test_seconds=5)
    )
    watchdog = RecordingWatchdog()
    recorder.slow_test = watchdog
    try:
        drive(recorder, "setup")   # a test that dies before teardown
        drive(recorder, "setup")   # the next one
    finally:
        recorder.close()

    assert watchdog.calls == ["arm", "arm"]


def test_a_cut_stack_says_it_was_cut(tmp_path):
    """The deepest frames are the ones kept, so what a cap removes is the
    outer half - the part that says who called it. Cut without a word, the
    remainder reads as the whole story and nobody goes looking for the rest.
    """
    path = tmp_path / "gw0.crash"
    path.write_text(
        "Fatal Python error: Segmentation fault\n"
        "Current thread 0x00007f00 (most recent call first):\n"
        + "".join(f'  File "/app/a.py", line {n} in frame_{n}\n' for n in range(50)),
        encoding="utf-8",
    )

    lines = crash_stack.read(path, limit=10)
    assert lines[-1] == "... and 41 more frames"
    # The banner still leads, so is_fatal and the verdict are unaffected.
    assert crash_stack.is_fatal(lines)

    # And nothing is added when nothing was dropped.
    assert not any("more frames" in line for line in crash_stack.read(path, limit=200))


def test_a_dump_that_could_not_be_deleted_is_tried_again(tmp_path, monkeypatch):
    """Windows refuses to unlink a file another process has open, and the other
    process is the controller reading this very stack while it assesses a
    stall. Forgetting the file on a refused unlink stranded it for the rest of
    the run: no later test tried again, and the next stall read a finished
    test's frames as the live worker's."""
    watchdog = crash_stack.SlowTestWatchdog(tmp_path / "gw0.slow", timeout=0.01)
    watchdog.path.write_text("Timeout (0:00:01)!\n", encoding="utf-8")
    watchdog._on_disk = True

    refused = []
    real_unlink = Path.unlink

    def refuse_once(self, *arguments, **keywords):
        if self == watchdog.path and not refused:
            refused.append(self)
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(self, *arguments, **keywords)

    monkeypatch.setattr(Path, "unlink", refuse_once)

    watchdog.end_test()
    assert watchdog.path.exists(), "the unlink was refused, as the test arranged"

    watchdog.start_test()
    watchdog.end_test()
    assert not watchdog.path.exists(), "the refused dump was never tried again"


def test_a_dump_somebody_else_removed_is_not_retried_forever(tmp_path):
    watchdog = crash_stack.SlowTestWatchdog(tmp_path / "gw0.slow", timeout=0.01)
    watchdog._on_disk = True

    watchdog.end_test()
    assert watchdog._on_disk is False


# -- the one timer two plugins share ---------------------------------------


def test_the_fallback_stands_down_when_it_is_given_no_stream(tmp_path):
    """How the worker says "pytest owns the timer". There is one
    dump_traceback_later per process and arming it cancels what was armed
    before - so a fallback that re-arms every second silently takes over a
    configured faulthandler_timeout, exit and all."""
    fallback = crash_stack.FrozenInterpreterFallback(None, interval=0.05)
    assert fallback.enabled is False

    import faulthandler

    marker = tmp_path / "someone-else.txt"
    with marker.open("w", buffering=1, encoding="utf-8") as stream:
        faulthandler.dump_traceback_later(0.5, repeat=False, file=stream, exit=False)
        try:
            deadline = time.time() + 1.5
            while time.time() < deadline:
                fallback.tick()      # must not cancel and re-aim the timer
                time.sleep(0.05)
            assert marker.stat().st_size > 0, "the other plugin's timer never fired"
        finally:
            faulthandler.cancel_dump_traceback_later()

    # And stopping a fallback that armed nothing must not disarm whoever did.
    with marker.open("w", buffering=1, encoding="utf-8") as stream:
        faulthandler.dump_traceback_later(0.5, repeat=False, file=stream, exit=False)
        try:
            fallback.stop()
            time.sleep(1.5)
            assert marker.stat().st_size > 0, "the fallback cancelled a timer it never armed"
        finally:
            faulthandler.cancel_dump_traceback_later()


def test_a_worker_leaves_pytest_s_own_timeout_alone(tmp_path):
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    from pytest_failure_instrumentation.config import Settings

    recorder = WorkerRecorder(
        tmp_path, "gw0", Settings(heartbeat_interval=1), faulthandler_timeout=30.0
    )
    try:
        assert recorder.frozen.enabled is False
        assert not (tmp_path / "gw0.frozen").exists()
    finally:
        recorder.close()

    events = (tmp_path / "gw0.events").read_text(encoding="utf-8")
    assert "frozen_fallback_stood_down" in events
    assert "faulthandler_timeout" in events


def test_a_worker_with_no_timeout_configured_still_arms_the_fallback(tmp_path):
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    from pytest_failure_instrumentation.config import Settings

    recorder = WorkerRecorder(tmp_path, "gw0", Settings(heartbeat_interval=1))
    try:
        assert recorder.frozen.enabled is True
        assert (tmp_path / "gw0.frozen").exists()
    finally:
        recorder.heartbeat.stop()
        recorder.close()
