"""Telling a worker doing slow work from one that is wedged.

The controller only hears from a worker when a phase *completes*, so silence
alone means nothing: a twenty-minute test looks exactly like a deadlock. What
separates them is the worker's own heartbeat, which carries CPU time:

* the heartbeat stopped        - FROZEN: the worker's background thread cannot
                                 run at all. Native code is holding the GIL, or
                                 the process is stopped.
* heartbeat alive, no CPU      - BLOCKED: the test thread is waiting on
                                 something that is not coming.
* heartbeat alive, CPU burning - not stalled. Slow, and nothing to report.
* no heartbeat ever            - SILENT: the watchdog is off, so there is no
                                 passive evidence either way.

Nothing here signals the worker or reads a file. Everything is decided from
beats already on disk, because asking a wedged process a question can change
its answer: a raw syscall in native code that does not handle EINTR returns
early when a signal arrives, and the stall being measured resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: Cores' worth of CPU below which a worker counts as making no progress. Not
#: zero: a heartbeat thread waking every few seconds burns a little itself.
BUSY_THRESHOLD = 0.05

#: How stale a heartbeat must be before its absence is worth confirming.
STALE_BEATS = 2.0


@dataclass
class Assessment:
    #: None when the worker is busy rather than stalled, and nothing is raised.
    state: Optional[str]
    reason: str
    cpu_rate: Optional[float] = None
    heartbeat_age: Optional[float] = None
    #: The caller should wait one heartbeat interval and call ``confirm``.
    needs_confirmation: bool = False
    #: Set when this particular assessment is weaker than the state usually
    #: warrants - a BLOCKED reached without a CPU figure is the same verdict
    #: resting on half the evidence, and has to say so.
    confidence: Optional[str] = None


def last_beat_time(beats: list[dict[str, Any]]) -> float:
    return float(beats[-1].get("time") or 0) if beats else 0.0


def cpu_rate(beats: list[dict[str, Any]]) -> Optional[float]:
    """Cores' worth of CPU burned between the first and last heartbeat."""
    if len(beats) < 2:
        return None
    elapsed = float(beats[-1].get("time") or 0) - float(beats[0].get("time") or 0)
    used = float(beats[-1].get("cpu_seconds") or 0) - float(
        beats[0].get("cpu_seconds") or 0
    )
    if elapsed <= 0:
        return None
    return used / elapsed


def lane_beats(
    beats: list[dict[str, Any]], readings: Any, interval: float
) -> list[dict[str, Any]]:
    """A lane's own CPU readings as beats every rule here reads, or none.

    A lane of pytest-threadlanes shares its process, and a process's CPU is
    every lane's summed - so one busy sibling makes a lane that is waiting on
    a socket read as working, and the verdict this module exists for, "burning
    CPU: slow, not stuck", hides exactly the lane that is stuck. The heartbeat
    of a process with lanes reads each lane's thread CPU at every beat - see
    :class:`..lanes.LaneCpu` - and this turns one lane's readings into beats.

    Empty, and never the process's beats, where the lane has no rate of its
    own: fewer than two readings, or readings that stopped while the
    process's beats went on - its thread has ended, or this platform cannot
    number threads. A caller reads that as "could not tell", which is what it
    is; the process's figure there made an idle lane beside a busy one read
    as working. ``interval`` is the beat's: a reading a beat behind the
    latest beat is one a reader caught between the two writes.
    """
    measured = []
    for reading in readings if isinstance(readings, list) else []:
        if (
            isinstance(reading, list)
            and len(reading) == 2
            and all(isinstance(value, (int, float)) for value in reading)
        ):
            measured.append({"time": float(reading[0]), "cpu_seconds": float(reading[1])})
    if len(measured) < 2 or not beats:
        return []
    if measured[-1]["time"] < _current_floor(beats, interval):
        return []
    return measured


#: Slack for a lane's newest reading to trail the beat before the newest
#: by, and still be current: the two clocks are read a moment apart.
CURRENT_READING_SLACK = 0.5


