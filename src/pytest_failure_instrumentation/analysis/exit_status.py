"""What an exit status means. A table, not a probe.

Retrieval is platform I/O and lives in probes/process.py. Decoding is a pure
function of an integer, so it unit-tests on any machine - which matters,
because the Windows codes below cannot be produced on Linux.
"""

from __future__ import annotations

import signal

# NTSTATUS values Windows reports as exit codes. Written in hex, as Windows
# and every crash report print them.
WINDOWS_STATUS = {
    0xC0000005: ("NATIVE_CRASH", "0xC0000005 access violation"),
    0xC0000006: ("NATIVE_CRASH", "0xC0000006 in-page I/O error"),
    0xC000001D: ("NATIVE_CRASH", "0xC000001D illegal instruction"),
    0xC0000094: ("NATIVE_CRASH", "0xC0000094 integer division by zero"),
    0xC00000FD: ("NATIVE_CRASH", "0xC00000FD stack overflow"),
    0xC0000374: ("NATIVE_CRASH", "0xC0000374 heap corruption"),
    0xC0000409: ("NATIVE_CRASH", "0xC0000409 stack buffer overrun"),
    # Out of memory, as Windows has it: commit charge is exhausted, and the
    # allocation fails rather than a killer choosing a victim.
    0xC0000017: ("NATIVE_CRASH", "0xC0000017 out of memory"),
    0xC000012D: ("NATIVE_CRASH", "0xC000012D commit limit reached"),
    # A process that died starting up, before any of its code ran.
    0xC0000135: ("NATIVE_CRASH", "0xC0000135 DLL not found"),
    0xC0000142: ("NATIVE_CRASH", "0xC0000142 DLL initialization failed, "
                                 "often an exhausted desktop heap"),
    0xC000013A: ("INTERRUPTED", "0xC000013A control-C or console shutdown"),
}

# An NTSTATUS's top two bits are its severity, and 0b11 is an error. A
# process that ends with one ended on an unhandled exception or a failure
# the loader or runtime raised; nothing calls exit() with it. No exit status
# on POSIX comes near this range, so it is safe to ask on any platform.
NTSTATUS_ERROR = 0xC0000000


def is_ntstatus_error(status: int | None) -> bool:
    """Whether ``status`` is an error-severity NTSTATUS, listed above or not."""
    return status is not None and NTSTATUS_ERROR <= status <= 0xFFFFFFFF

# Signals that mean the process faulted.
FATAL_SIGNALS = {
    getattr(signal, name): description
    for name, description in (
        ("SIGSEGV", "segmentation fault in native code"),
        ("SIGABRT", "abort() in native code"),
        ("SIGBUS", "bus error in native code"),
        ("SIGILL", "illegal instruction in native code"),
        ("SIGFPE", "floating point error in native code"),
    )
    if hasattr(signal, name)
}

# Signals that mean somebody asked it to stop. Not defects.
DELIBERATE_SIGNALS = {
    getattr(signal, name): description
    for name, description in (
        ("SIGTERM", "a shutdown request - CI cancellation, a timeout enforcer, "
                    "or an orchestrator stopping the run"),
        ("SIGINT", "an interrupt - Ctrl-C, or a supervisor interrupting the run"),
        ("SIGHUP", "a hangup - the controlling terminal or session went away"),
    )
    if hasattr(signal, name)
}


def signal_name(received: int) -> str:
    """``SIGSEGV`` for 11, or ``signal 37`` where the platform has no name."""
    try:
        return signal.Signals(received).name
    except (ValueError, AttributeError):
        return f"signal {received}"


def describe(status: int | None) -> str:
    if status is None:
        return "unknown"
    if status < 0:
        received = -status
        try:
            name = signal.Signals(received).name
        except ValueError:
            name = f"signal {received}"
        if received in FATAL_SIGNALS:
            return f"{name}: {FATAL_SIGNALS[received]}"
        if received in DELIBERATE_SIGNALS:
            return f"{name}: {DELIBERATE_SIGNALS[received]}"
        if hasattr(signal, "SIGKILL") and received == signal.SIGKILL:
            return f"{name}: uncatchable kill (OOM killer or external kill)"
        return name
    if status in WINDOWS_STATUS:
        return WINDOWS_STATUS[status][1]
    if is_ntstatus_error(status):
        return f"0x{status:08X} NTSTATUS error"
    if 128 < status < 192:
        # Shells, container runtimes and wrapper scripts report a signal death
        # as 128 + signal rather than passing the signal through.
        try:
            name = signal.Signals(status - 128).name
        except ValueError:
            name = f"signal {status - 128}"
        return f"exit code {status}, conventionally {name} reported by a wrapper"
    return f"process exited with code {status}"
