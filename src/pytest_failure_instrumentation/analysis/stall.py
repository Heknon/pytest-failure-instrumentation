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


def assess(
    beats: list[dict[str, Any]], now: float, silent_for: float, interval: float
) -> Assessment:
    """First pass, from beats already written."""
    if not beats:
        return Assessment(
            state="SILENT",
            reason="the worker never wrote a heartbeat this run; the watchdog "
            "is disabled, or nothing it wrote is still on disk, so no passive "
            "evidence is available",
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
            reason=f"the worker's own background thread has not run for {age:.0f}s: "
            "native code is holding the GIL, or the process is stopped",
            heartbeat_age=age,
        )
    return _from_cpu(beats, now, silent_for, now - last_beat_time(beats))


def _from_cpu(
    beats: list[dict[str, Any]], now: float, silent_for: float, age: float
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
            reason="the heartbeat is still running but no CPU figure could be "
            "measured, so this rests on the silence alone - a busy worker "
            "cannot be ruled out",
            cpu_rate=None,
            heartbeat_age=age,
            confidence="low",
        )
    return Assessment(
        state="BLOCKED",
        reason="no CPU progress while the heartbeat still runs: the test thread "
        "is blocked, and the stack says on what",
        cpu_rate=rate,
        heartbeat_age=age,
    )
