"""What an exit status means. A table, not a probe.

Retrieval is platform I/O and lives in probes/process.py. Decoding is a pure
function of an integer, so it unit-tests on any machine - which matters,
because the Windows codes below cannot be produced on Linux.
"""

from __future__ import annotations

import signal

# NTSTATUS values Windows reports as exit codes.
WINDOWS_STATUS = {
    3221225477: ("NATIVE_CRASH", "0xC0000005 access violation"),
    3221225725: ("NATIVE_CRASH", "0xC00000FD stack overflow"),
    3221226505: ("NATIVE_CRASH", "0xC0000409 stack buffer overrun"),
    3221225786: ("INTERRUPTED", "0xC000013A control-C or console shutdown"),
}

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
    if 128 < status < 192:
        # Shells, container runtimes and wrapper scripts report a signal death
        # as 128 + signal rather than passing the signal through.
        try:
            name = signal.Signals(status - 128).name
        except ValueError:
            name = f"signal {status - 128}"
        return f"exit code {status}, conventionally {name} reported by a wrapper"
    return f"process exited with code {status}"
