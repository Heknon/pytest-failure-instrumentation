"""Reading another process's Python stack, from outside it.

Everything else in this package asks a process to describe itself. That needs
the process to be able to run Python, and the one case where a stack matters
most is the case where it cannot: native code holding the GIL means no Python
thread is scheduled, so nothing inside the worker can be asked for anything.

py-spy reads the target's memory instead - ptrace on Linux, mach on macOS,
``ReadProcessMemory`` on Windows - and needs no cooperation from it at all. It
is a Rust binary with no Python API, so this shells out to it.

Two things make it worth the subprocess. It sees a wedged interpreter, which
is the whole point. And it *stops the target before reading*, so it never walks
a frame that is being torn down - the failure mode that makes faulthandler's C
timer dangerous enough to need :class:`~..capture.crash_stack.FrozenInterpreterFallback`'s
careful arming. When a read here goes wrong, py-spy dies; the worker does not.

It is never a dependency. Absent, unpermitted or too slow, every function here
returns a reason instead of a stack, because a UI that says *why* it has no
stack is worth more than one that shows an empty pane.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .platform_flags import IS_LINUX, IS_MACOS, IS_WINDOWS

#: Long enough for py-spy to attach and walk a large heap on a loaded runner,
#: short enough that a process wedged in an uninterruptible syscall - where
#: the attach itself blocks - does not hold a request open indefinitely.
DEFAULT_TIMEOUT = 15.0

#: What to suggest when the attach was refused. The cause is nearly always one
#: of these three, and a bare "Operation not permitted" sends people looking in
#: the wrong place.
PERMISSION_HINTS = {
    "linux": (
        "ptrace is not permitted: check /proc/sys/kernel/yama/ptrace_scope "
        "(0 or 1 allows this; 1 requires the target to be a descendant of the "
        "reader, which xdist workers are), and add --cap-add=SYS_PTRACE if "
        "this is a container"
    ),
    "darwin": "py-spy needs root on macOS, because SIP blocks reading another process",
    "win32": "the reader needs permission to open the target process",
}


def executable() -> Optional[str]:
    """Where py-spy is, or None.

    ``which`` first, which finds it whenever the environment's scripts
    directory is on PATH - the ordinary case for a venv. Then that directory
    directly: a pytest launched by absolute path, or through a wrapper that
    sanitised PATH, has the binary installed beside its own interpreter and no
    way to find it by name.
    """
    found = shutil.which("py-spy")
    if found:
        return found
    name = "py-spy.exe" if IS_WINDOWS else "py-spy"
    beside = Path(sys.executable).parent / name
    return str(beside) if beside.exists() else None


def available() -> bool:
    return executable() is not None


def unavailable_reason() -> str:
    """Why there is no external reader, phrased for somebody who can fix it."""
    return (
        "py-spy is not installed in this environment (pip install py-spy), so "
        "the stack of another process cannot be read from outside it"
    )


def dump(pid: int, timeout: float = DEFAULT_TIMEOUT) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """``(threads, error)`` for a live process - exactly one of them is None.

    The target is paused for the read and resumed immediately after. If this
    call's own timeout fires and py-spy is killed mid-attach, the kernel
    detaches the tracer on its death and the target resumes on its own, so a
    timeout here cannot leave a worker stopped.
    """
    binary = executable()
    if binary is None:
        return None, unavailable_reason()

    command = [binary, "dump", "--pid", str(pid), "--json"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            # Never inherit a terminal: py-spy is being run from inside a
            # pytest session whose stdout is the report a human is reading.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, (
            f"py-spy did not answer within {timeout:g}s, which usually means "
            "the process is in an uninterruptible syscall"
        )
    except OSError as failure:
        return None, f"py-spy could not be run: {failure!r}"

    if completed.returncode != 0:
        return None, _explained(completed.stderr, pid)

    try:
        payload = json.loads(completed.stdout or b"[]")
    except ValueError:
        return None, "py-spy answered with something that is not JSON"

    return [_thread(entry) for entry in payload], None


#: Where py-spy stops describing the target and starts describing itself. It
#: reports a message, then a "Caused by" section carrying the real errno, then
#: a Rust backtrace of its own frames - and that backtrace says nothing about
#: the process being read. Taking the *last* line of stderr, which is the
#: obvious thing to do, therefore reported "10: main" as the reason a pid could
#: not be read. Measured, from the shape py-spy actually writes.
BACKTRACE_MARKERS = ("stack backtrace:", "backtrace:")


def _is_backtrace(line: str) -> bool:
    """Only the banner, never the numbered lines under it.

    The frames are numbered ``0:``, ``1:`` - and so are the entries in the
    "Caused by" section above them, which is where the errno lives. Treating a
    numbered line as the start of the backtrace therefore threw away the one
    fact that says *which* failure this is: "Operation not permitted (os error
    1)" and "No such file or directory (os error 2)" are the difference between
    a permission hint and a dead process, and both were being dropped.
    """
    return line.strip().lower() in BACKTRACE_MARKERS


def _explained(stderr: bytes, pid: int) -> str:
    """py-spy's own words, plus what to do about them where that is knowable.

    Everything up to the backtrace, joined: the first line is the message and
    the "Caused by" lines under it carry the errno that says which failure this
    actually is. Both matter and neither is the last line.
    """
    lines = (stderr or b"").decode("utf-8", "replace").splitlines()
    message = []
    for line in lines:
        if _is_backtrace(line):
            break
        stripped = line.strip()
        if stripped and stripped.lower() != "caused by:":
            message.append(stripped)
    said = " - ".join(message)[:400] or "py-spy failed with no output"

    lowered = said.lower()
    if "permission" in lowered or "denied" in lowered or "operation not permitted" in lowered:
        platform_key = "linux" if IS_LINUX else ("darwin" if IS_MACOS else "win32")
        return f"{said} - {PERMISSION_HINTS[platform_key]}"
    if (
        "no such process" in lowered
        or "check that the process is running" in lowered
        or "not found" in lowered
    ):
        return f"process {pid} is not running ({said})"
    return said


def _thread(entry: dict[str, Any]) -> dict[str, Any]:
    """One thread in the shape this package uses everywhere.

    py-spy's own keys are not passed through. The in-process reader produces
    the same records from ``sys._current_frames``, and a caller that has to
    switch on which mechanism answered has been handed two APIs rather than
    one - which is exactly what a UI would end up encoding.
    """
    return {
        "thread_id": entry.get("thread_id"),
        "thread_name": entry.get("thread_name"),
        "owns_gil": entry.get("owns_gil"),
        "active": entry.get("active"),
        "frames": [
            {
                "function": frame.get("name"),
                "file": frame.get("filename"),
                "line": frame.get("line"),
            }
            # py-spy lists the innermost frame first, which is the order this
            # package reports stacks in everywhere else.
            for frame in entry.get("frames") or []
        ],
    }


def can_read(pid: int) -> bool:
    """Whether this process is a plausible target at all.

    Only cheap, local facts: that a reader exists and that the pid is not this
    very process, which has a free answer that needs no ptrace. Whether the
    attach will actually be permitted is not knowable without trying.
    """
    return pid != os.getpid() and available()
