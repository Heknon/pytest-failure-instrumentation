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
    if measured[-1]["time"] < last_beat_time(beats) - interval - CURRENT_READING_SLACK:
        return []
    return measured


#: Slack on top of one beat's interval for a lane's newest reading to trail
#: the process's newest beat by and still be current.
CURRENT_READING_SLACK = 0.5


def assess_lane(
    beats: list[dict[str, Any]],
    lane: list[dict[str, Any]],
    now: float,
    silent_for: float,
    interval: float,
) -> Assessment:
    """:func:`assess` for a lane: whether its process is running at all, from
    the process's beats, and whether its own thread is, from ``lane`` - its
    readings as :func:`lane_beats` gives them."""
    first = assess(beats, now, silent_for, interval)
    if first.state == "SILENT" or first.needs_confirmation:
        return first
    return _from_cpu(lane, now, silent_for, first.heartbeat_age or 0.0, LANE_THREAD)


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
) -> Assessment:
    window = [
        beat for beat in beats if now - float(beat.get("time") or 0) <= silent_for + 5
    ] or beats[-2:]
    rate = cpu_rate(window)
    if rate is not None and rate > BUSY_THRESHOLD:
        return Assessment(
            state=None,
            reason=f"burning {rate:.2f} cores: slow, not stuck",
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
