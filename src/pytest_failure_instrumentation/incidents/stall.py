"""A worker that stopped reporting but is still alive.

This is the one kind no hook fires for. ``pytest_testnodedown`` needs a dead
process and ``pytest_internalerror`` needs an exception; a wedged worker
produces neither, and the run simply never ends. So it is polled, and the
polling is the source.

Silence on its own proves nothing - the controller only hears from a worker
when a phase completes, so a twenty-minute test and a deadlock look identical
from outside. The verdict comes from passive evidence only (see
``analysis.stall``); a stack is asked for afterwards, once the decision is
already made, because the asking can perturb what it measures.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import ClassVar, Literal, Optional

from pydantic import ConfigDict, Field

from .. import probes
from ..analysis import stall as assessment
from ..capture import crash_stack
from ..capture import events as event_log
from ..capture.state import read_state
from .base import Incident

#: How long to wait for a signalled worker to write its stack.
STACK_WAIT_SECONDS = 2.0
STACK_POLL_SECONDS = 0.1
STACK_LINES = 14

CONFIDENCE = {"BLOCKED": "high", "FROZEN": "high", "SILENT": "low"}


class WorkerStallIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    # The run cannot finish while a worker is wedged: xdist waits for work it
    # handed out and never gets back.
    ends_run: ClassVar[bool] = True

    kind: Literal["worker_stall"] = "worker_stall"

    #: BLOCKED, FROZEN or SILENT - see analysis.stall.
    state: str = "SILENT"
    reason: str = ""
    silent_for_seconds: float = 0.0
    #: Cores' worth of CPU across the sampled window. None when it could not
    #: be measured, which is not the same as zero.
    cpu_rate: Optional[float] = None
    heartbeat_age_seconds: Optional[float] = None
    worker_pid: Optional[int] = None

    test_in_flight: Optional[str] = None
    phase: Optional[str] = None

    stack: list[str] = Field(default_factory=list)
    #: Whether a stack was asked for at all. False on Windows, and whenever
    #: failure_stack_probe is off - absence of a stack is a platform fact, not
    #: a finding about the worker.
    stack_probed: bool = False
    #: Why no stack was asked for, when none was. The three reasons are not
    #: interchangeable, and telling a Linux user their platform cannot do
    #: something they turned off themselves is worse than saying nothing.
    stack_unavailable_reason: Optional[str] = None
    #: How old the stack was when it was read. A probed stack is taken now; a
    #: watchdog dump can be most of ``failure_slow_test_seconds`` old, and a
    #: reader cannot tell the two apart from the frames alone.
    stack_age_seconds: Optional[float] = None

    def raw_stack(self) -> list[str]:
        return self.stack

    def blame_stack(self) -> tuple[list[str], bool]:
        return self.stack, False  # faulthandler prints deepest first

    def suspect_nodeid(self) -> Optional[str]:
        return self.test_in_flight

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict, self.state]

    def details(self) -> list[str]:
        where = self.test_in_flight or "no test in flight"
        phase = f" ({self.phase})" if self.phase else ""
        line = f"silent for {self.silent_for_seconds:.0f}s in {where}{phase}"
        if self.cpu_rate is not None:
            line += f"  cpu={self.cpu_rate:.2f} cores"
        if self.heartbeat_age_seconds is not None:
            line += f"  last beat {self.heartbeat_age_seconds:.0f}s ago"
        lines = [line]
        if self.reason:
            lines.append(self.reason)
        if not self.stack and self.stack_unavailable_reason:
            lines.append(f"no stack: {self.stack_unavailable_reason}")
        elif self.stack and not self.stack_probed and self.stack_age_seconds is not None:
            # A stack nobody asked for was left by the slow-test watchdog while
            # the run went on around it. Reading it as a picture of now is the
            # mistake to head off, so it says where it came from as well as
            # when - a fresh one from the watchdog is still not a probe.
            lines.append(
                f"stack written {self.stack_age_seconds:.0f}s ago by the "
                "slow-test watchdog, not taken just now"
            )
        return lines


def build(
    worker: str,
    directory: Path,
    silent_for: float,
    interval: float,
    stack_probe: bool,
) -> Optional[WorkerStallIncident]:
    """Assess a silent worker. Returns None when it is merely slow.

    Runs on the stall watcher's own thread, which is why it may sleep: the
    second pass is what separates a missed beat from a frozen process.
    """
    path = directory / f"{worker}.events"
    events = event_log.read_events(path)
    beats = event_log.heartbeats(events)
    verdict = assessment.assess(beats, time.time(), silent_for, interval)

    if verdict.needs_confirmation:
        previous = assessment.last_beat_time(beats)
        time.sleep(interval * 1.2)
        beats = event_log.heartbeats(event_log.read_events(path))
        verdict = assessment.confirm(beats, previous, time.time(), silent_for)

    if verdict.state is None:
        return None  # burning CPU: slow, not stuck. Say nothing and re-arm.

    state = read_state(directory / f"{worker}.state")
    pid = state.get("pid") or event_log.worker_pid(events)
    stack, probed, why, written = _stack(directory, worker, pid, stack_probe)

    return WorkerStallIncident(
        worker=worker,
        verdict=f"STALLED_{verdict.state}",
        # The assessment overrides only when it reached the state on weaker
        # evidence than the state normally implies.
        confidence=verdict.confidence or CONFIDENCE.get(verdict.state, "low"),
        state=verdict.state,
        reason=verdict.reason,
        silent_for_seconds=round(silent_for, 1),
        cpu_rate=round(verdict.cpu_rate, 3) if verdict.cpu_rate is not None else None,
        heartbeat_age_seconds=(
            round(verdict.heartbeat_age, 1) if verdict.heartbeat_age is not None else None
        ),
        worker_pid=pid,
        test_in_flight=state.get("nodeid"),
        phase=state.get("phase"),
        stack=stack,
        stack_probed=probed,
        stack_unavailable_reason=why,
        stack_age_seconds=(
            round(max(0.0, time.time() - written), 1) if written is not None else None
        ),
        evidence=[
            f"no report from {worker} for {silent_for:.0f}s while the run continued",
            verdict.reason,
        ],
    )


def _stack(
    directory: Path, worker: str, pid: Optional[int], allowed: bool
) -> tuple[list[str], bool, Optional[str], Optional[float]]:
    """Ask the worker for a stack, and read only what it added.

    Returns the stack, whether anything was asked for, why not when nothing
    was, and when the stack it found was written. A test that outlived
    ``failure_slow_test_seconds`` has already dumped one unprompted into its
    own file, so a stack may be here without anything having been signalled -
    and that one describes whenever the watchdog last fired rather than now.
    """
    crash_file = directory / f"{worker}.crash"
    try:
        before = crash_file.stat().st_size
    except OSError:
        before = 0

    why = _cannot_probe(pid, allowed)
    if why is not None or pid is None:
        # Whatever the worker wrote on its own is still evidence.
        stack, written = _passive_stack(directory, worker)
        return stack, False, why, written
    if not probes.request_stack(pid):
        stack, written = _passive_stack(directory, worker)
        return (
            stack,
            False,
            "the worker could not be signalled; it may already be gone",
            written,
        )

    deadline = time.time() + STACK_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(STACK_POLL_SECONDS)
        try:
            if crash_file.stat().st_size > before:
                return (
                    crash_stack.read(crash_file, limit=STACK_LINES, offset=before),
                    True,
                    None,
                    time.time(),  # it was written in answer to the signal
                )
        except OSError:
            break
    fresh = crash_stack.read(crash_file, limit=STACK_LINES)
    if fresh:
        return fresh, True, None, crash_stack.written_at(crash_file)
    stack, written = _passive_stack(directory, worker)
    return (
        stack,
        True,
        None if stack else "the worker was signalled but wrote nothing back",
        written,
    )


def _cannot_probe(pid: Optional[int], allowed: bool) -> Optional[str]:
    """Why a live stack cannot be asked for, or None when it can."""
    if not allowed:
        return "failure_stack_probe is off, so the worker was left undisturbed"
    if not probes.can_request_stack():
        return "this platform cannot ask a live process for one"
    if not pid:
        return "the worker's pid was never recorded, so there is nothing to ask"
    return None


def _passive_stack(directory: Path, worker: str) -> tuple[list[str], Optional[float]]:
    """Whatever the worker dumped on its own, without being asked, and when.

    The slow-test watchdog's file first: a wedged test outlives the timeout, so
    that is where a stalled worker's stack normally is. The crash file is the
    fallback, and on Windows the watchdog is the only source there is.
    """
    for suffix in (".slow", ".crash"):
        path = directory / f"{worker}{suffix}"
        lines = crash_stack.read(path, limit=STACK_LINES)
        if lines:
            return lines, crash_stack.written_at(path)
    return [], None
