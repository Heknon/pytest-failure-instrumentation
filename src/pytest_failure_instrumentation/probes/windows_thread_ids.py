"""Discover this process's native threads without scanning the whole machine.

PSS_CAPTURE_THREADS captures IDs only: no address-space clone, handles,
thread contexts or allocation tracing. Callers retain a portable fallback
when the API (Windows 8.1+) is unavailable or a snapshot cannot be read.
"""

from __future__ import annotations

import ctypes
import os
import threading
from typing import Any


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _ThreadEntry(ctypes.Structure):
    # PSS_THREAD_ENTRY from processsnapshot.h. FILETIME has DWORD alignment,
    # not the alignment of a uint64; using the latter shifts all later fields.
    _fields_ = [
        ("ExitStatus", ctypes.c_uint32), ("TebBaseAddress", ctypes.c_void_p),
        ("ProcessId", ctypes.c_uint32), ("ThreadId", ctypes.c_uint32),
        ("AffinityMask", ctypes.c_size_t), ("Priority", ctypes.c_int32),
        ("BasePriority", ctypes.c_int32), ("LastSyscallFirstArgument", ctypes.c_void_p),
        ("LastSyscallNumber", ctypes.c_uint16), ("CreateTime", _FileTime),
        ("ExitTime", _FileTime), ("KernelTime", _FileTime), ("UserTime", _FileTime),
        ("Win32StartAddress", ctypes.c_void_p), ("CaptureTime", _FileTime),
        ("Flags", ctypes.c_uint32), ("SuspendCount", ctypes.c_uint16),
        ("SizeOfContextRecord", ctypes.c_uint16), ("ContextRecord", ctypes.c_void_p),
    ]


class WindowsThreadIds:
    def __init__(self) -> None:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError("direct thread discovery requires a 64-bit interpreter")
        # Release the GIL for capture, which may take time in a process with
        # many threads. Walking/freeing the captured metadata retains it:
        # handing it back to a busy test for each entry costs a switch interval.
        self.api: Any = ctypes.PyDLL("kernel32", use_last_error=True)
        self.capture_api: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        signatures = {
            "PssCaptureSnapshot": (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.POINTER(ctypes.c_void_p)),
            "PssWalkMarkerCreate": (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            "PssWalkSnapshot": (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_uint32),
            "PssWalkMarkerFree": (ctypes.c_void_p,),
            "PssFreeSnapshot": (ctypes.c_void_p, ctypes.c_void_p),
        }
        for name, arguments in signatures.items():
            library = self.capture_api if name == "PssCaptureSnapshot" else self.api
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = ctypes.c_uint32

    def read(self) -> list[int]:
        snapshot, marker = ctypes.c_void_p(), ctypes.c_void_p()
        process = ctypes.c_void_p(-1)  # GetCurrentProcess's pseudo handle
        self._check(self.capture_api.PssCaptureSnapshot(process, 0x80, 0, ctypes.byref(snapshot)))
        try:
            self._check(self.api.PssWalkMarkerCreate(None, ctypes.byref(marker)))
            try:
                entry = _ThreadEntry()
                tids = []
                while True:
                    status = self.api.PssWalkSnapshot(snapshot, 3, marker,
                                                      ctypes.byref(entry), ctypes.sizeof(entry))
                    if status == 259:  # ERROR_NO_MORE_ITEMS
                        break
                    self._check(status)
                    if entry.ProcessId == os.getpid() and entry.ThreadId:
                        tids.append(int(entry.ThreadId))
                # A layout/API mismatch must not silently hide native threads.
                if threading.get_native_id() not in tids:
                    raise OSError("the thread snapshot omitted its calling thread")
                return tids
            finally:
                self.api.PssWalkMarkerFree(marker)
        finally:
            self.api.PssFreeSnapshot(process, snapshot)

    @staticmethod
    def _check(status: int) -> None:
        if status:
            raise OSError(status, "Windows thread snapshot failed")