def _current_floor(beats: list[dict[str, Any]], interval: float) -> float:
    """The oldest a current lane reading can be: that of the beat before the
    newest. Each beat's readings are taken after the beat is written, so a
    reader can see a beat before its readings - and the beat before it can be
    any distance behind: fifty lanes running Python kept the heartbeat from
    the GIL for up to three seconds a beat at a one-second interval."""
    if len(beats) >= 2:
        return float(beats[-2].get("time") or 0) - CURRENT_READING_SLACK
    return last_beat_time(beats) - interval - CURRENT_READING_SLACK


def lane_cpu_current(newest: Optional[float], beats: list[dict[str, Any]], interval: float) -> bool:
    """Whether a process's ``.lanecpu`` file is still being written: its
    newest reading at most a beat behind the process's newest beat.

    A file that stopped - its writes failing on a full descriptor table, or
    on a file another program holds open on Windows - still holds the
    readings it had, and a lane read from it would wait for readings that are
    never coming. Such a file is read as no file at all.
    """
    if newest is None or not beats:
        return False
    return newest >= _current_floor(beats, interval)


#: Cores' worth of CPU a process of lanes has to be burning before its lanes
#: are judged against their share of it rather than against a fixed floor.
#: Its lanes then contend for the GIL - one core, give or take what C code
#: running without it adds - or for the cores, and fifty lanes running Python
#: get about a fiftieth of a core each: every one of them under
#: :data:`BUSY_THRESHOLD`, and every one read as waiting.
SATURATED_CORES = 0.8

#: The fraction of its fair share a lane of a saturated process must burn to
#: count as working. A lane waiting on something burns next to nothing,
#: however many siblings it has; a busy one gets roughly its share.
FAIR_SHARE_FRACTION = 0.25

#: CPU seconds a lane's fair share must add up to over the window before a
#: lane burning less than it can be told from one the scheduler has not got
#: round to: ten of the interpreter's 5 ms switch intervals.
RESOLVABLE_CPU_SECONDS = 0.05


@dataclass
class Share:
    """A lane's share of a saturated process - see :func:`fair_share`."""

    process_rate: float
    lanes: int
    fair: float
    threshold: float
    #: Whether the window is long enough for the share to be told from zero.
    resolvable: bool


def fair_share(
    lane: list[dict[str, Any]], process: list[dict[str, Any]], lanes_in_flight: int
) -> Optional[Share]:
    """How a lane's CPU over ``lane``'s window compares with its share of its
    process's, where the process is saturated; None where it is not, and a
    lane is judged against :data:`BUSY_THRESHOLD` as any worker is."""
    if lanes_in_flight < 2 or len(lane) < 2:
        return None
    start = float(lane[0].get("time") or 0) - 1.0
    window = [beat for beat in process if float(beat.get("time") or 0) >= start]
    rate = cpu_rate(window if len(window) >= 2 else process[-2:])
    if rate is None or rate < SATURATED_CORES:
        return None
    fair = rate / lanes_in_flight
    span = float(lane[-1].get("time") or 0) - float(lane[0].get("time") or 0)
    return Share(
        process_rate=rate,
        lanes=lanes_in_flight,
        fair=fair,
        threshold=min(BUSY_THRESHOLD, fair * FAIR_SHARE_FRACTION),
        resolvable=fair * span >= RESOLVABLE_CPU_SECONDS,
    )


def assess_lane(
    beats: list[dict[str, Any]],
    lane: list[dict[str, Any]],
    now: float,
    silent_for: float,
    interval: float,
    lanes_in_flight: int = 1,
) -> Assessment:
    """:func:`assess` for a lane: whether its process is running at all, from
    the process's beats, and whether its own thread is, from ``lane`` - its
    readings as :func:`lane_beats` gives them - against its share of its
    process where the process is saturated (see :func:`fair_share`)."""
    first = assess(beats, now, silent_for, interval)
    if first.state == "SILENT" or first.needs_confirmation:
        return first
    return _from_cpu(
        lane, now, silent_for, first.heartbeat_age or 0.0, LANE_THREAD,
        process=beats, lanes_in_flight=lanes_in_flight,
    )


#: Whose CPU a verdict speaks of: a worker's process, or a lane's own thread.
PROCESS = "the process"
LANE_THREAD = "the lane's thread"


