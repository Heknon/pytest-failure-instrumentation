"""Who called ``TerminateProcess``: the Windows counterpart of the tracepoint.

Windows has no signals. A kill is one process calling ``TerminateProcess`` on
another, and the exit code is whatever the caller chose - ``1`` from
``taskkill /F`` and from Go programs like the GitLab runner, ``-1`` from
.NET's ``Process.Kill``, ``15`` from Python's ``os.kill``. Nothing about the
caller survives in the wait status, exactly as on Linux.

The kernel keeps the record anyway. The ``Microsoft-Windows-Kernel-Audit-API-
Calls`` ETW provider writes an event from ``NtTerminateProcess`` - event 2,
``KERNEL_AUDIT_API_TERMINATEPROCESS`` - carrying the target's pid and the
return code, and every ETW event's header carries the pid of the process it
was written in, which here is the caller. That is "who called TerminateProcess
on gw3", the same question ``signal_generate`` answers on Linux.

Consuming it needs a real-time ETW session, which takes administrator rights
or membership of Performance Log Users; so it is done by a sidecar, the same
shape as the Linux one: a second interpreter running :func:`serve`, started by
:class:`..signal_trace.SignalTracer`, writing one JSON line per termination
into the run's directory and stopping the session when the run's end closes
its stdin. Sessions are system-wide and outlive a consumer that dies without
stopping them, and there are at most 64 on a machine, so a sidecar sweeps the
sessions of this package's name whose owning pid is gone before it starts its
own.

Everything ETW is spelled out here in ctypes rather than pulled from a
library, for the same reason the Linux sidecar is stdlib-only. The structures
are defined at module level and loaded lazily, so the module imports anywhere:
their sizes are asserted by a test that runs on every 64-bit platform, which
is the check a Windows-only code path most needs and least often gets.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional

#: Microsoft-Windows-Kernel-Audit-API-Calls.
PROVIDER = "e02a841c-75a3-4fa7-afc8-ae09cf9b7f23"
#: ``KERNEL_AUDIT_API_TERMINATEPROCESS``: template ``TargetProcessId``
#: (UInt32), ``ReturnCode`` (UInt32).
TERMINATE_PROCESS_EVENT = 2
SESSION_PREFIX = "pytest-failure-"
#: What the parent's stop request and a dead parent both look like.
EXIT_OK, EXIT_NO_TRACE, EXIT_ACCESS_DENIED, EXIT_NO_CONSUMER = 0, 3, 5, 4

WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_TRACE_CONTROL_STOP = 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
TRACE_LEVEL_VERBOSE = 5
INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
#: Seconds between 1601 and 1970, in 100 ns units.
FILETIME_EPOCH = 116444736000000000

TRACEHANDLE = ctypes.c_uint64
#: ``WINFUNCTYPE`` exists only on Windows; the stand-in keeps the structures
#: definable, and therefore measurable, everywhere.
FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, text: str) -> GUID:
        value = uuid.UUID(text)
        return cls(
            value.time_low,
            value.time_mid,
            value.time_hi_version,
            (ctypes.c_ubyte * 8)(*value.bytes[8:]),
        )


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [
        ("BufferSize", ctypes.c_uint32),
        ("ProviderId", ctypes.c_uint32),
        ("HistoricalContext", ctypes.c_uint64),
        ("TimeStamp", ctypes.c_int64),
        ("Guid", GUID),
        ("ClientContext", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
    ]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("Wnode", WNODE_HEADER),
        ("BufferSize", ctypes.c_uint32),
        ("MinimumBuffers", ctypes.c_uint32),
        ("MaximumBuffers", ctypes.c_uint32),
        ("MaximumFileSize", ctypes.c_uint32),
        ("LogFileMode", ctypes.c_uint32),
        ("FlushTimer", ctypes.c_uint32),
        ("EnableFlags", ctypes.c_uint32),
        ("AgeLimit", ctypes.c_int32),
        ("NumberOfBuffers", ctypes.c_uint32),
        ("FreeBuffers", ctypes.c_uint32),
        ("EventsLost", ctypes.c_uint32),
        ("BuffersWritten", ctypes.c_uint32),
        ("LogBuffersLost", ctypes.c_uint32),
        ("RealTimeBuffersLost", ctypes.c_uint32),
        ("LoggerThreadId", ctypes.c_void_p),
        ("LogFileNameOffset", ctypes.c_uint32),
        ("LoggerNameOffset", ctypes.c_uint32),
    ]


class EVENT_TRACE_HEADER(ctypes.Structure):
    # The unions are flattened to fields of the same size: nothing here reads
    # them, and the layout is what matters.
    _fields_ = [
        ("Size", ctypes.c_uint16),
        ("FieldTypeFlags", ctypes.c_uint16),
        ("Version", ctypes.c_uint32),
        ("ThreadId", ctypes.c_uint32),
        ("ProcessId", ctypes.c_uint32),
        ("TimeStamp", ctypes.c_int64),
        ("Guid", GUID),
        ("ProcessorTime", ctypes.c_uint64),
    ]


class EVENT_TRACE(ctypes.Structure):
    _fields_ = [
        ("Header", EVENT_TRACE_HEADER),
        ("InstanceId", ctypes.c_uint32),
        ("ParentInstanceId", ctypes.c_uint32),
        ("ParentGuid", GUID),
        ("MofData", ctypes.c_void_p),
        ("MofLength", ctypes.c_uint32),
        ("ClientContext", ctypes.c_uint32),
    ]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint16) for name in (
        "wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds",
    )]


class TIME_ZONE_INFORMATION(ctypes.Structure):
    # WCHAR[32] as sixteen-bit units, not c_wchar: c_wchar is four bytes on
    # Linux, and this layout has to measure the same everywhere.
    _fields_ = [
        ("Bias", ctypes.c_int32),
        ("StandardName", ctypes.c_uint16 * 32),
        ("StandardDate", SYSTEMTIME),
        ("StandardBias", ctypes.c_int32),
        ("DaylightName", ctypes.c_uint16 * 32),
        ("DaylightDate", SYSTEMTIME),
        ("DaylightBias", ctypes.c_int32),
    ]


class TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [
        ("BufferSize", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("ProviderVersion", ctypes.c_uint32),
        ("NumberOfProcessors", ctypes.c_uint32),
        ("EndTime", ctypes.c_int64),
        ("TimerResolution", ctypes.c_uint32),
        ("MaximumFileSize", ctypes.c_uint32),
        ("LogFileMode", ctypes.c_uint32),
        ("BuffersWritten", ctypes.c_uint32),
        ("LogInstanceGuid", GUID),
        ("LoggerName", ctypes.c_void_p),
        ("LogFileName", ctypes.c_void_p),
        ("TimeZone", TIME_ZONE_INFORMATION),
        ("BootTime", ctypes.c_int64),
        ("PerfFreq", ctypes.c_int64),
        ("StartTime", ctypes.c_int64),
        ("ReservedFlags", ctypes.c_uint32),
        ("BuffersLost", ctypes.c_uint32),
    ]


class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Id", ctypes.c_uint16),
        ("Version", ctypes.c_uint8),
        ("Channel", ctypes.c_uint8),
        ("Level", ctypes.c_uint8),
        ("Opcode", ctypes.c_uint8),
        ("Task", ctypes.c_uint16),
        ("Keyword", ctypes.c_uint64),
    ]


class EVENT_HEADER(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_uint16),
        ("HeaderType", ctypes.c_uint16),
        ("Flags", ctypes.c_uint16),
        ("EventProperty", ctypes.c_uint16),
        ("ThreadId", ctypes.c_uint32),
        ("ProcessId", ctypes.c_uint32),
        ("TimeStamp", ctypes.c_int64),
        ("ProviderId", GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR),
        ("ProcessorTime", ctypes.c_uint64),
        ("ActivityId", GUID),
    ]


class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("ProcessorNumber", ctypes.c_uint8),
        ("Alignment", ctypes.c_uint8),
        ("LoggerId", ctypes.c_uint16),
    ]


class EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventHeader", EVENT_HEADER),
        ("BufferContext", ETW_BUFFER_CONTEXT),
        ("ExtendedDataCount", ctypes.c_uint16),
        ("UserDataLength", ctypes.c_uint16),
        ("ExtendedData", ctypes.c_void_p),
        ("UserData", ctypes.c_void_p),
        ("UserContext", ctypes.c_void_p),
    ]


EVENT_RECORD_CALLBACK = FUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))


class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [
        ("LogFileName", ctypes.c_wchar_p),
        ("LoggerName", ctypes.c_wchar_p),
        ("CurrentTime", ctypes.c_int64),
        ("BuffersRead", ctypes.c_uint32),
        ("ProcessTraceMode", ctypes.c_uint32),
        ("CurrentEvent", EVENT_TRACE),
        ("LogfileHeader", TRACE_LOGFILE_HEADER),
        ("BufferCallback", ctypes.c_void_p),
        ("BufferSize", ctypes.c_uint32),
        ("Filled", ctypes.c_uint32),
        ("EventsLost", ctypes.c_uint32),
        ("EventRecordCallback", EVENT_RECORD_CALLBACK),
        ("IsKernelTrace", ctypes.c_uint32),
        ("Context", ctypes.c_void_p),
    ]


#: What the Windows SDK's headers give for these on x64. A structure that
#: measures differently here is a structure ETW will write past the end of.
EXPECTED_SIZES_X64 = {
    "GUID": 16,
    "WNODE_HEADER": 48,
    "EVENT_TRACE_PROPERTIES": 120,
    "EVENT_TRACE_HEADER": 48,
    "EVENT_TRACE": 88,
    "TIME_ZONE_INFORMATION": 172,
    "TRACE_LOGFILE_HEADER": 280,
    "EVENT_DESCRIPTOR": 16,
    "EVENT_HEADER": 80,
    "EVENT_RECORD": 112,
    "EVENT_TRACE_LOGFILEW": 448,
}

STRUCTURES = {
    "GUID": GUID,
    "WNODE_HEADER": WNODE_HEADER,
    "EVENT_TRACE_PROPERTIES": EVENT_TRACE_PROPERTIES,
    "EVENT_TRACE_HEADER": EVENT_TRACE_HEADER,
    "EVENT_TRACE": EVENT_TRACE,
    "TIME_ZONE_INFORMATION": TIME_ZONE_INFORMATION,
    "TRACE_LOGFILE_HEADER": TRACE_LOGFILE_HEADER,
    "EVENT_DESCRIPTOR": EVENT_DESCRIPTOR,
    "EVENT_HEADER": EVENT_HEADER,
    "EVENT_RECORD": EVENT_RECORD,
    "EVENT_TRACE_LOGFILEW": EVENT_TRACE_LOGFILEW,
}


# -- the session ------------------------------------------------------------

#: Room after the properties for the session name and a log file name, as
#: the API requires; 1024 wide characters each.
NAME_ROOM = 2 * 1024 * 2


def _library(name: str) -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError(0, f"{name} is a Windows library")
    return loader(name, use_last_error=True)


def _advapi32() -> Any:
    return _library("advapi32")


def _kernel32() -> Any:
    return _library("kernel32")


def _last_error() -> int:
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _properties(name: str = "", query: bool = False) -> tuple[Any, Any]:
    """An ``EVENT_TRACE_PROPERTIES`` with the name room behind it.

    Returned with the buffer that owns it, which the caller has to keep
    alive for as long as the pointer is used. ``query`` is for ControlTrace
    and QueryAllTraces, which *write* the session's names into the buffer at
    the offsets given and need both to point inside it; StartTrace wants a
    zero log-file offset for a session that logs to no file.
    """
    size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + NAME_ROOM
    buffer = ctypes.create_string_buffer(size)
    properties = ctypes.cast(buffer, ctypes.POINTER(EVENT_TRACE_PROPERTIES)).contents
    properties.Wnode.BufferSize = size
    properties.Wnode.Flags = WNODE_FLAG_TRACED_GUID
    # 2: system time, so every event's TimeStamp is a FILETIME rather than a
    # performance-counter reading that needs the session's frequency to place.
    properties.Wnode.ClientContext = 2
    properties.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
    # Small buffers, flushed every second: a handful of events a minute, read
    # as they arrive. The buffer *counts* are left to the kernel, whose rules
    # for them vary by version and answer a wrong pair with a refusal.
    properties.BufferSize = 32
    properties.FlushTimer = 1
    properties.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    properties.LogFileNameOffset = (
        ctypes.sizeof(EVENT_TRACE_PROPERTIES) + NAME_ROOM // 2 if query else 0
    )
    return buffer, properties


def _name_in(buffer: Any) -> str:
    """The logger name a filled-in properties buffer carries."""
    offset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    raw = buffer.raw[offset : offset + NAME_ROOM // 2]
    return raw.decode("utf-16-le", "replace").split("\0", 1)[0]


def start_session(name: str) -> tuple[int, Any, Any]:
    """Start a real-time session and enable the provider on it.

    Returns the session handle and the properties buffer. Raises ``OSError``
    with the Win32 code on refusal; ``ERROR_ACCESS_DENIED`` is the ordinary
    one, and means this user cannot trace.
    """
    advapi32 = _advapi32()
    advapi32.StartTraceW.restype = ctypes.c_uint32
    advapi32.EnableTraceEx2.restype = ctypes.c_uint32
    buffer, properties = _properties(name)
    handle = TRACEHANDLE(0)
    status = advapi32.StartTraceW(ctypes.byref(handle), name, ctypes.byref(properties))
    if status == ERROR_ALREADY_EXISTS:
        # A previous sidecar of this pid - a recycled pid, or a run that was
        # killed and restarted within its own name - left it running.
        stop_session(name)
        buffer, properties = _properties(name)
        status = advapi32.StartTraceW(ctypes.byref(handle), name, ctypes.byref(properties))
    if status != 0:
        raise OSError(status, f"StartTraceW failed with {status}")
    provider = GUID.of(PROVIDER)
    status = advapi32.EnableTraceEx2(
        handle,
        ctypes.byref(provider),
        EVENT_CONTROL_CODE_ENABLE_PROVIDER,
        TRACE_LEVEL_VERBOSE,
        ctypes.c_uint64(0xFFFFFFFFFFFFFFFF),
        ctypes.c_uint64(0),
        0,
        None,
    )
    if status != 0:
        stop_session(name)
        raise OSError(status, f"EnableTraceEx2 failed with {status}")
    return handle.value, buffer, properties


def stop_session(name: str) -> bool:
    advapi32 = _advapi32()
    advapi32.ControlTraceW.restype = ctypes.c_uint32
    _buffer, properties = _properties(name, query=True)
    status = advapi32.ControlTraceW(
        TRACEHANDLE(0), name, ctypes.byref(properties), EVENT_TRACE_CONTROL_STOP
    )
    return status == 0


def _process_is_running(pid: int) -> bool:
    kernel32 = _kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied is a process that exists and is not ours to ask; an
        # invalid parameter is no such process.
        return _last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def sweep_stale_sessions(prefix: str = SESSION_PREFIX) -> list[str]:
    """Stop the sessions of this package's name whose owning pid is gone.

    A session outlives the consumer that made it, and a machine holds at
    most 64 of them: a sidecar killed outright - ``taskkill /T`` takes the
    whole tree - would otherwise leave one behind every time.
    """
    advapi32 = _advapi32()
    advapi32.QueryAllTracesW.restype = ctypes.c_uint32
    count = 64
    buffers = [_properties(query=True) for _ in range(count)]
    array = (ctypes.POINTER(EVENT_TRACE_PROPERTIES) * count)(
        *(ctypes.pointer(properties) for _buffer, properties in buffers)
    )
    found = ctypes.c_uint32(0)
    status = advapi32.QueryAllTracesW(array, count, ctypes.byref(found))
    if status != 0:
        return []
    stopped: list[str] = []
    for buffer, _ in buffers[: min(count, found.value)]:
        name = _name_in(buffer)
        owner = name[len(prefix) :] if name.startswith(prefix) else ""
        if not owner.isdigit() or _process_is_running(int(owner)):
            continue
        if stop_session(name):
            stopped.append(name)
    return stopped


# -- the consumer -----------------------------------------------------------


def _image_of(pid: int) -> Optional[str]:
    """The caller's executable, read while the caller still exists."""
    kernel32 = _kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_uint32(1024)
        name = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(
            ctypes.c_void_p(handle), 0, name, ctypes.byref(size)
        ):
            return name.value
        return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def filetime_to_epoch(stamp: int) -> float:
    return (stamp - FILETIME_EPOCH) / 1e7


