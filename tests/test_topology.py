"""What every worker is doing, assembled from files the run wrote anyway.

The statuses are the interesting part, and each of them is a different pair of
facts. A dead process and a frozen one both stop beating; a sleeping test and a
deadlocked one both burn no CPU; a worker at full tilt whose beats collide is
indistinguishable from one doing nothing unless "could not measure" and
"measured zero" are kept apart. So the tests below are mostly about which
evidence produces which word.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from pytest_failure_instrumentation import topology
from pytest_failure_instrumentation.capture import events as events_module
from pytest_failure_instrumentation.capture.events import TAIL_BYTES, tail_events
from pytest_failure_instrumentation.probes import is_running

LIVE = os.getpid()
#: A pid nothing is using. Not reused within the life of one test run.
DEAD = 999999


@pytest.fixture
def evidence(tmp_path):
    """A base directory holding one run, with helpers to populate it."""

    class Evidence:
        def __init__(self) -> None:
            self.base = tmp_path
            self.run = tmp_path / "run-abc123"
            self.run.mkdir()
            (self.run / "owner.json").write_text(
                json.dumps({"pid": LIVE, "session_id": "run-abc123", "started_at": time.time()})
            )

        def state(self, worker: str, **fields) -> None:
            record = {
                "sequence": 4,
                "time": time.time(),
                "pid": LIVE,
                "nodeid": "test_pool.py::test_writes",
                "phase": "call",
                "tests_started": 3,
                "tests_finished": 2,
            }
            record.update(fields)
            (self.run / f"{worker}.state").write_bytes(
                json.dumps(record).encode() + b"\n"
            )

        def beats(self, worker: str, *, count: int = 4, cpu_step: float = 0.0,
                  age: float = 0.0, interval: float = 5.0,
                  run_id: str = "the-reported-run-id", append: bool = False) -> None:
            """``count`` heartbeats ending ``age`` seconds ago.

            ``run_id`` is what every line is stamped with, and ``append`` adds
            them after whatever is already in the file - which is how one
            events file comes to hold two runs' lines.
            """
            lines = [
                json.dumps({"event": "watchdog_started", "interval": interval,
                            "run_id": run_id})
            ]
            last = time.time() - age
            for index in range(count):
                when = last - (count - 1 - index) * interval
                lines.append(
                    json.dumps({
                        "event": "heartbeat",
                        "time": when,
                        "cpu_seconds": index * cpu_step * interval,
                        "rss_mb": 412,
                        "run_id": run_id,
                    })
                )
            path = self.run / f"{worker}.events"
            with path.open("a" if append else "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")

        def schedule(self, **workers) -> None:
            """What the controller writes about who was given what."""
            (self.run / "schedule.json").write_text(
                json.dumps(
                    {
                        "run_id": "the-reported-run-id",
                        "updated_at": time.time(),
                        "dist": "load",
                        "scheduler": "LoadScheduling",
                        "collected": 40,
                        "unassigned": 12,
                        "settled": False,
                        "workers": {
                            name: {
                                "assigned": assigned,
                                "completed": completed,
                                "pending": assigned - completed,
                            }
                            for name, (assigned, completed) in workers.items()
                        },
                    }
                )
            )

        def worker(self, name: str) -> dict:
            return topology.worker(self.run / f"{name}.state", time.time())

    return Evidence()


# -- the statuses ---------------------------------------------------------


def test_a_worker_burning_cpu_is_working(evidence):
    evidence.state("gw0")
    evidence.beats("gw0", cpu_step=0.98)

    described = evidence.worker("gw0")
    assert described["status"] == "working"
    assert described["cpu_rate"] == pytest.approx(0.98, abs=0.02)
    assert "burning" in described["why"]
    assert described["rss_mb"] == 412


def test_a_worker_beating_without_burning_is_blocked(evidence):
    """A sleeping test and a deadlocked one look the same from here, and both
    are waiting on something. Which one it is, the stack says."""
    evidence.state("gw0")
    evidence.beats("gw0", cpu_step=0.0)

    described = evidence.worker("gw0")
    assert described["status"] == "blocked"
    assert described["cpu_rate"] == 0.0
    assert "no CPU progress" in described["why"]


def test_a_worker_that_stopped_beating_is_frozen(evidence):
    """Its own background thread cannot run: native code holds the GIL, or the
    process is stopped. Reported as one observation, because a stall is
    normally confirmed over two passes and a snapshot has only this instant."""
    evidence.state("gw0")
    evidence.beats("gw0", cpu_step=0.5, age=60.0)

    described = evidence.worker("gw0")
    assert described["status"] == "frozen"
    assert described["heartbeat_age_s"] > 10
    assert "ask again to confirm" in described["why"]


def test_a_worker_whose_process_is_gone_says_what_it_was_doing(evidence):
    """The crash case. A replacement worker gets a new id rather than reusing
    this one, so the record of what died survives its death."""
    evidence.state("gw1", pid=DEAD, nodeid="test_pool.py::test_reads")
    evidence.beats("gw1", cpu_step=0.5, age=30.0)

    described = evidence.worker("gw1")
    assert described["status"] == "gone"
    assert described["process_exists"] is False
    assert described["nodeid"] == "test_pool.py::test_reads"
    assert "test_pool.py::test_reads" in described["why"]


def test_a_dead_process_outranks_a_stale_heartbeat(evidence):
    """Both are true of a killed worker - its beats stopped when it did - and
    "gone" is the one a reader can act on."""
    evidence.state("gw1", pid=DEAD)
    evidence.beats("gw1", age=600.0)

    assert evidence.worker("gw1")["status"] == "gone"


def test_a_worker_with_no_heartbeat_at_all_is_unmeasured(evidence):
    """Honest and useless beats a confident guess: with the watchdog off there
    is no passive evidence either way."""
    evidence.state("gw0")

    described = evidence.worker("gw0")
    assert described["status"] == "unmeasured"
    assert "failure_watchdog" in described["why"]
    assert described["cpu_rate"] is None


def test_one_beat_cannot_measure_a_rate_and_says_so(evidence):
    """"It burned nothing" and "we could not tell" are different findings, and
    a worker at full tilt whose beats collide produces the second."""
    evidence.state("gw0")
    evidence.beats("gw0", count=1)

    described = evidence.worker("gw0")
    assert described["cpu_rate"] is None
    assert described["status"] == "blocked"
    assert "cannot be ruled out" in described["why"]


def test_the_heartbeat_interval_is_read_rather_than_assumed(evidence):
    """Staleness is measured in beats. A run configured with a slower one would
    otherwise have every worker declared frozen between them."""
    evidence.state("gw0")
    evidence.beats("gw0", cpu_step=0.5, age=25.0, interval=60.0)

    assert evidence.worker("gw0")["status"] == "working"


# -- runs -----------------------------------------------------------------


def test_a_directory_without_our_marker_is_not_a_run(tmp_path):
    """failure_directory is a natural thing to point at an artifacts
    directory, and describing somebody's build output as a pytest run would be
    a confident lie."""
    stranger = tmp_path / "coverage-html"
    stranger.mkdir()
    (stranger / "index.html").write_text("<html>")

    assert topology.run(stranger) is None
    assert topology.snapshot(tmp_path)["runs"] == []


def test_every_run_on_the_machine_is_described(evidence):
    """A machine can be running several at once - that is the whole reason each
    has its own directory - and a view showing only one would be blind to
    exactly the case that motivates having a view."""
    second = evidence.base / "run-def456"
    second.mkdir()
    (second / "owner.json").write_text(json.dumps({"pid": LIVE}))
    evidence.state("gw0")
    evidence.beats("gw0", cpu_step=0.5)

    snapshot = topology.snapshot(evidence.base, served_by={"pid": LIVE})
    assert [run["session"] for run in snapshot["runs"]] == ["run-abc123", "run-def456"]
    assert snapshot["served_by"]["pid"] == LIVE
    assert snapshot["observed_at"] > 0


def test_a_run_reports_the_id_it_stamps_rather_than_its_directory_name(evidence):
    """The two differ on purpose: the directory is named before xdist has an id
    to offer, and the reported id prefers xdist's so incidents line up with its
    logs. The events are where they are tied together."""
    evidence.state("gw0")
    evidence.beats("gw0")

    described = topology.run(evidence.run)
    assert described["session"] == "run-abc123"
    assert described["run_id"] == "the-reported-run-id"
    assert described["controller"]["alive"] is True


def test_a_run_whose_controller_died_says_so(evidence):
    """Workers still beating under a controller that is gone is a run nobody is
    collecting the results of."""
    (evidence.run / "owner.json").write_text(json.dumps({"pid": DEAD}))
    evidence.state("gw0")

    assert topology.run(evidence.run)["controller"]["alive"] is False


def test_an_elided_node_id_is_flagged(evidence):
    """A consumer matching an id against a collection has to know it was cut."""
    evidence.state("gw0", nodeid="test_a.py::test_b[aaa...zzz]")
    assert evidence.worker("gw0")["nodeid_elided"] is True

    evidence.state("gw1", nodeid="test_a.py::test_b")
    assert evidence.worker("gw1")["nodeid_elided"] is False

    evidence.state("gw2", nodeid=None)
    assert evidence.worker("gw2")["nodeid_elided"] is False


def test_a_worker_between_tests_reports_no_node_id(evidence):
    """Null rather than stale: the last test it ran is not what it is doing."""
    evidence.state("gw0", nodeid=None, phase=None)

    described = evidence.worker("gw0")
    assert described["nodeid"] is None
    assert described["phase"] is None


# -- how much each worker was given ----------------------------------------


def test_a_worker_carries_the_total_it_cannot_know_itself(evidence):
    """Its own files count what it has run; nothing in them says how much it
    was given, because no worker is told. The controller works that out and
    writes it down, and this is where the two halves meet."""
    evidence.state("gw0", tests_started=8, tests_finished=7)
    evidence.beats("gw0", cpu_step=0.9)
    evidence.schedule(gw0=(14, 7), gw1=(11, 4))

    described = topology.run(evidence.run)["workers"][0]
    assert described["tests_finished"] == 7
    assert described["tests_assigned"] == 14
    assert described["tests_pending"] == 7


def test_a_run_says_how_much_of_it_is_nobody_s_yet(evidence):
    """A total that is still growing is a progress bar whose end moves, so the
    queue behind it is reported beside it rather than left to be inferred."""
    evidence.state("gw0")
    evidence.beats("gw0")
    evidence.schedule(gw0=(14, 7))

    described = topology.run(evidence.run)
    assert described["schedule"]["collected"] == 40
    assert described["schedule"]["unassigned"] == 12
    assert described["schedule"]["settled"] is False
    assert described["schedule"]["dist"] == "load"


def test_a_run_with_no_schedule_says_nothing_rather_than_zero(evidence):
    """A single-process run has no scheduler, and a distributed one has none
    until its workers have collected. Zero would be a worker with nothing left
    to do, which is the opposite finding."""
    evidence.state("gw0")
    evidence.beats("gw0")

    described = topology.run(evidence.run)
    assert described["workers"][0]["tests_assigned"] is None
    assert described["workers"][0]["tests_pending"] is None
    assert described["schedule"] == {
        "dist": None, "collected": None, "unassigned": None,
        "settled": None, "updated_at": None,
    }


def test_a_worker_the_schedule_does_not_mention_still_reports(evidence):
    """A worker that came up after the record was last written is a worker,
    not an error - everything else about it is readable from its own files."""
    evidence.state("gw3")
    evidence.beats("gw3")
    evidence.schedule(gw0=(14, 7))

    described = topology.worker(evidence.run / "gw3.state", time.time())
    assert described["worker"] == "gw3"
    assert described["tests_assigned"] is None


# -- evidence another run left in the same directory -----------------------


def test_a_state_slot_another_run_left_behind_is_not_reported_as_this_worker(evidence):
    """The live view was the one reader that skipped the run-id stamp.

    A state file is a fixed slot opened without O_TRUNC and overwritten in
    place, so an earlier run's record survives in it until this run's worker
    reaches its first phase transition - and two sessions sharing a directory,
    which one PYTEST_RUN_ID exported across a build job produces without
    anyone choosing it, overwrite each other's slot for the whole run, because
    every worker in every session is called gw0. The events file cannot be
    confused that way: it is truncated by whoever opens it and every line
    carries the run that wrote it.

    Reporting the slot anyway is not a wrong label on a dashboard. The pid in
    it is what ``sampling`` hands to py-spy, and py-spy stops the process it
    attaches to - so a foreign pid read here is another session's worker
    suspended by this one, on the strength of a record that says so itself.
    """
    evidence.state(
        "gw0",
        run_id="a-session-that-shared-this-directory",
        pid=LIVE,
        nodeid="test_theirs.py::test_not_ours",
    )
    evidence.beats("gw0", cpu_step=0.5, run_id="run-now")

    described = evidence.worker("gw0")
    assert described["pid"] is None
    assert described["nodeid"] is None
    assert described["phase"] is None
    # And through the run view, which is what /workers serves.
    assert topology.run(evidence.run)["workers"][0]["pid"] is None


def test_another_run_s_beats_are_not_this_run_s_evidence(evidence):
    """Which is the reading that produces a verdict, rather than a field.

    A worker that has started and not yet beaten has no passive evidence
    either way, and "unmeasured" says so. Counting the previous session's
    beats instead dates the silence from *their* last beat, which is however
    long ago they finished - so a worker that started a second ago is reported
    frozen, with a confident age attached, and a human is sent looking for a
    stack of a stall that is not happening.
    """
    evidence.state("gw0", run_id="run-now")
    evidence.beats("gw0", count=4, age=600.0, run_id="a-session-that-finished")
    # This run's worker: started, and not yet through a beat interval.
    evidence.beats("gw0", count=0, run_id="run-now", append=True)

    described = evidence.worker("gw0")
    assert described["status"] == "unmeasured"
    assert described["heartbeat_age_s"] is None
    assert described["rss_mb"] is None


def test_a_worker_that_was_never_told_the_run_id_keeps_its_evidence(evidence):
    """The asymmetry both filters are built around, and it is deliberate.

    Only a record naming a *different* run is refused. A worker the controller
    never reached with a run id wrote a run's worth of real state and real
    beats with no stamp on any of it, and dropping those to guard against
    nothing would report a working worker as one that never started.
    """
    evidence.state("gw0", run_id=None, pid=LIVE)
    evidence.beats("gw0", cpu_step=0.98, run_id=None)

    described = evidence.worker("gw0")
    assert described["pid"] == LIVE
    assert described["status"] == "working"


# -- liveness -------------------------------------------------------------


def test_a_killed_but_unreaped_process_is_not_running():
    """Signal 0 alone gets this wrong in the one case that matters most: a
    killed worker stays in the process table until its parent waits on it, and
    the kernel accepts signals for it the whole time."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        deadline = time.monotonic() + 10
        while not is_running(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert is_running(child.pid)

        child.kill()
        # Deliberately not reaped: this is the zombie window.
        deadline = time.monotonic() + 10
        while is_running(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not is_running(child.pid), "a zombie was reported as running"
    finally:
        child.wait(timeout=10)


# -- reading only the recent past -----------------------------------------


def test_the_events_tail_is_bounded_and_drops_the_partial_line(tmp_path):
    """A UI polling every second against a three-hour run would otherwise pay
    for the whole history each time, and pay more as the run goes on."""
    path = tmp_path / "gw0.events"
    lines = [json.dumps({"event": "heartbeat", "n": index}) for index in range(20000)]
    path.write_text("\n".join(lines) + "\n")
    assert path.stat().st_size > TAIL_BYTES

    tailed = tail_events(path)
    assert tailed
    assert len(tailed) < len(lines)
    # The end is intact, which is the part that matters.
    assert tailed[-1]["n"] == 19999
    # And nothing half-parsed came back from the seek landing mid-line.
    assert all(isinstance(event.get("n"), int) for event in tailed)


def test_a_short_events_file_is_read_whole(tmp_path):
    path = tmp_path / "gw0.events"
    path.write_text(json.dumps({"event": "heartbeat", "n": 0}) + "\n")
    assert [event["n"] for event in tail_events(path)] == [0]


def test_a_missing_events_file_is_not_an_error(tmp_path):
    assert tail_events(tmp_path / "nothing.events") == []


# -- asking about particular workers --------------------------------------


def test_only_the_workers_asked_for_are_described(evidence):
    """A caller watching one test does not want the other sixty-three read for
    it, and the filter is applied to the listing rather than to the results."""
    for name in ("gw0", "gw1", "gw2"):
        evidence.state(name, nodeid=f"test_{name}.py::test_one")
        evidence.beats(name, cpu_step=0.5)

    described = topology.run(evidence.run, only=["gw1"])
    assert [entry["worker"] for entry in described["workers"]] == ["gw1"]
    assert described["workers"][0]["nodeid"] == "test_gw1.py::test_one"

    described = topology.run(evidence.run, only=["gw0", "gw2"])
    assert [entry["worker"] for entry in described["workers"]] == ["gw0", "gw2"]


def test_a_name_that_matched_nothing_is_reported(evidence):
    """A caller cannot otherwise tell "not running" from "misspelt"."""
    evidence.state("gw0")
    evidence.beats("gw0")

    snapshot = topology.snapshot(evidence.base, only=["gw0", "gw9"])
    assert snapshot["filter"] == {"workers": ["gw0", "gw9"], "unmatched": ["gw9"]}
    assert [entry["worker"] for run in snapshot["runs"] for entry in run["workers"]] == ["gw0"]


def test_an_empty_filter_is_no_filter(evidence):
    """``?worker=`` is what a UI sends when its filter box is empty, and an
    empty list back would be technically defensible and useless."""
    evidence.state("gw0")
    evidence.beats("gw0")

    for asked in ([], [""], ["  "], None):
        snapshot = topology.snapshot(evidence.base, only=asked)
        assert [entry["worker"] for run in snapshot["runs"] for entry in run["workers"]] == ["gw0"]
        assert "filter" not in snapshot


def test_runs_with_no_matching_worker_drop_out(evidence):
    """A caller that asked about gw0 is not helped by three runs without one."""
    other = evidence.base / "run-def456"
    other.mkdir()
    (other / "owner.json").write_text(json.dumps({"pid": LIVE}))
    (other / "gw7.state").write_bytes(json.dumps({"pid": LIVE, "time": time.time()}).encode())
    evidence.state("gw0")

    snapshot = topology.snapshot(evidence.base, only=["gw0"])
    assert [run["session"] for run in snapshot["runs"]] == ["run-abc123"]
    # Unfiltered, both are there.
    assert len(topology.snapshot(evidence.base)["runs"]) == 2


def test_a_name_that_looks_like_a_path_reaches_nothing(evidence):
    """These names arrive from an HTTP query. They are compared against a
    directory listing and never joined onto one, so a traversal attempt is
    just a name that matches nothing."""
    evidence.state("gw0")
    outside = evidence.base / "outside.state"
    outside.write_bytes(json.dumps({"pid": LIVE, "nodeid": "secret"}).encode())

    snapshot = topology.snapshot(evidence.base, only=["../outside", "/etc/passwd"])
    assert [entry for run in snapshot["runs"] for entry in run["workers"]] == []
    assert snapshot["filter"]["unmatched"] == ["../outside", "/etc/passwd"]


def test_a_long_run_does_not_report_every_healthy_worker_as_frozen(tmp_path):
    """The failure a long run walks into, built as the file state it is.

    "Stale" is measured in heartbeat intervals, and the interval is written
    once - in watchdog_started, before the first beat. The events are read as
    a bounded tail, so once a run is long enough to push that record out of
    the window, the cadence falls back to the default and every worker on a
    slower beat reads as frozen. At a thirty-second beat the window holds
    roughly two and a half hours; a twelve-hour test is not an edge case.

    The cost is a wrong label in the one view that exists to say which
    worker is in trouble - every healthy worker on a slow beat reported frozen.
    """
    now = time.time()
    root = tmp_path / "run"
    root.mkdir()
    (root / "owner.json").write_text(json.dumps({"pid": os.getpid(), "started_at": now - 40_000}))

    state = {"pid": os.getpid(), "nodeid": "test_slow.py::test_long", "phase": "call",
             "time": now, "tests_started": 1, "tests_finished": 0}
    raw = json.dumps(state).encode()
    (root / "gw0.state").write_bytes(raw + b"\x00" * (5120 - len(raw)))

    interval = 30.0
    lines = [json.dumps({"event": "watchdog_started", "interval": interval,
                         "run_id": "r", "time": now - 40_000})]
    # Enough beats to push that first record well past the tail window, which
    # is what a long run does on its own.
    beats = int(events_module.TAIL_BYTES / 180) + 200
    for i in range(beats):
        lines.append(json.dumps({
            "event": "heartbeat", "cpu_seconds": 100.0, "rss_mb": 40,
            "nodeid": "test_slow.py::test_long", "phase": "call", "run_id": "r",
            # The most recent beat is a normal fraction of an interval old,
            # which is what a healthy worker on this cadence looks like at any
            # given instant - and is over the *default* interval's staleness
            # bound while being well inside its own.
            "time": now - 25.0 - (beats - 1 - i) * interval,
        }))
    (root / "gw0.events").write_text("\n".join(lines) + "\n")

    tail = events_module.tail_events(root / "gw0.events")
    assert not any(e.get("event") == "watchdog_started" for e in tail), (
        "the setup did not actually push the record out of the tail window"
    )

    described = topology.run(root, now)
    worker = described["workers"][0]
    # Beating on time, on its configured cadence, using no CPU: blocked, which
    # is a stall worth a look - but emphatically not frozen, which claims the
    # worker's own thread has stopped running.
    assert worker["status"] != "frozen", (
        f"a healthy worker on a {interval:g}s beat was reported frozen: {worker['why']}"
    )
