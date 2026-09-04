"""Verdicts, from evidence rather than from the fact that something died.

Every branch says what it can prove. An exit status of -9 is identical for the
OOM killer, a cancelled CI job and a stray kill, so only something that
witnessed the kill licenses a verdict beyond "it was killed": the kernel log
naming the victim, the cgroup counter moving, the signal tracepoint naming the
sender, or the SIGTERM the controller was sent just before. Without any of
them the honest answer is still that it was killed - and the incident then
says which of those witnesses this machine withheld, and why, so the reader
knows what was denied rather than merely absent.
"""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING, Optional

from ..capture import crash_stack
from . import exit_status as status_table

if TYPE_CHECKING:  # importing it for real would close a cycle: death -> classify
    from ..incidents.death import WorkerDeathIncident
    from ..incidents.killer import SignalRecord

#: The verdicts that mean somebody outside the run stopped it, and a witness
#: saw who. No test is at fault, so no test is suspected.
DELIBERATE_STOPS = frozenset({"KILLED_BY_PROCESS", "KILLED_AFTER_SIGTERM"})


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


def oom_evidence(incident: WorkerDeathIncident) -> list[str]:
    """What the kernel itself printed when it chose this process."""
    oom = incident.oom
    if oom is None:
        return []
    where = {
        "CONSTRAINT_MEMCG": f"the limit of cgroup {oom.memcg or oom.task_memcg or '(unnamed)'}",
        "CONSTRAINT_NONE": "the machine's own memory",
        "CONSTRAINT_CPUSET": "a NUMA node's memory (cpuset)",
        "CONSTRAINT_MEMORY_POLICY": "a NUMA node's memory (memory policy)",
    }.get(oom.constraint or "", "a limit this kernel did not name")
    resident = f" at {oom.anon_rss_mb} MB anonymous resident" if oom.anon_rss_mb is not None else ""
    matched = (
        "matched by pid"
        if oom.matched_by == "pid"
        else "matched by cgroup and time - the pid the kernel printed belongs to another namespace"
    )
    lines = [
        f"the kernel log ({oom.source}) records the OOM killer choosing pid "
        f"{oom.victim_pid} ({oom.victim_comm}){resident}, having hit {where}; {matched}"
    ]
    if oom.tasks_considered:
        weighed = (
            f"it weighed {oom.tasks_considered} tasks holding {oom.tasks_rss_mb} MB; "
            f"{oom.run_tasks} of them were this run's, holding {oom.run_rss_mb} MB together"
        )
        if oom.victim_rank:
            weighed += f"; the victim was the {_ordinal(oom.victim_rank)} largest"
        lines.append(weighed)
        if oom.largest:
            lines.append(
                "largest: "
                + ", ".join(
                    f"{task.get('name')} pid {task.get('pid')}"
                    + (f" [{task['role']}]" if task.get("role") else "")
                    + f" {task.get('rss_mb')} MB"
                    for task in oom.largest
                )
            )
    if oom.pressure == "fleet":
        lines.append(
            f"fleet pressure: the victim was an ordinary member of the run (median "
            f"{oom.run_median_rss_mb} MB), so the run's {oom.run_tasks} processes exceeded "
            "the limit together - fewer workers or more memory, not one test"
        )
    elif oom.pressure == "own weight" and oom.run_median_rss_mb:
        ratio = (oom.anon_rss_mb or 0) / max(1, oom.run_median_rss_mb)
        lines.append(
            f"its own weight: {ratio:.1f}x the run's median of {oom.run_median_rss_mb} MB, "
            "so the test in flight is a fair suspect"
        )
    if oom.triggered_by_pid is not None:
        role = f", {oom.triggered_by_role}" if oom.triggered_by_role else ""
        lines.append(
            f"the kill was made in the context of {oom.triggered_by_comm} "
            f"(pid {oom.triggered_by_pid}{role}), whose allocation is what hit the limit"
        )
    return lines


def sources_consulted(incident: WorkerDeathIncident) -> list[str]:
    """Which witnesses this machine offered, on the verdicts that had none.

    Said only where it matters - a verdict that could not be reached - so the
    reader learns what was withheld and by what, rather than being left to
    wonder whether anything was asked at all.
    """
    sources = incident.kill_sources
    if sources is None:
        return []
    return [
        f"kill witnesses: kernel log {sources.kernel_log}; signal tracepoint "
        f"{sources.signal_trace}; controller SIGTERM witness {sources.controller_witness}"
    ]


