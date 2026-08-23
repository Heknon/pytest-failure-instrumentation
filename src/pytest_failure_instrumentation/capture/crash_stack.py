"""Getting a stack out of a worker - by the worker's own timer, not a signal.

The obvious design is for the controller to signal a stalled worker and have
faulthandler answer. It has two flaws. Windows has no SIGUSR1 and ``os.kill``
there cannot deliver one, so the whole mechanism is absent. And on POSIX the
signal *perturbs the subject*: a C extension blocked in a syscall that does not
handle EINTR returns early when the signal lands, so the worker resumes and the
stall being measured disappears.

So the worker arms its own watchdog instead. ``dump_traceback_later`` runs a
timer thread inside the process: arm it when a test starts, cancel it when the
test ends, and any test outliving the timeout writes its own stack. It works on
every platform, it interrupts no syscall, and it still dumps while native code
holds the GIL - which is exactly the case a signal was needed for.

Arming and cancelling costs about 78 microseconds per test, which is affordable
in a path where almost nothing else is.

The signal handler is still registered on POSIX, but only as an extra: a way to
ask for a *fresh* stack once the controller has already decided a worker is
stalled, when the risk of nudging an already-wedged process is acceptable.

The two dumps go to two files. A watchdog dump means "this test is taking a
while" and is written by tests that go on to pass; a fatal dump means the
process is ending. Sharing one file made the first indistinguishable from the
second, so a slow test that passed could be read afterwards as the crash that
killed the worker - and blamed for it. ``.crash`` holds fatal and on-demand
dumps; ``.slow`` holds the watchdog's.
"""

from __future__ import annotations

import faulthandler
import os
import signal
from pathlib import Path
from typing import Optional, TextIO

from ..probes import stacks


def arm_fatal_handler(stream: TextIO) -> bool:
    """Point fatal-signal dumps at this worker's own file.

    pytest's own faulthandler plugin enables at configure time with
    ``trylast``, aiming at shared stderr where every worker's output
    interleaves. Calling this later claims the handler back.

    Returns whether an on-demand stack can also be requested later.
    """
    faulthandler.enable(file=stream, all_threads=True)
    if not stacks.can_request_stack():
        return False
    faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True, chain=False)
    return True


class SlowTestWatchdog:
    """Leaves a stack for any test still running after ``timeout`` seconds.

    The timeout is a cadence, not just a threshold: ``repeat=True`` means a
    test that goes on running keeps refreshing its stack, so whatever is on
    disk describes the test that is running *now* and is at most one timeout
    old. That is the only stack a stalled worker has on Windows, where nothing
    can ask a live process for one.

    Which is why the file is emptied when the test ends. Only the running
    test's dumps are ever wanted - the controller reads the most recent one -
    and keeping the rest turns a cheap cadence into a file that grows all run
    for no reader. Emptied rather than rotated, because the whole point is
    that there is nothing to keep.
    """

    def __init__(self, stream: TextIO, timeout: float) -> None:
        self.stream = stream
        self.timeout = timeout
        self.enabled = timeout > 0
        #: Set once the timer has been armed, so a suite of fast tests never
        #: pays for the reset below.
        self._armed = False

    def start_test(self) -> None:
        if not self.enabled:
            return
        faulthandler.dump_traceback_later(
            self.timeout, repeat=True, file=self.stream, exit=False
        )
        self._armed = True

    def end_test(self) -> None:
        if not self.enabled:
            return
        faulthandler.cancel_dump_traceback_later()
        if self._armed:
            self._armed = False
            self._empty()

    def _empty(self) -> None:
        """Truncate through the descriptor faulthandler writes to.

        It holds the raw fd and writes at the fd's own offset, so the offset
        has to be rewound as well - truncating alone would leave the next dump
        written past a hole of NULs.
        """
        try:
            descriptor = self.stream.fileno()
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except (OSError, ValueError):
            pass  # bookkeeping must never break a run


#: What faulthandler writes before the first thread when the process is dying.
FATAL_BANNER = "Fatal Python error"