def decode(record: Any) -> Optional[dict[str, Any]]:
    """One termination out of an ``EVENT_RECORD``, or None for anything else."""
    header = record.EventHeader
    if bytes(header.ProviderId) != PROVIDER_BYTES:
        return None
    if header.EventDescriptor.Id != TERMINATE_PROCESS_EVENT or record.UserDataLength < 8:
        return None
    target, code = struct.unpack("<II", ctypes.string_at(record.UserData, 8))
    return {
        "via": "TerminateProcess",
        "sender_pid": int(header.ProcessId),
        "target_pid": int(target),
        "exit_code": int(code),
        "wall": round(filetime_to_epoch(int(header.TimeStamp)), 6),
    }


PROVIDER_BYTES = bytes(GUID.of(PROVIDER))


def consume(name: str, on_record: Callable[[dict[str, Any]], None]) -> tuple[int, Any]:
    """Open the session for real-time reading. Returns the consumer handle
    and the callback, which the caller keeps alive while it processes."""
    advapi32 = _advapi32()
    advapi32.OpenTraceW.restype = TRACEHANDLE

    def callback(pointer: Any) -> None:
        try:
            found = decode(pointer.contents)
            if found is not None:
                on_record(found)
        except Exception:  # noqa: BLE001 - never raise into ETW's callback
            pass

    keep = EVENT_RECORD_CALLBACK(callback)
    logfile = EVENT_TRACE_LOGFILEW()
    logfile.LoggerName = name
    logfile.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
    logfile.EventRecordCallback = keep
    handle = advapi32.OpenTraceW(ctypes.byref(logfile))
    if handle == INVALID_PROCESSTRACE_HANDLE:
        raise OSError(_last_error(), "OpenTraceW failed")
    return int(handle), (keep, logfile)


