"""A worker process that ended when it should not have.

xdist reports this as ``node down: Not properly terminated`` - a placeholder it
substitutes when the channel closed without the remote sending anything. The
cause never left the dead process, so everything below is read from what the
worker wrote before it died, plus the exit status its parent can still be asked
for.
"""

from __future__ import annotations

import signal
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

#: The signals faulthandler installs a handler for, and therefore the deaths
#: that are expected to leave a dump behind. A worker killed by anything else -
#: SIGKILL above all, which cannot be caught - never wrote one, and waiting for
#: it would cost every OOM-killed worker a delay for a file that is not coming.
DUMPING_SIGNALS = frozenset(
    number
    for number in (
        getattr(signal, name, None)
        for name in ("SIGSEGV", "SIGFPE", "SIGABRT", "SIGBUS", "SIGILL")
    )
    if number is not None
)

#: How long to wait for the dump of a worker that died in a way that writes
#: one. The dying process writes it before it exits, so it is normally on disk
#: already; this covers only the window where the controller notices the death
#: first.
FATAL_DUMP_WAIT_SECONDS = 1.0
FATAL_DUMP_POLL_SECONDS = 0.05


def _expects_a_dump(status: Optional[int]) -> bool:
    """Whether this death is one that writes a dump on its way out.

    Decided from the status rather than from the kind string, because the kind
    is not one value. ``waitid`` answers ``killed`` normally and
    ``killed-core-dumped`` when core dumps are enabled - which is the *usual*
    case for a real SIGSEGV, so keying on ``killed`` alone skipped the wait for
    exactly the deaths it exists for. And the path checked before either of
    them, ``popen.returncode``, reports no kind at all.

    A negative status is the POSIX convention for "killed by signal N" and is
    the one thing all three paths agree on. Windows statuses are normalised to
    unsigned, so they never match here and never wait.
    """
    return status is not None and status < 0 and abs(status) in DUMPING_SIGNALS


def _crash_dump(path: Path, status: Optional[int]) -> list[str]:
    """The dump that describes the death, waiting for it if it is still landing.

    The crash file accumulates, and an on-demand stack taken while the worker
    was merely stalled has no banner at all - so if the fatal dump has not been
    written yet, the newest thing in the file is the *probe* stack, and reading
    it there reports the frames from before the crash as the frames of the
    crash. The verdict still says NATIVE_CRASH, because that comes from the
    exit status: a confident wrong answer of exactly the kind this package
    exists to prevent.

    That window is real rather than theoretical. It is widest when the stall
    probe is what perturbed the worker into crashing - the signal returns a
    blocked C call early - because then the two dumps are microseconds apart
    instead of minutes.

    Waited for only when the exit status says a dump is coming. Everything else
    reads once and moves on.
    """
    dump = crash_stack.read(path, limit=40)
    if crash_stack.is_fatal(dump) or not _expects_a_dump(status):
        return dump
    deadline = time.monotonic() + FATAL_DUMP_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(FATAL_DUMP_POLL_SECONDS)
        landed = crash_stack.read(path, limit=40)
        if crash_stack.is_fatal(landed):
            return landed
    # It never arrived. The probe stack is still evidence and is still
    # returned - is_fatal() is what tells a reader it is not the death stack,
    # and dropping it would trade a labelled stack for none at all.
    return dump



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
    #: The most recent test this worker ran, whether or not it finished. A
    #: worker that died in the gap between two tests died *after* this one,
    #: not in it - which is why it is not ``test_in_flight``, and why the
    #: attribution below says which of the two it is working from.
    last_test: Optional[str] = None
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

    def raw_stack(self) -> list[str]:
        return self.crash_stack

    def blame_stack(self) -> tuple[list[str], bool]:
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
        return self.test_in_flight or self.last_test

    def suspect_basis_for(self, path: str) -> str:
        if self.test_in_flight:
            return f"owner of the test in flight ({path})"
        return (
            f"owner of the last test this worker finished ({path}); the worker "
            "died between tests, so no test was running"
        )

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict, str(self.exit_status)]

    def details(self) -> list[str]:
        counted = f"started={self.tests_started} finished={self.tests_finished}"
        if self.test_in_flight:
            phase = f"  phase={self.phase}" if self.phase else ""
            return [f"in flight {self.test_in_flight}{phase}  {counted}"]
        if self.last_test:
            return [f"no test in flight; last was {self.last_test}  {counted}"]
        return [f"no test in flight  {counted}"]


def build(
    node: Any,
    error: object,
    directory: Path,
    baseline_oom_kills: int | None,
    run_id: str | None = None,
) -> WorkerDeathIncident:
    worker = node.gateway.id
    crash_file = directory / f"{worker}.crash"
    events = event_log.read_events(directory / f"{worker}.events")
    # The run id keeps a record an earlier run left behind - one this run could
    # not delete, which on Windows is any file somebody still had open - from
    # being read as this worker's last moments.
    state = read_state(directory / f"{worker}.state", run_id)
    pid = state.get("pid") or event_log.worker_pid(events)
    popen = getattr(getattr(node.gateway, "_io", None), "popen", None)
    # Read before the dump, because it decides whether a dump is still coming.
    status, status_kind, source = probes.exit_status(pid, popen)
    dump = _crash_dump(crash_file, status)
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
        last_test=state.get("last_nodeid"),
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
