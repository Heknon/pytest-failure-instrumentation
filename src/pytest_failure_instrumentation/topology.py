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

There is a fourth file and it is not a worker's. ``schedule.json`` is the
controller's, and it carries the one fact no worker can write about itself:
how many tests it was given. See :mod:`.schedule` - it is read once per run
here rather than once per worker, so that every row in a snapshot is measured
against the same instant.

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
from .capture.events import head_events, tail_events, this_run
from .capture.heartbeat import DEFAULT_INTERVAL
from .capture.state import ELIDED, read_state
from .probes import is_running
from .schedule import read as read_schedule
from .schedule import worker_rows

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
    # One read for the whole run, not one per worker: it is a single file the
    # controller assembles from a single object, and reading it per worker
    # would report a run whose rows came from different instants.
    schedule = read_schedule(directory)
    rows = worker_rows(schedule)
    workers = [
        worker(state, moment, rows.get(state.stem))
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
        "schedule": _schedule_summary(schedule),
        "workers": workers,
    }


def _schedule_summary(schedule: dict[str, Any]) -> dict[str, Any]:
    """The run-level half of the schedule record, without the per-worker rows.

    Those are already on the workers, and a payload that carried them twice
    would let a consumer join a row to a worker it does not belong to. What is
    left is what a worker's own numbers cannot say: how big the run is, how
    much of it is nobody's yet, and whether any of it can still move.
    """
    return {
        name: schedule.get(name)
        for name in ("dist", "collected", "unassigned", "settled", "updated_at")
    }


def worker(
    state_path: Path, now: float, schedule: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """One worker: what it is doing, and whether it is still doing it.

    The events are read before the state, and that order is the point. An
    events file is opened truncated by the worker that owns it and every line
    in it carries the run that wrote the line, so it says which run the worker
    behind these two files belongs to. A state file says nothing of the kind
    on its own: it is a fixed slot opened without O_TRUNC and overwritten in
    place, so an earlier run's record sits there intact until this run's
    worker reaches its first phase transition - and two sessions that share a
    directory, which ``PYTEST_RUN_ID`` exported across a whole build job
    produces without anyone choosing it, overwrite each other's slot for the
    whole run, because every worker in every session is called ``gw0``.

    So the run id comes from the events and both readings are made as that
    run: ``read_state`` refuses a slot stamped with a different one, and
    ``this_run`` drops beats stamped with a different one. Everything below is
    reported as this run's - the pid most of all. ``sampling`` takes that pid
    and hands it to py-spy, which stops the process it attaches to; an
    unfiltered read there is not a wrong label on a dashboard, it is another
    session's worker suspended by this one.

    Both filters keep an *unstamped* record rather than dropping it - see
    ``read_state`` and ``events.this_run``. A worker the controller never
    reached with a run id wrote a run's worth of real evidence, and discarding
    that to guard against nothing would report a live worker as one that never
    started.

    ``schedule`` is this worker's row out of the controller's record, or None
    where there is not one. It is passed in rather than read here because it
    lives in one file for the whole run - see :func:`run`.
    """
    events = tail_events(state_path.with_name(f"{state_path.stem}.events"))
    run_id = _worker_run_id(events)
    events = this_run(events, run_id)
    record = read_state(state_path, run_id)
    beats = [event for event in events if event.get("event") == "heartbeat"]

    assigned, pending = _progress(record, schedule or {})
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
        # The denominator for the two above, and the only figure here that
        # does not come out of this worker's own files: no worker knows how
        # much it has been given, so the controller works it out and writes it
        # down - see :mod:`.schedule`. None where there is no scheduler to ask
        # (a single-process run) or none yet (before the workers have
        # collected), which is not the same as a worker with nothing to do.
        "tests_assigned": assigned,
        # What is left of that total. Measured from the worker's own count
        # rather than carried over from the controller's, so the three numbers
        # in this row always agree - see _progress.
        "tests_pending": pending,
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


def _progress(
    record: dict[str, Any], schedule: dict[str, Any]
) -> tuple[Optional[int], Optional[int]]:
    """How much this worker was given, and how much of it is left.

    The two halves are written by different processes: the total by the
    controller, into one file for the run, and the count of what has been run
    by the worker itself, into its own slot. So a row that simply reported
    both as it found them could say a worker had finished more tests than it
    was ever given - which is not a lag a reader can interpret, it is a row
    that cannot be true. It happened, on a suite whose tests were shorter than
    the interval the controller's file was then written on.

    A worker cannot finish a test it was never given, so its own count is a
    floor under the total, and what is left is measured from that same count.
    The row then holds together however far apart the two files were written -
    the worst a stale total can do is understate what is left, where before it
    could contradict the line above it.
    """
    assigned = schedule.get("assigned")
    if not isinstance(assigned, int) or isinstance(assigned, bool):
        return None, None
    finished = record.get("tests_finished")
    if not isinstance(finished, int) or isinstance(finished, bool) or finished < 0:
        pending = schedule.get("pending")
        return assigned, pending if isinstance(pending, int) else None
    assigned = max(assigned, finished)
    return assigned, assigned - finished


def _worker_run_id(events: list[dict[str, Any]]) -> Optional[str]:
    """Which run wrote these events, from the events already in hand.

    The most recent stamped record wins. Freshness is the tie-breaker that
    matters: the only way one file holds two runs' lines is two sessions
    writing it at once, and a live view describing what is happening now
    should follow the writer that wrote last.

    Deliberately not a second read of the file. ``worker`` is called once per
    worker per request to ``/workers``, and unlike the heartbeat interval -
    which only ``watchdog_started`` carries, once, at the head, which is why
    ``_interval`` has to go back for it - the run id is on *every* line
    ``EventLog`` writes. The tail already in hand answers it whenever the file
    is stamped at all, so reaching for ``head_events`` here would buy a read
    per worker per poll and nothing else.

    None when nothing is stamped, which is not the same as "no match": both
    callers below read that as "cannot tell, so keep what there is".
    """
    for event in reversed(events):
        if event.get("run_id"):
            return str(event["run_id"])
    return None


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
    test is not an edge case here, it is the case. And a wrong verdict here is
    read by a human watching the run: every healthy worker labelled frozen, in
    the one view that exists to say which worker is in trouble.

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
