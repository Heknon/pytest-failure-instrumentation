"""Verdicts, from evidence rather than from the fact that something died.

Every branch says what it can prove. An exit status of -9 is identical for the
OOM killer, a cancelled CI job and a stray kill, so only the cgroup counter
licenses the OOM verdict; without it the honest answer is that it was killed.
"""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING

from ..capture import crash_stack
from . import exit_status as status_table

if TYPE_CHECKING:  # importing it for real would close a cycle: death -> classify
    from ..incidents.death import WorkerDeathIncident


def memory_evidence(incident: WorkerDeathIncident) -> list[str]:
    """Only for deaths where memory could be the cause, as the parts of the
    measured line.

    A segfault's resident size explains nothing and sends the reader to look at
    memory on a crash that has nothing to do with it.
    """
    parts = []
    if incident.rss_mb_at_death is not None:
        part = f"{incident.rss_mb_at_death} MB resident at the last heartbeat"
        limit = incident.cgroup.max_mb if incident.cgroup else None
        if limit:
            part += f", of a {limit} MB cgroup limit"
        parts.append(part)
    if incident.system_available_mb is not None:
        parts.append(f"{incident.system_available_mb} MB free on the machine")
    delta = incident.cgroup_oom_kills_since_start
    if delta:
        parts.append(f"cgroup OOM kills during this run: {delta}")
    return parts


def _written_ago(age: float | None) -> str:
    """A stack is evidence about a moment, so say which moment."""
    if age is None:
        return ""
    return f" from {age:.0f} s before this report" if age >= 1 else " from moments before"


def of(incident: WorkerDeathIncident) -> tuple[str, str, list[str]]:
    """Returns (verdict, confidence, evidence).

    What happened and to which test is the headline's, built from the fields
    at render time; the evidence is the facts behind it - where the status
    came from, whether the stack is the death's, the memory figures where
    memory could be the cause - and what each fact means by construction.
    """
    status = incident.exit_status
    evidence: list[str] = []

    if status is not None:
        evidence.append(
            f"Exit status {status} read via {incident.exit_status_source} "
            f"(pid {incident.worker_pid})."
        )
    elif incident.recovered_from_run:
        # Two ways to have no status and they have different remedies, so they
        # are not allowed to read the same. This one is not a gateway that
        # could not be asked: it is a process whose parent was the run that
        # died, found afterwards by somebody who was never entitled to its
        # status. Nothing can recover it, which is worth saying plainly rather
        # than leaving a reader to look for a configuration that would.
        evidence.append(
            f"Exit status unavailable (pid {incident.worker_pid}): only the parent "
            "process could read it, and the parent was the run that died. An OOM "
            "kill, a segfault and an os._exit cannot be told apart without it."
        )
    else:
        evidence.append(
            f"Exit status unavailable (pid {incident.worker_pid}): the worker ran on a "
            "remote gateway, with no local process to ask."
        )

    fatal_dump = crash_stack.is_fatal(incident.crash_stack)
    if fatal_dump:
        evidence.append("The worker wrote a stack as it died.")
    elif incident.crash_stack:
        # A dump with no fatal banner did not come from a dying process. It is
        # still context, but it is not evidence of a crash and must not be
        # allowed to outrank the exit status below. How old it is decides
        # whether it is context at all: a stack from a slow test four minutes
        # ago describes a test that has since finished.
        evidence.append(
            "A stack is on file"
            + _written_ago(incident.crash_stack_age_seconds)
            + ", written by a process that went on running. It is context, not "
            "the stack of the death."
        )

    look = f"Look at: {incident.test_in_flight}." if incident.test_in_flight else ""

    counted = (
        f"{incident.tests_started} test{'s' if incident.tests_started != 1 else ''} started "
        f"and {incident.tests_finished} finished on this worker"
    )

    def close(lines: list[str], *, with_memory: bool = False) -> list[str]:
        lines = evidence + lines
        if look:
            lines.append(look)
        parts = (memory_evidence(incident) if with_memory else []) + [counted]
        return lines + ["Measured: " + ". ".join(parts) + "."]

    if status is not None and status < 0:
        received = -status
        if hasattr(signal, "SIGKILL") and received == signal.SIGKILL:
            if incident.cgroup_oom_kills_since_start:
                return "OOM_KILLED", "high", close([
                    "The cgroup's OOM kill counter rose during this run, so the kill "
                    "came from the cgroup's memory limit."
                ], with_memory=True)
            return "SIGKILLED", "medium", close([
                "No cgroup OOM kill was counted during this run. SIGKILL cannot be "
                "caught, and is sent by a host OOM killer, a container or CI "
                "cancellation, or a kill command."
            ], with_memory=True)
        if received in status_table.FATAL_SIGNALS:
            return "NATIVE_CRASH", "high", close([])
        if received in status_table.DELIBERATE_SIGNALS:
            return f"SIGNAL_{received}", "high", close([
                "A stop request rather than a fault, so it is informational."
            ])
        return f"SIGNAL_{received}", "medium", close([])

    if status in status_table.WINDOWS_STATUS:
        verdict, _description = status_table.WINDOWS_STATUS[status]
        return verdict, "high", close([])

    if fatal_dump:
        # On Windows abort() exits with 3, exactly as a deliberate os._exit(3)
        # does, so the dump is the only thing that separates them - which is
        # why it has to be a dump the dying process wrote, and not the one a
        # slow test left behind an hour earlier.
        return "NATIVE_CRASH", "medium", close([])

    if status is not None and 128 < status < 192:
        return "PROBABLY_SIGNALLED", "low", close([
            "Exit codes 129 to 191 are the 128+signal convention shells and "
            "container runtimes use; the signal itself was not passed through."
        ])

    if status is not None:
        # Zero included. A worker that left the run without being asked to has
        # gone wrong whatever number it exited with, and os._exit(0) inside a
        # test is a real way to do it - reported as UNKNOWN it reads as a
        # status nobody could obtain, which is the one thing this is not.
        return "SELF_EXIT", "medium", close([
            "The exit was requested from inside the worker: sys.exit(), os._exit(), "
            "or a plugin abort. A worker is not expected to exit before the "
            "session ends."
        ])

    # No status at all - a remote gateway, or a run found after the fact with
    # nothing left that was entitled to read one.
    return "UNKNOWN", "low", close([], with_memory=True)