def sender_evidence(incident: WorkerDeathIncident) -> list[str]:
    """Who sent the signal that ended it, when a witness saw."""
    killer = incident.killer
    if killer is None:
        return []
    if killer.origin == "self":
        return [f"{killer.name} was sent by the worker to itself (os.kill from inside the process)"]
    if killer.origin == "kernel":
        return [
            f"{killer.name} came from the kernel itself (si_code SI_KERNEL), generated in the "
            f"context of {killer.who()}"
        ]
    return [f"{killer.name} was sent by {killer.who()}"]


def _latest_term(incident: WorkerDeathIncident) -> Optional[SignalRecord]:
    term = getattr(signal, "SIGTERM", None)
    for record in reversed(incident.signals_before_death):
        if record.signal == term:
            return record
    return None


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _written_ago(age: float | None) -> str:
    """A stack is evidence about a moment, so say which moment."""
    if age is None:
        return ""
    return f", {age:.0f}s before this report" if age >= 1 else ", moments before"


def _timeout_line(incident: WorkerDeathIncident) -> str:
    where = f" in the {incident.phase} phase" if incident.phase else ""
    return (
        f"the test had been running {incident.test_seconds}s{where} when the worker "
        f"died, at or beyond the configured {incident.timeout_source} of "
        f"{incident.matched_timeout}s: a timeout enforcer ended it"
    )


def _killed(incident: WorkerDeathIncident, evidence: list[str]) -> tuple[str, str, list[str]]:
    """SIGKILL, and everything that can be said about who sent it.

    In order of how much each witness proves. The kernel log names the
    victim outright; the cgroup counter says the OOM killer took something in
    the cgroup at the time; the tracepoint names a sender; the controller's
    own SIGTERM says the run was being stopped. Each verdict below is licensed
    by exactly one of those, and the last one by none.
    """
    if incident.oom is not None:
        return "OOM_KILLED", "high", evidence + oom_evidence(incident)
    if incident.cgroup_oom_kills_since_start:
        return "OOM_KILLED", "high", evidence
    killer = incident.killer
    if killer is not None:
        if killer.origin == "kernel":
            return "KILLED_BY_KERNEL", "medium", evidence + [
                "SIGKILL came from the kernel itself (si_code SI_KERNEL), not from any "
                "process: the OOM killer, whose log this run could not read, or a cgroup "
                f"kill; it was generated in the context of {killer.who()}"
            ] + sources_consulted(incident)
        if killer.origin == "self":
            return "SELF_KILLED", "high", evidence + sender_evidence(incident)
        if killer.sender_role and killer.sender_role != "outside this run":
            return "KILLED_BY_RUN", "high", evidence + [
                f"SIGKILL was sent by {killer.who()}"
            ] + (
                ["execnet kills a worker that has not exited within its timeout, which is "
                 "what a SIGKILL from the controller usually is"]
                if killer.sender_role == "this run's controller"
                else []
            )
        return "KILLED_BY_PROCESS", "high", evidence + [
            f"SIGKILL was sent by {killer.who()}: something outside this run stopped it - "
            "a job cancellation, a timeout enforcer, an orchestrator, or a hand on the keyboard"
        ]
    term = _latest_term(incident)
    if term is not None:
        ago = (
            f"{term.seconds_before_death:.0f}s before this kill"
            if term.seconds_before_death is not None
            else "shortly before this kill"
        )
        return "KILLED_AFTER_SIGTERM", "medium", evidence + [
            f"{term.target} received SIGTERM from {term.who()} {ago}: the run was being "
            "stopped, and SIGKILL is what follows a SIGTERM that is not answered in time"
        ]
    return "SIGKILLED", "medium", evidence + [
        "SIGKILL with no cgroup OOM event: a host-level OOM killer, a "
        "container or CI cancellation, or an external kill"
    ] + sources_consulted(incident)


