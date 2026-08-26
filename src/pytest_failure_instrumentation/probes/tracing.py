"""Letting the reader read us, where the kernel would otherwise refuse.

Reading another process's stack needs ptrace on Linux, and Linux's Yama LSM
restricts who may do it. At ``ptrace_scope=1`` - the default on Ubuntu, Debian
and most desktop distributions - the *tracer* must be an ancestor of the
tracee.

That sounds like it lets a run read its own workers, and it does not. The
reader is not the controller: it is py-spy, a subprocess the controller spawns
to do the reading. py-spy is a child of the controller and a worker is a child
of the controller, so the two are *siblings* - and a sibling is not an
ancestor. Without this module, ``/stack?pid=<worker>`` is refused on the most
common Linux configuration there is, for a run reading nothing but its own
processes.

``PR_SET_PTRACER`` is the exception Yama provides for exactly this: a process
may nominate a pid whose descendants are allowed to trace it. A worker
nominates its parent - the controller - so the controller's own py-spy is
permitted and nothing else on the machine gains anything. ``PR_SET_PTRACER_ANY``
would also work and is what most projects reach for; it is not used here
because it opens the process to every uid that could already ptrace, which is a
much larger promise than this feature needs.

The call is a no-op wherever it is not needed. Kernels without Yama answer
EINVAL, other platforms never call it, and a failure to obtain the exception
costs a stack rather than a run - the endpoint already reports a refusal with
the remedy attached.
"""

from __future__ import annotations

import os

from .platform_flags import IS_LINUX

#: prctl's option number for Yama's tracer exception. The value spells "Yama"
#: in ASCII, which is how it is written in the kernel's own headers.
PR_SET_PTRACER = 0x59616D61


def permit_parent_to_trace() -> bool:
    """Nominate this process's parent as a permitted tracer.

    True when the exception was granted, False when it was not needed or not
    available - the two are not worth telling apart by the caller, since
    neither costs the run anything.
    """
    if not IS_LINUX:
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        # The parent under xdist is the controller, which is also py-spy's
        # parent. Yama walks the tracer's ancestry to the nominated pid, so
        # naming the controller covers whatever it spawns to do the reading.
        return libc.prctl(PR_SET_PTRACER, ctypes.c_ulong(os.getppid()), 0, 0, 0) == 0
    except Exception:  # noqa: BLE001 - a missing exception costs a stack, not a run
        return False