def assess(
    beats: list[dict[str, Any]], now: float, silent_for: float, interval: float
) -> Assessment:
    """First pass, from beats already written."""
    if not beats:
        return Assessment(
            state="SILENT",
            reason="No heartbeat was written this run: the watchdog is off, or nothing "
            "it wrote is on disk, so there is no passive evidence either way.",
        )

    age = now - last_beat_time(beats)
    if age > STALE_BEATS * interval:
        # Do not conclude yet. One missed beat is a scheduling hiccup; what
        # proves a frozen process is a beat that does not advance.
        return Assessment(
            state=None,
            reason="the heartbeat looks stale",
            heartbeat_age=age,
            needs_confirmation=True,
        )
    return _from_cpu(beats, now, silent_for, age)


def confirm(
    beats: list[dict[str, Any]], previous_last: float, now: float, silent_for: float
) -> Assessment:
    """Second pass, one interval later, on freshly read beats."""
    if not beats or last_beat_time(beats) <= previous_last:
        age = now - previous_last
        return Assessment(
            state="FROZEN",
            reason=f"The worker's heartbeat thread has not run for {age:.0f} s. A Python "
            "thread stops running when native code holds the GIL or the process is "
            "stopped.",
            heartbeat_age=age,
        )
    return _from_cpu(beats, now, silent_for, now - last_beat_time(beats))


def _from_cpu(
    beats: list[dict[str, Any]],
    now: float,
    silent_for: float,
    age: float,
    whose: str = PROCESS,
    process: Optional[list[dict[str, Any]]] = None,
    lanes_in_flight: int = 1,
) -> Assessment:
    window = [
        beat for beat in beats if now - float(beat.get("time") or 0) <= silent_for + 5
    ] or beats[-2:]
    rate = cpu_rate(window)
    share = fair_share(window, process, lanes_in_flight) if process else None
    threshold = share.threshold if share is not None else BUSY_THRESHOLD
    if rate is not None and rate > threshold:
        return Assessment(
            state=None,
            reason=f"burning {rate:.2f} cores: slow, not stuck",
            cpu_rate=rate,
            heartbeat_age=age,
        )
    if rate is not None and share is not None:
        shared = (
            f"its process is saturated: its {share.lanes} lanes with a test in flight "
            f"shared {share.process_rate:.2f} cores, about {share.fair:.3f} each"
        )
        if not share.resolvable:
            # A thousand lanes running Python each get a slice of the GIL every
            # few seconds: over this window a busy one can read nothing at all.
            return Assessment(
                state="BLOCKED",
                reason=f"The heartbeat thread is running and {whose} used {rate:.3f} "
                f"cores over the window, but {shared} - too little over this window "
                "to tell a lane waiting on something from one waiting for the GIL.",
                cpu_rate=rate,
                heartbeat_age=age,
                confidence="low",
            )
        return Assessment(
            state="BLOCKED",
            reason=f"The heartbeat thread is running and {whose} used {rate:.3f} cores "
            f"over the window, while {shared}: well under its share, so the test "
            "thread is waiting on something rather than for its turn.",
            cpu_rate=rate,
            heartbeat_age=age,
        )
    if rate is None:
        # One beat, or two stamped at the same instant. "It burned nothing" and
        # "we could not tell" are different findings, and the difference is the
        # whole basis of this verdict - a worker at full tilt produces exactly
        # this input when its beats collide. Still worth reporting, since the
        # heartbeat is alive and the worker has been silent past the limit, but
        # not at the confidence a measured zero earns.
        return Assessment(
            state="BLOCKED",
            reason="The heartbeat thread is running, but no CPU figure could be "
            "measured. This rests on the silence alone, and a busy worker cannot be "
            "ruled out.",
            cpu_rate=None,
            heartbeat_age=age,
            confidence="low",
        )
    return Assessment(
        state="BLOCKED",
        reason=f"The heartbeat thread is running and {whose} used {rate:.2f} cores "
        "over the window: the test thread is waiting on something.",
        cpu_rate=rate,
        heartbeat_age=age,
    )