def _terminated(incident: WorkerDeathIncident, evidence: list[str]) -> tuple[str, str, list[str]]:
    """A Windows ``TerminateProcess``, with the caller the audit event named."""
    killer = incident.killer
    assert killer is not None
    code = f" with exit code {killer.exit_code}" if killer.exit_code is not None else ""
    if killer.origin == "self":
        return "SELF_KILLED", "high", evidence + [
            f"the worker called TerminateProcess on itself{code}"
        ]
    if killer.sender_role and killer.sender_role != "outside this run":
        return "KILLED_BY_RUN", "high", evidence + [
            f"TerminateProcess was called on it by {killer.who()}{code}"
        ] + (
            ["execnet terminates a worker that has not exited within its timeout, which "
             "is what a termination from the controller usually is"]
            if killer.sender_role == "this run's controller"
            else []
        )
    return "KILLED_BY_PROCESS", "high", evidence + [
        f"TerminateProcess was called on it by {killer.who()}{code}: something outside "
        "this run stopped it - a job cancellation, taskkill, an orchestrator, or a hand on "
        "the keyboard"
    ]


def _output_evidence(incident: WorkerDeathIncident) -> list[str]:
    """The last non-empty line of the worker's stderr, when it was kept.

    One line, because a crash's cause is a line - and the last one, because a
    library that logs before it aborts puts the reason nearest the end. The
    whole tail is on ``incident.recent_output`` for a reader who wants more.
    """
    lines = [line.strip() for line in incident.recent_output if line.strip()]
    return [f"last stderr: {lines[-1]}"] if lines else []