def process(handle: int) -> int:
    """Blocks until the handle is closed; the callback runs on this thread."""
    advapi32 = _advapi32()
    advapi32.ProcessTrace.restype = ctypes.c_uint32
    handles = (TRACEHANDLE * 1)(TRACEHANDLE(handle))
    return int(advapi32.ProcessTrace(handles, 1, None, None))


def close(handle: int) -> None:
    advapi32 = _advapi32()
    advapi32.CloseTrace.restype = ctypes.c_uint32
    advapi32.CloseTrace(TRACEHANDLE(handle))


# -- the sidecar ------------------------------------------------------------


REPORTER = "from pytest_failure_instrumentation.incidents.reporter import main; main()"
REPORTER_TIMEOUT = 300.0
CREATE_NO_WINDOW = 0x08000000


def _report(payload: dict[str, Any], output: str) -> None:
    """The run that started this died without saying goodbye: hand what it
    left to the reporter, in a child, with the run's own environment."""
    import subprocess

    directory = os.path.dirname(os.path.abspath(output))
    with open(os.path.join(directory, "reporter.log"), "ab") as log:
        child = None
        try:
            child = subprocess.Popen(
                [payload.get("python") or sys.executable, "-c", REPORTER],
                stdin=subprocess.PIPE, stdout=log, stderr=log,
                cwd=payload.get("rootdir") or None, env=payload.get("env") or None,
                creationflags=CREATE_NO_WINDOW,
            )
            assert child.stdin is not None
            child.communicate(input=json.dumps(payload).encode("utf-8"), timeout=REPORTER_TIMEOUT)
        except Exception as failure:  # noqa: BLE001 - the log is the only reader
            log.write(f"the reporter could not be run: {failure!r}\n".encode())
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()


