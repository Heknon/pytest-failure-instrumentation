"""Getting a stack out of a worker that is still running, without a signal.

The obvious design is for the controller to signal a slow worker and have
faulthandler answer. It has two flaws. Windows has no SIGUSR1 and ``os.kill``
there cannot deliver one, so the whole mechanism is absent. And on POSIX the
signal *perturbs the subject*: a C extension blocked in a syscall that does not
handle EINTR returns early when the signal lands, so the worker resumes and the
stall being measured disappears.

So the worker writes its own stack instead, on a cadence, while the test that
is taking a while is still running. The signal handler is still registered on
POSIX, but only as an extra: a way to ask for a *fresh* stack once the
controller has already decided a worker is stalled, when the risk of nudging
an already-wedged process is acceptable.

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
import time
from datetime import timedelta
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

    The timeout is a cadence, not just a threshold: a test that goes on
    running keeps refreshing its stack, so whatever is on disk describes the
    test that is running *now* and is at most one timeout old. That is the
    only stack a stalled worker has on Windows, where nothing can ask a live
    process for one.

    **Why this is not faulthandler's own timer.** ``dump_traceback_later`` is
    the obvious mechanism and is what this used to be. It dumps from a C
    thread that does not hold the GIL, walking every other thread's frames
    while those threads are pushing and popping them. A dump that lands while
    the interpreter is *executing* rather than blocked reads a frame that is
    being torn down, and the worker segfaults. That is not theoretical: over a
    suite whose tests were four times the dump cadence long, a repeating timer
    killed the worker in 10 runs out of 10, against 0 out of 10 with the
    repeat turned off. The evidence is left on the file - the last dump ends
    mid-frame with a nonsense line number, and the crash file is empty because
    the fault is inside the dumper itself.

    So the dump is taken by the heartbeat thread, from Python, holding the
    GIL. Nothing else can be mutating what is being walked, which makes it
    safe by construction rather than by luck; this object is one of the
    heartbeat's tickers for that reason and does nothing on its own.

    What that costs is the one case the timer covered and this cannot: native
    code holding the GIL. No Python thread runs then, so nothing here can be
    asked to write anything. :class:`FrozenInterpreterFallback` covers exactly
    that, with the same C timer armed so that it can only fire once nothing is
    executing for it to trip over.

    The cadence is as fine as the heartbeat's *wake*, which is a second - not
    its beat, which is five. A dump is a rare thing on a healthy suite and a
    deadline check is a float comparison, so there is no reason for the first
    stack of a wedged test to wait for the next beat.
    """

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.enabled = timeout > 0
        #: When the running test started, or None between tests. Written by
        #: the main thread and read by the heartbeat's; a single attribute
        #: rather than a lock, because a tick that reads the previous value
        #: is a dump one interval early or late and nothing worse.
        self._started_at: Optional[float] = None
        #: When this test was last dumped, so the cadence is per test rather
        #: than per tick.
        self._dumped_at: Optional[float] = None
        #: Whether there is a dump to clean up. A suite of fast tests never
        #: writes one, and must not pay a failing unlink per test to find out.
        self._on_disk = False

    def start_test(self) -> None:
        if not self.enabled:
            return
        self._started_at = time.monotonic()
        # The cadence is measured within a test, not across the run: a test
        # that begins just after the previous one was dumped would otherwise
        # wait out the rest of that interval before writing its own first.
        self._dumped_at = None

    def end_test(self) -> None:
        """Only the running test's stack is ever wanted.

        A dump left behind by a test that finished describes a test that
        finished, and the next reader has no way to know that. It is dated,
        which is honest, but a stale file is still a file somebody will read.
        """
        if not self.enabled:
            return
        self._started_at = None
        if self._on_disk:
            self._discard()

    def stop(self) -> None:
        """The heartbeat's ticker protocol. Nothing to wind down: what is on
        disk at the end of a run is the last test's, and the next run clears
        the directory before it reads anything."""

    def tick(self) -> None:
        started = self._started_at
        if not self.enabled or started is None:
            return
        now = time.monotonic()
        if now - started < self.timeout:
            return
        if self._dumped_at is not None and now - self._dumped_at < self.timeout:
            return
        self._dumped_at = now
        self._dump()

    def _dump(self) -> None:
        """Write the whole dump beside the file, then move it into place.

        A reader on the controller cannot tell a file being written from a
        finished one, and half a dump is worse than none: the threads
        faulthandler had reached are there and the one running the test is
        not, so the report names whichever thread happened to be printed
        first - this plugin's own heartbeat. Renaming means every read sees a
        complete dump, or the one before it.

        The banner is faulthandler's own, because that is what this is - the
        same dump on the same cadence, taken from a thread that is allowed to
        walk the frames.
        """
        temporary = self.path.with_name(self.path.name + ".part")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(f"{TIMEOUT_BANNER}{timedelta(seconds=self.timeout)})!\n")
                handle.flush()  # faulthandler writes to the descriptor, not the buffer
                faulthandler.dump_traceback(file=handle, all_threads=True)
            os.replace(temporary, self.path)
            self._on_disk = True
        except (OSError, ValueError, RuntimeError):
            # Bookkeeping must never break a run, and a missing stack costs
            # the report a section rather than the run a worker. The cadence
            # is not advanced, so the next tick tries again: Windows refuses
            # the rename while a reader has the file open, and the reader is
            # the controller assessing a stall - which is when the freshest
            # stack matters most.
            self._dumped_at = None
            try:
                temporary.unlink()
            except OSError:
                pass

    def _discard(self) -> None:
        self._on_disk = False
        try:
            self.path.unlink()
        except OSError:
            pass  # bookkeeping must never break a run


