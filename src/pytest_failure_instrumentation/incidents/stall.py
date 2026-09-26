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

import os
import threading
import time
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from .. import lanes as thread_lanes
from .. import probes
from ..analysis import stall as assessment
from ..capture import crash_stack
from ..capture import events as event_log
from ..capture.state import read_state
from ..config import SOLE_WORKER
from ..lanes import ThreadKey
from ..probes import process as process_probe
from .base import Incident

#: How long to wait for a signalled worker to write its stack.
STACK_WAIT_SECONDS = 2.0
STACK_POLL_SECONDS = 0.1
STACK_LINES = 14

CONFIDENCE = {"BLOCKED": "high", "FROZEN": "high", "SILENT": "low"}

#: Which file a passive stack came out of, in the order they are tried, and
#: what each one means. The watchdog writes while a test is merely taking a
#: while; the fallback writes only once the worker's own threads have stopped
#: running, which is why it is worth naming separately.
PASSIVE_SOURCES = ((".slow", "watchdog"), (".frozen", "frozen-fallback"), (".crash", "crash"))

SOURCE_WORDING = {
    "watchdog": "the slow-test watchdog",
    "frozen-fallback": "the fallback timer, after the worker stopped running Python",
    "crash": "the worker, into its crash file",
    "py-spy": "py-spy, reading the process from outside it",
    "probe": "the worker, in answer to the stall probe",
    "frames": "this process, reading the lane's own thread",
}


class WorkerStallIncident(Incident):
    model_config = ConfigDict(extra="allow")

    # The run cannot finish while a worker is wedged: xdist waits for work it
    # handed out and never gets back. That is an inference from the evidence at
    # detection time rather than an observation - the alert goes out while the
    # run is still going, which is the whole point of it - and a worker that
    # comes back falsifies it. The one thing that can say so is the run summary,
    # which exists only because the session reached its end; it says "raised as
    # run-ending" rather than "ended the session" for exactly this reason.
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
    #: The most recent test this worker ran, whether or not it finished. Only
    #: ever context: a worker wedged between two tests is not wedged *in* the
    #: one that already passed, and the two must not share a field.
    last_test: Optional[str] = None
    #: The sha256 of each of the two ids above, whole. The worker took them
    #: before its fixed-size slot trimmed anything, so these identify the
    #: tests even where the text beside them lost its middle - see
    #: :mod:`..nodeid`. Null where the id itself is.
    test_in_flight_hash: Optional[str] = None
    last_test_hash: Optional[str] = None
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
    #: Which mechanism left the stack: "probe", "watchdog", "frozen-fallback"
    #: or "crash". They mean different things - the third is the interpreter
    #: having stopped responding, which is a finding in itself - and the
    #: frames look identical.
    stack_source: Optional[str] = None
    #: For a process of pytest-threadlanes' lanes that stopped running as a
    #: whole: every lane that had a test in flight, ``{lane, nodeid,
    #: nodeid_hash, phase}`` each - they all stopped with it. Absent from the
    #: payload, rather than empty, for every other stall.
    lanes_in_flight: list[dict[str, Any]] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _without_absent_lanes(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        return thread_lanes.without_unset(handler(self), self, ("lanes_in_flight",))

    def raw_stack(self) -> list[str]:
        return self.stack

    def blame_stack(self) -> tuple[list[str], bool]:
        return self.stack, False  # faulthandler prints deepest first

    def suspect_nodeid(self) -> Optional[str]:
        return self.test_in_flight or self.last_test

    def suspect_basis_for(self, path: str) -> str:
        if self.test_in_flight:
            return f"the test that was running, {path}"
        return (
            f"the last test this worker ran, {path}; nothing was running when it "
            "went silent"
        )

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict, self.state]

    def summary(self) -> str:
        if self.verdict == "INSTRUMENTATION_FAILED":
            return super().summary()
        line = f"Worker {self.worker} has been silent for {self.silent_for_seconds:.0f} s"
        if self.test_in_flight:
            phase = f" ({self.phase})" if self.phase else ""
            line += f" while running {self.test_in_flight}{phase}"
        elif self.lanes_in_flight:
            count = len(self.lanes_in_flight)
            line += f" while running {count} test{'s' if count != 1 else ''} on its lanes"
        elif self.last_test:
            line += f" with no test running (the last was {self.last_test})"
        else:
            line += " with no test running"
        if self.state == "FROZEN":
            line += ", and its heartbeat thread has stopped"
        elif self.state == "BLOCKED":
            line += ", alive but using no CPU" if self.cpu_rate is not None else ", alive"
        elif self.state == "SILENT":
            line += ", with no heartbeat on record"
        if self.blamed_frame is not None:
            line += f", in {self.blamed_frame.named()}"
        return line

    def details(self) -> list[str]:
        """Where the stack came from, from the fields that say so. A stack
        nobody asked for was left while the run went on around it, and
        reading it as a picture of now is the mistake to head off - so it
        says where it came from as well as when. A fresh one from the
        watchdog is still not a probe, and one from the fallback says
        something the frames do not."""
        if not self.stack and self.stack_unavailable_reason:
            return [f"No stack: {self.stack_unavailable_reason}."]
        if self.stack and self.stack_probed:
            return [f"Stack taken now, by {SOURCE_WORDING.get(self.stack_source or '', 'the worker')}."]
        if self.stack and self.stack_age_seconds is not None:
            return [
                f"Stack from {SOURCE_WORDING.get(self.stack_source or '', 'the worker')}, "
                f"written {self.stack_age_seconds:.0f} s ago; it is not a picture of now."
            ]
        return []


