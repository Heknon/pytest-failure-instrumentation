"""Choosing the dump that describes the death, when it is still landing.

The crash file accumulates. An on-demand stack, taken while the worker was
merely stalled, has no banner at all - so until the fatal dump is written the
newest thing in the file is the *probe* stack. Read then, the incident reports
the frames from before the crash as the frames of the crash, while the verdict
says NATIVE_CRASH because that comes from the exit status.

Observed on a macOS runner, where the stall probe perturbed a blocked C call
into returning early: the crash followed microseconds after the probe instead
of minutes, and the two dumps raced. That timing cannot be reproduced on
demand - but it is only ever a *file state*, and a file state can be built.
"""

from __future__ import annotations

import signal
import threading
import time

import pytest

from pytest_failure_instrumentation.incidents import death

PROBE_DUMP = (
    "Thread 0x00000001 (most recent call first):\n"
    '  File "/app/victim.py", line 9 in wait_then_crash\n'
    '  File "/app/test_c.py", line 4 in test_stalls_then_segfaults\n'
)
FATAL_DUMP = (
    "Fatal Python error: Segmentation fault\n\n"
    "Current thread 0x00000001 (most recent call first):\n"
    '  File "/app/victim.py", line 10 in wait_then_crash\n'
    '  File "/app/test_c.py", line 4 in test_stalls_then_segfaults\n'
)

SIGSEGV = int(getattr(signal, "SIGSEGV", 11))


def test_the_probe_stack_is_not_reported_as_the_crash_stack(tmp_path):
    """The bug, as a file state: only the bannerless probe dump is on disk when
    the controller reads, and the exit status says a fatal dump is coming."""
    crash = tmp_path / "gw0.crash"
    crash.write_text(PROBE_DUMP, encoding="utf-8")

    # Landing shortly after the read begins, exactly as the dying process does.
    def finish_dying():
        time.sleep(0.15)
        with crash.open("a", encoding="utf-8") as handle:
            handle.write(FATAL_DUMP)

    writer = threading.Thread(target=finish_dying)
    writer.start()
    try:
        dump = death._crash_dump(crash, -SIGSEGV, "killed")
    finally:
        writer.join(timeout=5)

    assert dump[0].startswith("Fatal Python error"), (
        "reported the pre-crash probe stack as the stack of the crash"
    )
    assert any("line 10" in line for line in dump), "reported the probe's frame, not the crash's"


def test_a_dump_already_on_disk_is_not_waited_for(tmp_path):
    """The overwhelmingly common case, and it must cost nothing."""
    crash = tmp_path / "gw0.crash"
    crash.write_text(PROBE_DUMP + FATAL_DUMP, encoding="utf-8")

    started = time.monotonic()
    dump = death._crash_dump(crash, -SIGSEGV, "killed")
    assert dump[0].startswith("Fatal Python error")
    assert time.monotonic() - started < 0.2, "waited for a dump that was already there"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (-int(getattr(signal, "SIGKILL", 9)), "killed"),  # uncatchable: never writes one
        (137, "exited"),
        (None, None),
    ],
)
def test_a_death_that_writes_no_dump_is_never_waited_for(tmp_path, status, kind):
    """A worker the OOM killer took wrote nothing and never will. Waiting for
    each of them would put a second on every incident in the worst run a user
    ever has - the one where the whole matrix is being killed."""
    crash = tmp_path / "gw0.crash"
    crash.write_text(PROBE_DUMP, encoding="utf-8")

    started = time.monotonic()
    dump = death._crash_dump(crash, status, kind)
    assert time.monotonic() - started < 0.2, "waited for a dump that was not coming"
    # The probe stack is still returned: it is evidence, and is_fatal() is what
    # says it is not the death stack.
    assert dump and not death.crash_stack.is_fatal(dump)


def test_a_dump_that_never_arrives_still_yields_the_probe_stack(tmp_path):
    """Bounded, and it does not trade a labelled stack for none at all."""
    crash = tmp_path / "gw0.crash"
    crash.write_text(PROBE_DUMP, encoding="utf-8")

    started = time.monotonic()
    dump = death._crash_dump(crash, -SIGSEGV, "killed")
    waited = time.monotonic() - started

    assert dump, "gave up the only stack it had"
    assert not death.crash_stack.is_fatal(dump)
    assert waited >= death.FATAL_DUMP_WAIT_SECONDS
    assert waited < death.FATAL_DUMP_WAIT_SECONDS + 1.0, "the wait is not bounded"


def test_signals_that_leave_a_dump_are_the_ones_faulthandler_handles(tmp_path):
    """Pinned against faulthandler's documented set rather than a list someone
    remembered: a signal missing here is a race left open, and one too many is
    a delay on every death that shape."""
    for name in ("SIGSEGV", "SIGFPE", "SIGABRT", "SIGBUS", "SIGILL"):
        number = getattr(signal, name, None)
        if number is not None:
            assert int(number) in death.DUMPING_SIGNALS, f"{name} would not be waited for"
    kill = getattr(signal, "SIGKILL", None)
    if kill is not None:
        assert int(kill) not in death.DUMPING_SIGNALS, "SIGKILL cannot be caught"
