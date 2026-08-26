"""Asking a process for its stack - this one, or another one.

Another process is asked with a signal, which needs both a signal to send and
a handler able to answer it. Windows has neither, so a stall there is reported
without a stack rather than with a guess. Reading the target's memory instead
lifts that restriction and lives in :mod:`.pyspy`.

*This* process needs no asking at all: its frames are already here, and
:func:`own_threads` reads them in the shape the external reader returns.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType
from typing import Any, Optional


def can_request_stack() -> bool:
    """Whether a stalled worker can be asked for its stack.

    Needs both a signal to send and a handler able to answer it. Windows has
    neither, so a stall there is reported without a stack rather than with a
    guess.
    """
    import faulthandler

    return hasattr(signal, "SIGUSR1") and hasattr(faulthandler, "register")


def request_stack(pid: int) -> bool:
    if not can_request_stack():
        return False
    try:
        os.kill(pid, signal.SIGUSR1)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def own_threads() -> list[dict[str, Any]]:
    """This process's own threads, in the shape :mod:`.pyspy` returns.

    The free answer. Reading another process needs ptrace and a subprocess;
    reading this one is a dict lookup, so a request that happens to name the
    serving process should never pay for the external reader.

    What it cannot do is the case the external reader exists for. Building
    these records is Python, so a thread holding the GIL in native code stops
    this from running at all - and the caller gets no answer rather than a
    wrong one, which is the honest failure here.
    """
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    threads: list[dict[str, Any]] = []
    for thread_id, top in sys._current_frames().items():
        frames: list[dict[str, Any]] = []
        frame: Optional[FrameType] = top
        while frame is not None:
            code = frame.f_code
            frames.append(
                {
                    "function": code.co_name,
                    "file": code.co_filename,
                    "line": frame.f_lineno,
                }
            )
            frame = frame.f_back
        # f_back walks outwards, so this is already innermost-first - the
        # order py-spy uses and the order the rest of this package reports.
        threads.append(
            {
                "thread_id": thread_id,
                "thread_name": names.get(thread_id),
                # Only the external reader can say which thread holds the GIL.
                # Reaching this code at all means *some* Python is running, but
                # not which thread was executing when the request arrived.
                "owns_gil": None,
                "active": None,
                "frames": frames,
            }
        )
    return threads