def build(
    worker: str,
    directory: Path,
    silent_for: float,
    interval: float,
    stack_probe: bool,
    *,
    run_id: Optional[str] = None,
    live_pid: Optional[int] = None,
    cancel: Optional[threading.Event] = None,
    known_lane: bool = False,
    shared: Optional[dict[Any, Any]] = None,
) -> Optional[WorkerStallIncident]:
    """Assess a silent worker. Returns None when it is merely slow.

    Runs on the stall watcher's own thread, which is why it may wait: the
    second pass is what separates a missed beat from a frozen process. Every
    wait here is against ``cancel``, so the session can end without waiting
    out an assessment that will be thrown away - the alternative is a hook
    call arriving after the run summary, or during interpreter shutdown.

    ``live_pid`` is the pid the controller can still see running for this
    worker's gateway. It is what licenses the signal in :func:`_stack`; a pid
    read back from a file is only a number, and the process that owns it now
    may be somebody else's.

    A run with no workers reaches all of this unchanged. The evidence is the
    same evidence, written to the same files by the same recorder, and the
    only thing that differs is that the process holding it is this one - which
    :func:`_stack` notices for itself.

    **A lane of pytest-threadlanes** is a worker that shares its process - see
    :mod:`..lanes`. Its state is its own and names the process, whose files
    hold everything else: the beats, read with the lane's own thread's CPU
    where they carry it, and the dumps, read for the lane's own thread. A lane
    with no test in flight is idle - out of work at the end of an uneven run,
    or waiting to be given some - and is never a stall: its process is
    answering for it, and a sibling lane that is stuck says so for itself.

    And the process such a lane runs in is their container, whose silence is
    theirs: while any of them has a test in flight, that lane is watched and
    judged on its own, and the process says nothing. Only once none has is its
    silence its own, and it is judged as any worker with no test running is.

    ``known_lane`` is the engine saying this name is a lane. A lane that has
    never started a test has no record yet - more lanes than there is work
    for, say - and is idle like any other. ``shared`` is what one poll of the
    engine has already read of each process - see :func:`_build_lane`.
    """
    record = read_state(directory / f"{worker}.state", run_id)
    lane = _lane(directory, worker, record)
    if lane is None and known_lane:
        return None  # a lane with no record has never had a test: idle
    if lane is not None:
        return _build_lane(
            worker, lane, directory, silent_for, interval, stack_probe,
            run_id=run_id, live_pid=live_pid, cancel=cancel, shared=shared,
        )
    lanes: Optional[list[tuple[str, dict[str, Any]]]] = None
    if thread_lanes.is_container(record):
        lanes = thread_lanes.lane_records(directory, worker, run_id)
        if thread_lanes.in_flight(lanes):
            return None  # its lanes are watched, and judged, one by one
    path = directory / f"{worker}.events"
    # Only this run's. An earlier run's beats - left where the directory could
    # not be cleared, which on Windows is any file somebody still had open -
    # are old by definition, and old beats are exactly what FROZEN is read off.
    events = event_log.this_run(event_log.read_events(path), run_id)
    beats = event_log.heartbeats(events)
    verdict = assessment.assess(beats, time.time(), silent_for, interval)

    if verdict.needs_confirmation:
        previous = assessment.last_beat_time(beats)
        if _wait(cancel, interval * 1.2):
            return None  # the run is ending; nobody is left to tell
        beats = event_log.heartbeats(
            event_log.this_run(event_log.read_events(path), run_id)
        )
        verdict = assessment.confirm(beats, previous, time.time(), silent_for)

    if verdict.state is None:
        return None  # burning CPU: slow, not stuck. Say nothing and re-arm.

    state = read_state(directory / f"{worker}.state", run_id)
    pid = state.get("pid") or event_log.worker_pid(events)
    in_flight = state.get("nodeid")
    stack, probed, why, written, source = _stack(
        directory, worker, pid, stack_probe, live_pid, cancel
    )

    evidence = [verdict.reason]
    confidence = verdict.confidence or CONFIDENCE.get(verdict.state, "low")
    if not in_flight and verdict.state != "FROZEN":
        # Nothing was running. A worker between two tests, still collecting, or
        # waiting for the scheduler to hand it work looks exactly like this
        # from outside, and all three are ordinary. The silence is still worth
        # reporting - the run cannot end while a worker never comes back - but
        # not at the confidence a wedged test earns, and not blamed on a test.
        evidence.append(
            "No test was running. A worker between tests, still collecting, or "
            "waiting to be given work looks the same from outside."
        )
        if lanes is not None:
            evidence.append(
                f"None of this process's {len(lanes)} lanes had a test in flight."
            )
        confidence = "low"
    evidence.append(
        "The run cannot finish while xdist waits for work this worker was handed."
    )
    age = round(max(0.0, time.time() - written), 1) if written is not None else None
    if in_flight:
        evidence.append(f"Look at: {in_flight}.")
    measured = [f"silent for {silent_for:.0f} s"]
    if verdict.heartbeat_age is not None:
        measured.append(f"last heartbeat {verdict.heartbeat_age:.0f} s ago")
    if verdict.cpu_rate is not None:
        measured.append(f"{verdict.cpu_rate:.2f} cores of CPU over the window")
    evidence.append("Measured: " + ", ".join(measured) + ".")

    return WorkerStallIncident(
        worker=worker,
        verdict=f"STALLED_{verdict.state}",
        # The assessment overrides only when it reached the state on weaker
        # evidence than the state normally implies.
        confidence=confidence,
        state=verdict.state,
        reason=verdict.reason,
        silent_for_seconds=round(silent_for, 1),
        cpu_rate=round(verdict.cpu_rate, 3) if verdict.cpu_rate is not None else None,
        heartbeat_age_seconds=(
            round(verdict.heartbeat_age, 1) if verdict.heartbeat_age is not None else None
        ),
        worker_pid=pid,
        test_in_flight=in_flight,
        last_test=state.get("last_nodeid"),
        test_in_flight_hash=state.get("nodeid_hash"),
        last_test_hash=state.get("last_nodeid_hash"),
        phase=state.get("phase"),
        stack=stack,
        stack_probed=probed,
        stack_unavailable_reason=why,
        stack_age_seconds=age,
        stack_source=source,
        evidence=evidence,
    )


