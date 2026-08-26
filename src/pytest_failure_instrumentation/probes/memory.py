"""How much memory this process holds, and what the ceiling is.

Every function returns its value together with the mechanism that produced it.
macOS without psutil can only offer a *peak* figure from getrusage, and
reporting that as a current one would be a lie the reader cannot detect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psutil

from .platform_flags import IS_LINUX, IS_MACOS, IS_WINDOWS


def resident_megabytes() -> tuple[int | None, str]:
    """Current resident set size, and how it was obtained."""
    if IS_LINUX:
        try:
            with open("/proc/self/statm", "rb") as handle:
                pages = int(handle.read().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / 1048576), "procfs"
        except (OSError, IndexError, ValueError):
            pass

    if psutil is not None:
        try:
            return round(psutil.Process().memory_info().rss / 1048576), "psutil"
        except Exception:
            pass

    if IS_WINDOWS:
        value = _windows_working_set()
        if value is not None:
            return value, "psapi"

    if IS_MACOS:
        try:
            import resource

            # ru_maxrss is bytes on macOS, and it is a *peak*: reported as
            # such so it is never mistaken for the current figure.
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return round(peak / 1048576), "rusage-peak"
        except Exception:
            pass

    return None, "unavailable"


def _windows_working_set() -> int | None:
    """Current working set from psapi.

    Every signature below is declared. ``GetCurrentProcess`` returns a HANDLE,
    which is 64-bit, and ctypes defaults a return value to a 32-bit int: the
    pseudo-handle is truncated, the call fails, and resident memory silently
    reads as unavailable on every Windows machine without psutil.

    The library is loaded into its own object rather than through
    ``ctypes.windll``, which is process-global - declaring types on that would
    change how somebody else's code calls the same functions.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        )
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not kernel32.K32GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return None
        return round(counters.WorkingSetSize / 1048576)
    except Exception:
        return None


def system_available_megabytes() -> tuple[int | None, str]:
    """Free memory on the machine.

    Without a cgroup counter this is what separates an OOM kill from a
    cancellation: a worker killed with gigabytes free was not out of memory.
    """
    if IS_LINUX:
        try:
            with open("/proc/meminfo", "rb") as handle:
                for line in handle:
                    if line.startswith(b"MemAvailable:"):
                        return round(int(line.split()[1]) / 1024), "procfs"
        except (OSError, IndexError, ValueError):
            pass

    if psutil is not None:
        try:
            return round(psutil.virtual_memory().available / 1048576), "psutil"
        except Exception:
            pass

    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(status)
            ):
                return round(status.ullAvailPhys / 1048576), "kernel32"
        except Exception:
            pass

    return None, "unavailable"


def cgroup_oom_kills() -> int | None:
    """Linux only. Elsewhere there is no such counter to read."""
    if not IS_LINUX:
        return None
    for path in (
        "/sys/fs/cgroup/memory.events",
        "/sys/fs/cgroup/memory/memory.oom_control",
    ):
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition(" ")
                if name == "oom_kill":
                    return int(value)
        except (OSError, ValueError):
            continue
    return None


def cgroup_memory() -> dict[str, Any] | None:
    if not IS_LINUX:
        return None
    values: dict[str, Any] = {}
    for name, path in (
        ("current_mb", "/sys/fs/cgroup/memory.current"),
        ("peak_mb", "/sys/fs/cgroup/memory.peak"),
        ("max_mb", "/sys/fs/cgroup/memory.max"),
    ):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            values[name] = None
            continue
        try:
            values[name] = round(int(raw) / 1048576)
        except ValueError:
            continue
    return values or None


def memory_limit() -> dict[str, Any]:
    """The ceiling this process will actually hit, and where it comes from."""
    try:
        import resource

        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            constant = getattr(resource, name, None)
            if constant is None:
                continue
            soft, _ = resource.getrlimit(constant)
            if soft != resource.RLIM_INFINITY:
                return {"limit_mb": round(soft / 1048576), "limit_source": name}
    except Exception:
        pass  # no resource module on Windows

    if IS_LINUX:
        for path in (
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ):
            try:
                raw = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if raw in ("max", ""):
                continue
            try:
                value = int(raw)
            except ValueError:
                continue
            if value < (1 << 62):  # cgroup v1's "unlimited" sentinel
                return {"limit_mb": round(value / 1048576), "limit_source": path}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return {
                        "limit_mb": round(int(line.split()[1]) / 1024),
                        "limit_source": "MemTotal",
                    }
        except OSError:
            pass

    if psutil is not None:
        try:
            return {
                "limit_mb": round(psutil.virtual_memory().total / 1048576),
                "limit_source": "psutil-total",
            }
        except Exception:
            pass

    if IS_WINDOWS:
        try:
            import ctypes

            total = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(  # type: ignore[attr-defined]
                ctypes.byref(total)
            ):
                return {"limit_mb": round(total.value / 1024), "limit_source": "kernel32"}
        except Exception:
            pass

    return {"limit_mb": None, "limit_source": None}
