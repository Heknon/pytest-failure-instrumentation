"""The denominator: how many tests each worker has been given.

Nothing here is a number xdist keeps. A worker's total is the tests it still
owes - which the scheduler has - plus the tests it has already run, which only
the controller's own count of finished tests knows, and the cases below are
mostly about that sum and about when it stops moving.

The fakes are the scheduler shapes xdist actually has: ``node2pending`` for
``load``, ``worksteal`` and ``each``, and ``assigned_work`` for the loadscope
family. They are what they are because this package reads another project's
internals; the integration test at the bottom is what says the shapes are
still real.
"""

from __future__ import annotations

import json

import pytest

from pytest_failure_instrumentation import schedule, topology
from pytest_failure_instrumentation.schedule import ScheduleTracker

from .conftest import needs_xdist


class Gateway:
    def __init__(self, name: str) -> None:
        self.id = name


class Node:
    """Stands in for xdist's WorkerController, which is only ever read for its
    gateway id here."""

    def __init__(self, name: str) -> None:
        self.gateway = Gateway(name)

    def __hash__(self) -> int:
        return hash(self.gateway.id)


class Pending:
    """``load``, ``worksteal`` and ``each``: indices still owed, and nothing
    that says how many have been through."""

    def __init__(self, collection=None, pending=None, collected=True, **outstanding):
        self.collection = list(collection) if collection is not None else []
        self.pending = list(pending) if pending is not None else []
        self.collection_is_completed = collected
        self.node2pending = {Node(name): list(items) for name, items in outstanding.items()}

    def node(self, name: str) -> Node:
        for node in self.node2pending:
            if node.gateway.id == name:
                return node
        raise KeyError(name)

    def complete(self, name: str, count: int = 1) -> None:
        """What ``mark_test_complete`` does: the index is simply dropped."""
        del self.node2pending[self.node(name)][:count]

    def hand_out(self, name: str, count: int) -> None:
        indices, self.pending = self.pending[:count], self.pending[count:]
        self.node2pending[self.node(name)].extend(indices)

    def steal(self, name: str, count: int) -> None:
        queue = self.node2pending[self.node(name)]
        stolen, queue[-count:] = queue[-count:], []
        self.pending.extend(stolen)


class Each(Pending):
    """``each`` keeps no global queue and no one collection - every worker has
    its own, and every worker runs all of it."""

    def __init__(self, collection, **outstanding):
        super().__init__(**outstanding)
        del self.pending
        del self.collection
        self.node2collection = {node: list(collection) for node in self.node2pending}
        self.collection_is_completed = True


class Workload:
    """The loadscope family: every test it ever gave a node, with a done flag."""

    def __init__(self, collection=None, queued=None, **work):
        self.collection = list(collection) if collection is not None else []
        self.collection_is_completed = True
        self.workqueue = dict(queued or {})
        self.assigned_work = {
            Node(name): {scope: dict(tests) for scope, tests in scopes.items()}
            for name, scopes in work.items()
        }


def rows(tracker: ScheduleTracker, scheduler) -> dict:
    return tracker.record(scheduler)["workers"]


# -- the total ---------------------------------------------------------------


def test_a_worker_that_has_run_nothing_is_all_pending():
    scheduler = Pending(collection=range(10), pending=[4, 5], gw0=[0, 1], gw1=[2, 3])
    tracker = ScheduleTracker("load")

    assert rows(tracker, scheduler)["gw0"] == {"assigned": 2, "completed": 0, "pending": 2}


def test_the_total_grows_as_the_scheduler_hands_out_more():
    """Under load the queue is doled out in chunks, so a worker's total is a
    running answer rather than a plan. Nothing else here would be honest: the
    scheduler itself does not know which worker will get index 9."""
    scheduler = Pending(collection=range(6), pending=[2, 3, 4, 5], gw0=[0], gw1=[1])
    tracker = ScheduleTracker("load")

    tracker.saw_a_test_finish("gw0")
    scheduler.complete("gw0")
    scheduler.hand_out("gw0", 2)

    assert rows(tracker, scheduler)["gw0"] == {"assigned": 3, "completed": 1, "pending": 2}


