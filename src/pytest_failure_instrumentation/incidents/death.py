"""A worker process that ended when it should not have.

xdist reports this as ``node down: Not properly terminated`` - a placeholder it
substitutes when the channel closed without the remote sending anything. The
cause never left the dead process, so everything below is read from what the
worker wrote before it died, plus the exit status its parent can still be asked
for.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import ConfigDict, Field

from .. import probes
from ..analysis import classify, exit_status
from ..capture import crash_stack
from ..capture import events as event_log
from ..capture.state import read_state
from .base import CgroupMemory, Incident


class WorkerDeathIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    # xdist starts a replacement worker and the run continues, so a death
    # costs the session one worker rather than ending it.
    ends_run: ClassVar[bool] = False

    kind: Literal["worker_death"] = "worker_death"

    #: What xdist itself said, kept verbatim so its wording can be matched
    #: against the far more specific verdict beside it.
    xdist_error: str = ""
    worker_pid: Optional[int] = None

    exit_status: Optional[int] = None
    exit_status_kind: Optional[str] = None
    exit_status_source: Optional[str] = None
    exit_status_meaning: str = "unknown"

    test_in_flight: Optional[str] = None
    phase: Optional[str] = None
    tests_started: int = 0
    tests_finished: int = 0

    rss_mb_at_death: Optional[int] = None
    system_available_mb: Optional[int] = None
    cgroup: Optional[CgroupMemory] = None
    cgroup_oom_kills: Optional[int] = None
    cgroup_oom_kills_since_start: Optional[int] = None
    high_water: Optional[list[dict[str, Any]]] = None

    crash_stack: list[str] = Field(default_factory=list)
    #: How long before this report the dump on file was written. Around zero
    #: for a fatal dump, which is written as the process dies. Large for
    #: anything else on file, which is the case worth saying out loud.
    crash_stack_age_seconds: Optional[float] = None

    def stack_lines(self) -> tuple[list[str], bool]:
        """Frames to attribute the death to - only from a dump that belongs
        to it.

        A dump without a fatal banner was written by a process that went on
        living: an on-demand stack taken while the worker was merely stalled,
        say. Blaming the death on it names whatever that stack happened to be
        doing, which can be a different test in a different module that passed
        - and if that module is the product's, an unrelated clean exit pages
        somebody at severity=critical.
        """
        if not crash_stack.is_fatal(self.crash_stack):
            return [], False
        return self.crash_stack, False  # faulthandler prints deepest first

    def suspect_nodeid(self) -> str | None:
        return self.test_in_flight

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict, str(self.exit_status)]

    def details(self) -> list[str]:
        counted = f"started={self.tests_started} finished={self.tests_finished}"
        if self.test_in_flight:
            phase = f"  phase={self.phase}" if self.phase else ""
            return [f"in flight {self.test_in_flight}{phase}  {counted}"]
        return [f"no test in flight  {counted}"]


def build(
    node: Any,
    error: object,
    directory: Path,
    baseline_oom_kills: int | None,
) -> WorkerDeathIncident:
    worker = node.gateway.id
    crash_file = directory / f"{worker}.crash"
    dump = crash_stack.read(crash_file, limit=40)
    events = event_log.read_events(directory / f"{worker}.events")
    state = read_state(directory / f"{worker}.state")
    pid = state.get("pid") or event_log.worker_pid(events)
    popen = getattr(getattr(node.gateway, "_io", None), "popen", None)
    status, status_kind, source = probes.exit_status(pid, popen)
    oom_kills = probes.cgroup_oom_kills()
    beats = event_log.heartbeats(events)
    cgroup = probes.cgroup_memory()

    incident = WorkerDeathIncident(
        worker=worker,
        worker_pid=pid,
        xdist_error=str(error),
        exit_status=status,
        exit_status_kind=status_kind,
        exit_status_source=source,
        exit_status_meaning=exit_status.describe(status),
        test_in_flight=state.get("nodeid"),
        phase=state.get("phase"),
        tests_started=state.get("tests_started") or 0,
        tests_finished=state.get("tests_finished") or 0,
        rss_mb_at_death=beats[-1].get("rss_mb") if beats else None,
        system_available_mb=_last_available(events),
        cgroup=CgroupMemory(**cgroup) if cgroup else None,
        cgroup_oom_kills=oom_kills,
        cgroup_oom_kills_since_start=_delta(oom_kills, baseline_oom_kills),
        crash_stack=dump,
        # Only when there is something to date. The file is created empty at
        # worker start, so its mtime answers even when no dump was ever
        # written - and an age attached to nothing reads as a stack the
        # reader cannot find.
        crash_stack_age_seconds=_age_of(crash_file) if dump else None,
        high_water=event_log.high_water_marks(events)[-1:] or None,
    )
    incident.verdict, incident.confidence, incident.evidence = classify.of(incident)
    return incident


def _age_of(path: Path) -> float | None:
    written = crash_stack.written_at(path)
    return None if written is None else round(max(0.0, time.time() - written), 1)


def _last_available(events: list[dict[str, Any]]) -> int | None:
    for event in reversed(events):
        if event.get("system_available_mb") is not None:
            return int(event["system_available_mb"])
    return None


def _delta(current: int | None, baseline: int | None) -> int | None:
    if current is None or baseline is None:
        return None
    return current - baseline
