"""The rare-event log: startup, memory high-water marks, internal errors.

Deliberately not per test. A passing test appends nothing here - what a worker
is doing right now lives in the fixed-size state file instead, so this file
stays a handful of lines per worker per run however large the suite is.

Line-buffered and flushed, because its whole purpose is to survive a kill that
gives the process no chance to close anything.

Every line carries the run it belongs to. The evidence directory outlives a
run - a single-process run does not clean it, and a crash can leave it behind -
so a reader that does not check would attribute an earlier run's failure to
this one, with full confidence and the wrong worker's name on it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: Path, run_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self._stream = path.open("w", buffering=1, encoding="utf-8")

    def record(self, event: str, **fields: Any) -> None:
        fields["event"] = event
        fields.setdefault("time", round(time.time(), 3))
        fields.setdefault("run_id", self.run_id)
        try:
            self._stream.write(json.dumps(fields) + "\n")
            self._stream.flush()
        except (ValueError, OSError, TypeError):
            pass  # bookkeeping must never break a run

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:
            pass


def read_events(path: Path) -> list[dict[str, Any]]:
    """Tolerates a truncated final line - a hard kill can cut one in half."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def heartbeats(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event") == "heartbeat"]


def high_water_marks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event") == "memory_high_water"]


def internal_errors(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event") == "internal_error"]


def of_run(events: list[dict[str, Any]], run_id: str | None) -> list[dict[str, Any]]:
    """Only the events this run wrote.

    An unstamped event predates the run id and cannot be placed, so it is
    dropped rather than assumed to be ours: the whole point of the check is
    that a confident wrong answer is worse than no answer.
    """
    if not run_id:
        return events
    return [event for event in events if event.get("run_id") == run_id]


def worker_pid(events: list[dict[str, Any]]) -> int | None:
    for event in events:
        if event.get("event") == "worker_start" and event.get("pid"):
            return int(event["pid"])
    return None


#: How much of an event log to read when only the recent past matters. Beats
#: accumulate with wall-clock rather than with the suite - roughly twelve lines
#: per worker per minute - so a long run's log is large while the part worth
#: reading stays the same size. 64 KiB is many minutes of beats.
TAIL_BYTES = 64 * 1024


def tail_events(path: Path, limit: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """The most recent events, at constant cost however long the run has been.

    ``read_events`` parses the whole file, which is right for a report written
    once and wrong for anything polled: a UI asking every second would pay for
    the entire history each time, and pay more as the run goes on.

    Seeking into the middle of a file lands mid-line, so the first line is
    dropped rather than parsed - the same tolerance ``read_events`` has for a
    truncated last line, at the other end.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]  # the seek landed inside it
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
