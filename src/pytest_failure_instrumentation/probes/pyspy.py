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
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .platform_flags import IS_LINUX, IS_MACOS, IS_WINDOWS

#: Long enough for py-spy to attach and walk a large heap on a loaded runner,
#: short enough that a process wedged in an uninterruptible syscall - where
#: the attach itself blocks - does not hold a request open indefinitely.
DEFAULT_TIMEOUT = 15.0

#: What py-spy renders a value as when it cannot render it at all. The field
#: is optional in py-spy's JSON and a missing one is not the same as a value
#: whose repr is empty, so it is said rather than left blank.
UNRENDERABLE = "<no repr available>"

#: Said when ``--native`` and ``--nonblocking`` are asked for together. py-spy
#: refuses the pair outright - "Can't get native stack traces with the
#: --nonblocking option" - so one of them has to go, and it is native.
#:
#: Which one to drop is the only real choice here, and it is not a coin toss.
#: ``--nonblocking`` is a promise about the *target*: do not stop this process
#: to look at it. ``--native`` is about the *detail* of the answer. Honouring
#: the promise and returning less costs a caller some frames; honouring the
#: detail would pause a process somebody explicitly asked not to have paused,
#: which is the one thing neither of these options is allowed to do quietly.
NATIVE_NEEDS_A_PAUSE = (
    "native frames need the process paused, and --nonblocking was asked for "
    "as well; py-spy refuses that pair, so the stack was read without native "
    "frames. Ask for one or the other"
)

#: Said when py-spy itself has no native unwinding to offer. The published
#: wheels carry it on Linux and Windows, but a build from source without the
#: ``unwind`` feature - or an older release - answers an error instead of a
#: stack, and the frames are still worth having without it.
NO_NATIVE_UNWINDING = (
    "this py-spy could not collect native frames, so the Python frames were "
    "read without them"
)

#: What a failed ``--native`` read says when the flag is the problem rather
#: than the process. Matched loosely on purpose: py-spy rejects an unsupported
#: ``--native`` at the argument parser on a build without the feature, and in
#: the unwinder on a build that has it but cannot use it here, and those are
#: not the same sentence.
NATIVE_REFUSALS = ("native", "unwind", "unexpected argument")


@dataclass(frozen=True)
class StackOptions:
    """What a caller wants the reader to do, in the reader's own terms.

    Each maps to one py-spy flag, and each costs something. ``native`` needs
    the process paused and a py-spy that can unwind; ``locals`` reads every
    frame's variables, which is the difference between "waiting on a lease"
    and "waiting on a lease it has already waited 27 seconds for", and is also
    the one option that can put a fixture's credentials in an HTTP response;
    ``nonblocking`` gives up accuracy to avoid stopping the target at all.

    Nothing here is on by default. A caller that wants more says so, and what
    it gets back says what it actually got - see :class:`Reading`.
    """

    native: bool = False
    locals: bool = False
    nonblocking: bool = False

    def as_payload(self) -> dict[str, bool]:
        return {
            "native": self.native,
            "locals": self.locals,
            "nonblocking": self.nonblocking,
        }


@dataclass(frozen=True)
class Reading:
    """A stack read, and the truth about how it was taken.

    ``options`` is what was **applied**, not what was asked for. The two differ
    whenever the reader could not honour a request - an impossible pair, a
    py-spy that cannot unwind - and a caller that displayed its own request
    back to a user would be captioning frames with a setting that did not
    produce them. Every downgrade puts its reason in ``notes``.
    """

    threads: Optional[list[dict[str, Any]]]
    error: Optional[str]
    options: StackOptions
    notes: tuple[str, ...] = ()


#: What to suggest when the attach was refused. The cause is nearly always one
#: of these, and a bare "Operation not permitted" sends people looking in
#: the wrong place.
#:
#: A tracer that is already attached answers EPERM too, and it is worth naming
#: first: a target can only be suspended by one reader at a time, so a second
#: read of the same worker fails with the same errno a permission problem
#: gives. Measured - four concurrent reads of one process, three answered and
#: the fourth was told "Failed to suspend process - EPERM" and then advised to
#: go and change ptrace_scope on a machine with no Yama at all. :func:`dump`
#: now serialises this plugin's own reads per pid, so what is left here is
#: somebody else's debugger or profiler.
PERMISSION_HINTS = {
    "linux": (
        "another tracer may already be attached - a process can only be "
        "suspended by one at a time, so a debugger or profiler on this pid "
        "gives exactly this error; otherwise ptrace is not permitted: check "
        "/proc/sys/kernel/yama/ptrace_scope "
        "(0 allows this; at 1 the tracer must be an ancestor of the target, "
        "and py-spy is a sibling of the worker rather than its ancestor - "
        "workers grant the exception themselves at startup, so a refusal here "
        "usually means the target is not one of ours), and add "
        "--cap-add=SYS_PTRACE if this is a container"
    ),
    "darwin": (
        "another tracer may already be attached, since a process can only be "
        "suspended by one at a time; otherwise py-spy needs root on macOS, "
        "because SIP blocks reading another process"
    ),
    "win32": "the reader needs permission to open the target process",
}