def test_what_a_worker_has_run_is_counted_rather_than_inferred():
    """The scheduler discards an index the moment its test completes, so what
    has been through a worker exists nowhere but here."""
    scheduler = Pending(collection=range(4), pending=[], gw0=[0, 1, 2, 3])
    tracker = ScheduleTracker("load")

    for _ in range(3):
        tracker.saw_a_test_finish("gw0")
        scheduler.complete("gw0")

    assert rows(tracker, scheduler)["gw0"] == {"assigned": 4, "completed": 3, "pending": 1}


def test_a_stolen_test_stops_being_this_worker_s_total():
    """``worksteal`` moves outstanding work to an idle worker. The tests go
    with it: reporting them here would have two workers owing the same test,
    and their totals adding up to more than the run."""
    scheduler = Pending(collection=range(8), pending=[], gw0=[0, 1, 2, 3], gw1=[])
    tracker = ScheduleTracker("worksteal")
    assert rows(tracker, scheduler)["gw0"]["assigned"] == 4

    scheduler.steal("gw0", 2)

    assert rows(tracker, scheduler)["gw0"] == {"assigned": 2, "completed": 0, "pending": 2}


def test_the_loadscope_family_is_read_rather_than_added_up():
    """It keeps both numbers itself - every test it gave a node with a flag
    beside it - so nothing here has to come from two sources."""
    scheduler = Workload(
        collection=range(4),
        gw0={"test_a.py": {"test_a.py::one": True, "test_a.py::two": False}},
        gw1={"test_b.py": {"test_b.py::one": True}},
    )
    tracker = ScheduleTracker("loadscope")

    assert rows(tracker, scheduler) == {
        "gw0": {"assigned": 2, "completed": 1, "pending": 1},
        "gw1": {"assigned": 1, "completed": 1, "pending": 0},
    }


def test_each_gives_every_worker_the_whole_collection():
    scheduler = Each(range(3), gw0=[0, 1, 2], gw1=[0, 1, 2])
    tracker = ScheduleTracker("each")
    record = tracker.record(scheduler)

    assert record["collected"] == 3
    assert record["unassigned"] == 0
    assert record["settled"] is True
    assert record["workers"]["gw1"] == {"assigned": 3, "completed": 0, "pending": 3}


def test_a_scheduler_this_package_does_not_know_reports_nothing():
    """A plugin may supply its own. Reporting zeros for it would be a
    denominator somebody drew a progress bar with."""

    class Bespoke:
        pass

    record = ScheduleTracker("custom").record(Bespoke())
    assert record["workers"] == {}
    assert record["collected"] is None
    assert record["unassigned"] is None
    assert record["settled"] is None


# -- what the run-level figures are for --------------------------------------


def test_a_queue_with_anything_in_it_is_not_settled():
    scheduler = Pending(collection=range(6), pending=[4, 5], gw0=[0, 1], gw1=[2, 3])
    record = ScheduleTracker("load").record(scheduler)

    assert record["collected"] == 6
    assert record["unassigned"] == 2
    assert record["settled"] is False


def test_a_run_still_collecting_is_never_settled():
    """The queue does not exist until every worker has registered a
    collection, and xdist hands work out inside the same call that announces
    the last one - after the hook this is driven from. An empty queue read at
    that moment would draw a finished progress bar over a run that has not
    started."""
    scheduler = Pending(collection=[], pending=[], collected=False, gw0=[], gw1=[])
    record = ScheduleTracker("load").record(scheduler)

    assert record["settled"] is False
    assert record["unassigned"] is None


def test_an_empty_queue_settles_every_total():
    scheduler = Pending(collection=range(4), pending=[], gw0=[0, 1], gw1=[2, 3])
    assert ScheduleTracker("load").record(scheduler)["settled"] is True


def test_worksteal_is_not_settled_while_two_workers_still_have_work():
    """Its queue emptying stops totals *growing* and not moving: the whole
    point of the mode is that a busy worker's tests can go to an idle one."""
    scheduler = Pending(collection=range(4), pending=[], gw0=[0, 1], gw1=[2, 3])
    assert ScheduleTracker("worksteal").record(scheduler)["settled"] is False

    scheduler.complete("gw1", 2)
    assert ScheduleTracker("worksteal").record(scheduler)["settled"] is True


