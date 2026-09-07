"""Session-owned Windows counters. Imported only on Windows."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w


class Performance(c.Structure):
    _fields_ = [("cb", w.DWORD)] + [(name, c.c_size_t) for name in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        (name, w.DWORD) for name in ("HandleCount", "ProcessCount", "ThreadCount")]


class CounterValue(c.Union):
    _fields_ = [("doubleValue", c.c_double), ("largeValue", c.c_longlong)]


class Formatted(c.Structure):
    _fields_ = [("status", w.DWORD), ("value", CounterValue)]


class WindowsMetrics:
    def __init__(self) -> None:
        self.unavailable: dict[str, str] = {}
        self.psapi = c.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        self.psapi.GetPerformanceInfo.argtypes = [c.POINTER(Performance), w.DWORD]
        self.psapi.GetPerformanceInfo.restype = w.BOOL
        self.pdh = c.WinDLL("pdh", use_last_error=True)  # type: ignore[attr-defined]
        self.query = w.HANDLE()
        self.counters: dict[str, w.HANDLE] = {}
        self.pdh.PdhOpenQueryW.argtypes = [w.LPCWSTR, c.c_size_t, c.POINTER(w.HANDLE)]
        self.pdh.PdhAddEnglishCounterW.argtypes = [w.HANDLE, w.LPCWSTR, c.c_size_t, c.POINTER(w.HANDLE)]
        self.pdh.PdhCollectQueryData.argtypes = [w.HANDLE]
        self.pdh.PdhGetFormattedCounterValue.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.DWORD), c.POINTER(Formatted)]
        self.pdh.PdhCloseQuery.argtypes = [w.HANDLE]
        for name in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData",
                     "PdhGetFormattedCounterValue", "PdhCloseQuery"):
            getattr(self.pdh, name).restype = w.LONG
        if self.pdh.PdhOpenQueryW(None, 0, c.byref(self.query)) != 0:
            self.unavailable["pdh"] = "counter_unavailable"
            return
        for name, path in {
            "page_reads_per_second_count": r"\Memory\Page Reads/sec",
            "pages_input_per_second_count": r"\Memory\Pages Input/sec",
            "pages_output_per_second_count": r"\Memory\Pages Output/sec",
            "processor_queue_count": r"\System\Processor Queue Length",
            "disk_read_latency_seconds": r"\PhysicalDisk(_Total)\Avg. Disk sec/Read",
            "disk_write_latency_seconds": r"\PhysicalDisk(_Total)\Avg. Disk sec/Write",
            "disk_queue_count": r"\PhysicalDisk(_Total)\Current Disk Queue Length",
        }.items():
            handle = w.HANDLE()
            if self.pdh.PdhAddEnglishCounterW(self.query, path, 0, c.byref(handle)) == 0:
                self.counters[name] = handle
            else:
                self.unavailable[name] = "counter_unavailable"
        self.pdh.PdhCollectQueryData(self.query)

    def host(self) -> dict[str, float]:
        values: dict[str, float] = {}
        info = Performance()
        info.cb = c.sizeof(info)
        if self.psapi.GetPerformanceInfo(c.byref(info), info.cb):
            for source, name in (("CommitTotal", "commit_bytes"), ("CommitLimit", "commit_limit_bytes"),
                                 ("CommitPeak", "commit_peak_bytes"), ("KernelPaged", "kernel_paged_bytes"),
                                 ("KernelNonpaged", "kernel_nonpaged_bytes")):
                values[name] = getattr(info, source) * info.PageSize
            values["commit_headroom_bytes"] = values["commit_limit_bytes"] - values["commit_bytes"]
            for source, name in (("HandleCount", "handle_count"), ("ProcessCount", "process_count"),
                                 ("ThreadCount", "thread_count")):
                values[name] = getattr(info, source)
            self.unavailable.pop("performance_info", None)
        else:
            self.unavailable["performance_info"] = "query_failed"
        if self.query:
            collected = self.pdh.PdhCollectQueryData(self.query)
            for name, handle in self.counters.items():
                result = Formatted()
                status = self.pdh.PdhGetFormattedCounterValue(handle, 0x200, None, c.byref(result))
                if collected == 0 and status == 0 and result.status in (0, 1):
                    values[name] = result.value.doubleValue
                    self.unavailable.pop(name, None)
                else:
                    self.unavailable[name] = "counter_not_ready"
        return values

    def close(self) -> None:
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = w.HANDLE()
