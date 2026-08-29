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
nominates a pid, and that pid *and every descendant of it* may then trace it. A
worker nominates its parent - the controller - which is what admits the
controller's own py-spy.

Be exact about what else it admits, because the reader it exists for is not the
extent of it. The controller's descendants are the entire process tree of the
run: every other worker, and any subprocess a test spawns while the declaration
stands, for as long as it runs under a uid that could ptrace at all. That is
wider than the one read being paid for, and it is why the declaration is made
only where something is actually going to read a worker's stack rather than on
every run - see ``Settings.tracer_in_force``, which resolves that on the
controller and hands each worker the answer.

``PR_SET_PTRACER_ANY`` is wider again: it drops the ancestry requirement
altogether, so anything the uid could already ptrace may read the process. It
is what the "any" policy below declares, and it is not the default - a *shared*
server is the one case that needs it, because that reader is no descendant of
this run's controller.

The call is a no-op wherever it is not needed. Kernels without Yama answer
EINVAL, other platforms never call it, and a failure to obtain the exception
costs a stack rather than a run - the endpoint already reports a refusal with
the remedy attached.
"""

from __future__ import annotations

import os
from typing import Optional

from .platform_flags import IS_LINUX

#: prctl's option number for Yama's tracer exception. The value spells "Yama"
#: in ASCII, which is how it is written in the kernel's own headers.
PR_SET_PTRACER = 0x59616D61

#: Yama nominates any descendant of this pid as a permitted tracer.
PTRACE_ANY = -1

#: Where Yama publishes what it enforces. Absent on kernels built without it,
#: which is the same as "no restriction" for our purposes.
PTRACE_SCOPE = "/proc/sys/kernel/yama/ptrace_scope"


def ptrace_scope() -> Optional[int]:
    """What this machine enforces, or None where Yama is not present.

    Worth reporting rather than inferring. 0 permits any read a uid could make
    anyway; 1 - the Ubuntu and Debian default - is the setting that makes the
    difference between a live view that works and one that is refused for every
    worker; 2 and 3 are progressively stricter and no exception helps.
    """
    try:
        with open(PTRACE_SCOPE, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


#: What a worker may declare. "parent" nominates the controller, whose
#: descendants include the py-spy it spawns - enough for a session to read its
#: own workers, which is how the live view is used by default. "any" drops the
#: relationship requirement, which is what a *shared* server needs: another
#: session's py-spy is no descendant of this session's controller. "off"
#: declares nothing and leaves the kernel's answer as it stands, which is what
#: a run with nothing reading worker stacks declares - most runs.
POLICIES = ("parent", "any", "off")


def permit_tracing(policy: str = "parent") -> bool:
    """Declare who may read this process, under the caller's chosen policy.

    Separated from the default because the two modes need different answers
    and only the person running knows which they are in. A drawn port is read
    by its own session and "parent" covers it exactly; a named port shared
    across sessions is read by somebody else's process, which no ancestry
    covers and only "any" permits.
    """
    if policy == "off" or not IS_LINUX:
        return False
    if policy == "any":
        return _declare(PTRACE_ANY)
    return _declare(os.getppid())


def permit_own_children() -> bool:
    """Let a py-spy *this* process spawns read *this* process.

    Not a policy, and deliberately not a fourth word in :data:`POLICIES`:
    those are what a run declares about who may read its workers, and this is
    a process answering for itself at the moment it reads itself. A run with
    no workers has no controller to nominate - the reader is its own child,
    and Yama's rule runs the other way, admitting only tracers that are
    *ancestors* of the tracee.

    ``PR_SET_PTRACER`` with our own pid is the exception that covers exactly
    that and nothing more: the nominated pid and its descendants, which here
    is the py-spy we are about to spawn and whatever else this run already
    started. Narrower than "parent", which admits everything under the process
    that started us, and far narrower than "any".

    Declared at the moment of the read rather than at startup, because by then
    a stall has already been diagnosed - so the permission is granted on the
    runs that use it and on no others, which is the rule
    :attr:`..config.Settings.tracer_in_force` exists to keep.
    """
    return _declare(os.getpid()) if IS_LINUX else False


def _declare(tracer: int) -> bool:
    """Make the prctl call, with every argument's type spelled out.

    ``prctl`` is variadic - ``int prctl(int option, ...)`` - and the kernel
    reads all four of its arguments as ``unsigned long``. Left to ctypes'
    defaults a Python ``0`` goes out as a 32-bit C ``int``, so on a 64-bit
    platform the upper half of what the kernel reads is whatever was in the
    register. That is the same hazard this package declares argtypes for
    everywhere it touches Windows handles, and it is worse here because the
    failure mode is silent: prctl answers EINVAL, this returns False, and a
    worker is simply never readable, on the one Linux configuration the whole
    module exists to handle.

    The parent under xdist is the controller, which is also py-spy's parent.
    Yama walks the tracer's ancestry to the nominated pid, so naming the
    controller covers whatever it spawns to do the reading.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        prctl.restype = ctypes.c_int
        # PTRACE_ANY is the kernel's ((unsigned long)-1), which is what
        # c_ulong(-1) produces at either width.
        return prctl(PR_SET_PTRACER, ctypes.c_ulong(tracer).value, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001 - a missing exception costs a stack, not a run
        return False