def test_a_worker_that_shut_down_cleanly_ran_everything_it_was_given():
    """xdist fires ``pytest_testnodedown`` before it takes the node out of the
    scheduler, so there is one accurate reading left at that point: an empty
    queue, and everything it was given accounted for."""
    scheduler = Pending(collection=range(2), pending=[], gw0=[0, 1])
    tracker = ScheduleTracker("load")
    for _ in range(2):
        tracker.saw_a_test_finish("gw0")
        scheduler.complete("gw0")

    assert rows(tracker, scheduler)["gw0"] == {"assigned": 2, "completed": 2, "pending": 0}


def test_a_worker_the_scheduler_has_dropped_keeps_what_it_was_owed():
    """A worker that died owing three tests is the case the frozen row exists
    for: xdist takes the node out of the scheduler shortly after reporting it
    down, and "how much was left" is what a death is triaged with."""
    scheduler = Pending(collection=range(5), pending=[], gw0=[0, 1, 2], gw1=[3, 4])
    tracker = ScheduleTracker("load")
    assert rows(tracker, scheduler)["gw0"]["pending"] == 3

    del scheduler.node2pending[scheduler.node("gw0")]

    assert rows(tracker, scheduler)["gw0"] == {"assigned": 3, "completed": 0, "pending": 3}


# -- the file ----------------------------------------------------------------


def test_the_record_is_written_where_readers_look(tmp_path):
    scheduler = Pending(collection=range(4), pending=[], gw0=[0, 1], gw1=[2, 3])
    tracker = ScheduleTracker("load")

    assert tracker.write(scheduler, tmp_path, "run-id") is True
    written = json.loads((tmp_path / schedule.SCHEDULE_FILE).read_text())

    assert written["run_id"] == "run-id"
    assert written["dist"] == "load"
    assert written["workers"]["gw0"]["assigned"] == 2
    assert schedule.read(tmp_path) == written


def test_writes_are_throttled_and_counting_is_not(tmp_path):
    """The numbers change on every test and a reader polls at seconds, so the
    file is not rewritten per test - but nothing is lost to that, because what
    is throttled is the write and not the count behind it."""
    scheduler = Pending(collection=range(4), pending=[], gw0=[0, 1])
    tracker = ScheduleTracker("load")
    assert tracker.write(scheduler, tmp_path) is True

    tracker.saw_a_test_finish("gw0")
    scheduler.complete("gw0")
    assert tracker.write(scheduler, tmp_path) is False  # too soon

    assert tracker.write(scheduler, tmp_path, force=True) is True
    written = json.loads((tmp_path / schedule.SCHEDULE_FILE).read_text())
    assert written["workers"]["gw0"] == {"assigned": 2, "completed": 1, "pending": 1}


def test_a_directory_with_no_record_reads_as_nothing(tmp_path):
    assert schedule.read(tmp_path) == {}
    assert schedule.worker_rows(schedule.read(tmp_path)) == {}


def test_a_record_that_is_not_ours_reads_as_nothing(tmp_path):
    """``failure_directory`` is a natural thing to point at an artifacts
    directory, so anything at all may be sitting under this name. A row that
    is not a row has to come back as a missing figure rather than as an
    attribute error out of a live view."""
    (tmp_path / schedule.SCHEDULE_FILE).write_text("[1, 2, 3]")
    assert schedule.read(tmp_path) == {}
    assert schedule.worker_rows({"workers": "not a mapping"}) == {}
    assert schedule.worker_rows({"workers": {"gw0": "not a row", "gw1": {"assigned": 2}}}) == {
        "gw1": {"assigned": 2}
    }


def test_a_write_that_cannot_land_costs_this_one_and_not_the_run(tmp_path):
    scheduler = Pending(collection=range(2), pending=[], gw0=[0, 1])
    missing = tmp_path / "no-such-directory"

    assert ScheduleTracker("load").write(scheduler, missing) is False


# -- and what a real run produces --------------------------------------------


CRASHING_SUITE = """
import os
import time

import pytest


@pytest.mark.parametrize("i", range(24))
def test_thing(i):
    if i == 3:
        time.sleep(0.4)
        os._exit(1)
    time.sleep(0.05)
"""