def _lane(
    directory: Path, worker: str, record: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """The worker's state, if it is a lane's whose process can be named."""
    if not thread_lanes.is_lane(record):
        return None
    process = record.get(thread_lanes.PROCESS_KEY)
    if thread_lanes.sibling(directory / f"{worker}.state", process, ".events") is None:
        return None
    return record


def _build_lane(
    worker: str,
    lane: dict[str, Any],
    directory: Path,
    silent_for: float,
    interval: float,
    stack_probe: bool,
    *,
    run_id: Optional[str],
    live_pid: Optional[int],
    cancel: Optional[threading.Event],
    shared: Optional[dict[Any, Any]],
) -> Optional[WorkerStallIncident]:
    """:func:`build` for a lane of pytest-threadlanes.

    Whether its process is running at all is read from the process's beats,
    and whether the lane's own thread is doing anything from the CPU the
    heartbeat reads for it - see :class:`..lanes.LaneCpu`. A lane with no
    test in flight is idle and says nothing.

    A process that has stopped running altogether - its heartbeat frozen by
    native code holding the GIL, or the process stopped - is not a lane's
    finding: every lane of it went silent at once, and each blaming its own
    test would be as many incidents about as many innocent tests, with the
    real one liable to be dropped as their duplicate. It is one incident, for
    the process, listing its lanes in flight - see :func:`_frozen_process`.
    """
    if not lane.get("nodeid"):
        return None  # idle; the engine re-arms it, and nothing is said
    process = str(lane[thread_lanes.PROCESS_KEY])
    path = directory / f"{process}.events"
    events = event_log.this_run(event_log.read_events(path), run_id)
    beats = event_log.heartbeats(events)
    # Whether the process is running at all comes first, and from its beats
    # alone: a process frozen as a whole is its own finding, whatever its
    # lanes had or had not been measured doing before it froze.
    first = assessment.assess(beats, time.time(), silent_for, interval)
    if first.needs_confirmation:
        previous = assessment.last_beat_time(beats)
        pid = lane.get("pid") or event_log.worker_pid(events)
        before = process_probe.process_thread_cpu(pid) if isinstance(pid, int) else {}
        if _wait(cancel, interval * 1.2):
            return None  # the run is ending; nobody is left to tell
        beats = event_log.heartbeats(event_log.this_run(event_log.read_events(path), run_id))
        second = assessment.confirm(beats, previous, time.time(), silent_for)
        if second.state == "FROZEN" and _lanes_taking_turns(
            before, pid, _lane_records(directory, process, run_id, shared)
        ):
            # Not frozen: starved. Fifty lanes running Python keep the
            # heartbeat from the GIL for seconds at a time, and its beat is
            # then as late as a frozen process's - but the GIL is changing
            # hands, and the lanes' threads are burning CPU in turn. A process
            # that is frozen - native code holding the GIL, or stopped - runs
            # one thread or none. Each lane is judged on its own at the next
            # poll, once the beat has caught up.
            return None
        if second.state == "FROZEN":
            return _frozen_process(
                process, directory, silent_for, second, events,
                run_id=run_id, live_pid=live_pid, shared=shared, stack_probe=stack_probe,
            )
    records = _lane_records(directory, process, run_id, shared)
    busy = [name for name, record in records if record.get("nodeid")]
    measured = _read_lane_cpu(directory, process, run_id, shared)
    current = assessment.lane_cpu_current(measured.newest, beats, interval)
    readings = measured.get(worker) or []
    mine = assessment.lane_beats(beats, readings, interval) if current else []
    if current and not mine and len(readings) < 2 and _just_started(lane, interval):
        # Its process measures its lanes, and this one not twice yet: it began
        # its test a beat or two ago. The next poll has its rate; a verdict
        # without one now would call a lane burning a core stuck. Only while
        # the file is being written, and only just after the test began: a
        # file whose writes stopped would otherwise defer this lane forever.
        return None
    whose = bool(mine)
    verdict = (
        assessment.assess_lane(
            beats, mine, time.time(), silent_for, interval, max(1, len(busy))
        )
        if whose
        else assessment.assess(beats, time.time(), silent_for, interval)
    )
    if verdict.state is None:
        return None  # burning CPU: slow, not stuck - or a beat late again

    state = read_state(directory / f"{worker}.state", run_id)
    in_flight = state.get("nodeid")
    if not in_flight:
        return None  # the lane's test ended while it was being assessed
    pid = state.get("pid") or event_log.worker_pid(events)
    thread = (state.get("thread_ident"), state.get("thread_name"))
    stack, probed, why, written, source = _lane_stack(
        directory, process, pid, state, stack_probe, live_pid, cancel, shared
    )
    if read_state(directory / f"{worker}.state", run_id).get("nodeid") != in_flight:
        return None  # it moved on while its stack was read: that stack is not this test's
    siblings = [name for name in busy if name != worker]
    evidence = [verdict.reason]
    confidence = verdict.confidence or CONFIDENCE.get(verdict.state, "low")
    named = thread[1] or "named for it"
    others = (
        f", whose other {len(siblings)} lane{'s' if len(siblings) != 1 else ''} with a "
        "test in flight go on running"
        if siblings
        else ""
    )
    if whose:
        figure = "The CPU figure is that thread's own"
    elif measured:
        figure = (
            "Its process stopped writing its lanes' own CPU readings "
            f"{max(0.0, assessment.last_beat_time(beats) - (measured.newest or 0)):.0f} s "
            "before its last beat, so the figure is the whole process's, every lane's "
            "together"
        )
    else:
        figure = (
            "No CPU is read per thread for this process, so the figure is the whole "
            "process's, every lane's together"
        )
    evidence.append(
        f"Lane {worker} is the thread {named} in worker process {process}{others}. "
        f"{figure}, and the stack is that thread's."
    )
    if process == SOLE_WORKER:
        evidence.append("The run cannot finish while this lane's test is still running.")
    else:
        evidence.append(
            "The run cannot finish while xdist waits for the test this lane was handed."
        )
    evidence.append(f"Look at: {in_flight}.")
    facts = [f"silent for {silent_for:.0f} s"]
    if verdict.heartbeat_age is not None:
        facts.append(f"last heartbeat {verdict.heartbeat_age:.0f} s ago")
    if verdict.cpu_rate is not None:
        on = "on the lane's thread" if whose else "in the whole process"
        facts.append(f"{verdict.cpu_rate:.2f} cores of CPU {on} over the window")
    evidence.append("Measured: " + ", ".join(facts) + ".")
    age = round(max(0.0, time.time() - written), 1) if written is not None else None
    return WorkerStallIncident(
        worker=worker,
        verdict=f"STALLED_{verdict.state}",
        confidence=confidence,
        state=verdict.state,
        reason=verdict.reason,
        silent_for_seconds=round(silent_for, 1),
        cpu_rate=round(verdict.cpu_rate, 3) if verdict.cpu_rate is not None else None,
        heartbeat_age_seconds=(
            round(verdict.heartbeat_age, 1) if verdict.heartbeat_age is not None else None
        ),
        worker_pid=pid,
        test_in_flight=in_flight,
        last_test=state.get("last_nodeid"),
        test_in_flight_hash=state.get("nodeid_hash"),
        last_test_hash=state.get("last_nodeid_hash"),
        phase=state.get("phase"),
        stack=stack,
        stack_probed=probed,
        stack_unavailable_reason=why,
        stack_age_seconds=age,
        stack_source=source,
        evidence=evidence,
    )


def _frozen_process(
    process: str,
    directory: Path,
    silent_for: float,
    verdict: assessment.Assessment,
    events: list[dict[str, Any]],
    *,
    run_id: Optional[str],
    live_pid: Optional[int],
    shared: Optional[dict[Any, Any]],
    stack_probe: bool = True,
) -> WorkerStallIncident:
    """One incident for a process of lanes that has stopped running.

    Its lanes in flight are all listed, and one is blamed only on evidence
    that it is the one holding the others up: py-spy, reading the stopped
    interpreter, finds that lane's thread holding the GIL. A process stopped
    by a signal holds nobody up - whichever thread had the GIL when it
    stopped is an accident - and no lane is blamed there.
    """
    record = read_state(directory / f"{process}.state", run_id)
    lanes = _lane_records(directory, process, run_id, shared)
    flying = thread_lanes.in_flight(lanes)
    pid = record.get("pid") or event_log.worker_pid(events)
    evidence = [verdict.reason]
    culprit: Optional[dict[str, Any]] = None
    stack: list[str] = []
    why: Optional[str] = None
    reading: Optional[list[dict[str, Any]]] = None
    if isinstance(pid, int) and _stopped(pid):
        why = f"process {pid} is stopped by a signal, so no lane is holding it up"
        evidence.append(
            f"Process {pid} is stopped - SIGSTOP, or a debugger - not running native "
            "code: none of its lanes is to blame for it."
        )
    elif isinstance(pid, int) and _cannot_probe(pid, stack_probe, live_pid) is None:
        reading, error = _read_process(pid, shared)
        holders = [thread for thread in reading or [] if thread.get("owns_gil")]
        for name, lane_record in lanes:
            if name not in {entry["lane"] for entry in flying}:
                continue
            if any(_is_lanes_thread(thread, lane_record) for thread in holders):
                culprit = {**lane_record, "lane": name}
        if culprit is None:
            why = error or "no lane's thread was found holding the GIL"
    else:
        why = (
            _cannot_probe(pid if isinstance(pid, int) else None, stack_probe, live_pid)
            or "the worker's process could not be read from outside it"
        )
    if culprit is not None and reading is not None:
        held = [entry for entry in reading if _is_lanes_thread(entry, culprit)]
        stack = crash_stack.from_threads(held, limit=STACK_LINES)
        evidence.append(
            f"py-spy found lane {culprit['lane']}'s thread holding the GIL: its test is "
            "the one blamed, and the others are held up behind it."
        )
    if flying:
        running = "; ".join(f"{entry['lane']}: {entry['nodeid']}" for entry in flying)
        evidence.append(
            f"Every lane of this process stopped with it. Lanes with a test in flight: "
            f"{running}."
        )
    evidence.append(
        "The run cannot finish while xdist waits for work this worker was handed."
        if process != SOLE_WORKER
        else "The run cannot finish while this process is stopped."
    )
    measured = [f"silent for {silent_for:.0f} s"]
    if verdict.heartbeat_age is not None:
        measured.append(f"last heartbeat {verdict.heartbeat_age:.0f} s ago")
    evidence.append("Measured: " + ", ".join(measured) + ".")
    return WorkerStallIncident(
        worker=process,
        verdict="STALLED_FROZEN",
        confidence=CONFIDENCE["FROZEN"],
        state="FROZEN",
        reason=verdict.reason,
        silent_for_seconds=round(silent_for, 1),
        heartbeat_age_seconds=(
            round(verdict.heartbeat_age, 1) if verdict.heartbeat_age is not None else None
        ),
        worker_pid=pid if isinstance(pid, int) else None,
        test_in_flight=culprit.get("nodeid") if culprit else None,
        test_in_flight_hash=culprit.get("nodeid_hash") if culprit else None,
        phase=culprit.get("phase") if culprit else None,
        stack=stack,
        stack_probed=reading is not None,
        stack_unavailable_reason=None if stack else why,
        stack_age_seconds=None,
        stack_source="py-spy" if stack else None,
        lanes_in_flight=flying,
        evidence=evidence,
    )


def _stopped(pid: int) -> bool:
    """Whether ``pid`` is stopped by a signal rather than running.

    Stopped, not traced: a process py-spy is reading - this engine's own read,
    or the live view's - is in tracing-stop for as long as the read takes, and
    is not a process somebody stopped.
    """
    try:
        import psutil

        return bool(psutil.Process(pid).status() == psutil.STATUS_STOPPED)
    except Exception:  # noqa: BLE001 - a process that cannot be asked is not known stopped
        return False


#: The share of its lanes' CPU one lane thread must hold, over the wait that
#: confirms a freeze, for the process to count as frozen by that thread
#: rather than as its lanes taking turns at the GIL.
FROZEN_BY_ONE = 0.8

#: Lane CPU, in seconds over that wait, below which nothing is taking turns.
TAKING_TURNS_CPU = 0.05


def _lanes_taking_turns(
    before: dict[int, float], pid: Any, records: list[tuple[str, dict[str, Any]]]
) -> bool:
    """Whether its lanes shared the CPU since ``before``: a process whose GIL
    is changing hands, rather than one frozen by a thread holding it.

    A frozen process runs one thread - the one holding the GIL, in native
    code - or none, if it is stopped; its lanes waiting on that thread wake
    every few milliseconds to ask for the GIL, which the kernel can charge a
    clock tick for. So it is the spread that decides, not whether a second
    thread moved at all: fifty lanes taking turns each burn a fiftieth of it.

    Only the lanes' own threads count - not the heartbeat's, whose silence is
    the question, and not a native library's own threads, which run without
    the GIL whether the process is frozen or not. Read from outside the
    process, by native id; where that cannot be done (macOS numbers threads by
    position) nothing is said, and the freeze stands.
    """
    if not before or not isinstance(pid, int):
        return False
    after = process_probe.process_thread_cpu(pid)
    lanes = {
        record.get("thread_id") for _, record in records
        if isinstance(record.get("thread_id"), int)
    }
    burned = [
        max(0.0, after[native] - before[native])
        for native in lanes if native in before and native in after
    ]
    total = sum(burned)
    if total < TAKING_TURNS_CPU:
        return False  # nothing ran: stopped, or held by a thread that is no lane
    return max(burned) < FROZEN_BY_ONE * total


def _just_started(lane: dict[str, Any], interval: float) -> bool:
    """Whether a lane's current phase began too recently to have been measured
    twice: a beat or two, and a beat's slack."""
    started = lane.get("phase_started")
    if not isinstance(started, (int, float)):
        return False
    return time.time() - float(started) < 3 * interval + assessment.CURRENT_READING_SLACK


def _lane_records(
    directory: Path, process: str, run_id: Optional[str], shared: Optional[dict[Any, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Every lane record of ``process``, read once per poll of the engine
    however many of its lanes are assessed: a thousand lanes each listing a
    thousand records was a million reads a poll."""
    if shared is None:
        return thread_lanes.lane_records(directory, process, run_id)
    key = ("lanes", process)
    if key not in shared:
        shared[key] = thread_lanes.lane_records(directory, process, run_id)
    found: list[tuple[str, dict[str, Any]]] = shared[key]
    return found


def _read_lane_cpu(
    directory: Path, process: str, run_id: Optional[str], shared: Optional[dict[Any, Any]]
) -> thread_lanes.LaneCpuRead:
    """``process``'s lanes' CPU, read once per poll of the engine."""
    if shared is None:
        return thread_lanes.read_lane_cpu(directory, process, run_id)
    key = ("lanecpu", process)
    if key not in shared:
        shared[key] = thread_lanes.read_lane_cpu(directory, process, run_id)
    found: thread_lanes.LaneCpuRead = shared[key]
    return found


def _read_process(
    pid: int, shared: Optional[dict[Any, Any]]
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """py-spy's reading of ``pid``, once per poll of the engine however many
    of its lanes ask: every stalled lane reading the whole process for itself
    was forty reads of one process, each pausing it, in one poll."""
    if shared is None:
        return probes.live_stack(pid)
    key = ("py-spy", pid)
    if key not in shared:
        shared[key] = probes.live_stack(pid)
    found: tuple[Optional[list[dict[str, Any]]], Optional[str]] = shared[key]
    return found


def _is_lanes_thread(thread: dict[str, Any], lane: dict[str, Any]) -> bool:
    """Whether a live reading's thread is ``lane``'s: by native id, which
    py-spy reports as ``os_thread_id``, or by its ident."""
    native, ident = lane.get("thread_id"), lane.get("thread_ident")
    if native is not None and thread.get("os_thread_id") == native:
        return True
    return ident is not None and thread.get("thread_id") == ident


def _lane_stack(
    directory: Path,
    process: str,
    pid: Any,
    lane: dict[str, Any],
    stack_probe: bool,
    live_pid: Optional[int],
    cancel: Optional[threading.Event],
    shared: Optional[dict[Any, Any]],
) -> tuple[list[str], bool, Optional[str], Optional[float], Optional[str]]:
    """The lane's own thread's stack - never a sibling's in its place.

    A faulthandler dump stops at a hundred threads, so in a process of more
    lanes the thread asked for may not be in it, and the thread a dump would
    otherwise fall back to is another lane's, whose test was not stuck. So
    the lane's thread is read where it lives. In a run with no ``-n`` that is
    this process, whose own frames are right here. In a worker, py-spy reads
    it, once per poll of the process whichever lanes ask; where it cannot, a
    dump is asked for - once per process per poll too - and used only if the
    lane's thread is in it. Otherwise there is no stack, and the incident
    says why.
    """
    ident, name = lane.get("thread_ident"), lane.get("thread_name")
    thread = (ident, name)
    if pid == os.getpid():
        import sys

        if not isinstance(ident, int):
            return [], False, "the lane's record does not say which thread it is", None, None
        frame = sys._current_frames().get(ident)
        if frame is None:
            return [], True, "the lane's thread is no longer running", None, None
        stack = crash_stack.from_frame(frame, ident, name or "lane", limit=STACK_LINES)
        return stack, True, None, time.time(), "frames"
    if isinstance(pid, int) and _cannot_probe(pid, stack_probe, live_pid) is None:
        # Not a signal, but py-spy pauses the worker while it reads it, and
        # failure_stack_probe = false is a promise to leave workers alone.
        reading, error = _read_process(pid, shared)
        if reading is not None:
            mine = [entry for entry in reading if _is_lanes_thread(entry, lane)]
            if mine:
                return (
                    crash_stack.from_threads(mine, limit=STACK_LINES),
                    True, None, time.time(), "py-spy",
                )
            return [], True, "py-spy's reading of the process has no thread of this lane", None, None
    if shared is not None and ("probe", pid) in shared:
        lines, written = shared[("probe", pid)]
    else:
        lines, written = _probed(directory, process, pid, stack_probe, live_pid, cancel)
        if shared is not None:
            shared[("probe", pid)] = (lines, written)
    stack = crash_stack.pick(lines, STACK_LINES, thread) if lines else []
    if stack:
        return stack, True, None, written, "probe"
    passive, written, source = _passive_stack(directory, process, thread)
    if passive:
        return passive, False, None, written, source
    refused = _cannot_probe(pid if isinstance(pid, int) else None, stack_probe, live_pid)
    if refused is not None:
        return [], False, refused, None, None
    return [], bool(lines), (
        "no dump of the process holds this lane's thread - faulthandler writes at "
        "most a hundred threads - and py-spy could not read it"
    ), None, None


def _probed(
    directory: Path,
    process: str,
    pid: Any,
    allowed: bool,
    live_pid: Optional[int],
    cancel: Optional[threading.Event],
) -> tuple[list[str], Optional[float]]:
    """What one on-demand dump of a lane's process added to its crash file."""
    if not isinstance(pid, int) or _cannot_probe(pid, allowed, live_pid) is not None:
        return [], None
    crash_file = directory / f"{process}.crash"
    before = crash_stack.size(crash_file)
    if not probes.request_stack(pid):
        return [], None
    deadline = time.time() + STACK_WAIT_SECONDS
    while time.time() < deadline:
        if _wait(cancel, STACK_POLL_SECONDS):
            break
        if crash_stack.size(crash_file) > before:
            _wait(cancel, STACK_POLL_SECONDS)  # the rest of a large dump
            break
    try:
        with crash_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(before)
            lines = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return [], None
    return lines, time.time()


def _wait(cancel: Optional[threading.Event], seconds: float) -> bool:
    """Sleep, unless and until the run ends. True when it did."""
    if cancel is None:
        time.sleep(seconds)
        return False
    return cancel.wait(seconds)


def _stack(
    directory: Path,
    worker: str,
    pid: Optional[int],
    allowed: bool,
    live_pid: Optional[int] = None,
    cancel: Optional[threading.Event] = None,
    thread: Optional[ThreadKey] = None,
) -> tuple[list[str], bool, Optional[str], Optional[float], Optional[str]]:
    """Ask the worker for a stack, and read only what it added.

    Returns the stack, whether anything was asked for, why not when nothing
    was, when the stack it found was written, and which mechanism wrote it. A
    test that outlived ``failure_slow_test_seconds`` has already dumped one
    unprompted into its own file, so a stack may be here without anything
    having been signalled - and that one describes whenever the watchdog last
    fired rather than now.

    ``worker`` names the files, which for a lane are its process's; ``thread``
    is then the lane's thread, which every read below picks out of the dump.
    """
    if pid == os.getpid():
        return _own_stack(directory, worker, thread)

    crash_file = directory / f"{worker}.crash"
    try:
        before = crash_file.stat().st_size
    except OSError:
        before = 0

    why = _cannot_probe(pid, allowed, live_pid)
    if why is not None or pid is None:
        # Whatever the worker wrote on its own is still evidence.
        stack, written, source = _passive_stack(directory, worker, thread)
        return stack, False, why, written, source
    if not probes.request_stack(pid):
        stack, written, source = _passive_stack(directory, worker, thread)
        return (
            stack,
            False,
            "the worker could not be signalled; it may already be gone",
            written,
            source,
        )

    deadline = time.time() + STACK_WAIT_SECONDS
    while time.time() < deadline:
        if _wait(cancel, STACK_POLL_SECONDS):
            break
        try:
            if crash_file.stat().st_size > before:
                return (
                    crash_stack.read(
                        crash_file, limit=STACK_LINES, offset=before, thread=thread
                    ),
                    True,
                    None,
                    time.time(),  # it was written in answer to the signal
                    "probe",
                )
        except OSError:
            break
    fresh = crash_stack.read(crash_file, limit=STACK_LINES, thread=thread)
    if fresh:
        return fresh, True, None, crash_stack.written_at(crash_file), "probe"
    stack, written, source = _passive_stack(directory, worker, thread)
    return (
        stack,
        True,
        None if stack else "the worker was signalled but wrote nothing back",
        written,
        source,
    )


def _own_stack(
    directory: Path,
    worker: str,
    thread: Optional[ThreadKey] = None,
) -> tuple[list[str], bool, Optional[str], Optional[float], Optional[str]]:
    """The stack of a run with no workers, which is this process.

    Read the way every other live process in this package is read: py-spy,
    from outside. The frames are also directly available here - this is a
    thread of the process being assessed - and reading them that way was a
    second mechanism for a question that already had one, with its own
    failure modes to reason about and its own source to explain to whoever
    reads the incident. One reader, one shape, one thing to keep working.

    It buys two things beyond the tidiness. py-spy pauses the target and reads
    its memory rather than asking it to run anything, so it does not need the
    stalled process to cooperate and it says which thread holds the GIL - and
    it is not a signal, so nothing here can return a blocked syscall early and
    dissolve the stall being measured. ``failure_stack_probe`` is therefore
    not consulted: it exists to keep a signal away from a worker, and there is
    no signal on this path to withhold.

    What it costs is a dependency. Without ``pip install
    pytest-failure-instrumentation[stacks]`` there is no reader, and the stall
    is reported with whatever the watchdog last wrote and the reason py-spy
    gives - the same position ``/stack`` has always been in, and better than a
    stack whose absence is unexplained.

    The Yama declaration a self-read needs travels with the reader rather than
    being made here - see :func:`..probes.stacks.live_stack`.
    """
    threads, error = probes.live_stack(os.getpid())
    stack = crash_stack.from_threads(threads or [], limit=STACK_LINES, thread=thread)
    if stack:
        return stack, True, None, time.time(), "py-spy"
    # Whatever this process wrote for itself unprompted is still evidence, and
    # is what a run without py-spy is left with. The watchdog's dump names the
    # same blocked test; it is only older, and the incident says by how much.
    passive, written, source = _passive_stack(directory, worker, thread)
    return passive, True, error or "py-spy returned no frames", written, source


def _cannot_probe(
    pid: Optional[int], allowed: bool, live_pid: Optional[int]
) -> Optional[str]:
    """Why a live stack cannot be asked for, or None when it can.

    The last clause is the one that is not about capability. SIGUSR1's default
    disposition is to *terminate*, and the pid here was read back out of a file
    the worker wrote - so if that worker has since exited and the kernel has
    handed its number to something else, signalling it does not produce a bad
    report, it kills a process that has nothing to do with this run. So the pid
    is signalled only once something says it is still ours: the controller can
    see that process running under this worker's gateway, or the machine can be
    asked and answers that it is a child of ours. A machine that cannot be
    asked at all is not taken as a yes.
    """
    if not allowed:
        return "failure_stack_probe is off, so the worker was left undisturbed"
    if not probes.can_request_stack():
        return "this platform cannot ask a live process for one"
    if not pid:
        return "the worker's pid was never recorded, so there is nothing to ask"
    if live_pid is not None and live_pid != pid:
        return (
            f"the worker's gateway is running pid {live_pid}, not the {pid} on "
            "file, so nothing here is the process to ask"
        )
    if live_pid is None and probes.is_own_child(pid) is not True:
        return (
            f"pid {pid} could not be confirmed as this run's worker, and a "
            "recycled pid belongs to somebody else's process"
        )
    return None


def _passive_stack(
    directory: Path,
    worker: str,
    thread: Optional[ThreadKey] = None,
) -> tuple[list[str], Optional[float], Optional[str]]:
    """Whatever the worker dumped on its own, without being asked, and when.

    The slow-test watchdog's file first: a wedged test outlives the timeout, so
    that is where a stalled worker's stack normally is. Then the fallback's,
    which is the only one written when native code holds the GIL and the
    worker's own threads cannot run - on Windows that is the only way such a
    stack exists at all. The crash file last.
    """
    for suffix, source in PASSIVE_SOURCES:
        path = directory / f"{worker}{suffix}"
        lines = crash_stack.read(path, limit=STACK_LINES, thread=thread)
        if lines:
            return lines, crash_stack.written_at(path), source
    return [], None, None