#: What ``dump_traceback_later`` writes. The test may well go on to pass.
TIMEOUT_BANNER = "Timeout ("

#: Lines faulthandler writes before the first thread, which say what kind of
#: dump this is.
BANNERS = (FATAL_BANNER, TIMEOUT_BANNER)

#: Frames only ever present on the thread running a test. What makes a dump
#: useful is finding *that* thread, not the first one printed.
RUNTEST_MARKERS = (
    "pytest_runtest_protocol",
    "runtestprotocol",
    "pytest_runtest_call",
    "pytest_pyfunc_call",
)

OWN_PACKAGE = "/pytest_failure_instrumentation/"


def read(path: Path, limit: int = 12, offset: int = 0) -> list[str]:
    """One thread's stack out of the *latest* dump - most recent call first.

    Two things have to be picked correctly here, and getting either wrong
    blames code that was not running.

    **Which dump.** A file holds as many dumps as were written to it, and
    ``dump_traceback_later(repeat=True)`` writes one every timeout for as long
    as a test runs - so a worker wedged for a minute leaves dozens. Reading
    from the top returns the oldest, which is the stack of whatever was slow
    *first*: a test that may have finished, passed, and been forgotten. Only
    the last dump describes the present.

    **Which thread within it.** A dump is written with ``all_threads=True``, so
    it holds several stacks. The first printed is almost never the interesting
    one: in a pytest worker it is this plugin's own heartbeat thread, and after
    that execnet's receiver.

    ``offset`` reads only what was appended past a known point, so a stack
    written now is never confused with one written earlier.
    """
    lines = _lines(path, offset)
    if not lines:
        return []

    lines = _latest_dump(lines)
    banner = lines[0] if lines[0].startswith(BANNERS) else None
    sections = _thread_sections(lines)
    if not sections:
        return lines[:limit]

    section = _most_relevant(sections)[:limit]
    return ([banner] + section) if banner else section


def _lines(path: Path, offset: int = 0) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            return [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return []


def _latest_dump(lines: list[str]) -> list[str]:
    """Everything from the last banner on.

    A banner is what starts a dump, so the last one starts the last dump. Dumps
    with no banner at all - an on-demand SIGUSR1 stack - cannot be split this
    way, which is why the caller that asks for one reads from a recorded offset
    instead of relying on this.
    """
    starts = [index for index, line in enumerate(lines) if line.startswith(BANNERS)]
    return lines[starts[-1]:] if starts else lines


def written_at(path: Path) -> Optional[float]:
    """When this file was last written to, which is when its last dump landed.

    A stack is evidence about a moment, and the reader has no way to tell a
    stack taken just now from one the watchdog left behind minutes ago unless
    the report says which it is.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _thread_sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith(("Current thread", "Thread 0x")):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    return sections


def _most_relevant(sections: list[list[str]]) -> list[str]:
    """The thread worth reporting, in descending order of certainty.

    A fatal signal and an on-demand SIGUSR1 both label the thread they reached
    as "Current thread", and that is the answer. ``dump_traceback_later``
    labels nothing - it dumps from a C timer thread - so a slow test has to be
    found by what is on its stack instead.
    """
    for section in sections:
        if section[0].startswith("Current thread"):
            return section
    for section in sections:
        if any(marker in line for marker in RUNTEST_MARKERS for line in section):
            return section
    for section in sections:
        if not _mentions_us(section):
            return section
    return sections[0]


def _mentions_us(section: list[str]) -> bool:
    """Our own heartbeat thread sits mostly in threading.py, so a section is
    deprioritised for containing any frame of ours, not only for being all
    ours."""
    return any(OWN_PACKAGE in line.replace("\\", "/") for line in section)


def is_fatal(lines: list[str]) -> bool:
    """Whether a dump was written by a process that was ending.

    A watchdog dump and a crash dump are the same shape, and only the banner
    separates them. On Windows that separation is load-bearing: ``abort()``
    exits with 3, exactly as a deliberate ``os._exit(3)`` does, so the dump is
    the only evidence that one was a crash.
    """
    return bool(lines) and lines[0].startswith(FATAL_BANNER)


def size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
