"""How much memory this process holds, and what the ceiling is.

Every function returns its value together with the mechanism that produced it.
macOS without psutil can only offer a *peak* figure from getrusage, and
reporting that as a current one would be a lie the reader cannot detect.
"""

from __future__ import annotations

import os
import re
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


def heap_in_use_megabytes() -> tuple[int | None, str]:
    """Bytes the C allocator currently has handed out, and how that was read.

    Resident memory says what the process holds; this says what it is *using*.
    The gap between them is the allocator keeping freed pages mapped, which is
    what makes a worker that freed everything still sit at four gigabytes -
    and it is the difference between a leak and fragmentation, which are
    fixed in different places.

    glibc only, through ``mallinfo2`` (2.33+; ``mallinfo`` before it wraps at
    2 GB and is not consulted). Python's small objects live in arenas mmapped
    outside malloc, so this sees the large allocations and
    ``sys.getallocatedblocks`` covers the small ones; the two together are
    the live heap.
    """
    if not IS_LINUX:
        return None, "unavailable"
    try:
        info = _mallinfo2()
    except Exception:
        return None, "unavailable"
    if info is None:
        return None, "unavailable"
    return round((info.uordblks + info.hblkhd) / 1048576), "mallinfo2"


#: The libc handle once ``mallinfo2`` has been found in it, and False once it
#: has been looked for and is not there. The negative answer is kept because
#: looking is not free: ``find_library`` spawns a subprocess, and this is
#: read twenty-five times a second from the profiler's sampling thread. On a
#: glibc before 2.33, or on musl, every call used to pay that.
_libc: Any = None


def _mallinfo2() -> Any:
    global _libc
    if _libc is None:
        _libc = _load_mallinfo2() or False
    if _libc is False:
        return None
    return _libc.mallinfo2()


def allocator_figures() -> tuple[dict[str, int] | None, str]:
    """What the C allocator holds that nothing is using, and where.

    From glibc's ``malloc_info``: how many arenas there are, how much free
    memory sits in them, how much of that is in the main arena as against
    the ones threads were given, and how much is mapped in all. ``trim_mb``
    is ``mallinfo2``'s ``keepcost``: what ``malloc_trim(0)`` would hand back
    right now. Together they are what tells the two causes of a worker
    that "freed everything and still sits at four gigabytes" apart, which
    matter because they are fixed by different things: many arenas each
    keeping what they freed is ``MALLOC_ARENA_MAX``; one fragmented main
    heap is ``malloc_trim`` or ``MALLOC_TRIM_THRESHOLD_``, and the arena
    variable does nothing for it.

    Walks every arena under its lock, so it is read at test boundaries and
    never from the sampling thread. About a fifth of a millisecond.
    """
    if not IS_LINUX:
        return None, "unavailable"
    try:
        text = _malloc_info()
        info = _mallinfo2()
    except Exception:
        return None, "unavailable"
    if text is None:
        return None, "unavailable"
    figures = parse_malloc_info(text)
    if figures is None:
        return None, "unavailable"
    figures["trim_mb"] = round(info.keepcost / 1048576) if info is not None else 0
    return figures, "malloc_info"


def parse_malloc_info(text: str) -> dict[str, int] | None:
    """``malloc_info`` XML into megabytes: one ``<heap nr=N>`` per arena,
    each with its free space as ``<total type="fast">`` and ``type="rest"``
    and its mapping as ``<system type="current">``. The totals after the
    last heap are the process's and are not read: the heaps say the same
    and say which arena as well."""
    heaps = re.findall(r'<heap nr="(\d+)">(.*?)</heap>', text, re.DOTALL)
    if not heaps:
        return None
    free = main_free = mapped = 0
    for number, body in heaps:
        held = sum(
            int(size)
            for size in re.findall(r'<total type="(?:fast|rest)" count="\d+" size="(\d+)"/>', body)
        )
        free += held
        if number == "0":
            main_free += held
        current = re.search(r'<system type="current" size="(\d+)"/>', body)
        if current:
            mapped += int(current.group(1))
    return {
        "arenas": len(heaps),
        "free_mb": round(free / 1048576),
        "main_free_mb": round(main_free / 1048576),
        "mapped_mb": round(mapped / 1048576),
    }


#: The libc handle with ``malloc_info`` and the stream calls it needs bound,
#: or False once looked for and missing - see ``_libc``.
_libc_info: Any = None


def _malloc_info() -> str | None:
    """``malloc_info(0, stream)`` into a string, through ``open_memstream``."""
    global _libc_info
    if _libc_info is None:
        _libc_info = _load_malloc_info() or False
    if _libc_info is False:
        return None
    import ctypes

    lib = _libc_info
    buffer = ctypes.c_char_p()
    size = ctypes.c_size_t()
    stream = lib.open_memstream(ctypes.byref(buffer), ctypes.byref(size))
    if not stream:
        return None
    try:
        status = lib.malloc_info(0, stream)
    finally:
        lib.fclose(stream)
    try:
        if status != 0 or not buffer:
            return None
        return ctypes.string_at(ctypes.cast(buffer, ctypes.c_void_p), size.value).decode("utf-8", "replace")
    finally:
        if buffer:
            lib.free(ctypes.cast(buffer, ctypes.c_void_p))


def _load_malloc_info() -> Any:
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("c")
    if not name:
        return None
    library = ctypes.PyDLL(name)
    for symbol in ("malloc_info", "open_memstream", "fclose", "free"):
        if not hasattr(library, symbol):
            return None
    library.open_memstream.restype = ctypes.c_void_p
    library.open_memstream.argtypes = (ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_size_t))
    library.malloc_info.restype = ctypes.c_int
    library.malloc_info.argtypes = (ctypes.c_int, ctypes.c_void_p)
    library.fclose.restype = ctypes.c_int
    library.fclose.argtypes = (ctypes.c_void_p,)
    library.free.restype = None
    library.free.argtypes = (ctypes.c_void_p,)
    return library


def _load_mallinfo2() -> Any:
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("c")
    if not name:
        return None
    # PyDLL, not CDLL: a CDLL call releases the GIL around the call, and
    # this is read from the profiler's sampling thread beside a busy test
    # thread, where every release costs a whole switch interval to get
    # the GIL back. mallinfo2 walks the allocator's own bookkeeping and
    # blocks on nothing, so it is safe to call holding it.
    library = ctypes.PyDLL(name)
    if not hasattr(library, "mallinfo2"):
        return None

    class MallInfo2(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_size_t)
            for name in (
                "arena", "ordblks", "smblks", "hblks", "hblkhd",
                "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost",
            )
        ]

    library.mallinfo2.restype = MallInfo2
    library.mallinfo2.argtypes = ()
    return library


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
