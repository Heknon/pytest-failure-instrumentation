"""A process that ended when it should not have.

Usually a worker, and then xdist reports it as ``node down: Not properly
terminated`` - a placeholder it substitutes when the channel closed without the
remote sending anything. The cause never left the dead process, so everything
below is read from what it wrote before it died, plus the exit status its
parent can still be asked for.

:func:`recover` builds the same incident for a process nobody was watching:
the one process of a run with no workers, or a whole run reclaimed with its
controller. There is no parent left to ask and no hook that fires, so it is
raised by a later run reading the directory - see :mod:`.leftovers`. What
differs is one fact and it is stated rather than papered over: no exit status
was obtainable, so the number that separates an OOM kill from a segfault was
never anybody's to read.
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
from ..probes import signal_trace
from . import killer
from .base import CgroupMemory, Incident
from .killer import KillSources, OomKillRecord, SignalRecord

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

    #: Which run this incident is *about*, when that is not the run raising
    #: it. Set only for a death recovered from a directory somebody else left
    #: behind, and the first thing the alert says - a reader who takes this
    #: for the current run's crash goes looking for a failure that is not
    #: there. ``run_id`` is the dead run's too; ``raised_at`` is now.
    recovered_from_run: Optional[str] = None
    #: When that run was last known to be running, from its final heartbeat.
    #: The death is somewhere after this and before it was found, and nothing
    #: on either side can narrow it further.
    last_seen_at: Optional[float] = None

    crash_stack: list[str] = Field(default_factory=list)
    #: How long before this report the dump on file was written. Around zero
    #: for a fatal dump, which is written as the process dies. Large for
    #: anything else on file, which is the case worth saying out loud.
    crash_stack_age_seconds: Optional[float] = None

    #: Who sent the signal that ended it, when a witness saw - see
    #: :mod:`.killer`. The kernel's tracepoint names a sender for every
    #: SIGKILL, which the wait status never will.
    killer: Optional[SignalRecord] = None
    #: The kernel's own record of choosing this process, when its log held
    #: one: the constraint, the cgroup, and the table of every task it weighed.
    oom: Optional[OomKillRecord] = None
    #: Signals to this worker or to the controller shortly before the death -
    #: above all the SIGTERM an orchestrator sends before the SIGKILL it will
    #: not otherwise explain.
    signals_before_death: list[SignalRecord] = Field(default_factory=list)
    #: What each witness said about itself on this machine, so a verdict that
    #: reached none of them says which was withheld and by what.
    kill_sources: Optional[KillSources] = None

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
        if self.verdict in classify.DELIBERATE_STOPS:
            # Somebody outside the run stopped it, and a witness saw who. The
            # test that happened to be running is not a lead; naming it would
            # put an owner on a cancellation.
            return None
        return self.test_in_flight or self.last_test

    def suspect_basis_for(self, path: str) -> str:
        if self.test_in_flight:
            return f"owner of the test in flight ({path})"
        return (
            f"owner of the last test this worker finished ({path}); the worker "
            "died between tests, so no test was running"
        )

    def fingerprint_parts(self) -> list[str]:
        parts = [self.kind, self.verdict, str(self.exit_status)]
        if self.killer is not None and self.killer.origin == "process":
            # Killed by whom is what recurs: every kill from the same runner
            # binary is one incident, whichever test it landed on.
            parts.append(self.killer.sender_comm or "")
        return parts

    def details(self) -> list[str]:
        counted = f"started={self.tests_started} finished={self.tests_finished}"
        if self.recovered_from_run:
            # First, and on a line of its own, because everything after it
            # describes a run that is already over.
            return [
                f"recovered from {self.recovered_from_run}, which ended without "
                "reaching session finish"
            ] + self._where(counted)
        return self._where(counted)

    def _where(self, counted: str) -> list[str]:
        """Which worker, and what it was doing.

        The worker leads, because it is the one fact the reader already has
        from xdist's own line - ``[gw7] node down: Not properly terminated`` -
        and the one that ties this alert to it. A replacement worker arrives
        under a new id, so a run that lost two is two alerts that differ in
        nothing else, and a reader who cannot see the id cannot tell which
        ``<worker>.crash`` on the runner holds the rest of the dump.
        """
        who = f"worker={self.worker}"
        if self.test_in_flight:
            phase = f"  phase={self.phase}" if self.phase else ""
            return [f"{who}  in flight {self.test_in_flight}{phase}  {counted}"]
        if self.last_test:
            return [f"{who}  no test in flight; last was {self.last_test}  {counted}"]
        return [f"{who}  no test in flight  {counted}"]


def build(
    node: Any,
    error: object,
    directory: Path,
    baseline_oom_kills: int | None,
    run_id: str | None = None,
    sources: Optional[killer.Sources] = None,
) -> WorkerDeathIncident:
    died_at = time.time()
    worker = node.gateway.id
    crash_file = directory / f"{worker}.crash"
    events = event_log.this_run(
        event_log.read_events(directory / f"{worker}.events"), run_id
    )
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
    if sources is not None:
        _attach_witnesses(
            incident, sources, pid, status, event_log.started_at(events), died_at
        )
    incident.verdict, incident.confidence, incident.evidence = classify.of(incident)
    return incident


def _attach_witnesses(
    incident: WorkerDeathIncident,
    sources: killer.Sources,
    pid: Optional[int],
    status: Optional[int],
    started_at: Optional[float],
    died_at: float,
) -> None:
    """Ask every witness, and never let the asking cost the incident.

    Attribution is extra evidence on top of a death already established. A
    kernel log that cannot be parsed or a trace file that is half-written
    must degrade to "this source failed", written on the incident, and not
    to a degraded incident with nothing else on it.
    """
    try:
        found = killer.attribute(
            sources, pid=pid, exit_status=status, started_at=started_at, died_at=died_at
        )
    except Exception as failure:  # noqa: BLE001 - see the docstring
        incident.kill_sources = KillSources(
            kernel_log=f"failed ({failure!r})",
            signal_trace=sources.trace_status,
            controller_witness=sources.witness_status,
        )
        return
    incident.killer = found.killer
    incident.oom = found.oom
    incident.signals_before_death = found.before
    incident.kill_sources = found.sources


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


def recover(
    events_path: Path, session: str, elevate: bool = False
) -> Optional[WorkerDeathIncident]:
    """The death of a process nobody was watching, from what it left on disk.

    ``events_path`` is one process's event log inside a run directory that is
    over and never reached session finish - see :mod:`.leftovers` for how that
    set is arrived at. Returns None when this file describes no death: a
    process that wrote ``worker_finish`` ended its session cleanly, and one
    that never wrote ``worker_start`` never had a session to end.

    The difference from :func:`build` is the exit status, and it is a real
    loss rather than a formatting one. Only a parent may read a child's
    status, the parent here was the run that died, and the process is long
    gone - so ``-9``, ``-11`` and ``os._exit(1)`` cannot be told apart from
    the outside. What is left still separates most of them: the state slot
    says which test was in flight and in which phase, the beats say how much
    memory it was using and whether it was burning CPU, and a fatal dump - if
    this run kept one - names the frame.

    Nothing about *this* machine's present is attached to it. The cgroup
    figures and the OOM counter describe the moment they are read, and reading
    them now would put this run's memory pressure on a death that happened an
    hour ago, under a verdict that reads as a finding. The witnesses are the
    exception, because they are records of moments rather than readings of
    now: the dead run's own trace file and controller log, and the kernel log,
    which is machine-wide and timestamped - a kill in the dead run's window
    is the dead run's. ``elevate`` is the *reporting* run's permission to
    spend sudo on that log.
    """
    worker = events_path.stem
    directory = events_path.parent
    events = event_log.read_events(events_path)
    if not events:
        return None
    # The file is opened truncated by whoever writes it, so it holds one
    # process's run - but a second session pointed at the same directory by
    # PYTEST_RUN_ID overwrites it, and the reader has to be the run that wrote
    # last rather than a mixture of two.
    run_id = _run_id(events)
    events = event_log.this_run(events, run_id)
    if not any(event.get("event") == "worker_start" for event in events):
        return None  # nothing here ever started a session
    if any(event.get("event") == "worker_finish" for event in events):
        return None  # it reached its own session finish; this is not a death

    state = read_state(directory / f"{worker}.state", run_id)
    beats = event_log.heartbeats(events)
    dump = crash_stack.read(directory / f"{worker}.crash", limit=40)

    incident = WorkerDeathIncident(
        worker=worker,
        worker_pid=state.get("pid") or event_log.worker_pid(events),
        recovered_from_run=session,
        run_id=run_id or session,
        last_seen_at=(beats[-1].get("time") if beats else None),
        exit_status_source="unavailable",
        exit_status_meaning="unknown",
        test_in_flight=state.get("nodeid"),
        last_test=state.get("last_nodeid"),
        phase=state.get("phase"),
        tests_started=state.get("tests_started") or 0,
        tests_finished=state.get("tests_finished") or 0,
        rss_mb_at_death=beats[-1].get("rss_mb") if beats else None,
        system_available_mb=_last_available(events),
        crash_stack=dump,
        crash_stack_age_seconds=_age_of(directory / f"{worker}.crash") if dump else None,
        high_water=event_log.high_water_marks(events)[-1:] or None,
    )
    trace = directory / signal_trace.TRACE_FILE
    witness = directory / killer.CONTROLLER_EVENTS
    sources = killer.Sources(
        directory=directory,
        elevate=elevate,
        trace_status=(
            "the dead run's trace file" if trace.exists() else "off: the dead run left no trace file"
        ),
        witness_status=(
            "the dead run's controller log"
            if witness.exists()
            else "off: the dead run left no controller log"
        ),
    )
    # The death is somewhere after the last beat and before the next one
    # would have been due; a generous window, because the beat interval is
    # the dead run's setting and not this one's to know.
    last_seen = beats[-1].get("time") if beats else None
    died_by = (last_seen + RECOVERED_DEATH_WINDOW_SECONDS) if last_seen else time.time()
    _attach_witnesses(
        incident, sources, incident.worker_pid, None, event_log.started_at(events), died_by
    )
    incident.verdict, incident.confidence, incident.evidence = classify.of(incident)
    incident.evidence.extend(_what_was_kept(events, dump))
    return incident


#: How long after its last heartbeat a recovered process is taken to have
#: died within. Three default intervals: past that a beat would have landed.
RECOVERED_DEATH_WINDOW_SECONDS = 15.0


def recover_controller(
    directory: Path, session: str, marker: dict[str, Any], elevate: bool = False
) -> Optional[WorkerDeathIncident]:
    """The death of a run's controller, which nothing else on disk records.

    A worker leaves an event log with a start and, if it got there, a finish.
    The controller of a distributed run leaves neither: it runs no tests and
    keeps no heartbeat. What it leaves is the marker with its pid and, from
    :mod:`.killer`, a log of the SIGTERM it was sent. A cancelled CI job is
    exactly this case - and its workers are *not* the evidence, because
    execnet sends each of them SIGINT once the controller is gone, and they
    finish their sessions cleanly five seconds later. For a long time that
    made a cancelled run a run about which nothing was said at all.

    ``marker`` is the directory's ``owner.json``, already read and already
    judged to name a process that is over and never stamped its finish. A run
    with no workers is not this: its one process is ``main`` and has an event
    log of its own, and this returns None for it rather than reporting one
    death twice.
    """
    pid = marker.get("pid")
    if not isinstance(pid, int):
        return None
    if pid in {read_state(state, None).get("pid") for state in directory.glob("*.state")}:
        return None  # a run with no workers: its process is recovered as "main"
    controller_log = event_log.read_events(directory / killer.CONTROLLER_EVENTS)
    run_id = _run_id(controller_log) or next(
        (
            _run_id(event_log.read_events(path))
            for path in sorted(directory.glob("*.events"))
            if path.name != killer.CONTROLLER_EVENTS
        ),
        None,
    )
    started_at = marker.get("started_at") if isinstance(marker.get("started_at"), (int, float)) else None
    last_seen = _last_seen(directory)

    incident = WorkerDeathIncident(
        worker="controller",
        worker_pid=pid,
        recovered_from_run=session,
        run_id=run_id or session,
        last_seen_at=last_seen,
        exit_status_source="unavailable",
        exit_status_meaning="unknown",
    )
    trace = directory / signal_trace.TRACE_FILE
    witness = directory / killer.CONTROLLER_EVENTS
    sources = killer.Sources(
        directory=directory,
        elevate=elevate,
        trace_status=(
            "the dead run's trace file" if trace.exists() else "off: the dead run left no trace file"
        ),
        witness_status=(
            "the dead run's controller log"
            if witness.exists()
            else "off: the dead run left no controller log"
        ),
    )
    died_by = (last_seen + RECOVERED_DEATH_WINDOW_SECONDS) if last_seen else time.time()
    _attach_witnesses(incident, sources, pid, None, started_at, died_by)
    incident.verdict, incident.confidence, incident.evidence = classify.of(incident)
    return incident


def _last_seen(directory: Path) -> Optional[float]:
    """The last moment anything in this run was known to be alive: the newest
    line any of its processes wrote."""
    latest: Optional[float] = None
    for path in directory.glob("*.events"):
        for event in event_log.tail_events(path):
            stamp = event.get("time")
            if isinstance(stamp, (int, float)) and (latest is None or stamp > latest):
                latest = float(stamp)
    return latest


def _run_id(events: list[dict[str, Any]]) -> Optional[str]:
    """The id the dead run reported, which is not the directory's name.

    The last stamped line wins, for the same reason the live view takes the
    last one: the only way a file holds two runs' lines is two sessions
    writing it, and the later writer is the one the rest of this directory
    belongs to.
    """
    for event in reversed(events):
        if event.get("run_id"):
            return str(event["run_id"])
    return None


def _what_was_kept(events: list[dict[str, Any]], dump: list[str]) -> list[str]:
    """Why there is no stack, when there is none and one was possible.

    An absence with no reason beside it reads as "it crashed without leaving
    anything", which is a finding. The truth is usually that the dump was
    written somewhere this file could not keep it, and that is a setting away
    from being fixed for next time.
    """
    if dump:
        return []
    armed = [event for event in events if event.get("event") == "faulthandler_armed"]
    if armed and armed[-1].get("fatal_stack") == "stderr":
        return [
            "no stack was kept here: this run had no workers, so its fatal dump "
            "went to the terminal pytest's faulthandler plugin writes to rather "
            "than into a file - set failure_crash_stack to keep a copy instead"
        ]
    return []