@needs_xdist
def test_a_crashed_worker_keeps_what_it_was_owed_and_the_rest_is_reassigned(pytester):
    """The one case where a row outlives the worker it describes.

    xdist drops a dead worker's queue back into the global one and starts a
    replacement, so what that worker was *given* is a fact about a process
    that no longer exists - and it is the fact a death is triaged with. It
    survives because ``pytest_testnodedown`` fires before xdist takes the node
    out of the scheduler, and because a worker the scheduler no longer has is
    never recomputed.

    The test it died *in* is not reassigned to anybody: xdist reports it
    failed and moves on. So what every worker finished, added up, is the run
    minus that one - and the crash report, which arrives on the dead node and
    is not a teardown, is counted by nobody.
    """
    evidence = pytester.path / "evidence"
    pytester.makepyfile(test_suite=CRASHING_SUITE)
    pytester.makeini(f"[pytest]\nfailure_directory = {evidence}\n")

    result = pytester.runpytest_subprocess("-n2", "--dist=load")
    result.assert_outcomes(passed=23, failed=1)

    runs = [path for path in evidence.iterdir() if path.is_dir()]
    record = schedule.read(runs[0])
    rows_by_worker = record["workers"]

    # The replacement is a worker of its own, under an id of its own - the
    # dead one's row is not overwritten by the process that took its place.
    assert len(rows_by_worker) == 3, rows_by_worker

    owing = {name: row for name, row in rows_by_worker.items() if row["pending"]}
    assert len(owing) == 1, rows_by_worker
    dead = next(iter(owing.values()))
    # At least the test it was inside. Whatever else it had queued went back
    # to the scheduler and was run by somebody else.
    assert dead["pending"] >= 1
    assert dead["completed"] < dead["assigned"]

    # Every test that ran to completion is counted once, by whichever worker
    # ran it - the one that crashed is counted by nobody.
    assert sum(row["completed"] for row in rows_by_worker.values()) == 23
    assert record["collected"] == 24
    assert record["unassigned"] == 0

    # And the sum of what the workers were *given* is larger than the run,
    # by the tests the dead worker was given and somebody else then ran.
    assert sum(row["assigned"] for row in rows_by_worker.values()) > 24


SUITE = """
def test_one(): pass
def test_two(): pass
def test_three(): pass
def test_four(): pass
def test_five(): pass
def test_six(): pass
"""


@needs_xdist
@pytest.mark.parametrize("dist", ["load", "worksteal", "loadfile", "each"])
def test_a_real_run_accounts_for_every_test(pytester, dist):
    """The shapes above are another project's internals, and this is what says
    they are still the shapes. Asserted at the end of the run, where the answer
    is settled: every test in the suite was assigned to somebody, and every
    worker ran everything it was given."""
    evidence = pytester.path / "evidence"
    pytester.makepyfile(test_suite=SUITE)
    pytester.makeini(f"[pytest]\nfailure_directory = {evidence}\n")

    result = pytester.runpytest_subprocess("-n2", f"--dist={dist}")
    result.assert_outcomes(passed=6 if dist != "each" else 12)

    runs = [path for path in evidence.iterdir() if path.is_dir()]
    assert len(runs) == 1, runs
    record = schedule.read(runs[0])

    assert record["dist"] == dist
    assert record["collected"] == 6
    assert record["unassigned"] == 0
    assert record["workers"], record
    for worker, row in record["workers"].items():
        assert row["pending"] == 0, (worker, row)
        assert row["completed"] == row["assigned"], (worker, row)

    ran = sum(row["assigned"] for row in record["workers"].values())
    assert ran == (12 if dist == "each" else 6), record["workers"]

    # And the whole chain: what the controller wrote is what a reader of the
    # evidence directory gets back on the worker row, beside the counters that
    # worker wrote about itself.
    described = topology.run(runs[0])
    assert described["schedule"]["collected"] == 6
    assert described["schedule"]["settled"] is True
    for row in described["workers"]:
        assert row["tests_assigned"] == record["workers"][row["worker"]]["assigned"]
        assert row["tests_pending"] == 0