def _listen(on_message: Callable[[dict[str, Any]], None]) -> bool:
    """Read the run's messages off stdin until EOF. True if told to stop."""
    told_to_stop = False
    inbound = b""
    while True:
        try:
            chunk = os.read(0, 65536)
        except OSError:
            break
        if not chunk:
            break  # the run that started this is gone, or asked it to stop
        inbound += chunk
        lines = inbound.split(b"\n")
        inbound = lines.pop()
        for raw in lines:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("stop"):
                told_to_stop = True
            on_message(message)
    return told_to_stop


def serve(session: str, output: str, mode: str = "trace") -> int:
    """The sidecar's whole life: sweep, start, consume until stdin closes, stop.

    ``watch`` mode traces nothing: it exists so that a run with a reporter
    configured has a survivor even where no ETW session can be had.
    """
    payload: dict[str, Any] = {}

    def remember(message: dict[str, Any]) -> None:
        if isinstance(message.get("reporter"), dict):
            payload.update(message["reporter"])

    if mode != "trace":
        with open(output, "a", buffering=1, encoding="utf-8") as out:
            out.write(json.dumps({
                "header": True, "pid": os.getpid(), "mode": mode, "wall": time.time(),
                "monotonic": time.monotonic(), "platform": sys.platform,
            }) + "\n")
        told_to_stop = _listen(remember)
        if payload and not told_to_stop:
            _report(payload, output)
        return EXIT_OK

    if sys.platform != "win32":
        return EXIT_NO_TRACE
    try:
        sweep_stale_sessions()
    except Exception:  # noqa: BLE001 - a sweep that fails costs nothing
        pass
    try:
        session_handle, buffer, properties = start_session(session)
    except OSError as failure:
        return EXIT_ACCESS_DENIED if failure.errno == ERROR_ACCESS_DENIED else EXIT_NO_TRACE

    out = open(output, "a", buffering=1, encoding="utf-8")
    lock = threading.Lock()

    def write(record: dict[str, Any]) -> None:
        sender = record.get("sender_pid")
        if isinstance(sender, int):
            image = _image_of(sender)
            if image:
                record["sender_exe"] = image
                record["sender_comm"] = image.rsplit("\\", 1)[-1]
        record["read_at"] = round(time.time(), 6)
        with lock:
            out.write(json.dumps(record) + "\n")

    try:
        consumer, keep_alive = consume(session, write)
    except OSError:
        stop_session(session)
        out.close()
        return EXIT_NO_CONSUMER

    out.write(json.dumps({
        "header": True, "pid": os.getpid(), "mode": mode, "session": session,
        "wall": time.time(), "monotonic": time.monotonic(), "platform": "win32",
    }) + "\n")

    worker = threading.Thread(target=process, args=(consumer,), name="etw", daemon=True)
    worker.start()
    told_to_stop = False
    try:
        told_to_stop = _listen(remember)
        # EOF: give the events already queued a moment to be flushed and
        # written, the run's own termination among them, before the session
        # is torn down.
        if not told_to_stop:
            time.sleep(1.5)
    finally:
        try:
            close(consumer)
        except Exception:  # noqa: BLE001
            pass
        try:
            stop_session(session)
        except Exception:  # noqa: BLE001
            pass
        worker.join(timeout=5.0)
        del keep_alive
        out.close()
    if payload and not told_to_stop:
        _report(payload, output)
    return EXIT_OK


def main() -> None:
    mode = sys.argv[3] if len(sys.argv) > 3 else "trace"
    sys.exit(serve(sys.argv[1], sys.argv[2], mode))


if __name__ == "__main__":
    main()
