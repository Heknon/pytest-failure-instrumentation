"""Picking one thread out of a dump that holds all of them.

The first section printed in a pytest worker is this plugin's own heartbeat
thread, so reporting it would blame the instrumentation for the failure it came
to explain. These are real dump shapes, written to a file the way a worker
writes them.
"""

from __future__ import annotations

import time

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


def test_the_dump_file_is_emptied_when_the_test_that_filled_it_ends(tmp_path):
    """The cadence is what makes a stalled worker's stack recent, and repeat=True
    is what makes that cadence real - but only the running test's dumps are
    ever read. Keeping the rest turns a cheap cadence into a file that grows
    all run for no reader at all.
    """
    path = tmp_path / "gw0.slow"
    with path.open("w", buffering=1, encoding="utf-8") as stream:
        watchdog = crash_stack.SlowTestWatchdog(stream, timeout=0.2)
        watchdog.start_test()
        time.sleep(0.6)          # long enough for the timer to fire
        assert path.stat().st_size > 0, "the watchdog wrote nothing to empty"
        watchdog.end_test()
        assert path.stat().st_size == 0

        # And the descriptor is still usable: the next test's dump has to land
        # at the start of the file rather than past a hole of NULs.
        watchdog.start_test()
        time.sleep(0.6)
        watchdog.end_test()
    assert "\x00" not in path.read_text(encoding="utf-8")


def test_a_suite_of_fast_tests_never_pays_for_the_reset(tmp_path):
    """Nothing was armed, so there is nothing to empty."""
    path = tmp_path / "gw0.slow"
    with path.open("w", buffering=1, encoding="utf-8") as stream:
        watchdog = crash_stack.SlowTestWatchdog(stream, timeout=0)
        watchdog.start_test()
        watchdog.end_test()
    assert path.read_text(encoding="utf-8") == ""


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
    """Armed at setup and cancelled at teardown.

    Not per phase: dump_traceback_later resets the timer every time it is
    called, so re-arming at each phase would mean a test that spent most of
    the interval in setup and the rest in the call never reached it - the
    timer would keep starting over and no stack would ever be written.
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
    """A missed cancel self-heals: the next setup calls dump_traceback_later
    again, which resets the timer rather than stacking a second one."""
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
