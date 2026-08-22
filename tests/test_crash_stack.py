"""Picking one thread out of a dump that holds all of them.

The first section printed in a pytest worker is this plugin's own heartbeat
thread, so reporting it would blame the instrumentation for the failure it came
to explain. These are real dump shapes, written to a file the way a worker
writes them.
"""

from __future__ import annotations

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
