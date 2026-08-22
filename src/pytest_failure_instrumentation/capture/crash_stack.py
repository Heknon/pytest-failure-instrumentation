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
"""

from __future__ import annotations

import faulthandler
import signal
from pathlib import Path
from typing import TextIO

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
    """Dumps the stack of any test that outlives ``timeout`` seconds."""

    def __init__(self, stream: TextIO, timeout: float) -> None:
        self.stream = stream
        self.timeout = timeout
        self.enabled = timeout > 0

    def start_test(self) -> None:
        if not self.enabled:
            return
        # repeat=True: a test stuck for an hour writes a stack every timeout,
        # so the controller sees whether it is moving between dumps.
        faulthandler.dump_traceback_later(
            self.timeout, repeat=True, file=self.stream, exit=False
        )

    def end_test(self) -> None:
        if not self.enabled:
            return
        faulthandler.cancel_dump_traceback_later()


def read(path: Path, limit: int = 12, offset: int = 0) -> list[str]:
    """The head of a dump - most recent call first.

    ``offset`` reads only what was appended past a known point, so a stack
    written now is never confused with one written earlier.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            lines = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return []
    if not lines:
        return []
    banner = lines[0] if lines[0].startswith("Fatal Python error") else None
    for index, line in enumerate(lines):
        if line.startswith(("Current thread", "Thread 0x")):
            section = lines[index : index + limit]
            return ([banner] + section) if banner else section
    return lines[:limit]


def size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
