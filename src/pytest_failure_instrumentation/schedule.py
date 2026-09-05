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

**Two scheduler shapes, one formula.** The loadscope family keeps
``assigned_work`` - every test it ever gave a node - and everything else keeps
``node2pending``, which is what a node still *owes*. So the total is read
directly from one and added up from the other, and what has gone through is
the controller's own count of finished tests in both cases.

That count is deliberately not taken from ``assigned_work``, which carries a
done flag per test and could answer it. Reading it means walking every test
ever assigned, on every write - which is per test, so the bookkeeping goes
quadratic in the size of the suite. Measured before it was taken out: 1.7ms a
write at sixty thousand assigned tests, near two minutes of controller time
over such a run. A counter the controller already keeps costs a lookup.

**Every figure here is a length, a counter or a count of work units**,
deliberately. The obvious way to reconstruct what has left a queue is to diff
it against the last reading, and that costs the whole outstanding set per test
- which under ``worksteal`` and ``each``, where a worker is handed its share up
front, is the *suite* per test. Quadratic bookkeeping to date a progress bar is
not a trade this package makes, and nothing here scales with the number of
tests: a write is one length per worker, or one length per *scope* per worker
where the scheduler groups them.

What that costs is one instant of imprecision, and where it lands is chosen: a
finished test reaches the controller as a report before the scheduler is told -
the worker sends the two as separate events - so between those two moments the
test is counted as finished *and* still outstanding, and the total reads one
high. The record is written from the start of a test rather than the end of
one, which is a moment that worker is never inside that window, so what is left
is the rare case of a *different* worker's two events straddling this one's.

**Written on every test, and that is what makes it usable.** It was throttled
to twice a second at first, on the reasoning that a reader polls at seconds
anyway. That reasoning is wrong, and measurably: a worker's ``.state`` slot is
read *live*, so pairing it with a record up to half a second old put both in
one row, and on a suite of tenth-of-a-second tests the row said a worker had
finished nineteen tests out of the fifteen it had been given. A number that
cannot be true is worse than a number that is late.

What made the throttle look necessary was writing the file by rename, at 54
microseconds a time. A record is a small buffer at a fixed offset, which is
what :mod:`.capture.state` already writes a worker's state with and costs
0.4 microseconds - so the file is now rewritten whenever a test starts, and a
reader is never more than one test behind. Torn reads are the price, and
:func:`read` retries through them exactly as ``read_state`` does.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

#: What ``worksteal`` will not let a worker's queue fall below before it takes
#: work off somebody else - ``xdist.scheduler.worksteal.MIN_PENDING``, mirrored
#: rather than imported so that a version without it is a missing mode rather
#: than an ImportError at collection. It decides only whether a total that has
#: stopped *growing* can still shrink; see :meth:`ScheduleTracker._settled`.
STEAL_MIN_PENDING = 2

#: Written beside ``owner.json`` at the top of a run's directory, and
#: overwritten in place for the life of the run. One file per run rather than
#: one per worker: it is assembled in one process from one object, and
#: splitting it would mean a reader could see two workers at two different
#: instants.
SCHEDULE_FILE = "schedule.json"