#: One reader at a time per target, because two cannot suspend one process and
#: the loser's errno is indistinguishable from a permission problem. Without
#: this two UIs polling the same wedged worker, or a UI and the stall probe,
#: raced the
#: server answering a UI polling /stack for the same worker, and the collision
#: was reported as a ptrace policy the user was then invited to go and change.
#:
#: One lock per pid ever read, which is bounded by the workers a run has.
_attach_locks: dict[int, threading.Lock] = {}
_attach_registry = threading.Lock()


def _attach_lock(pid: int) -> threading.Lock:
    with _attach_registry:
        return _attach_locks.setdefault(pid, threading.Lock())


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
        "py-spy is not installed in this environment, so a live process's "
        "stack cannot be read from outside it. It is a dependency of this "
        "package, so this install is missing one (pip install py-spy)"
    )


def dump(pid: int, timeout: float = DEFAULT_TIMEOUT) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """``(threads, error)`` for a live process - exactly one of them is None.

    The plain read, with nothing asked for beyond the frames, and the shape
    every caller inside this package wants: a stall being assessed and an
    incident being rendered have no use for a note about an option they never
    set. :func:`read` is the same read for a caller that does - the live
    view's ``/stack``, which offers the options to a UI.

    The target is paused for the read and resumed immediately after. If this
    call's own timeout fires and py-spy is killed mid-attach, the kernel
    detaches the tracer on its death and the target resumes on its own, so a
    timeout here cannot leave a worker stopped.
    """
    reading = read(pid, timeout=timeout)
    return reading.threads, reading.error


