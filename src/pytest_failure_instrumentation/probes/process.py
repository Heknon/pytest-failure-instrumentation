"""Retrieving the exit status of a worker process, and confirming its identity.

POSIX hands a child's status to its parent exactly once, so ``waitid`` with
``WNOWAIT`` reads it without consuming it and whoever owns the process can
still reap normally. Windows has no such rule: any handle you can open
answers. Decoding what the number *means* lives in analysis/exit_status.

:func:`is_own_child` answers a different question, and a sharper one. A pid
read back from a file is a number, not a process: the worker it named may have
exited and the kernel handed the number to something else. Nothing here does
anything to a pid, but :mod:`..probes.stacks` sends it a signal - and SIGUSR1's
default disposition is to *terminate*. Signalling a recycled pid is not a bad
report, it is an unrelated process killed. It is the mirror of
:func:`is_running`: that one errs towards saying a process is there, this one
towards saying it is not ours.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import psutil

from .platform_flags import IS_WINDOWS


def is_running(pid: int) -> bool:
    """Whether a process still exists. Never touches it.

    **Signal 0 is a POSIX answer and only a POSIX answer.** On Windows
    ``os.kill`` sends a console event for ``CTRL_C_EVENT`` and
    ``CTRL_BREAK_EVENT`` and, for every other value including zero, calls
    ``TerminateProcess`` with it. A liveness check written as ``os.kill(pid,
    0)`` therefore *kills the worker it is asking about* there - and this one
    is called for every worker on every request to the live view. So the
    platform decides the mechanism before anything else does.

    Errs towards "yes" everywhere. EPERM means the process exists and is not
    ours to signal, and an unreadable answer must not be turned into "it
    died": the callers delete evidence and report workers as gone on the
    strength of this.

    A pid can be reused, so this answers "something with this pid exists"
    rather than "that worker is alive". It is the weakest of the three
    liveness signals for exactly that reason - the heartbeat is the one that
    says whether a worker is *progressing*.
    """
    if IS_WINDOWS:
        return _windows_is_running(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return not _is_zombie(pid)


def _windows_is_running(pid: int) -> bool:
    """psutil's answer, which is a different mechanism from the POSIX one.

    Signal 0 is not available here in any form - ``os.kill`` on Windows is an
    action rather than a question - so this is not a fallback but the only
    path there is.

    Errs towards "yes" if psutil itself raises, for the same reason as
    everywhere else here: a wrong "it died" deletes a live run's evidence.
    """
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001 - a liveness check must never raise
        return True


def _is_zombie(pid: int) -> bool:
    """Whether a process has died but not yet been reaped by its parent.

    Signal 0 alone gets this wrong, and gets it wrong in the one case that
    matters most here. A killed worker stays in the process table until the
    controller waits on it, and until then the kernel happily accepts a signal
    for it - so a worker that was killed a moment ago reads as alive, which is
    the opposite of what a crash view is for. Measured: a worker sent SIGKILL
    mid-test reported as running rather than gone.

    Linux answers from procfs, which this package already reads for memory and
    which is cheaper than building a psutil object per worker per request.
    Everywhere else psutil answers.
    """
    state = _procfs_state(pid)
    if state is not None:
        return state == b"Z"
    try:
        return bool(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    except Exception:  # noqa: BLE001 - never let a liveness check raise
        return False


def _procfs_state(pid: int) -> Optional[bytes]:
    """The one-letter state from ``/proc/<pid>/stat``, or None off Linux.

    Split from the right: the second field is the executable name in
    parentheses and may itself contain spaces and parentheses, so anything
    counting fields from the left reads the wrong one for a process whose
    name is unhelpful.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        return raw.rsplit(b")", 1)[1].split()[0]
    except IndexError:
        return None


