"""Retrieving the exit status of a worker process.

POSIX hands a child's status to its parent exactly once, so ``waitid`` with
``WNOWAIT`` reads it without consuming it and whoever owns the process can
still reap normally. Windows has no such rule: any handle you can open
answers. Decoding what the number *means* lives in analysis/exit_status.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .platform_flags import IS_WINDOWS, optional_psutil


def exit_status(pid: int | None, popen: Any, timeout: float = 5.0) -> tuple[int | None, str | None, str]:
    """(status, kind, source) for a process that has ended.

    POSIX hands a child's status to its parent exactly once; ``waitid`` with
    ``WNOWAIT`` reads it without consuming it, so whoever owns the process can
    still reap normally. Windows has no such rule: any handle you can open
    answers, which makes it the easier platform here.
    """
    if popen is not None and getattr(popen, "returncode", None) is not None:
        return int(popen.returncode), None, "popen.returncode"

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
            return int(status), None, "popen.wait"
        except Exception:
            pass

    return None, None, "unavailable"


def _waitid_status(pid: int, timeout: float) -> tuple[int, str | None, str] | None:
    import time

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
    psutil = optional_psutil()
    if psutil is not None:
        try:
            return int(psutil.Process(pid).wait(timeout=5)), "exited", "psutil"
        except Exception:
            pass
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if code.value == STILL_ACTIVE:
                return None
            return int(code.value), "exited", "GetExitCodeProcess"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None
