"""Getting a stack out of a live process, whichever process it is.

Two mechanisms, and they answer different questions.

**Asking** - :func:`request_stack` - sends a signal and lets the target's own
faulthandler write the answer into its crash file. It needs both a signal to
send and a handler able to answer it, so Windows has neither, and it perturbs
the target: a C extension blocked in a syscall that does not handle EINTR
returns early when the signal lands, which is exactly the stall it was sent to
measure. It is a way to get a *fresher* stack out of a worker already
diagnosed, and nothing more.

**Reading** - :func:`live_stack` - pauses the target and reads its memory
through py-spy, which asks it to run nothing and so works on a process that is
not running anything. That is the one this package uses to answer "what is
this process doing", for any process including the one asking.

Including the one asking, deliberately. This process's own frames are also
directly available through ``sys._current_frames``, and reading them that way
was a second reader for a question that already had one: its own failure
modes, its own source to explain to whoever reads the incident, and its own
shape to keep in step. One reader is worth a subprocess.
"""

from __future__ import annotations

import os
import signal
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # imported lazily below, so a run that never reads a stack
    from .pyspy import Reading, StackOptions  # never pays for this module


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


def live_stack(pid: int) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """``(threads, error)`` for any live process. Exactly one of them is None.

    The single entry point, so that everything asking what a process is doing
    - the live view's ``/stack``, a stall being assessed - gets the same
    frames, the same thread shape and the same reason when there are none.

    **Reading ourselves is not a special case, except to Yama.** py-spy is a
    subprocess, so a read of this process is our own child tracing its parent
    - and at ``ptrace_scope=1``, the Ubuntu and Debian default, a tracer must
    be an *ancestor* of its target. The declaration that admits it is made
    here rather than at startup, so it is made by the runs that read a stack
    and no others; it names this process, which admits this run's own
    descendants and nothing else. See :mod:`.tracing`, where the two wider
    policies live.
    """
    from . import pyspy, tracing

    if pid == os.getpid():
        tracing.permit_own_children()
    return pyspy.dump(pid)


def live_reading(pid: int, options: Optional[StackOptions] = None) -> Reading:
    """:func:`live_stack` for a caller that has options to ask for.

    The same read, the same Yama declaration and the same single reader - what
    differs is that the answer carries the options that were *applied* and a
    note for any that could not be, which a caller offering those options to a
    user needs and a caller with none does not.
    """
    from . import pyspy, tracing

    if pid == os.getpid():
        tracing.permit_own_children()
    return pyspy.read(pid, options)
