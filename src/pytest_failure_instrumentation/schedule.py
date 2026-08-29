"""How many tests each worker was given - a number xdist never keeps.

A live view that says ``gw3 is on test_pool.py::test_writes`` and nothing else
leaves the reader with the one question they came with: *is it nearly done?*
The worker's own ``.state`` slot counts what it has started and finished, so
the numerator has always been there. This module is the denominator.

**Nobody on either side of the run knows it.** A worker collects the whole
suite and is then fed indices a chunk at a time, so it cannot tell an empty
queue from a pause; the controller's scheduler holds what is *outstanding* per
node and discards an index the moment that test completes, so it cannot say how
many that node has been through either. The total exists only as the sum of the
two, and only one process can see both - which is why it is assembled on the
controller and written down for readers rather than derived on demand.

**It is a running total, not a plan.** Under ``--dist load`` and its relatives
the scheduler hands out work in chunks and keeps the rest in a queue nobody is
assigned yet, so a worker's total grows for as long as that queue has anything
in it. ``unassigned`` is what is left there and ``settled`` says when the answer
has stopped moving; a consumer that shows a percentage without them is drawing a
bar whose end moves. Under ``--dist each`` it is settled from the first moment,
because every worker is given the whole collection at once.

**Two scheduler shapes.** The loadscope family keeps ``assigned_work`` - every
test it ever gave a node, with a flag per test saying whether it is done - so
both numbers are read straight off it. Everything else keeps ``node2pending``,
which is indices *outstanding* and nothing more; what has already gone through
is the controller's own count of finished tests, and the total is the two added
together.

**Every figure here is a length or a counter**, deliberately. The obvious way to
reconstruct what has left a queue is to diff it against the last reading, and
that costs the whole outstanding set per test - which under ``worksteal`` and
``each``, where a worker is handed its share up front, is the *suite* per test.
Quadratic bookkeeping to date a progress bar is not a trade this package makes,
so nothing here is bigger than an integer per worker.

What that costs is one instant of imprecision, and where it lands is chosen: a
finished test reaches the controller as a report before the scheduler is told -
the worker sends the two as separate events - so between those two moments the
test is counted as finished *and* still outstanding, and the total reads one
high. The record is written from the start of a test rather than the end of
one, which is a moment that worker is never inside that window, so what is left
is the rare case of a *different* worker's two events straddling this one's.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

#: Written beside ``owner.json`` at the top of a run's directory, and replaced
#: for the life of the run. One file per run rather than one per worker: it is
#: assembled in one process from one object, and splitting it would mean a
#: reader could see two workers at two different instants.
SCHEDULE_FILE = "schedule.json"

#: Smallest gap between two writes of that file. The numbers change on every
#: test, and a controller that wrote them all would pay a rename per test on a
#: run whose readers poll at seconds.
WRITE_INTERVAL = 0.5


def read(directory: Path) -> dict[str, Any]:
    """This run's schedule record, or ``{}`` if there is not one.

    Empty is the ordinary answer, not an error: a single-process run has no
    scheduler, a distributed one has not written this until its workers have
    collected, and a directory belonging to some other tool has none of it.
    Every reader below treats a missing figure as "cannot say" rather than as
    zero, so an empty record degrades to the view there was before this
    existed.
    """
    try:
        loaded = json.loads((directory / SCHEDULE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def worker_rows(record: dict[str, Any]) -> dict[str, Any]:
    """The per-worker part of a record, one row per worker.

    Shaped rather than trusted. Anything may be sitting under this name in a
    directory that is not ours - ``failure_directory`` is a natural thing to
    point at an artifacts directory - and a row that is not a mapping would
    reach the reader as an attribute error out of a live view rather than as
    the missing figure it is.
    """
    rows = record.get("workers")
    if not isinstance(rows, dict):
        return {}
    return {
        name: row
        for name, row in rows.items()
        if isinstance(name, str) and isinstance(row, dict)
    }


class ScheduleTracker:
    """The controller's running count of what each worker has been given.

    One per run, on the controller, driven by the incident engine's hooks. It
    holds no test ids, no collection and no queue - one counter and one row per
    worker - so a sixty-thousand-test suite costs it what a sixty-test one
    does.
    """

    def __init__(self, dist: str = "") -> None:
        #: What ``--dist`` was asked for, reported as-is. It decides nothing
        #: here except whether work can move *between* workers; the shapes
        #: below are read from the scheduler itself, because a plugin may
        #: supply a scheduler of its own under any name.
        self.dist = dist
        #: How many tests each worker has reported finishing. Half of the
        #: total for every scheduler that keeps only what is outstanding.
        self._finished: dict[str, int] = {}
        #: The last row computed for each worker, kept so that one xdist has
        #: let go of still has its final numbers. A worker that died owing
        #: thirteen tests is exactly the case a reader wants them for, and
        #: ``pytest_testnodedown`` is the last moment anything can be read
        #: about it - which is early enough, because xdist fires that hook
        #: before it takes the node out of the scheduler.
        #:
        #: A replacement worker arrives under a new gateway id and so gets a
        #: row of its own; the dead one's is not overwritten by the process
        #: that took over its work. What that costs is that a test the dead
        #: worker was given and somebody else then ran is counted in both
        #: rows - which is why ``collected`` is the run's size and the sum of
        #: these is not.
        self._rows: dict[str, dict[str, int]] = {}
        self._written = 0.0

    def saw_a_test_finish(self, worker: Optional[str]) -> None:
        """A test on ``worker`` has run its last phase.

        Called for every one of them, and the whole per-test cost of this
        module: one dictionary increment. What is *outstanding* is read off
        the scheduler when the record is written, and the two added together
        are the total.
        """
        if worker:
            self._finished[worker] = self._finished.get(worker, 0) + 1

    # -- what it produces ------------------------------------------------

    def record(self, scheduler: Any, run_id: Optional[str] = None) -> dict[str, Any]:
        """Everything a reader needs, as it stands now.

        Workers xdist has already let go of keep the row they had when it did
        - a dead one's is the whole point of keeping any of them.
        """
        workload = _assigned_work(scheduler)
        if workload is not None:
            for name, (assigned, done) in workload.items():
                self._rows[name] = _row(assigned, done)
        else:
            for name, outstanding in (_outstanding(scheduler) or {}).items():
                done = self._finished.get(name, 0)
                self._rows[name] = _row(done + outstanding, done)

        # Before every worker has registered a collection there is no queue
        # yet, and an empty queue read then says "settled" about a run where
        # nothing has been handed out at all - which is the one reading that
        # would have a consumer draw a finished progress bar over a run that
        # has not started.
        collecting = _collection_complete(scheduler) is False
        unassigned = None if collecting else _unassigned(scheduler)
        return {
            "run_id": run_id,
            "updated_at": round(time.time(), 3),
            "dist": self.dist,
            "scheduler": type(scheduler).__name__,
            "collected": _collected(scheduler),
            "unassigned": unassigned,
            "settled": False if collecting else self._settled(scheduler, unassigned),
            "workers": {name: dict(row) for name, row in self._rows.items()},
        }

    def _settled(self, scheduler: Any, unassigned: Optional[int]) -> Optional[bool]:
        """Whether any worker's total can still change.

        Not the same question as "is the queue empty", because ``worksteal``
        moves work *between* workers once it is: a total that has stopped
        growing there can still shrink, and a reader drawing a progress bar
        needs to be told that rather than to find out.
        """
        if unassigned is None:
            return None
        if unassigned:
            return False
        if self.dist != "worksteal":
            return True
        outstanding = _outstanding(scheduler) or {}
        return sum(1 for count in outstanding.values() if count) < 2

    # -- and writes down -------------------------------------------------

    def write(
        self,
        scheduler: Any,
        directory: Path,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """Put the record where readers look, at most every ``WRITE_INTERVAL``.

        ``force`` is for the moments that change the answer rather than
        advance it - a worker going down, a collection arriving - where being
        half a second late is being wrong about which workers exist.

        Replaced rather than rewritten in place: a reader gets the old record
        or the new one and never half of each, which for a file this size is
        the difference between a poll that reports nothing and one that
        reports the truth. Nothing here may break a run, so a filesystem that
        refuses the rename - Windows, if a reader has the file open at that
        instant - costs this write and not the next.
        """
        now = time.time()
        if not force and now - self._written < WRITE_INTERVAL:
            return False
        try:
            payload = json.dumps(self.record(scheduler, run_id))
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a run
            return False
        temporary = directory / f"{SCHEDULE_FILE}.{os.getpid()}.tmp"
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, directory / SCHEDULE_FILE)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            return False
        self._written = now
        return True


def _row(assigned: int, completed: int) -> dict[str, int]:
    return {
        "assigned": assigned,
        "completed": completed,
        "pending": max(0, assigned - completed),
    }


def worker_of(node: Any) -> Optional[str]:
    """The gateway id xdist knows a node by, which is what names its files."""
    name = getattr(getattr(node, "gateway", None), "id", None)
    return str(name) if name else None


def _outstanding(scheduler: Any) -> Optional[dict[str, int]]:
    """How many tests each worker still owes, for the schedulers that say.

    ``load``, ``worksteal`` and ``each`` all keep ``node2pending``; the
    loadscope family does not have it at all, which is what the caller reads a
    ``None`` here as. A length, never the indices: see the module docstring.
    """
    mapping = getattr(scheduler, "node2pending", None)
    if not isinstance(mapping, dict):
        return None
    found: dict[str, int] = {}
    for node, indices in list(mapping.items()):
        name = worker_of(node)
        if name:
            try:
                found[name] = len(indices)
            except TypeError:
                continue
    return found


def _assigned_work(scheduler: Any) -> Optional[dict[str, tuple[int, int]]]:
    """``(assigned, completed)`` per worker, where the scheduler keeps both.

    The loadscope family - ``loadscope``, ``loadfile``, ``loadgroup`` - holds
    every test it has given a node with a done flag beside it, so nothing has
    to be added up from two sources and nothing can drift: both numbers come
    off one object, and the flag is set at the same moment the scheduler
    considers the test complete.
    """
    mapping = getattr(scheduler, "assigned_work", None)
    if not isinstance(mapping, dict):
        return None
    found: dict[str, tuple[int, int]] = {}
    for node, workload in list(mapping.items()):
        name = worker_of(node)
        if not name or not isinstance(workload, dict):
            continue
        assigned = completed = 0
        for scope in workload.values():
            if not isinstance(scope, dict):
                continue
            assigned += len(scope)
            completed += sum(1 for done in scope.values() if done)
        found[name] = (assigned, completed)
    return found


def _collection_complete(scheduler: Any) -> Optional[bool]:
    """Whether every worker has registered a collection.

    Every scheduler xdist ships answers this, as a property or as a plain
    attribute. ``None`` is "it did not say", which is not "no": a scheduler
    somebody else wrote is not second-guessed here.
    """
    value = getattr(scheduler, "collection_is_completed", None)
    return value if isinstance(value, bool) else None


def _collected(scheduler: Any) -> Optional[int]:
    """How many tests the run has, once the workers have agreed on that."""
    collection = getattr(scheduler, "collection", None)
    if isinstance(collection, list):
        return len(collection)
    # ``each`` never builds one list: every worker keeps its own, and they are
    # the same list when the run is healthy. The largest is the one the other
    # variants are reported as missing tests from, which is the convention the
    # collection mismatch report already uses.
    per_worker = getattr(scheduler, "node2collection", None)
    if isinstance(per_worker, dict) and per_worker:
        return max(len(ids) for ids in per_worker.values())
    return None


def _unassigned(scheduler: Any) -> Optional[int]:
    """Tests that are nobody's yet - what every worker's total can still grow by."""
    queue = getattr(scheduler, "pending", None)
    if isinstance(queue, list):
        return len(queue)  # load, worksteal
    queue = getattr(scheduler, "workqueue", None)
    if isinstance(queue, dict):  # the loadscope family, by whole scopes
        return sum(len(unit) for unit in queue.values() if isinstance(unit, dict))
    # ``each`` holds no queue because it has nothing to hold back: every
    # worker is given the whole collection the moment they have all reported
    # one. Before that there is nothing to say, which is not the same as zero.
    if getattr(scheduler, "node2collection", None) is not None:
        return 0 if _collection_complete(scheduler) else None
    return None
