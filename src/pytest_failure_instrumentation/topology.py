"""What every worker on this machine is doing, assembled rather than recorded.

Nothing here writes anything. Every fact already exists on disk because the
run needs it for its own reasons, and this module is the reader that turns
those files into the one question a UI actually asks: *which test is running
where, and is it still going?*

**Three files, three different questions.** Conflating them is what makes a
live view either expensive or wrong.

``.state`` says *what* a worker is doing - node id, phase, pid - and is
overwritten in place on every phase transition. It is the freshest source
there is, because a worker writes it before the phase runs rather than after,
which is earlier than the controller learns anything. What it cannot say is
whether the worker is still alive: a twenty-minute test writes nothing for
twenty minutes, so a stale record is exactly as consistent with slow work as
with a dead process.

``.events`` carries a heartbeat every few seconds whatever the test is doing,
and that is what answers *whether* rather than *what*. Its cadence is the
resolution of everything below, and the CPU time each beat carries is the only
thing separating a worker that is working from one that is stuck.

The pid answers the narrowest question of the three - does a process with this
number exist - and answers it about a number that can be reused. It is the
weakest signal and is treated as such.

**Nothing here asks a worker anything.** The verdicts come from beats already
written, never from signalling the process, because asking a wedged process a
question can change its answer: a raw syscall in native code that does not
handle EINTR returns early when a signal lands, and the stall being observed
resumes. A view that perturbs what it is viewing is worse than no view.

The classification is :mod:`.analysis.stall`'s, in its own words, with one
difference: a stall is confirmed over two passes an interval apart, and a
snapshot has only this instant. So ``frozen`` here says it is unconfirmed, and
a caller that polls confirms it for free by asking again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection
from pathlib import Path
from typing import Any, Optional

from .analysis import stall as stall_analysis
from .capture.events import head_events, tail_events
from .capture.heartbeat import DEFAULT_INTERVAL
from .capture.state import ELIDED, read_state
from .probes import is_running

#: Written at the top of a run's directory by the controller. Its presence is
#: what makes a directory a run of ours rather than somebody's build output.
OWNER_FILE = "owner.json"

#: Beats to measure a CPU rate over. Few enough that a worker which blocked
#: ten seconds ago reads as blocked rather than as the average of the minute
#: before it; more than two, so a single pair of beats stamped in the same
#: instant does not decide it.
RATE_WINDOW = 4


def snapshot(
    directory: Path,
    served_by: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
    only: Optional[Collection[str]] = None,
) -> dict[str, Any]:
    """Every run under ``directory``, and every worker in each.

    ``directory`` is the *base* - the one ``failure_directory`` names - not a
    single run's. A machine can be running several at once, which is the whole
    reason each has its own directory, and a view that showed only the run it
    happened to be served by would be blind to exactly the situation that
    motivates having a view at all.

    ``only`` narrows it to named workers. A caller watching one test does not
    want the other sixty-three read for it, and the saving is real rather than
    cosmetic: the filter is applied to the directory listing, so a worker that
    was not asked for costs a name comparison instead of a state read and an
    event tail.

    Runs left with no workers at all drop out, since a caller that asked about
    ``gw0`` is not helped by three runs that do not have one. Names that
    matched nothing anywhere are reported rather than silently dropped - a
    caller cannot otherwise tell "not running" from "misspelt".
    """
    moment = time.time() if now is None else now
    wanted = _wanted(only)
    runs = []
    seen: set[str] = set()
    try:
        candidates = sorted(path for path in directory.iterdir() if path.is_dir())
    except OSError:
        candidates = []
    for path in candidates:
        described = run(path, moment, only=wanted)
        if described is None:
            continue
        seen.update(entry["worker"] for entry in described["workers"])
        if wanted is not None and not described["workers"]:
            continue
        runs.append(described)

    found: dict[str, Any] = {
        "served_by": served_by or {},
        "observed_at": round(moment, 3),
        "runs": runs,
    }
    if wanted is not None:
        found["filter"] = {
            "workers": sorted(wanted),
            "unmatched": sorted(wanted - seen),
        }
    return found


def _wanted(only: Optional[Collection[str]]) -> Optional[set[str]]:
    """The set to keep, or None for everything.

    An empty request means "no filter" rather than "nothing": ``?worker=`` is
    what a UI sends when its filter box is empty, and answering that with an
    empty list would be technically defensible and useless.
    """
    if only is None:
        return None
    names = {name.strip() for name in only if name and name.strip()}
    return names or None


def run(
    directory: Path,
    now: Optional[float] = None,
    only: Optional[Collection[str]] = None,
) -> Optional[dict[str, Any]]:
    """One run, or None if this directory is not one of ours.

    The owner file is the test, not the name: ``failure_directory`` is a
    natural thing to point at an artifacts directory, and describing a
    stranger's build output as a pytest run would be a confident lie.
    """
    moment = time.time() if now is None else now
    owner = _owner(directory)
    if owner is None:
        return None

    # Filtered by the name the listing already gave us, never by building a
    # path out of one. These names arrive from an HTTP query, and a filename
    # assembled from that is a directory traversal waiting to be found; the
    # saving is the same either way, because what costs is reading the file
    # rather than listing it.
    wanted = _wanted(only)
    workers = [
        worker(state, moment)
        for state in sorted(directory.glob("*.state"))
        if wanted is None or state.stem in wanted
    ]
    controller_pid = owner.get("pid")
    return {
        "session": directory.name,
        "run_id": _run_id(directory),
        "directory": str(directory),
        "controller": {
            "pid": controller_pid,
            # A controller that is gone while workers are not is a run nobody
            # is collecting the results of any more.
            "alive": is_running(int(controller_pid)) if controller_pid else None,
        },
        "started_at": owner.get("started_at"),
        "workers": workers,
    }


def worker(state_path: Path, now: float) -> dict[str, Any]:
    """One worker: what it is doing, and whether it is still doing it."""
    record = read_state(state_path)
    events = tail_events(state_path.with_name(f"{state_path.stem}.events"))
    beats = [event for event in events if event.get("event") == "heartbeat"]

    pid = record.get("pid")
    exists = is_running(int(pid)) if pid else None
    beat_age = (now - stall_analysis.last_beat_time(beats)) if beats else None
    rate = stall_analysis.cpu_rate(beats[-RATE_WINDOW:]) if len(beats) >= 2 else None
    status, why = _status(
        exists, beats, beat_age, rate, _interval(events, state_path), record
    )

    nodeid = record.get("nodeid")
    return {
        "worker": state_path.stem,
        "pid": pid,
        "nodeid": nodeid,
        # A node id that did not fit its slot is trimmed from both ends. A
        # consumer matching it against a collection has to know that happened.
        "nodeid_elided": ELIDED in nodeid if isinstance(nodeid, str) else False,
        "phase": record.get("phase"),
        "tests_started": record.get("tests_started"),
        "tests_finished": record.get("tests_finished"),
        "state_age_s": _age(now, record.get("time")),
        "rss_mb": beats[-1].get("rss_mb") if beats else None,
        "status": status,
        "why": why,
        "process_exists": exists,
        "heartbeat_age_s": None if beat_age is None else round(beat_age, 2),
        # None is not zero. "It burned nothing" and "we could not tell" are
        # different findings, and a worker at full tilt whose beats collide
        # produces exactly the second one.
        "cpu_rate": None if rate is None else round(rate, 3),
    }


def _status(
    exists: Optional[bool],
    beats: list[dict[str, Any]],
    beat_age: Optional[float],
    rate: Optional[float],
    interval: float,
    record: dict[str, Any],
) -> tuple[str, str]:
    """:mod:`.analysis.stall`'s truth table, as a status rather than a verdict.

    The order matters. A dead process outranks everything - its last heartbeat
    is as old as its death and would otherwise read as ``frozen`` - and the
    absence of any heartbeat at all outranks reasoning from beats there are
    none of.
    """
    if exists is False:
        doing = record.get("nodeid")
        where = f"; last seen in {record.get('phase')} of {doing}" if doing else ""
        return "gone", f"process {record.get('pid')} no longer exists{where}"

    if not beats:
        return (
            "unmeasured",
            "the worker never wrote a heartbeat, so there is no passive "
            "evidence either way: failure_watchdog is off",
        )

    assert beat_age is not None  # beats is non-empty, so this was measured
    if beat_age > stall_analysis.STALE_BEATS * interval:
        return (
            "frozen",
            f"the worker's own background thread has not run for {beat_age:.0f}s: "
            "native code is holding the GIL, or the process is stopped. One "
            "observation - ask again to confirm it is not a scheduling hiccup",
        )

    if rate is None:
        return (
            "blocked",
            "the heartbeat is running but no CPU figure could be measured, so "
            "this rests on the silence alone - a busy worker cannot be ruled out",
        )
    if rate > stall_analysis.BUSY_THRESHOLD:
        return "working", f"heartbeat {beat_age:.1f}s old, burning {rate:.2f} cores"
    return (
        "blocked",
        f"heartbeat {beat_age:.1f}s old but no CPU progress: the test thread is "
        "waiting on something",
    )


def _interval(events: list[dict[str, Any]], state_path: Optional[Path] = None) -> float:
    """The heartbeat cadence this worker was actually started with.

    Read rather than assumed: it is what "stale" is measured in, and a run
    configured with a slower beat would otherwise have every worker declared
    frozen between beats.

    Which is what happened. ``watchdog_started`` is written once, before the
    first beat, and the events are read as a bounded *tail* - so on a run long
    enough to push that record out of the window, the cadence fell back to the
    default and every healthy worker on a slower beat read as frozen. At a
    thirty-second beat the window holds about two and a half hours, so a long
    test is not an edge case here, it is the case. Worse, the sampler acts on
    that verdict: every worker looking frozen is every worker read with py-spy,
    on every pass.

    So the tail is asked first - it is already in hand and holds the answer for
    any run short enough - and the head of the file is read only when the tail
    has scrolled past it.
    """
    for event in reversed(events):
        if event.get("event") == "watchdog_started" and event.get("interval"):
            return float(event["interval"])
    if state_path is not None:
        head = head_events(state_path.with_name(f"{state_path.stem}.events"))
        for event in head:
            if event.get("event") == "watchdog_started" and event.get("interval"):
                return float(event["interval"])
    return DEFAULT_INTERVAL


def _run_id(directory: Path) -> Optional[str]:
    """The id this run reports, which is not the directory's name.

    The directory is named by something the controller fixes for itself before
    xdist exists; the reported id prefers xdist's own, so that an incident
    lines up with xdist's logs. Every event line carries it, which is the only
    place the two are tied together - see ``IncidentEngine.directory``.
    """
    for path in sorted(directory.glob("*.events")):
        for event in tail_events(path):
            if event.get("run_id"):
                return str(event["run_id"])
    return None


def _owner(directory: Path) -> Optional[dict[str, Any]]:
    try:
        loaded = json.loads((directory / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _age(now: float, stamp: Any) -> Optional[float]:
    try:
        return round(now - float(stamp), 2)
    except (TypeError, ValueError):
        return None