def read(
    pid: int,
    options: Optional[StackOptions] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Reading:
    """A :class:`Reading` for a live process, with the options honoured or said.

    Conflicting requests are settled *before* py-spy runs rather than after.
    ``--native --nonblocking`` is refused by py-spy with a one-line message and
    a non-zero exit, and passing the pair through would turn a request this can
    answer perfectly well into a failed read - so the impossible half is
    dropped here and the caller is told which, in :attr:`Reading.notes`.
    """
    asked = options or StackOptions()
    applied, notes = _settled(asked)

    binary = executable()
    if binary is None:
        return Reading(None, unavailable_reason(), applied, notes)

    # Refused rather than queued, for the same reason the server refuses past
    # its concurrency bound: a caller polling on a timer wants to be told to
    # come back, not held until its own deadline passes.
    lock = _attach_lock(pid)
    if not lock.acquire(blocking=False):
        return Reading(
            None,
            f"a stack read of process {pid} is already in flight - a target can "
            "only be suspended by one reader at a time; retry shortly",
            applied,
            notes,
        )
    try:
        return _read(binary, pid, applied, timeout, notes)
    finally:
        lock.release()


def _settled(options: StackOptions) -> tuple[StackOptions, tuple[str, ...]]:
    """The options as they can actually be asked for, and what that cost.

    One pair is impossible and the rest are independent, so this is the whole
    of it - see :data:`NATIVE_NEEDS_A_PAUSE` for why native is the half that
    gives way.
    """
    if not (options.native and options.nonblocking):
        return options, ()
    return replace(options, native=False), (NATIVE_NEEDS_A_PAUSE,)


def _command(binary: str, pid: int, options: StackOptions) -> list[str]:
    """py-spy's argv for this read.

    ``--json`` carries both the full path and the shortened one on every
    frame, so ``--full-filenames`` is not passed: the field this package reads
    is already absolute without it.
    """
    command = [binary, "dump", "--pid", str(pid), "--json"]
    if options.native:
        command.append("--native")
    if options.locals:
        # Once, not twice. ``-ll`` expands containers a level further, which
        # multiplies the size of a payload that is already the largest thing
        # this server sends and can only widen what a repr discloses.
        command.append("--locals")
    if options.nonblocking:
        command.append("--nonblocking")
    return command


def _is_a_native_refusal(stderr: bytes) -> bool:
    """Whether a failed read failed *because of* ``--native``.

    Asked before retrying without it, so that a refused ptrace or a dead
    process is reported as itself rather than quietly re-read and reported as
    a py-spy without unwinding.
    """
    said = (stderr or b"").decode("utf-8", "replace").lower()
    return any(marker in said for marker in NATIVE_REFUSALS)


def _read(
    binary: str,
    pid: int,
    options: StackOptions,
    timeout: float,
    notes: tuple[str, ...],
) -> Reading:
    command = _command(binary, pid, options)
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
        return Reading(
            None,
            f"py-spy did not answer within {timeout:g}s, which usually means "
            "the process is in an uninterruptible syscall",
            options,
            notes,
        )
    except OSError as failure:
        return Reading(None, f"py-spy could not be run: {failure!r}", options, notes)

    if completed.returncode != 0:
        said = _explained(completed.stderr, pid)
        if options.native and _is_a_native_refusal(completed.stderr):
            # Read again without it rather than answering with nothing. The
            # Python frames are the part that explains a stall, and a caller
            # that asked for native as well is better served by them plus a
            # sentence than by an error naming a py-spy build feature.
            #
            # Terminates: the retry clears the flag that reaches this branch.
            return _read(
                binary,
                pid,
                replace(options, native=False),
                timeout,
                notes + (f"{NO_NATIVE_UNWINDING} ({said})",),
            )
        return Reading(None, said, options, notes)

    try:
        payload = json.loads(completed.stdout or b"[]")
    except ValueError:
        return Reading(
            None, "py-spy answered with something that is not JSON", options, notes
        )

    # Shape-checked, not just parsed. py-spy is a separate program on a
    # floating version - the dependency is ``py-spy>=0.3`` with no ceiling -
    # and a release that wrapped its threads in an object, or answered
    # ``null``, would land straight here. Iterating that raised out of a
    # function whose whole contract is that it returns a reason instead of a
    # stack, and the server does not wrap this call: the request got no reply
    # at all rather than the 502 that would have said why. A dict was quieter
    # and worse - it iterates its keys, so the answer was zero threads and no
    # error, which reads as "this process has no Python threads".
    if not isinstance(payload, list) or not all(
        isinstance(entry, dict) for entry in payload
    ):
        return Reading(
            None,
            "py-spy answered with JSON this does not understand - expected a "
            f"list of threads, got {type(payload).__name__}; the installed "
            "py-spy may be newer than this plugin knows about",
            options,
            notes,
        )

    return Reading([_thread(entry, options) for entry in payload], None, options, notes)


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


def _thread(entry: dict[str, Any], options: StackOptions) -> dict[str, Any]:
    """One thread in the shape this package uses everywhere.

    py-spy's own keys are not passed through. What a caller gets is this
    package's record whichever process it asked about, so nothing downstream -
    the live view, a stall's rendered stack, a UI - has to know that py-spy is
    what answered.

    **``owns_gil`` and ``active`` are dropped under ``--nonblocking``**, and
    that is a correction rather than a passthrough: py-spy still reports both,
    but it read them without stopping the process, so they describe some
    instant other than the one the frames came from. A UI captioning a thread
    "holds the GIL" from a value taken at a different moment than the stack
    beneath it is being told something nobody measured. None says "this reader
    cannot know", which is the truth.
    """
    certain = not options.nonblocking
    return {
        "thread_id": entry.get("thread_id"),
        "thread_name": entry.get("thread_name"),
        #: The kernel's id for the thread, which is what shows up in ``top``,
        #: ``gdb`` and a perf trace - the ids anybody correlating this stack
        #: against another tool is holding.
        "os_thread_id": entry.get("os_thread_id"),
        "owns_gil": entry.get("owns_gil") if certain else None,
        "active": entry.get("active") if certain else None,
        # py-spy lists the innermost frame first, which is the order this
        # package reports stacks in everywhere else.
        "frames": [_frame(frame, options) for frame in entry.get("frames") or []],
    }


def _frame(frame: dict[str, Any], options: StackOptions) -> dict[str, Any]:
    """One frame, Python or native.

    ``module`` is what tells the two apart. py-spy fills it with the binary a
    native frame is in and leaves it null for a Python frame, so ``native`` is
    derived rather than guessed from a missing line number - a Python frame in
    a stripped or generated file can have no useful line either, and blaming
    the extension for that would be a wrong answer rather than a missing one.

    ``locals`` distinguishes *not asked for* from *none to show*: None for the
    first, an empty list for the second. A native frame holds no Python
    variables, so it answers with the empty list when locals were asked for -
    saying None there would read as "you did not ask", and the caller did.
    """
    module = frame.get("module")
    return {
        "function": frame.get("name"),
        "file": frame.get("filename"),
        #: 0 on a native frame, which has no line to point at.
        "line": frame.get("line"),
        "module": module,
        "native": module is not None,
        "locals": _locals(frame.get("locals")) if options.locals else None,
    }


def _locals(entries: Any) -> list[dict[str, Any]]:
    """A frame's variables, as strings that were rendered inside py-spy.

    Nothing here is a live object and nothing here can become one: py-spy
    formats each value in its own process while the target is stopped, and
    what crosses back is text. So a repr that would have executed code does
    not, and a value that has since been collected is still readable.

    ``addr`` is dropped. It is the only field py-spy offers that says nothing
    about the *value* - it is where the object happened to sit in a process
    that has already resumed - and publishing an address bypasses whatever
    ASLR was buying.
    """
    if not isinstance(entries, list):
        return []
    return [_local(entry) for entry in entries if isinstance(entry, dict)]


def _local(entry: dict[str, Any]) -> dict[str, Any]:
    rendered = entry.get("repr")
    return {
        "name": entry.get("name"),
        "repr": rendered if isinstance(rendered, str) else UNRENDERABLE,
        #: Whether this was an argument to the frame rather than a local bound
        #: inside it. Worth keeping apart: the arguments are the ones that say
        #: what this call was *asked* to do.
        "argument": bool(entry.get("arg")),
    }


def can_read(pid: int) -> bool:
    """Whether this process is a plausible target at all.

    Only cheap, local facts: that a reader exists and that the pid is not this
    very process, which has a free answer that needs no ptrace. Whether the
    attach will actually be permitted is not knowable without trying.
    """
    return pid != os.getpid() and available()
