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
    """Only for deaths where memory could be the cause.

    A segfault's resident size explains nothing and sends the reader to look at
    memory on a crash that has nothing to do with it.
    """
    evidence = []
    if incident.rss_mb_at_death is not None:
        line = f"resident memory {incident.rss_mb_at_death} MB at last checkpoint"
        limit = incident.cgroup.max_mb if incident.cgroup else None
        if limit:
            line += f" of a {limit} MB cgroup limit"
        evidence.append(line)
    if incident.system_available_mb is not None:
        evidence.append(f"system had {incident.system_available_mb} MB free")
    delta = incident.cgroup_oom_kills_since_start
    if delta:
        evidence.append(f"cgroup OOM kill counter rose by {delta} during this run")
    return evidence


def _written_ago(age: float | None) -> str:
    """A stack is evidence about a moment, so say which moment."""
    if age is None:
        return ""
    return f", {age:.0f}s before this report" if age >= 1 else ", moments before"


def of(incident: WorkerDeathIncident) -> tuple[str, str, list[str]]:
    """Returns (verdict, confidence, evidence)."""
    status = incident.exit_status
    evidence: list[str] = []

    if incident.test_in_flight:
        phase = f" ({incident.phase})" if incident.phase else ""
        evidence.append(f"died while running {incident.test_in_flight}{phase}")
    elif incident.tests_finished:
        evidence.append(
            f"died between tests, after finishing {incident.tests_finished}"
        )
    else:
        evidence.append("died before running any test (startup or collection)")

    if status is not None:
        evidence.append(
            f"exit status {status} - {status_table.describe(status)} "
            f"(pid {incident.worker_pid}, via {incident.exit_status_source})"
        )
    else:
        evidence.append(
            f"exit status unavailable (pid {incident.worker_pid}); remote "
            "gateways have no local process to query"
        )

    fatal_dump = crash_stack.is_fatal(incident.crash_stack)
    if fatal_dump:
        evidence.append("the worker wrote a fatal stack as it died")
    elif incident.crash_stack:
        # A dump with no fatal banner did not come from a dying process. It is
        # still context, but it is not evidence of a crash and must not be
        # allowed to outrank the exit status below. How old it is decides
        # whether it is context at all: a stack from a slow test four minutes
        # ago describes a test that has since finished.
        evidence.append(
            "a stack is on file but was not written by a dying process"
            + _written_ago(incident.crash_stack_age_seconds)
            + " - it is context, not evidence of a crash"
        )

    if status is not None and status < 0:
        received = -status
        if hasattr(signal, "SIGKILL") and received == signal.SIGKILL:
            evidence = evidence + memory_evidence(incident)
            if incident.cgroup_oom_kills_since_start:
                return "OOM_KILLED", "high", evidence
            return "SIGKILLED", "medium", evidence + [
                "SIGKILL with no cgroup OOM event: a host-level OOM killer, a "
                "container or CI cancellation, or an external kill"
            ]
        if received in status_table.FATAL_SIGNALS:
            return "NATIVE_CRASH", "high", evidence + [
                status_table.FATAL_SIGNALS[received]
            ]
        if received in status_table.DELIBERATE_SIGNALS:
            return f"SIGNAL_{received}", "high", evidence + [
                status_table.DELIBERATE_SIGNALS[received],
                "nothing to triage unless the run was not meant to be stopped",
            ]
        return f"SIGNAL_{received}", "medium", evidence

    if status in status_table.WINDOWS_STATUS:
        verdict, description = status_table.WINDOWS_STATUS[status]
        return verdict, "high", evidence + [description]

    if fatal_dump:
        # On Windows abort() exits with 3, exactly as a deliberate os._exit(3)
        # does, so the dump is the only thing that separates them - which is
        # why it has to be a dump the dying process wrote, and not the one a
        # slow test left behind an hour earlier.
        return "NATIVE_CRASH", "medium", evidence

    if status is not None and 128 < status < 192:
        return "PROBABLY_SIGNALLED", "low", evidence + [
            f"exit code {status} is the 128+signal convention used by shells and "
            "container runtimes; the true signal was not passed through"
        ]

    if status is not None:
        # Zero included. A worker that left the run without being asked to has
        # gone wrong whatever number it exited with, and os._exit(0) inside a
        # test is a real way to do it - reported as UNKNOWN it reads as a
        # status nobody could obtain, which is the one thing this is not.
        return "SELF_EXIT", "medium", evidence + [
            "the worker exited on its own: something called sys.exit() or "
            "os._exit(), or a plugin aborted"
        ]

    # No status at all - a remote gateway, with no local process to ask.
    return "UNKNOWN", "low", evidence + memory_evidence(incident)