class FrozenInterpreterFallback:
    """faulthandler's C timer, armed so that it can only fire safely.

    The watchdog above cannot write anything when native code holds the GIL:
    no Python thread runs, so nothing can be asked to take a dump. That is the
    case the C timer exists for - it dumps without the GIL - and it is also
    the case that makes the C timer dangerous, because a dump landing while
    the interpreter is *executing* walks frames that are being torn down and
    segfaults the worker.

    Both are true at once, and the way to have the first without the second is
    to arm the timer so that it can only fire when the interpreter has stopped
    executing. The heartbeat pushes the deadline forward on every beat. While
    Python runs at all the timer is always in the future and never fires; when
    several beats in a row are missed it fires once, and by then nothing is
    executing for it to trip over.

    Missing beats for that long has one realistic cause. A thread that holds
    the GIL and never releases it is running C, not Python. A main thread
    running Python releases the GIL every few milliseconds, so the heartbeat
    would have been scheduled long before the deadline; and a machine loaded
    badly enough to starve a daemon thread for three intervals would starve
    the timer's thread with it.

    Its dump goes to its own file. It means something different from the
    watchdog's - not "this test is slow" but "this process stopped responding"
    - and a reader that cannot tell them apart cannot say which.
    """

    #: Beats that have to be missed before the deadline passes. Two would be
    #: one slipped beat on a loaded runner; three is a process that is not
    #: running Python.
    MISSED_BEATS = 3

    def __init__(self, stream: TextIO, interval: float) -> None:
        self.stream = stream
        self.timeout = interval * self.MISSED_BEATS
        self.enabled = self.timeout > 0

    def rearm(self) -> None:
        """Push the deadline out by another window.

        ``repeat=False``: one dump is the whole answer here. A frozen
        interpreter's stack does not change, and a repeat would keep dumping
        into a process that may be recovering - which is the unsafe case.
        """
        if not self.enabled:
            return
        try:
            faulthandler.dump_traceback_later(
                self.timeout, repeat=False, file=self.stream, exit=False
            )
        except (RuntimeError, ValueError):
            self.enabled = False  # nothing here is worth failing a run over

    def tick(self) -> None:
        self.rearm()

    def stop(self) -> None:
        """Disarm before the heartbeat does, or the deadline outlives it.

        Session teardown is Python running flat out with nothing left to push
        the deadline forward, which is precisely the window the arming above
        exists to stay out of.
        """
        try:
            faulthandler.cancel_dump_traceback_later()
        except (RuntimeError, ValueError):
            pass


#: What faulthandler writes before the first thread when the process is dying.
FATAL_BANNER = "Fatal Python error"

#: What a watchdog dump is headed with - faulthandler's own wording, since
#: it is faulthandler's own dump. The test may well go on to pass.
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

    **Which dump.** A file holds as many dumps as were written to it. The
    crash file accumulates: an on-demand stack taken while a worker was merely
    stalled precedes the fatal dump that ends it. Reading from the top returns
    the oldest, which is the stack of whatever went wrong *first* - and where
    a file did once hold a run's worth of watchdog dumps, the oldest was a
    test that had finished, passed and been forgotten. Only the last dump
    describes the present.

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
        return _capped(lines, limit)

    section = _capped(_most_relevant(sections), limit)
    return ([banner] + section) if banner else section


def _capped(lines: list[str], limit: int) -> list[str]:
    """The deepest ``limit`` lines, saying so when there were more.

    A stack cut off without a word reads as the whole story: the deepest
    frames are the ones kept, so what is missing is the outer half - the part
    that says who called it. A reader who cannot see that it was cut has no
    reason to go and look at the file that holds the rest.
    """
    if len(lines) <= limit:
        return list(lines)
    return lines[:limit] + [f"... and {len(lines) - limit} more frames"]


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
    as "Current thread", and that is the answer.

    Except when "Current thread" is *us*. A watchdog dump is taken by the
    heartbeat thread, so faulthandler labels that one current - and taking the
    label at its word reported this plugin's own heartbeat as the frozen test,
    down to a psutil frame as the blamed function. A slow test has to be found
    by what is on its stack instead.
    """
    for section in sections:
        if section[0].startswith("Current thread") and not _mentions_us(section):
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
