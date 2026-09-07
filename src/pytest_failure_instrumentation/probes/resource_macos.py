"""Darwin public libproc/Mach counters; no subprocess per sample."""
from __future__ import annotations

import ctypes as c


class Usage(c.Structure):
    # rusage_info_v2, an explicitly versioned ABI (not rusage_info_current).
    _fields_ = [("uuid", c.c_ubyte * 16)] + [(name, c.c_uint64) for name in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins",
        "wired_size", "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime",
        "child_user_time", "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups",
        "child_pageins", "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten")]


class MacMetrics:
    def __init__(self) -> None:
        self.lib = c.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.proc = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.proc.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
        self.proc.proc_pid_rusage.restype = c.c_int
        self.lib.mach_host_self.argtypes = []
        self.lib.mach_host_self.restype = c.c_uint
        self.lib.host_statistics64.argtypes = [c.c_uint, c.c_int, c.c_void_p, c.POINTER(c.c_uint)]
        self.lib.host_statistics64.restype = c.c_int
        self.lib.host_page_size.argtypes = [c.c_uint, c.POINTER(c.c_size_t)]
        self.lib.host_page_size.restype = c.c_int
        self.lib.mach_port_deallocate.argtypes = [c.c_uint, c.c_uint]
        self.lib.mach_port_deallocate.restype = c.c_int
        self.host_port = self.lib.mach_host_self()
        self.page_size = c.c_size_t()
        if self.lib.host_page_size(self.host_port, c.byref(self.page_size)):
            self.close()
            raise OSError("host_page_size failed")

    def host(self) -> dict[str, int]:
        # VM_STATISTICS64 layout: four uint32 counts then uint64 counters;
        # use packed words to avoid imposing Python's platform alignment.
        storage = (c.c_uint64 * 32)()  # native vm_statistics64 requires 8-byte alignment
        words = c.cast(storage, c.POINTER(c.c_uint32))
        count = c.c_uint(64)
        if self.lib.host_statistics64(self.host_port, 4, c.byref(storage), c.byref(count)):
            raise OSError("host_statistics64 failed")
        def wide(index: int) -> int:
            return int(words[index]) | (int(words[index + 1]) << 32)
        values = {"wired_bytes": int(words[3]) * self.page_size.value}
        if count.value >= 16:
            values.update(pageins_total_count=wide(8), pageouts_total_count=wide(10),
                          page_faults_total_count=wide(12))
        if count.value >= 38:
            values.update(decompressions_total_count=wide(24), compressions_total_count=wide(26),
                          compressor_swapins_total_count=wide(28), compressor_swapouts_total_count=wide(30),
                          compressed_bytes=int(words[32]) * self.page_size.value,
                          compressor_uncompressed_bytes=wide(36) * self.page_size.value)
        return values

    def process(self, pid: int) -> dict[str, int]:
        usage = Usage()
        if self.proc.proc_pid_rusage(pid, 2, c.byref(usage)):
            raise OSError(c.get_errno(), "proc_pid_rusage failed")
        return {"physical_footprint_bytes": usage.phys_footprint,
                "read_total_bytes": usage.diskio_bytesread,
                "write_total_bytes": usage.diskio_byteswritten,
                "pageins_total_count": usage.pageins}

    def close(self) -> None:
        if self.host_port:
            task = c.c_uint.in_dll(self.lib, "mach_task_self_").value
            self.lib.mach_port_deallocate(task, self.host_port)
            self.host_port = 0