def unsigned_on_windows(status: int) -> int:
    """Windows exit codes are unsigned; some sources hand them back signed.

    An NTSTATUS is above 2^31, so ``0xC000013A`` arrives as either 3221225786
    or -1073741510 depending on who answered - and a negative status means
    "killed by signal N" to the classifier, which turns a Ctrl-C into
    SIGNAL_1073741510. Normalised here, where the platform is known, so
    everything downstream sees one form.
    """
    if IS_WINDOWS and status < 0:
        return status + (1 << 32)
    return status


def is_own_child(pid: int) -> bool:
    """Whether ``pid`` is a process this one started.

    **This one errs towards no, and :func:`is_running` above errs towards
    yes.** They are next to each other and they are not the same question, so
    the difference is worth saying: a wrong "yes" here sends a signal to a
    stranger's process, and a wrong "no" costs a stalled worker its stack. Only
    one of those is recoverable.

    Answers without asking the process anything. psutil is a hard dependency,
    so there is no machine this cannot be asked on and no third answer to
    handle - which is what lets the caller treat anything other than True as a
    refusal.

    A pid whose process has gone is not ours, and neither is one whose parent
    is somebody else. A zombie child still is: its parent is still this
    process, and a signal to it is a no-op rather than a stray kill.
    """
    if pid <= 0:
        return False
    try:
        return psutil.Process(pid).ppid() == os.getpid()
    except Exception:  # noqa: BLE001 - gone, or not ours to ask about
        return False


def exit_status(pid: int | None, popen: Any, timeout: float = 5.0) -> tuple[int | None, str | None, str]:
    """(status, kind, source) for a process that has ended.

    POSIX hands a child's status to its parent exactly once; ``waitid`` with
    ``WNOWAIT`` reads it without consuming it, so whoever owns the process can
    still reap normally. Windows has no such rule: any handle you can open
    answers, which makes it the easier platform here.
    """
    if popen is not None and getattr(popen, "returncode", None) is not None:
        return unsigned_on_windows(int(popen.returncode)), None, "popen.returncode"

    if pid and hasattr(os, "waitid"):
        result = _waitid_status(pid, timeout)
        if result is not None:
            return result

    if pid and IS_WINDOWS:
        result = _windows_exit_status(pid)
        if result is not None:
            return result

    if popen is not None:
        try:
            status = popen.poll()
            if status is None:
                status = popen.wait(timeout=timeout)
            return unsigned_on_windows(int(status)), None, "popen.wait"
        except Exception:
            pass

    return None, None, "unavailable"


def _waitid_status(pid: int, timeout: float) -> tuple[int, str | None, str] | None:
    flags = os.WEXITED | os.WNOWAIT | os.WNOHANG  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while True:
        try:
            info = os.waitid(os.P_PID, pid, flags)  # type: ignore[attr-defined]
        except (ChildProcessError, OSError, ValueError):
            return None
        if info is not None:
            break
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)

    if info.si_code == os.CLD_EXITED:  # type: ignore[attr-defined]
        return int(info.si_status), "exited", "waitid"
    if info.si_code == os.CLD_DUMPED:  # type: ignore[attr-defined]
        return -int(info.si_status), "killed-core-dumped", "waitid"
    if info.si_code == os.CLD_KILLED:  # type: ignore[attr-defined]
        return -int(info.si_status), "killed", "waitid"
    return None


def _windows_exit_status(pid: int) -> tuple[int, str | None, str] | None:
    try:
        return (
            unsigned_on_windows(int(psutil.Process(pid).wait(timeout=5))),
            "exited",
            "psutil",
        )
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        # Declared, not defaulted: OpenProcess returns a 64-bit HANDLE and
        # ctypes would truncate it to a 32-bit int. Real handles are usually
        # small enough to survive that, which is worse than failing - it works
        # until it does not. Loaded into its own object so declaring these
        # types cannot change how other code calls the same functions.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if code.value == STILL_ACTIVE:
                return None
            return int(code.value), "exited", "GetExitCodeProcess"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None