def read(directory: Path) -> dict[str, Any]:
    """This run's schedule record, or ``{}`` if there is not one.

    Empty is the ordinary answer, not an error: a single-process run has no
    scheduler, a distributed one has not written this until its workers have
    collected, and a directory belonging to some other tool has none of it.
    Every reader below treats a missing figure as "cannot say" rather than as
    zero, so an empty record degrades to the view there was before this
    existed.
    """
    path = directory / SCHEDULE_FILE
    for _ in range(2):
        try:
            raw = path.read_bytes().strip()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
        except ValueError:
            # The writer is mid-update. One small write at a fixed offset is
            # not formally atomic, and a stale-but-whole record beats none -
            # which is what ``read_state`` does with the same problem.
            time.sleep(0.01)
            continue
        return loaded if isinstance(loaded, dict) else {}
    return {}


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
        #: The test each worker last finished, which is what tells a rerun of
        #: it from the next test. See :meth:`saw_a_test_finish`.
        self._last_finished: dict[str, str] = {}
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
        #: Opened on the first write and held for the run, so a write is one
        #: syscall rather than a create, a write and a rename.
        self._descriptor: Optional[int] = None
        #: pwrite is one syscall but Unix-only; seek and write is the portable
        #: equivalent, as in :mod:`.capture.state`.
        self._pwrite = getattr(os, "pwrite", None)

    def saw_a_test_finish(self, worker: Optional[str], nodeid: Optional[str] = None) -> None:
        """A test on ``worker`` has run its last phase.

        Called for every one of them, and the whole per-test cost of this
        module: one dictionary increment. What is *outstanding* is read off
        the scheduler when the record is written, and the two added together
        are the total.

        Once per test, which is not once per teardown. A rerun plugin runs a
        failed test's phases again inside the same protocol, and every attempt
        sends the controller a teardown report for the same node id - while
        the scheduler, which is told once when the protocol ends, drops one
        index. Counting each report put the finished half of the total above
        what was ever handed out: 374 of 368 tests, on a suite where six were
        rerun once. So a report naming the test this worker has just finished
        is the same test again and is not counted. Only the last id is kept,
        one string per worker: a rerun is always immediately after the attempt
        it repeats, on the same worker, because it happens inside the same
        call. Given no id at all there is nothing to compare, and every call
        counts.

        What a report alone cannot tell from a rerun is the same id collected
        twice - ``--keep-duplicates`` - and run back to back on one worker.
        That reads one low here, and the worker's own count, which sees the
        protocol boundary and so can tell, floors the total back up where the
        two are joined - see :func:`.topology._progress`.
        """
        if not worker:
            return
        if nodeid is not None:
            if self._last_finished.get(worker) == nodeid:
                return
            self._last_finished[worker] = nodeid
        self._finished[worker] = self._finished.get(worker, 0) + 1

    # -- what it produces ------------------------------------------------

    def record(self, scheduler: Any, run_id: Optional[str] = None) -> dict[str, Any]:
        """Everything a reader needs, as it stands now.

        Workers xdist has already let go of keep the row they had when it did
        - a dead one's is the whole point of keeping any of them.
        """
        given = _assigned_work(scheduler)
        if given is not None:
            for name, assigned in given.items():
                self._rows[name] = _row(assigned, self._finished.get(name, 0))
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

        So the question there is whether a steal can still happen, and that
        takes both halves of xdist's own condition: somebody idle to give the
        work to, and somebody holding enough to be worth taking it from. One
        worker left running the tail of the run is the case that looks settled
        and is not - everyone else is idle, and its queue is exactly what gets
        raided. Two workers holding two tests each is the mirror image: it
        looks unsettled and nothing can move, because neither is idle and
        neither may be taken below the floor.
        """
        if unassigned is None:
            return None
        if unassigned:
            return False
        if self.dist != "worksteal":
            return True
        outstanding = list((_outstanding(scheduler) or {}).values())
        if not outstanding:
            return True
        idle = any(count < STEAL_MIN_PENDING for count in outstanding)
        worth_taking = max(outstanding) > STEAL_MIN_PENDING
        return not (idle and worth_taking)

    # -- and writes down -------------------------------------------------

    def write(
        self, scheduler: Any, directory: Path, run_id: Optional[str] = None
    ) -> bool:
        """Put the record where readers look. Called whenever a test starts.

        One small write at a fixed offset, into a descriptor held for the run.
        That is what lets this happen per test rather than on a timer, and a
        timer is what made a row able to contradict itself - see the module
        docstring. The file is truncated to the record rather than padded to a
        slot, so a run with sixty-four workers is not bounded by a size chosen
        for a run with two.

        Nothing here may break a run, so every failure costs this write and
        not the next one.
        """
        try:
            payload = json.dumps(self.record(scheduler, run_id)).encode("utf-8")
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a run
            return False
        descriptor = self._slot(directory)
        if descriptor is None:
            return False
        try:
            if self._pwrite is not None:
                self._pwrite(descriptor, payload, 0)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, payload)
            # After the write, never before: a reader that catches the gap
            # sees this record followed by the tail of the last one, which
            # does not parse and is retried. Truncating first would leave a
            # window where the file is empty, which parses as "no schedule".
            os.ftruncate(descriptor, len(payload))
        except OSError:
            return False
        return True

    def _slot(self, directory: Path) -> Optional[int]:
        if self._descriptor is None:
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
            try:
                self._descriptor = os.open(str(directory / SCHEDULE_FILE), flags, 0o644)
            except OSError:
                return None
        return self._descriptor

    def close(self) -> None:
        if self._descriptor is None:
            return
        try:
            os.close(self._descriptor)
        except OSError:
            pass
        self._descriptor = None


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


def _assigned_work(scheduler: Any) -> Optional[dict[str, int]]:
    """How many tests each worker has been given, where the scheduler says.

    The loadscope family - ``loadscope``, ``loadfile``, ``loadgroup`` - holds
    every test it has given a node, grouped into the scopes it hands out whole.
    A length per scope answers this; the done flag beside each test does not
    get read, because reading it is a walk of the whole assignment on every
    test - see the module docstring.
    """
    mapping = getattr(scheduler, "assigned_work", None)
    if not isinstance(mapping, dict):
        return None
    found: dict[str, int] = {}
    for node, workload in list(mapping.items()):
        name = worker_of(node)
        if not name or not isinstance(workload, dict):
            continue
        found[name] = sum(
            len(scope) for scope in workload.values() if isinstance(scope, dict)
        )
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