def of(incident: WorkerDeathIncident) -> tuple[str, str, list[str]]:
    """Returns (verdict, confidence, evidence)."""
    status = incident.exit_status
    evidence: list[str] = []

    if incident.worker == "controller" and incident.recovered_from_run:
        # It runs no tests, so neither of the phrasings below is about it.
        evidence.append(
            "the controller ended without reaching session finish; its workers "
            "were left to finish on their own"
        )
    elif incident.test_in_flight:
        phase = f" ({incident.phase})" if incident.phase else ""
        evidence.append(f"died while running {incident.test_in_flight}{phase}")
    elif incident.tests_finished:
        # Not "died in" - the last test had already finished, and saying
        # otherwise puts a passing test's name on a death it had no part in.
        after = (
            f", the last of them {incident.last_test}" if incident.last_test else ""
        )
        evidence.append(
            f"died between tests, after finishing {incident.tests_finished}{after}"
        )
    else:
        evidence.append("died before running any test (startup or collection)")

    if status is not None:
        evidence.append(
            f"exit status {status} - {status_table.describe(status)} "
            f"(pid {incident.worker_pid}, via {incident.exit_status_source})"
        )
    elif incident.recovered_from_run and incident.killer is None and incident.oom is None:
        # Two ways to have no status and they have different remedies, so they
        # are not allowed to read the same. This one is not a gateway that
        # could not be asked: it is a process whose parent was the run that
        # died, found afterwards by somebody who was never entitled to its
        # status. Nothing can recover it, which is worth saying plainly rather
        # than leaving a reader to look for a configuration that would - and
        # said only when no witness stands in for it, because "cannot be told
        # apart" beside a line that tells them apart is a contradiction.
        evidence.append(
            f"exit status unavailable (pid {incident.worker_pid}): nothing was "
            "left to read it. Only a parent may, the parent was the run that "
            "died, and by the time this evidence was found the process was "
            "gone - so an OOM kill, a segfault and an os._exit cannot be told "
            "apart here"
        )
    elif incident.recovered_from_run:
        evidence.append(
            f"exit status unavailable (pid {incident.worker_pid}): the parent was the run "
            "that died; what follows is from a witness instead"
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

    terminated = incident.killer is not None and incident.killer.name == "TerminateProcess"
    output = _output_evidence(incident)
    received = -status if status is not None and status < 0 else None
    if received is None and status is None and incident.killer is not None and not terminated:
        # No status to read - but a witness saw the signal that ended it,
        # which for a run found dead afterwards is the one thing that can
        # stand in for the number nobody was left to read.
        received = incident.killer.signal
        evidence.append(
            f"no exit status, but the kernel's signal trace saw "
            f"{incident.killer.name} sent to pid {incident.worker_pid}"
        )

    if received is not None:
        if hasattr(signal, "SIGKILL") and received == signal.SIGKILL:
            return _killed(incident, evidence + memory_evidence(incident) + output)
        if received in status_table.FATAL_SIGNALS:
            return "NATIVE_CRASH", "high", evidence + [
                status_table.FATAL_SIGNALS[received]
            ] + output + sender_evidence(incident)
        if hasattr(signal, "SIGALRM") and received == signal.SIGALRM and incident.matched_timeout:
            # pytest-timeout's signal method raises SIGALRM at the deadline.
            return "TIMED_OUT", "high", evidence + [_timeout_line(incident)]
        if received in status_table.DELIBERATE_SIGNALS:
            return f"SIGNAL_{received}", "high", evidence + [
                status_table.DELIBERATE_SIGNALS[received],
            ] + sender_evidence(incident) + [
                "nothing to triage unless the run was not meant to be stopped",
            ]
        return f"SIGNAL_{received}", "medium", evidence + sender_evidence(incident)

    if status in status_table.WINDOWS_STATUS:
        verdict, description = status_table.WINDOWS_STATUS[status]
        return verdict, "high", evidence + [description]

    if terminated:
        # After the NTSTATUS table on purpose: a fault ends a process through
        # the same kernel call, and a crash the exit code already names must
        # not be re-described as somebody's TerminateProcess.
        return _terminated(incident, evidence)

    if fatal_dump:
        # On Windows abort() exits with 3, exactly as a deliberate os._exit(3)
        # does, so the dump is the only thing that separates them - which is
        # why it has to be a dump the dying process wrote, and not the one a
        # slow test left behind an hour earlier.
        return "NATIVE_CRASH", "medium", evidence + output

    if status is not None and 128 < status < 192:
        return "PROBABLY_SIGNALLED", "low", evidence + [
            f"exit code {status} is the 128+signal convention used by shells and "
            "container runtimes; the true signal was not passed through"
        ]

    if status is not None:
        if incident.matched_timeout is not None and incident.phase and status in (1, -1):
            # pytest-timeout's thread method and faulthandler's
            # exit_on_timeout both os._exit(1) a hung worker, which is
            # otherwise indistinguishable from any other self-exit. The test
            # having reached a configured timeout is what tells them apart.
            #
            # A phase has to be open for that reading to mean anything: an
            # enforcer ends a test that is *running*, so a worker that died
            # with nothing in flight did not reach a timeout however long the
            # clock says. The recorder clears the clocks with the test for
            # this reason; the guard is here because a verdict of TIMED_OUT
            # names a test and blames its owner, and must not rest on one
            # slot being cleared somewhere else.
            return "TIMED_OUT", "high", evidence + [_timeout_line(incident)]
        # Zero included. A worker that left the run without being asked to has
        # gone wrong whatever number it exited with, and os._exit(0) inside a
        # test is a real way to do it - reported as UNKNOWN it reads as a
        # status nobody could obtain, which is the one thing this is not.
        exited = [
            "the worker exited on its own: something called sys.exit() or "
            "os._exit(), or a plugin aborted"
        ]
        if incident.test_seconds is not None and incident.phase:
            exited.append(
                f"the test in flight had been running {incident.test_seconds}s in the "
                f"{incident.phase} phase"
            )
        return "SELF_EXIT", "medium", evidence + exited

    # No status at all - a remote gateway, or a run found after the fact with
    # nothing left that was entitled to read one. The kernel log is the one
    # witness that still answers for a run found afterwards: it is
    # machine-wide and timestamped, and a kill in the dead run's window is
    # the dead run's.
    if incident.oom is not None:
        return "OOM_KILLED", "high", evidence + memory_evidence(incident) + oom_evidence(incident)
    term = _latest_term(incident)
    if term is not None and incident.recovered_from_run:
        ago = (
            f"{term.seconds_before_death:.0f}s before its last heartbeat"
            if term.seconds_before_death is not None
            else "shortly before its last heartbeat"
        )
        return "RUN_STOPPED", "medium", evidence + memory_evidence(incident) + [
            f"{term.target} received SIGTERM from {term.who()} {ago}: the run was told to "
            "stop and this process ended with it rather than on its own"
        ]
    return "UNKNOWN", "low", evidence + memory_evidence(incident) + sources_consulted(incident)
