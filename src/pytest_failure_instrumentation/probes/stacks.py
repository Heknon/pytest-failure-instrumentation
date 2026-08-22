"""Asking a live process for its stack.\n\n    Needs both a signal to send and a handler able to answer it, so Windows\n    has neither and a stall there is reported without a stack rather than\n    with a guess."""

from __future__ import annotations

import os
import signal


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
