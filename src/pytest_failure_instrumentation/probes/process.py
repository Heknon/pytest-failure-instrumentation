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
from collections import deque
from collections.abc import Iterator
from typing import Any, Optional

import psutil

from .platform_flags import IS_WINDOWS


def creation_time(pid: int) -> Optional[float]:
    """Process identity, unavailable when procfs belongs to another namespace."""
    try:
        if os.path.exists("/proc/self") and int(os.readlink("/proc/self")) != os.getpid():
            return None
        return float(psutil.Process(pid).create_time())
    except (OSError, ValueError, psutil.Error):
        return None


def same_process(pid: int, created: Any = None) -> bool:
    # Keep legacy records and access denial conservative. Never signal here.
    from .. import probes

    if not probes.is_running(pid):
        return False
    observed = creation_time(pid)
    return not (isinstance(created, (float, int)) and observed is not None
                and abs(observed - created) > 0.001)


def creation_time_agrees(pid: int, created: Any) -> bool:
    """Whether ``pid`` can still be the process a record claiming ``created``
    described.

    The weaker half of :func:`same_process`, and weaker on purpose. That one
    asks whether a process *is* the recorded one and answers no for a process
    that has since exited; this asks only whether the number has been handed
    to something else since, which is a question about a live process and the
    only one a caller holding a pid it is about to read has any use for.

    False is therefore a *disagreement* and nothing else: two creation times
    that are both known and are not the same instant. A record from before the
    field was written, or a machine whose procfs belongs to another namespace
    and cannot answer, leaves the claim unchecked rather than refused - the
    check is here to catch reuse where reuse can be proven, not to withdraw a
    facility from every platform that cannot prove it.
    """
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return True
    observed = creation_time(pid)
    return observed is None or abs(observed - created) <= 0.001


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
    try:
        if os.path.exists("/proc/self") and int(os.readlink("/proc/self")) != os.getpid():
            return False  # Foreign procfs must not label a live local PID a zombie.
    except (OSError, ValueError):
        return False
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


#: How far up an ancestry a membership question is followed before it is given
#: up on. A run's tree is a handful of levels deep - a controller, a worker,
#: whatever a test spawned and whatever that spawned in turn - and a walk that
#: reaches thirty-two hops without meeting the run has left it. The bound is
#: here because the chain is read hop by hop out of a live process table: a
#: walk with no ceiling is one loop away from never ending, on a machine that
#: is already in trouble.
MAX_ANCESTRY = 32

#: How many processes a descendant walk will describe before it stops. A reply
#: about a live machine must have a size that does not depend on what the
#: machine is doing, and a test that forked a thousand times is exactly the
#: kind of run somebody is looking at this for.
MAX_DESCENDANTS = 256


def ancestry(pid: int, limit: int = MAX_ANCESTRY) -> Iterator[int]:
    """Each process above ``pid``, nearest first, for as far as it can be read.

    Yielded rather than returned, because every caller is asking whether the
    chain meets something it already knows, and the answer is usually one or
    two hops up. A list would climb all the way to init to answer a question
    that stopped at the worker.

    **Each hop is psutil's, not a raw ppid.** A parent that has exited leaves a
    number the kernel may hand to something else, and a chain walked by number
    alone would climb into a stranger's ancestry and report what it found there
    as the caller's own. ``Process.parent()`` compares creation times and
    answers None rather than handing back the impostor, which is the whole
    reason this is not three lines of procfs reads.

    Stops rather than raises at every step. A process that exits mid-walk is
    the ordinary case here - these are the processes of a run in trouble - and
    a chain that ends early is an answer where an exception is not.

    This is the cheap direction of the question :func:`descendants` answers
    expensively, and a caller asking only whether one process is under another
    should come here: one chain of parents rather than a walk over every
    process on the machine, which matters when the caller is a request handler
    and the machine may have thousands.
    """
    try:
        current = psutil.Process(pid)
    except Exception:  # noqa: BLE001 - gone, or not ours to ask about
        return
    for _ in range(max(0, limit)):
        try:
            current = current.parent()
        except Exception:  # noqa: BLE001 - it exited, or its ppid is unreadable
            return
        if current is None:
            return  # init, or a parent gone and so no longer identifiable
        yield current.pid


def descendants(
    roots: dict[int, Any], limit: int = MAX_DESCENDANTS
) -> tuple[list[dict[str, Any]], bool]:
    """Every process under ``roots``, and whether the walk hit its bound.

    ``roots`` maps a pid to whatever the caller wants that subtree labelled
    with, and each row comes back under the label of the *nearest* root above
    it. That is what puts a worker's own children under the worker rather than
    under the controller they also descend from: the roots are all seeded
    before the walk starts, so reaching one from above never relabels it.

    One pass over the process table, not one per root. The table is what costs
    here - a read per process - and a sixty-four-worker run would otherwise pay
    for sixty-four passes to answer one request.

    A row is what can be had without touching the process: its number, its
    parent's, the name the kernel already holds and when it started.
    Deliberately not the command line. That is where a program's arguments are -
    a URL with a token in it, a password passed as a flag - and nothing that
    needs to know a process exists and belongs to the run needs to read them.
    """
    children: dict[int, list[int]] = {}
    rows: dict[int, dict[str, Any]] = {}
    try:
        listed = list(psutil.process_iter(["pid", "ppid", "name", "create_time"], ad_value=None))
    except Exception:  # noqa: BLE001 - a table that cannot be read is no rows
        return [], False
    for entry in listed:
        try:
            info = entry.info
            parent = info.get("ppid")
            if parent is None:
                continue
            rows[info["pid"]] = {
                "pid": info["pid"],
                "ppid": parent,
                "name": info.get("name"),
                "started_at": info.get("create_time"),
            }
            children.setdefault(parent, []).append(info["pid"])
        except Exception:  # noqa: BLE001 - it exited between the listing and the read
            continue

    found: list[dict[str, Any]] = []
    visited = set(roots)
    pending = deque(roots.items())
    truncated = False
    while pending:
        parent, label = pending.popleft()
        for child in sorted(children.get(parent, ())):
            if child in visited:
                continue  # a root in its own right, or already reached
            visited.add(child)
            row = rows.get(child)
            if row is None:
                continue
            if len(found) >= max(0, limit):
                truncated = True
                continue
            found.append({**row, "under": label})
            pending.append((child, label))
    return found, truncated


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
