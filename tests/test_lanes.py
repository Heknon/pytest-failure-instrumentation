"""Lanes of pytest-threadlanes, recorded as the workers they are.

pytest-threadlanes runs xdist's schedulers on threads it calls lanes, several
to a process: ``--lanes 3`` is one process of lanes ``ln0``-``ln2``, and
``-n 2 --lanes 3`` two xdist workers of three lanes each, ``gw0.ln0`` onwards.
One process is then no longer one test in flight, and everything this package
reads per process - the state slot, the stall clock, the dead worker's test -
named whichever lane wrote last. So each lane is recorded as a worker of its
own, and the process as the container of its lanes; see
``pytest_failure_instrumentation/lanes.py``.

Two halves. The first needs nothing installed: the readers - ``/workers``, the
stall and death incidents, the dumps - are fed evidence laid out the way a
process of lanes writes it, and a run *without* lanes is held to what 0.13.1
wrote, key for key. The second runs pytest-threadlanes for real, in all three
of its modes, and skips where it is not installed: it needs Python 3.12 or
later, and the plugin supports 3.9.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation import lanes as thread_lanes
from pytest_failure_instrumentation import stack_server, topology
from pytest_failure_instrumentation.analysis import stall as stall_analysis
from pytest_failure_instrumentation.capture import crash_stack
from pytest_failure_instrumentation.capture.heartbeat import Heartbeat
from pytest_failure_instrumentation.capture.state import WorkerState, read_state
from pytest_failure_instrumentation.incidents import death, stall
from pytest_failure_instrumentation.nodeid import hash_of
from pytest_failure_instrumentation.sampling import SampledWorker, WorkerSampler

from . import without_lanes

LIVE = os.getpid()
RUN_ID = "the-run"


# --- evidence laid out as a process of lanes writes it ---------------------


def _native(name: str) -> int:
    """The native thread id a fake lane records: distinct per name."""
    return 4000 + sum(ord(character) for character in name)


def _ident(name: str) -> int:
    """And the ident faulthandler would print for it."""
    return 0x7F0000000000 + sum(ord(character) for character in name)


class Lanes:
    """One run directory holding processes, their lanes, and their beats."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.run = base / "run-lanes"
        self.run.mkdir()
        (self.run / "owner.json").write_text(
            json.dumps({"pid": LIVE, "session_id": "run-lanes", "started_at": time.time()})
        )

    def process(self, name: str, **fields) -> None:
        """A process's own record, as the container of its lanes."""
        self._state(name, {"nodeid": None, "phase": None, "lanes": True, **fields})

    def lane(self, name: str, process: str, nodeid=None, phase=None, **fields) -> None:
        self._state(
            name,
            {
                "nodeid": nodeid,
                "nodeid_hash": hash_of(nodeid),
                "last_nodeid": nodeid,
                "last_nodeid_hash": hash_of(nodeid),
                "phase": phase,
                "tests_started": 2,
                "tests_finished": 1 if nodeid else 2,
                "process": process,
                "thread_name": f"lane-{name}",
                "thread_id": _native(name),
                "thread_ident": _ident(name),
                "lane": True,
                **fields,
            },
        )

    def _state(self, name: str, fields: dict) -> None:
        record = {
            "sequence": 3,
            "time": time.time(),
            "run_id": RUN_ID,
            "pid": LIVE,
            "tests_started": 0,
            "tests_finished": 0,
            **fields,
        }
        (self.run / f"{name}.state").write_bytes(json.dumps(record).encode() + b"\n")

    def beats(self, process: str, lanes: dict, *, process_step: float, count: int = 4,
              interval: float = 1.0, finish: bool = False) -> None:
        """``count`` beats a second apart, the process's CPU rising by
        ``process_step`` a beat, and on each lane's record a reading taken with
        each beat, rising by the lane's own step."""
        now = time.time()
        lines = [
            {"event": "worker_start", "pid": LIVE, "time": now - 60, "run_id": RUN_ID},
            {"event": "watchdog_started", "interval": interval, "run_id": RUN_ID},
        ]
        for index in range(count):
            lines.append({
                "event": "heartbeat",
                "time": now - (count - 1 - index) * interval,
                "cpu_seconds": index * process_step,
                "rss_mb": 120,
                "nodeid": None,
                "phase": None,
                "run_id": RUN_ID,
            })
        if finish:
            lines.append({"event": "worker_finish", "exitstatus": 0, "time": now, "run_id": RUN_ID})
        (self.run / f"{process}.events").write_text(
            "".join(json.dumps(line) + "\n" for line in lines)
        )
        if lanes:
            (self.run / f"{process}.lanecpu").write_text(json.dumps({
                "run_id": RUN_ID,
                "times": [now - (count - 1 - index) * interval + 0.001 for index in range(count)],
                "lanes": {
                    lane: [index * step for index in range(count)]
                    for lane, step in lanes.items()
                },
            }))


@pytest.fixture
def lanes(tmp_path) -> Lanes:
    return Lanes(tmp_path)


def _hybrid(lanes: Lanes) -> None:
    """``-n 1 --lanes 3``: ``gw0.ln1`` hung, its siblings busy, one idle."""
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "t.py::test_busy", "call")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_hung", "call")
    lanes.lane("gw0.ln2", "gw0")
    lanes.beats(
        "gw0", {"gw0.ln0": 1.0, "gw0.ln1": 0.0, "gw0.ln2": 0.0}, process_step=1.0
    )


def test_every_lane_is_a_row_and_the_process_is_not(lanes):
    _hybrid(lanes)
    rows = {row["worker"]: row for row in topology.run(lanes.run)["workers"]}

    assert sorted(rows) == ["gw0.ln0", "gw0.ln1", "gw0.ln2"]
    assert rows["gw0.ln1"]["nodeid"] == "t.py::test_hung"
    assert rows["gw0.ln1"]["nodeid_hash"] == hash_of("t.py::test_hung")
    assert rows["gw0.ln0"]["nodeid"] == "t.py::test_busy"
    assert rows["gw0.ln2"]["nodeid"] is None
    for name, row in rows.items():
        assert row["process"] == "gw0"
        assert row["thread_name"] == f"lane-{name}"
        assert row["thread_id"] == _native(name)
        # What is per process is the process's: its pid, memory and beat.
        assert row["pid"] == LIVE
        assert row["rss_mb"] == 120
        assert row["heartbeat_age_s"] is not None
        # And what the controller's schedule counts per xdist worker is not
        # a lane's to claim.
        assert row["tests_assigned"] is None


def test_a_lanes_cpu_is_its_own_threads_not_its_processes(lanes):
    """The process burns a core because one lane does; the hung lane beside it
    must still read as blocked, or a stall hides behind a busy sibling."""
    _hybrid(lanes)
    rows = {row["worker"]: row for row in topology.run(lanes.run)["workers"]}

    assert rows["gw0.ln0"]["status"] == "working"
    assert rows["gw0.ln0"]["cpu_rate"] == pytest.approx(1.0)
    assert rows["gw0.ln1"]["status"] == "blocked"
    assert rows["gw0.ln1"]["cpu_rate"] == 0.0


def test_a_lane_reads_its_processes_finish(lanes):
    _hybrid(lanes)
    lanes.beats("gw0", {"gw0.ln0": 0.0}, process_step=0.0, finish=True)
    rows = {row["worker"]: row for row in topology.run(lanes.run)["workers"]}
    assert {row["status"] for row in rows.values()} == {"finished"}


def test_a_process_that_has_not_started_a_lane_is_still_a_row(lanes):
    """Before its first lane starts a test - collecting, say - a process of
    lanes is a worker like any other, and the only row there is."""
    lanes._state("gw0", {"nodeid": None, "phase": None})
    lanes.beats("gw0", {}, process_step=0.0)
    rows = topology.run(lanes.run)["workers"]
    assert [row["worker"] for row in rows] == ["gw0"]
    assert "process" not in rows[0]


def test_lanes_are_filtered_by_name_like_any_worker(lanes):
    _hybrid(lanes)
    described = topology.snapshot(lanes.base, only=["gw0.ln1"])
    rows = described["runs"][0]["workers"]
    assert [row["worker"] for row in rows] == ["gw0.ln1"]


def test_asking_for_a_process_of_lanes_by_name_lists_its_lanes(lanes):
    _hybrid(lanes)
    lanes.process("gw1")
    lanes.lane("gw1.ln0", "gw1", "t.py::test_other", "call")
    lanes.beats("gw1", {"gw1.ln0": 0.0}, process_step=0.0)
    described = topology.snapshot(lanes.base, only=["gw0", "gw9"])
    rows = described["runs"][0]["workers"]
    assert [row["worker"] for row in rows] == ["gw0.ln0", "gw0.ln1", "gw0.ln2"]
    assert described["filter"]["unmatched"] == ["gw9"]


def test_a_lane_carries_its_own_progress_where_the_schedule_counts_lanes(lanes):
    """xdist's scheduler hands work to lanes in a run with -n, so the
    controller's record has a row per lane, meaning what a worker's row means."""
    _hybrid(lanes)
    (lanes.run / "schedule.json").write_text(json.dumps({
        "run_id": RUN_ID, "dist": "loadscope", "collected": 9, "unassigned": 0,
        "settled": True, "rerunning": 0,
        "workers": {"gw0.ln1": {"assigned": 3, "completed": 1, "pending": 2, "rerunning": False}},
    }))
    rows = {row["worker"]: row for row in topology.run(lanes.run)["workers"]}
    hung = rows["gw0.ln1"]
    assert (hung["tests_assigned"], hung["tests_running"], hung["tests_queued"]) == (3, 1, 1)
    assert hung["rerunning"] is False
    # A lane the record has no row for says nothing, as a worker would.
    assert rows["gw0.ln0"]["tests_assigned"] is None


def test_a_thousand_lanes_read_their_process_once(lanes, monkeypatch):
    """Each lane's beats are its process's, and so is the file of its CPU:
    one read of each per request, whatever the lane count."""
    lanes.process("main")
    names = {f"ln{index}": 0.0 for index in range(1000)}
    for name in names:
        lanes.lane(name, "main", f"t.py::test_x[e{name}]", "call")
    lanes.beats("main", names, process_step=0.0)
    reads = []
    real = topology.tail_events
    monkeypatch.setattr(topology, "tail_events", lambda path: reads.append(path) or real(path))
    started = time.perf_counter()
    rows = topology.run(lanes.run)["workers"]
    assert len(rows) == 1000
    assert {row["status"] for row in rows} == {"blocked"}
    assert all(row["cpu_rate"] == 0.0 for row in rows)
    assert len(reads) <= 2 + 1000  # its own, and one empty look per lane
    assert sum(1 for path in reads if path.name == "main.events") == 1
    assert time.perf_counter() - started < 20
    # Once the run has outgrown the tail, the cadence is read from the head
    # of the file: once, not once per lane.
    heads = []
    monkeypatch.setattr(topology, "head_events", lambda path: heads.append(path) or [])
    monkeypatch.setattr(topology, "tail_events", lambda path: [
        event for event in real(path) if event.get("event") != "watchdog_started"])
    topology.run(lanes.run)
    assert len(heads) == 1


def test_a_lane_and_its_process_are_both_addressable_for_a_stack(lanes):
    _hybrid(lanes)
    assert stack_server.worker_pid("gw0.ln1", lanes.base) == LIVE
    assert stack_server.worker_pid("gw0", lanes.base) == LIVE


def test_a_process_name_that_is_not_a_name_is_not_followed(lanes):
    """A lane's record names the file its beats are in, and a record is only
    what some process wrote. One naming ``../x`` reads no beats rather than a
    file outside the run."""
    lanes.lane("ln0", "../../elsewhere", "t.py::test_a", "call")
    row = topology.run(lanes.run)["workers"][0]
    assert row["status"] == "unmeasured"
    assert "process" not in row


def test_a_sample_carries_the_lane_and_a_row_without_one_does_not(lanes):
    _hybrid(lanes)
    sample = WorkerSampler(lanes.run).sample()
    dumped = {row["worker"]: row for row in sample.model_dump()["workers"]}
    assert dumped["gw0.ln1"]["process"] == "gw0"
    assert dumped["gw0.ln1"]["thread_name"] == "lane-gw0.ln1"
    assert "process" not in SampledWorker(worker="gw0").model_dump()
    assert "thread_id" not in json.loads(SampledWorker(worker="gw0").model_dump_json())


def test_the_client_model_carries_the_three_fields():
    pytest.importorskip("httpx")
    from pytest_failure_instrumentation.client import Worker

    row = Worker.model_validate(
        {"worker": "gw0.ln1", "process": "gw0", "thread_name": "lane-gw0.ln1", "thread_id": 7}
    )
    assert (row.process, row.thread_name, row.thread_id) == ("gw0", "lane-gw0.ln1", 7)
    assert Worker.model_validate({"worker": "gw0"}).process is None


def test_the_state_record_gains_keys_only_under_lanes(tmp_path):
    plain = WorkerState(tmp_path / "gw0.state", LIVE, RUN_ID)
    plain.update()
    keys = list(read_state(tmp_path / "gw0.state"))
    assert "lanes" not in keys and "process" not in keys

    plain.update(lanes=True)
    assert read_state(tmp_path / "gw0.state")["lanes"] is True

    lane = WorkerState(
        tmp_path / "gw0.ln0.state", LIVE, RUN_ID,
        lane={"process": "gw0", "thread_name": "lane-gw0.ln0", "thread_id": 5,
              "thread_ident": 6, "lane": True},
    )
    lane.update(nodeid="t.py::test_a", phase="setup")
    record = read_state(tmp_path / "gw0.ln0.state")
    # The record as it always was, then the lane's identity after it.
    assert list(record)[: len(keys)] == keys
    assert list(record)[len(keys):] == ["process", "thread_name", "thread_id", "thread_ident", "lane"]
    assert thread_lanes.is_lane(record) and not thread_lanes.is_lane(read_state(tmp_path / "gw0.state"))


# --- per-lane CPU -----------------------------------------------------------


def test_a_lane_has_its_own_rate_or_none_never_its_processes():
    """The process's CPU is every lane's: an idle lane beside a busy one read
    it, and read as working."""
    beats = [{"time": 1.0, "cpu_seconds": 1.0}, {"time": 2.0, "cpu_seconds": 2.0},
             {"time": 3.0, "cpu_seconds": 3.0}, {"time": 4.0, "cpu_seconds": 4.0}]
    # Current readings: the lane's own.
    measured = stall_analysis.lane_beats(beats, [[3.001, 0.5], [4.001, 0.5]], 1.0)
    assert stall_analysis.cpu_rate(measured) == 0.0
    # A reader caught between the beat and the lanes' readings: still current.
    assert stall_analysis.lane_beats(beats, [[2.001, 0.5], [3.001, 0.5]], 1.0)
    # Readings that stopped while the beats went on - the thread has ended.
    assert stall_analysis.lane_beats(beats, [[1.001, 0.5], [2.001, 0.5]], 1.0) == []
    # Measured once, not at all, or nonsense: no rate.
    assert stall_analysis.lane_beats(beats, [[4.001, 0.5]], 1.0) == []
    assert stall_analysis.lane_beats(beats, None, 1.0) == []
    assert stall_analysis.lane_beats(beats, [["x", 1], [3.0]], 1.0) == []


def test_an_idle_lane_beside_a_busy_one_reads_no_rate_not_working(lanes):
    lanes.process("main")
    lanes.lane("ln0", "main", "t.py::test_busy", "call")
    lanes.lane("ln1", "main")
    lanes.beats("main", {"ln0": 1.0}, process_step=1.0)
    rows = {row["worker"]: row for row in topology.run(lanes.run)["workers"]}
    assert rows["ln0"]["status"] == "working"
    assert rows["ln1"]["cpu_rate"] is None
    assert rows["ln1"]["status"] != "working"
    assert rows["ln1"]["why"].startswith("no test in flight on this lane")


def test_a_lanes_state_age_is_its_phases_and_grows(lanes):
    """The heartbeat writes nothing on a lane's record, so its age is the
    phase's: a lane hung for a minute reads a minute, not the last beat."""
    lanes.process("main")
    lanes.lane("ln0", "main", "t.py::test_hung", "call")
    record = json.loads((lanes.run / "ln0.state").read_text())
    record["time"] = time.time() - 60
    (lanes.run / "ln0.state").write_text(json.dumps(record))
    lanes.beats("main", {"ln0": 0.0}, process_step=0.0)
    (row,) = topology.run(lanes.run)["workers"]
    assert row["state_age_s"] >= 59


def test_the_lanes_cpu_is_one_file_and_the_beat_is_unchanged(tmp_path):
    """The beat of a process of lanes is the line it always was, however many
    lanes it has; every lane's figures are one file of the process, read at
    one instant, the newest six kept."""
    written = []
    calls = []
    Heartbeat(lambda event, **fields: written.append(fields),
              lane_cpu=lambda: calls.append(1))._beat()
    Heartbeat(lambda event, **fields: written.append(fields))._beat()
    assert calls == [1]
    assert list(written[0]) == list(written[1]) == [
        "cpu_seconds", "rss_mb", "nodeid", "nodeid_hash", "phase"]

    record = thread_lanes.LaneCpu(tmp_path / "main.lanecpu", RUN_ID)
    for index in range(8):
        record.record(100.0 + index, {"ln0": index * 0.5, **({"ln1": 1.0} if index < 3 else {})})
    readings = thread_lanes.lane_cpu(tmp_path, "main", RUN_ID)
    assert readings["ln0"] == [[100.0 + index, index * 0.5] for index in range(2, 8)]
    # A lane measured no more drops out once its readings are all gaps.
    assert readings["ln1"] == [[102.0, 1.0]]
    assert thread_lanes.lane_cpu(tmp_path, "main", "another-run") == {}


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="psutil numbers macOS threads by position, so no lane CPU is read there",
)
def test_every_lanes_cpu_is_recorded_after_a_beat(tmp_path):
    """The recorder of a process of lanes measures each lane's own thread."""
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    from pytest_failure_instrumentation.config import Settings

    recorder = WorkerRecorder(tmp_path, "main", Settings(watchdog=False), lanes=True)
    try:
        done = threading.Event()
        opened = []

        def lane() -> None:
            opened.append(recorder._open_lane("ln0"))
            sum(range(2_000_000))
            done.wait(5)

        thread = threading.Thread(target=lane, name="lane-ln0")
        thread.start()
        while not opened:
            time.sleep(0.01)
        recorder._record_lane_cpu()
        # A lane opening its slot holds the lanes' lock across file I/O; the
        # beat does not queue behind it.
        with recorder._lanes_lock:
            recorder._record_lane_cpu()
        done.set()
        thread.join()
        record = read_state(tmp_path / "ln0.state")
        assert record["thread_name"] == "lane-ln0"
        assert record["thread_id"] == thread.native_id
        assert "cpu" not in record
        assert len(thread_lanes.lane_cpu(tmp_path, "main")["ln0"]) == 2
        assert read_state(tmp_path / "main.state")["lanes"] is True
    finally:
        recorder.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's per-thread clocks")
def test_a_threads_cpu_is_read_by_its_native_id_without_procfs(monkeypatch):
    """Reading /proc per thread lets go of the GIL once per file, and each
    time the heartbeat has to win it back from every running lane: one
    reading took 17.7 s beside twenty busy lanes. The thread's own CPU clock
    is read in one call, and a thread that has gone is left out."""
    from pytest_failure_instrumentation.probes import process

    def refuse(*args, **kwargs):
        raise AssertionError("procfs was read")

    monkeypatch.setattr(process.psutil, "Process", refuse)
    ready, done = threading.Event(), threading.Event()

    def spin() -> None:
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            pass
        ready.set()
        done.wait(5)

    thread = threading.Thread(target=spin)
    thread.start()
    ready.wait(5)
    gone = threading.Thread(target=lambda: None)
    gone.start()
    gone.join()
    try:
        used = process.thread_cpu_seconds([thread.native_id, gone.native_id])
    finally:
        done.set()
        thread.join()
    assert set(used) == {thread.native_id}
    assert 0.2 <= used[thread.native_id] < 5


def test_macos_reads_no_lanes_cpu_and_says_so(tmp_path, monkeypatch):
    """psutil numbers macOS threads by position, not by native id."""
    from pytest_failure_instrumentation.capture import recorder as recorder_module
    from pytest_failure_instrumentation.config import Settings

    monkeypatch.setattr(recorder_module.sys, "platform", "darwin")
    recorder = recorder_module.WorkerRecorder(
        tmp_path, "main", Settings(watchdog=False), lanes=True
    )
    try:
        assert recorder._lane_cpu_readable() is False
        events = (tmp_path / "main.events").read_text()
        assert '"mechanism": "per_lane_cpu"' in events
    finally:
        recorder.close()


def test_lanes_are_asked_for_only_with_pytest_threadlanes_registered():
    """An option whose dest is ``lanes`` can be anybody's."""

    class Plugins:
        def __init__(self, names):
            self.names = names

        def hasplugin(self, name):
            return name in self.names

    class Config:
        def __init__(self, names, lanes):
            self.pluginmanager = Plugins(names)
            self.lanes = lanes

        def getoption(self, name, default=None):
            return self.lanes if name == "lanes" else default

    assert thread_lanes.requested(Config({"threadlanes"}, 3))
    assert not thread_lanes.requested(Config(set(), 3))
    assert not thread_lanes.requested(Config({"threadlanes"}, 0))
    assert not thread_lanes.requested(Config({"threadlanes"}, None))


# --- stalls -----------------------------------------------------------------


def _stall(lanes: Lanes, worker: str):
    return stall.build(worker, lanes.run, 10.0, 1.0, stack_probe=False, run_id=RUN_ID)


def test_a_hung_lane_in_a_busy_process_is_blamed_for_its_own_test(lanes):
    _hybrid(lanes)
    incident = _stall(lanes, "gw0.ln1")

    assert incident is not None
    assert incident.worker == "gw0.ln1"
    assert incident.state == "BLOCKED"
    assert incident.cpu_rate == 0.0
    assert incident.test_in_flight == "t.py::test_hung"
    assert incident.test_in_flight_hash == hash_of("t.py::test_hung")
    assert any("lane-gw0.ln1" in line and "gw0" in line for line in incident.evidence)


def test_a_busy_lane_is_slow_not_stuck_and_an_idle_one_is_neither(lanes):
    _hybrid(lanes)
    assert _stall(lanes, "gw0.ln0") is None
    assert _stall(lanes, "gw0.ln2") is None


def test_a_lane_that_never_had_a_test_is_idle_not_silent(lanes):
    """More lanes than work: a lane the engine knows of, with no record."""
    _hybrid(lanes)
    assert stall.build("ln9", lanes.run, 10.0, 1.0, False, run_id=RUN_ID,
                       known_lane=True) is None
    # A worker that is not a lane, with nothing on disk, is what it was.
    assert stall.build("gw9", lanes.run, 10.0, 1.0, False, run_id=RUN_ID).state == "SILENT"


def test_a_process_of_lanes_is_silent_for_its_lanes_while_any_has_a_test(lanes):
    """Its lanes are watched one by one. Its silence is its own only once none
    of them is running anything, and then it is judged as any worker with no
    test running: at low confidence, and saying its lanes were idle."""
    _hybrid(lanes)
    assert _stall(lanes, "gw0") is None

    lanes.lane("gw0.ln0", "gw0")
    lanes.lane("gw0.ln1", "gw0")
    lanes.beats("gw0", {"gw0.ln0": 0.0, "gw0.ln1": 0.0}, process_step=0.0)
    incident = _stall(lanes, "gw0")
    assert incident is not None
    assert incident.test_in_flight is None
    assert incident.confidence == "low"
    assert "None of this process's 3 lanes had a test in flight." in incident.evidence


def test_a_stack_is_the_lanes_thread_out_of_a_dump_of_all_of_them(tmp_path):
    lane, sibling = 0x7F00000000A1, 0x7F00000000B2
    dump = tmp_path / "gw0.crash"
    dump.write_text(
        "Current thread 0x00007f00000000c3 (most recent call first):\n"
        '  File "runner.py", line 318 in _pump_events\n'
        f"Thread 0x{sibling:016x} (most recent call first):\n"
        '  File "t.py", line 9 in test_busy\n'
        '  File "_pytest/runner.py", line 1 in pytest_runtest_call\n'
        f"Thread 0x{lane:016x} (most recent call first):\n"
        '  File "t.py", line 6 in test_hung\n'
        '  File "_pytest/runner.py", line 1 in pytest_runtest_call\n'
    )
    assert "_pump_events" in crash_stack.read(dump)[1]
    assert "test_hung" in crash_stack.read(dump, thread=(lane, None))[1]
    live = [
        {"thread_id": sibling, "thread_name": "lane-gw0.ln0",
         "frames": [{"file": "t.py", "line": 9, "function": "test_busy"},
                    {"file": "_pytest/runner.py", "line": 1, "function": "pytest_runtest_call"}]},
        {"thread_id": lane, "thread_name": "lane-gw0.ln1",
         "frames": [{"file": "t.py", "line": 6, "function": "test_hung"}]},
    ]
    assert "test_hung" in crash_stack.from_threads(live, thread=(None, "lane-gw0.ln1"))[1]
    assert "test_busy" in crash_stack.from_threads(live)[1]


def test_a_lanes_stack_is_its_thread_or_nothing_never_a_siblings(tmp_path):
    """faulthandler stops at a hundred threads: a lane's thread missing from a
    dump must not be stood in for by a sibling's, whose test was not stuck.
    The one found is named, as faulthandler does not name it."""
    lane, sibling = 0x7F00000000A1, 0x7F00000000B2
    dump = tmp_path / "gw0.crash"
    dump.write_text(
        f"Thread 0x{sibling:016x} (most recent call first):\n"
        '  File "t.py", line 9 in test_busy\n'
        '  File "_pytest/runner.py", line 1 in pytest_runtest_call\n'
    )
    assert crash_stack.read(dump, thread=(lane, "lane-gw0.ln1")) == []
    named = crash_stack.read(dump, thread=(sibling, "lane-gw0.ln0"))
    assert named[0] == f"Thread 0x{sibling:016x} (lane-gw0.ln0, most recent call first):"


def test_a_single_process_lanes_stack_is_read_from_its_own_frames(lanes):
    """In a run with no -n the process assessing the stall is the one running
    the lane: its thread's frames are read directly, whatever the thread
    count and whatever py-spy can read."""
    started, release = threading.Event(), threading.Event()

    def stuck_in_provisioning():
        started.set()
        release.wait(10)

    thread = threading.Thread(target=stuck_in_provisioning, name="lane-ln0")
    thread.start()
    started.wait(5)
    try:
        lanes.process("main")
        lanes.lane("ln0", "main", "t.py::test_hung", "call",
                   thread_ident=thread.ident, thread_name="lane-ln0")
        lanes.beats("main", {"ln0": 0.0}, process_step=1.0)
        incident = _stall(lanes, "ln0")
    finally:
        release.set()
        thread.join()
    assert incident.stack_source == "frames"
    assert incident.stack[0].startswith(f"Thread 0x{thread.ident:016x} (lane-ln0,")
    assert any("stuck_in_provisioning" in line for line in incident.stack)
    # The lane's own CPU, said as such.
    assert "the lane's thread used 0.00 cores" in incident.reason
    # Its siblings are idle, and it says the run waits on this lane - not on
    # xdist, which is not running this one.
    assert not any("go on running" in line for line in incident.evidence)
    assert "The run cannot finish while this lane's test is still running." in incident.evidence


def test_a_workers_lanes_share_one_read_of_their_process_per_poll(lanes, monkeypatch):
    """Forty stalled lanes of a worker were forty reads of it in one poll."""
    lanes.process("gw0", pid=424242)
    for name in ("gw0.ln0", "gw0.ln1"):
        lanes.lane(name, "gw0", f"t.py::test_{name[-1]}", "call", pid=424242)
    lanes.beats("gw0", {"gw0.ln0": 0.0, "gw0.ln1": 0.0}, process_step=0.0)
    reads = []

    def live_stack(pid):
        reads.append(pid)
        return [{"thread_id": _ident(name), "os_thread_id": _native(name),
                 "thread_name": f"lane-{name}",
                 "frames": [{"file": "t.py", "line": 3, "function": "wait_here"}]}
                for name in ("gw0.ln0", "gw0.ln1")], None

    monkeypatch.setattr(stall.probes, "live_stack", live_stack)
    shared = {}
    found = [
        stall.build(name, lanes.run, 10.0, 1.0, True, run_id=RUN_ID, live_pid=424242,
                    shared=shared)
        for name in ("gw0.ln0", "gw0.ln1")
    ]
    assert reads == [424242]
    for incident, name in zip(found, ("gw0.ln0", "gw0.ln1")):
        assert incident.stack_source == "py-spy"
        assert f"lane-{name}" in incident.stack[0]


def test_a_frozen_process_of_lanes_is_one_incident_blaming_no_innocent_lane(lanes):
    """Its heartbeat stopped: every lane went silent with it. That is one
    finding, the process's, and no lane is blamed without evidence."""
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "t.py::test_a", "call")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_b", "call")
    lanes.lane("gw0.ln2", "gw0")
    lanes.beats("gw0", {"gw0.ln0": 0.0, "gw0.ln1": 0.0}, process_step=0.0)
    events = lanes.run / "gw0.events"
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    for line in lines:
        if line.get("event") == "heartbeat":
            line["time"] -= 30  # nothing has beaten for half a minute
    events.write_text("".join(json.dumps(line) + "\n" for line in lines))
    incident = stall.build("gw0.ln1", lanes.run, 30.0, 1.0, False, run_id=RUN_ID)

    assert incident.worker == "gw0"
    assert incident.state == "FROZEN"
    assert incident.test_in_flight is None
    assert incident.suspect_nodeid() is None
    assert [lane["lane"] for lane in incident.lanes_in_flight] == ["gw0.ln0", "gw0.ln1"]
    assert "while running 2 tests on its lanes" in incident.summary()
    assert not any("go on running" in line for line in incident.evidence)


def test_a_frozen_process_blames_the_lane_py_spy_finds_holding_the_gil(lanes, monkeypatch):
    """Native code holding the GIL: py-spy reads the stopped interpreter and
    names the thread that owns it - that lane's test, and only that one."""
    lanes.process("gw0", pid=424242)
    for name in ("gw0.ln0", "gw0.ln1"):
        lanes.lane(name, "gw0", f"t.py::test_{name[-1]}", "call", pid=424242)
    lanes.beats("gw0", {"gw0.ln0": 0.0, "gw0.ln1": 0.0}, process_step=0.0)
    events = lanes.run / "gw0.events"
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    for line in lines:
        if line.get("event") == "heartbeat":
            line["time"] -= 30
    events.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def live_stack(pid):
        return [{"thread_id": _ident(name), "os_thread_id": _native(name),
                 "thread_name": f"lane-{name}", "owns_gil": name == "gw0.ln1",
                 "frames": [{"file": "t.py", "line": 3, "function": f"in_{name[-3:]}"}]}
                for name in ("gw0.ln0", "gw0.ln1")], None

    monkeypatch.setattr(stall.probes, "live_stack", live_stack)
    monkeypatch.setattr(stall, "_stopped", lambda pid: False)
    incident = stall.build("gw0.ln0", lanes.run, 30.0, 1.0, True, run_id=RUN_ID,
                           live_pid=424242, shared={})

    assert incident.worker == "gw0"
    assert incident.state == "FROZEN"
    assert incident.test_in_flight == "t.py::test_1"
    assert any("lane gw0.ln1's thread holding the GIL" in line for line in incident.evidence)
    assert incident.stack_source == "py-spy"
    assert "lane-gw0.ln1" in incident.stack[0]
    assert all("in_ln0" not in line for line in incident.stack)


def test_the_watchdog_keeps_a_clock_per_lane(tmp_path):
    watchdog = crash_stack.SlowTestWatchdog(tmp_path / "main.slow", 0.05)
    watchdog.start_lane("ln0")
    watchdog.start_lane("ln1")
    time.sleep(0.1)
    watchdog.tick()
    assert (tmp_path / "main.slow").exists()
    # One lane finishing leaves the dump for the other, which is still overdue.
    watchdog.end_lane("ln0")
    assert (tmp_path / "main.slow").exists()
    watchdog.end_lane("ln1")
    assert not (tmp_path / "main.slow").exists()


# --- deaths -----------------------------------------------------------------


def _died(lanes: Lanes):
    """A process of lanes that ended without reaching session finish, as a
    later run finds it."""
    return death.recover(lanes.run / "gw0.events", session="run-lanes")


def test_a_death_with_one_lane_in_flight_names_that_lanes_test(lanes):
    lanes.process("gw0", tests_started=0)
    lanes.lane("gw0.ln0", "gw0")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_crash", "call", tests_started=3, tests_finished=2)
    lanes.beats("gw0", {}, process_step=0.0)
    incident = _died(lanes)

    assert incident.test_in_flight == "t.py::test_crash"
    assert incident.phase == "call"
    # The process's counts, which are every lane's: the worker that died is
    # the process.
    assert incident.tests_started == 5
    assert incident.tests_finished == 4
    assert incident.lanes_in_flight == [{
        "lane": "gw0.ln1", "nodeid": "t.py::test_crash",
        "nodeid_hash": hash_of("t.py::test_crash"), "phase": "call",
    }]
    assert incident.suspect_nodeid() == "t.py::test_crash"
    assert "on lane gw0.ln1" in incident.summary()


def test_a_death_with_several_lanes_in_flight_lists_them_and_blames_none(lanes):
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "t.py::test_a", "call")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_b", "setup")
    lanes.lane("gw0.ln2", "gw0")
    lanes.beats("gw0", {}, process_step=0.0)
    incident = _died(lanes)

    assert incident.test_in_flight is None
    assert incident.last_test is None
    assert [lane["lane"] for lane in incident.lanes_in_flight] == ["gw0.ln0", "gw0.ln1"]
    assert incident.suspect_nodeid() is None
    assert incident.tests_started == 6
    assert "2 tests at once, on lanes gw0.ln0, gw0.ln1" in incident.summary()
    assert any("t.py::test_b (setup)" in line for line in incident.evidence)


def test_a_fatal_dump_on_one_lanes_thread_blames_that_lanes_test(lanes):
    """A native fault is delivered to the thread that faulted, and
    faulthandler calls it current: that is which lane took the process down.
    The others went down with it, and are still listed."""
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "t.py::test_a", "call")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_b", "call")
    lanes.beats("gw0", {}, process_step=0.0)
    culprit = _ident("gw0.ln1")
    (lanes.run / "gw0.crash").write_text(
        "Fatal Python error: Segmentation fault\n\n"
        f"Current thread 0x{culprit:016x} (most recent call first):\n"
        '  File "/src/t.py", line 9 in test_b\n'
        "Thread 0x00007f00000000ff (most recent call first):\n"
        '  File "/src/t.py", line 4 in test_a\n'
    )
    incident = _died(lanes)

    assert incident.test_in_flight == "t.py::test_b"
    assert incident.suspect_nodeid() == "t.py::test_b"
    assert [lane["lane"] for lane in incident.lanes_in_flight] == ["gw0.ln0", "gw0.ln1"]
    assert "on lane gw0.ln1" in incident.summary()
    assert any("written on lane gw0.ln1's thread" in line for line in incident.evidence)


def test_a_dump_that_is_not_fatal_blames_no_lane(lanes):
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "t.py::test_a", "call")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_b", "call")
    lanes.beats("gw0", {}, process_step=0.0)
    culprit = _ident("gw0.ln1")
    (lanes.run / "gw0.crash").write_text(
        f"Current thread 0x{culprit:016x} (most recent call first):\n"
        '  File "/src/t.py", line 9 in test_b\n'
    )
    assert _died(lanes).test_in_flight is None


def test_a_fault_on_no_lanes_thread_blames_no_lane(lanes):
    """A thread a passed test left behind faulted: the one lane running a test
    is not what died, and the dump says so."""
    lanes.process("gw0")
    lanes.lane("gw0.ln1", "gw0", "t.py::test_b", "call")
    lanes.beats("gw0", {}, process_step=0.0)
    (lanes.run / "gw0.crash").write_text(
        "Fatal Python error: Segmentation fault\n\n"
        "Current thread 0x00007f00000000ff (most recent call first):\n"
        '  File "/src/t.py", line 6 in <lambda>\n'
    )
    incident = _died(lanes)
    assert incident.test_in_flight is None
    assert incident.suspect_nodeid() is None
    assert [lane["lane"] for lane in incident.lanes_in_flight] == ["gw0.ln1"]
    assert "does not blame" in incident.summary()
    assert any("none of these lanes'" in line for line in incident.evidence)


def test_a_free_threaded_dump_blames_the_lane_whose_test_is_on_it(lanes):
    """With the GIL disabled faulthandler prints one stack and no thread id."""
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "tests/test_a.py::test_one[x]", "call")
    lanes.lane("gw0.ln1", "gw0", "tests/test_b.py::test_two[y]", "call")
    lanes.beats("gw0", {}, process_step=0.0)
    (lanes.run / "gw0.crash").write_text(FREE_THREADED_DUMP.format(function="test_two"))
    incident = _died(lanes)
    assert incident.test_in_flight == "tests/test_b.py::test_two[y]"
    assert len(incident.lanes_in_flight) == 2
    # Its Python stack, not the C stack trace Python 3.14 prints after it -
    # whose header, "Current thread's C stack trace", reads as a thread's.
    assert incident.crash_stack[1] == "Stack (most recent call first):"
    assert any("in test_two" in line for line in incident.crash_stack)
    assert not any("Binary file" in line for line in incident.crash_stack)


def test_a_free_threaded_dump_of_a_function_several_lanes_run_blames_none(lanes):
    """Two lanes in the same test function: the stack cannot tell them apart,
    and the dump names no thread - and that is what it says, not that the
    fault was on a thread that is none of theirs."""
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "tests/test_b.py::test_two[x]", "call")
    lanes.lane("gw0.ln1", "gw0", "tests/test_b.py::test_two[y]", "call")
    lanes.beats("gw0", {}, process_step=0.0)
    (lanes.run / "gw0.crash").write_text(FREE_THREADED_DUMP.format(function="test_two"))
    incident = _died(lanes)
    assert incident.test_in_flight is None
    assert any("running on 2 of these lanes (gw0.ln0, gw0.ln1)" in line
               for line in incident.evidence)
    assert not any("none of these lanes'" in line for line in incident.evidence)

    (lanes.run / "gw0.crash").write_text(FREE_THREADED_DUMP.format(function="helper"))
    incident = _died(lanes)
    assert incident.test_in_flight is None
    assert any("no lane's test is on the stack that faulted" in line
               for line in incident.evidence)


#: What a free-threaded 3.14 writes as it dies of a fault.
FREE_THREADED_DUMP = (
    "Fatal Python error: Segmentation fault\n\n"
    "<Cannot show all threads while the GIL is disabled>\n"
    "Stack (most recent call first):\n"
    '  File "/usr/lib/python3.14t/ctypes/__init__.py", line 590 in string_at\n'
    '  File "/src/tests/test_b.py", line 9 in {function}\n'
    "\n"
    "Current thread's C stack trace (most recent call first):\n"
    '  Binary file "/usr/bin/python3.14t", at _Py_DumpStack+0x30 [0x4b53b0]\n'
    '  Binary file "/lib/x86_64-linux-gnu/libc.so.6", at +0x45330 [0x7f6108645330]\n'
)


def test_a_death_between_tests_counts_every_lanes_tests(lanes):
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0")
    lanes.lane("gw0.ln1", "gw0")
    lanes.beats("gw0", {}, process_step=0.0)
    incident = _died(lanes)
    assert incident.test_in_flight is None
    assert incident.tests_finished == 4
    assert "lanes_in_flight" not in incident.model_dump()


def test_lanes_are_not_processes_to_anything_that_counts_processes(lanes):
    from pytest_failure_instrumentation.incidents import killer, leftovers

    _hybrid(lanes)
    assert killer.roles_in(lanes.run)[LIVE] == "gw0"
    assert [record["worker"] for record in leftovers.worker_records(lanes.run)] == ["gw0"]


def test_a_lanes_slot_holds_no_descriptor_between_writes(tmp_path):
    """Hundreds of lanes, each holding its slot open for the session, ended a
    run at its open-file limit."""
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("counts descriptors through /proc")
    before = len(list(Path("/proc/self/fd").iterdir()))
    slots = [WorkerState(tmp_path / f"ln{index}.state", LIVE, RUN_ID,
                         lane={"process": "main", "lane": True}) for index in range(50)]
    for slot in slots:
        slot.update(nodeid="t.py::test_a", phase="call")
    assert len(list(Path("/proc/self/fd").iterdir())) - before < 5
    assert read_state(tmp_path / "ln7.state")["nodeid"] == "t.py::test_a"


def test_the_watchdog_dumps_a_process_of_lanes_at_most_once_a_timeout(tmp_path, monkeypatch):
    """Two hundred long tests begun a moment apart came due a moment apart:
    eleven whole-process dumps in fifty seconds."""
    clock = [1000.0]
    monkeypatch.setattr(crash_stack.time, "monotonic", lambda: clock[0])
    watchdog = crash_stack.SlowTestWatchdog(tmp_path / "main.slow", 10.0)
    dumps = []
    monkeypatch.setattr(watchdog, "_dump", lambda: dumps.append(clock[0]))
    for index in range(200):
        clock[0] = 1000.0 + index * 0.25
        watchdog.start_lane(f"ln{index}")
    for _ in range(200):
        clock[0] += 0.25
        watchdog.tick()
    assert len(dumps) == 5  # 50 s of ticks, one dump every 10 s at most
    assert all(later - earlier >= 10.0 for earlier, later in zip(dumps, dumps[1:]))


# --- the tee, the resources, an internal error ------------------------------

TEE_SCRIPT = r"""
import os, subprocess, sys, threading
from pathlib import Path
from pytest_failure_instrumentation.capture import output

if sys.argv[2] in ("rotate", "grow"):
    output._punch_hole = lambda descriptor, length: False
if sys.argv[2] == "grow":
    output.ROTATES = False  # what macOS does: never swap fd 2 under writers
path = Path(sys.argv[1])
tee = output.StderrTee(path, limit=4096, append=True)
tee.start()
tee.take()
lock, stop = threading.Lock(), threading.Event()

def writer(key):
    for index in range(2000):
        os.write(2, f"w{key}-{index:05d}\n".encode())

def drainer():
    while not stop.is_set():
        with lock:
            tee.drain()

threads = [threading.Thread(target=writer, args=(key,)) for key in range(3)]
draining = threading.Thread(target=drainer)
draining.start()
for thread in threads:
    thread.start()
# A child holding fd 2, as a test's own subprocess does.
subprocess.run([sys.executable, "-c",
                "import os\nfor i in range(2000): os.write(2, f'c-{i:05d}\\n'.encode())"],
               check=True)
for thread in threads:
    thread.join()
stop.set()
draining.join()
with lock:
    # The file is given back to at each drain, which the heartbeat runs every
    # tick; the size is measured after one, not after however much a loaded
    # machine let the writers add since the drainer last ran.
    tee.drain()
    tee.hand_back()
print(os.stat(path).st_blocks * 512, os.path.getsize(path))
last = output.read_tail(path)[-1]
print(last)
# Session end: fd 2 is back, and the file is left as its tail rather than as
# a sparse file whose length is every byte the session wrote.
tee.compact()
print(os.path.getsize(path), output.read_tail(path)[-1] == last)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX only")
@pytest.mark.parametrize("how", ["punch", "rotate", "grow"])
def test_a_session_long_tee_passes_every_byte_on_once_and_stays_bounded(tmp_path, how):
    """Taken once for a session of lanes, the tee is never between phases,
    where it would otherwise be trimmed. It gives the disk back instead - by
    punching holes where it can, by rotating where it cannot, and where fd 2
    cannot be swapped safely under writing threads it grows - and none may
    lose a byte on its way to the terminal, or pass one on twice."""
    import subprocess

    if how in ("punch", "rotate") and not sys.platform.startswith("linux"):
        pytest.skip("holes are punched, and a file rotated, on Linux only")
    capture = tmp_path / "main.output"
    finished = subprocess.run(
        [sys.executable, "-c", TEE_SCRIPT, str(capture), how],
        capture_output=True, text=True, timeout=120,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    lines = finished.stderr.splitlines()
    for key in range(3):
        assert [line for line in lines if line.startswith(f"w{key}-")] == [
            f"w{key}-{index:05d}" for index in range(2000)
        ]
    child = [line for line in lines if line.startswith("c-")]
    if how in ("punch", "grow"):
        # The child's fd 2 is the same open file: none of its bytes is lost.
        assert child == [f"c-{index:05d}" for index in range(2000)]
    else:
        assert len(child) == len(set(child))
    on_disk, length = (int(value) for value in finished.stdout.split()[:2])
    compacted, tail_kept = finished.stdout.splitlines()[2].split()
    assert int(compacted) <= 4096
    assert tail_kept == "True"
    if how == "grow":
        assert length >= 3 * 2000 * 9 + 2000 * 8
        assert not (tmp_path / "main.output.prev").exists()
        return
    assert on_disk < 64 * 1024
    if how == "rotate":
        assert length < 3 * 4096 + 1024
        assert (tmp_path / "main.output.prev").exists()


LATE_WRITE_SCRIPT = r"""
import os, sys
from pathlib import Path
from pytest_failure_instrumentation.capture import output

output._punch_hole = lambda descriptor, length: False
output.ROTATES = True  # no thread writes fd 2 while it is swapped here
tee = output.StderrTee(Path(sys.argv[1]), limit=4096, append=True)
tee.start()
tee.take()
# A write already under way when the file is rotated resolved fd 2 to the old
# file before the switch; a duplicate taken now stands for it.
late = os.dup(2)
for index in range(1000):
    os.write(2, f"early-{index:05d}\n".encode())
tee.drain()                      # rotates
os.write(2, b"after-rotation\n")
tee.drain()                      # the drain that used to close the old file
os.write(late, b"late-write\n")  # the stalled write lands only now
os.close(late)
tee.drain()
tee.hand_back()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX only")
def test_a_write_that_lands_in_a_rotated_file_after_the_next_drain_is_still_passed_on(tmp_path):
    """A thread's write to fd 2 that began before a rotation lands in the old
    file whenever the scheduler lets it finish - on a loaded machine, after
    the next drain. The old file is followed until the next rotation, and
    drained once more when fd 2 is handed back, so that write is not lost."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", LATE_WRITE_SCRIPT, str(tmp_path / "main.output")],
        capture_output=True, text=True, timeout=60,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    lines = finished.stderr.splitlines()
    assert lines.count("late-write") == 1
    assert lines.count("after-rotation") == 1
    assert [line for line in lines if line.startswith("early-")] == [
        f"early-{index:05d}" for index in range(1000)
    ]


def test_a_process_of_lanes_says_how_many_are_running_in_its_resources(tmp_path):
    from pytest_failure_instrumentation.config import Settings
    from pytest_failure_instrumentation.resource_sampling import ResourceSampler

    lanes = Lanes(tmp_path)
    lanes.process("gw0")
    lanes.lane("gw0.ln0", "gw0", "t.py::test_a", "call")
    lanes.lane("gw0.ln1", "gw0")
    lanes._state("gw1", {"nodeid": "t.py::test_b", "phase": "call", "pid": 1})
    sampler = ResourceSampler(lanes.run, "run", Settings(resources_seconds=1))
    try:
        workers = sampler._workers()
    finally:
        sampler.close()
    assert workers[LIVE]["worker"] == "gw0"
    assert workers[LIVE]["lanes_running"] == 1
    assert "lanes_running" not in workers[1]


def test_an_internal_error_off_the_lanes_names_the_one_lane_in_flight(tmp_path):
    """pytest-threadlanes raises a lane's error on the main thread."""
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    from pytest_failure_instrumentation.config import Settings

    recorder = WorkerRecorder(tmp_path, "gw0", Settings(watchdog=False), lanes=True)
    try:
        slots = {}

        def open_lane(name):
            slots[name] = recorder._open_lane(name)

        for name in ("gw0.ln0", "gw0.ln1"):
            thread = threading.Thread(target=open_lane, args=(name,))
            thread.start()
            thread.join()
        slots["gw0.ln1"].state.update(nodeid="t.py::test_b", phase="call")
        recorder.pytest_internalerror("RuntimeError: boom")
        slots["gw0.ln0"].state.update(nodeid="t.py::test_a", phase="setup")
        recorder.pytest_internalerror("RuntimeError: again")
        events = [json.loads(line) for line in (tmp_path / "gw0.events").read_text().splitlines()]
        one, several = [event for event in events if event["event"] == "internal_error"]
        assert (one["nodeid"], one["lane"]) == ("t.py::test_b", "gw0.ln1")
        assert several["nodeid"] is None
        assert {lane["lane"] for lane in several["lanes_in_flight"]} == {"gw0.ln0", "gw0.ln1"}
    finally:
        recorder.close()


# --- without lanes, what 0.13.1 wrote --------------------------------------

BASELINE = Path(__file__).with_name("evidence_without_lanes.json")


@pytest.mark.skipif(sys.platform != "linux", reason="the baseline was recorded on Linux")
@pytest.mark.parametrize("mode", list(without_lanes.MODES))
def test_a_run_without_lanes_writes_what_it_always_wrote(pytester, mode):
    """Files, rows and samples, key for key and value for value, against the
    evidence 0.13.1 left for the same suite - see ``without_lanes.py``."""
    if mode == "n1":
        pytest.importorskip("xdist")
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))["runs"][mode]
    for name, body in without_lanes.BASELINE_SUITE.items():
        pytester.makepyfile(**{name: body})
    pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--failure-instrumentation", *without_lanes.MODES[mode]
    )
    runs = [path for path in (pytester.path / ".pytest-failures").iterdir() if path.is_dir()]
    assert len(runs) == 1
    found = json.loads(json.dumps(without_lanes.normalised(runs[0])))
    for part in expected:
        assert found[part] == expected[part], part


def test_incidents_without_lanes_dump_the_keys_they_always_did():
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))["incidents"]
    assert without_lanes.incident_keys() == expected


def test_the_clients_models_re_serve_a_payload_without_lanes_as_0_13_1_did():
    """A consumer that parses a resource sample with the client and serves it
    again - the Sahara API does - must not grow a ``lanes_running: null``."""
    pytest.importorskip("httpx")
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))["client_dumps"]
    assert without_lanes.client_dumps() == expected


def test_the_schemas_are_0_13_1s_plus_only_the_declared_lane_fields():
    """In both modes: a serializer that leaves a lane field out of a dump must
    not leave the model's serialization schema as "anything", which is what
    a wrap serializer annotated ``-> Any`` did."""
    import pydantic

    golden = json.loads(BASELINE.read_text(encoding="utf-8"))
    if pydantic.VERSION != golden["pydantic"]:
        pytest.skip(f"the schemas were recorded with pydantic {golden['pydantic']}")
    found = without_lanes.schemas()
    for name, schema in golden["schemas"].items():
        if name.startswith("client_") and name not in found:
            continue  # httpx is not installed
        assert found[name] == schema, name
    # And each declared property is really there, in both modes.
    from pytest_failure_instrumentation.incidents import registry
    from pytest_failure_instrumentation.sampling import WorkerSample

    for mode in ("validation", "serialization"):
        death = registry._adapter.json_schema(mode=mode)["$defs"]["WorkerDeathIncident"]
        assert "lanes_in_flight" in death["properties"]
        sampled = WorkerSample.model_json_schema(mode=mode)["$defs"]["SampledWorker"]
        assert {"process", "thread_name", "thread_id"} <= set(sampled["properties"])


# --- pytest-threadlanes, for real -------------------------------------------


def _needs_lanes() -> None:
    pytest.importorskip("xdist")
    pytest.importorskip("pytest_threadlanes")


#: What a lanes run needs on this interpreter: pytest-threadlanes refuses to
#: run concurrent lanes while warnings are process-wide, which they are before
#: Python 3.14; and pytest before 8.4 swaps two more global hooks per test.
def _lanes_flags() -> list[str]:
    flags = ["-p", "no:cacheprovider"]
    if sys.version_info < (3, 14) or not getattr(sys.flags, "context_aware_warnings", 0):
        flags += ["-p", "no:warnings"]
    if tuple(int(part) for part in pytest.__version__.split(".")[:2]) < (8, 4):
        flags += ["-p", "no:threadexception", "-p", "no:unraisableexception"]
    return flags


#: One environment per lane, as the user's own scheduler does it: the tests of
#: an environment run in order on one lane, and environments run in parallel.
LANES_CONFTEST = '''
import json
from xdist.scheduler import LoadScopeScheduling


class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]


def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)


def pytest_failure_incident(incident):
    with open("incidents.jsonl", "a") as handle:
        handle.write(incident.model_dump_json() + "\\n")
'''

MODES = {
    "xdist": ["-n", "2"],
    "lanes": ["--lanes", "3"],
    "hybrid": ["-n", "2", "--lanes", "3"],
}


def _lanes_run(pytester, mode_args, *extra, ini="", conftest=LANES_CONFTEST):
    _needs_lanes()
    pytester.makeconftest(conftest)
    pytester.makeini("[pytest]\nfailure_heartbeat_interval = 1\n" + ini)
    return pytester.runpytest_subprocess(
        *_lanes_flags(), "--failure-instrumentation", *mode_args, *extra, timeout=180
    )


def _incidents(pytester) -> list[dict]:
    path = pytester.path / "incidents.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


LIVE_SUITE = '''
import json, os, time
import pytest
from pathlib import Path

from pytest_failure_instrumentation import stack_server, topology


@pytest.mark.parametrize("env", ["envA", "envB", "envC", "envD", "envE", "envF"])
def test_live(env):
    """Wait until every lane is running one of these, then write down what
    /workers says - from inside the run, the only time the rows are live."""
    base = Path(".pytest-failures")
    deadline = time.time() + 30
    while time.time() < deadline:
        rows = [row for run in topology.snapshot(base)["runs"] for row in run["workers"]]
        busy = [row for row in rows if row["nodeid"]]
        if len(busy) >= int(os.environ["LANES_EXPECTED"]):
            break
        time.sleep(0.2)
    me = [row for row in rows if row["nodeid"] and row["nodeid"].endswith(f"[{env}]")]
    Path(f"seen-{env}.json").write_text(json.dumps({
        "rows": rows,
        "mine": me,
        "resolves": {row["worker"]: stack_server.worker_pid(row["worker"], base) for row in rows},
        "pid": os.getpid(),
    }))
    time.sleep(1.0)
'''


@pytest.mark.parametrize(("mode", "expected"), [("lanes", 3), ("hybrid", 6)])
def test_every_lane_is_live_as_a_worker_of_its_own(pytester, monkeypatch, mode, expected):
    monkeypatch.setenv("LANES_EXPECTED", str(expected))
    pytester.makepyfile(test_live=LIVE_SUITE)
    result = _lanes_run(pytester, MODES[mode])
    assert result.ret == 0, result.stdout.str()

    seen = [json.loads(path.read_text()) for path in sorted(pytester.path.glob("seen-*.json"))]
    assert len(seen) == 6
    for reading in seen:
        rows = reading["rows"]
        names = [row["worker"] for row in rows]
        # One row per lane, and no row for the processes that hold them.
        assert len(names) == expected, names
        assert all("process" in row for row in rows), names
        # Each lane its own test: six environments, one per lane at a time.
        busy = [row["nodeid"] for row in rows if row["nodeid"]]
        assert len(set(busy)) == len(busy)
        # The test that wrote this is on exactly one row, whose process is
        # the one the test ran in and whose thread is its lane's.
        (mine,) = reading["mine"]
        assert mine["pid"] == reading["pid"]
        assert mine["thread_name"] == f"lane-{mine['worker']}"
        assert isinstance(mine["thread_id"], int)
        # And every lane resolves for a stack, to its live process.
        assert all(reading["resolves"].values()), reading["resolves"]
        assert reading["resolves"][mine["worker"]] == reading["pid"]
    workers = {row["worker"] for reading in seen for row in reading["rows"]}
    if mode == "hybrid":
        assert workers == {f"gw{p}.ln{n}" for p in range(2) for n in range(3)}
        # xdist's scheduler hands work to lanes here, so each lane's row has
        # a lane's progress, meaning what a worker's does...
        for reading in seen:
            for row in reading["rows"]:
                assert row["tests_assigned"] == 1
                assert row["tests_running"] + row["tests_queued"] + row["tests_finished"] == 1
        # ...and the controller's record counted each finish on its lane.
        (directory,) = [path for path in (pytester.path / ".pytest-failures").iterdir()
                        if path.is_dir()]
        schedule = json.loads((directory / "schedule.json").read_text().strip("\0 \n"))
        assert set(schedule["workers"]) == workers
        assert all(row["completed"] == row["assigned"] == 1
                   for row in schedule["workers"].values())
    else:
        assert workers == {"ln0", "ln1", "ln2"}
        # No xdist controller writes a schedule for lanes in one process.
        assert all(row["tests_assigned"] is None for reading in seen for row in reading["rows"])


#: A hung test on one lane while the others go on, then finish and sit idle -
#: out of work at the end of an uneven run - for longer than the stall limit.
STALL_SUITE = '''
import time
import pytest


@pytest.mark.parametrize("step", range(4))
@pytest.mark.parametrize("env", ["envA", "envB", "envC", "envD"])
def test_s(env, step):
    if env == "envA" and step == 0:
        time.sleep(7)          # hung: blocked, no CPU
    elif env != "envA":
        time.sleep(0.3)
'''


@pytest.mark.parametrize("mode", list(MODES))
def test_a_hung_lane_is_blamed_for_its_own_test_and_nothing_else_is(pytester, mode):
    """Appendix B of the design: in every mode, one stall, of the hung test,
    at the line it hangs on. Not a sibling's test, not the process, and not
    the lanes that ran out of work and waited for the hung one to finish."""
    pytester.makepyfile(test_stall=STALL_SUITE)
    _lanes_run(pytester, MODES[mode], ini="failure_stall_seconds = 2\n")

    stalls = [incident for incident in _incidents(pytester) if incident["kind"] == "worker_stall"]
    assert len(stalls) == 1, [(i["worker"], i.get("test_in_flight")) for i in stalls]
    (incident,) = stalls
    assert incident["test_in_flight"] == "test_stall.py::test_s[envA-0]"
    assert incident["state"] == "BLOCKED"
    assert incident["blamed_frame"]["function"] == "test_s"
    assert incident["blamed_frame"]["line"] == 9
    if mode != "xdist":
        assert "ln" in incident["worker"]


BUSY_SUITE = '''
import time
import pytest


@pytest.mark.parametrize("step", range(3))
@pytest.mark.parametrize("env", ["envA", "envB", "envC"])
def test_b(env, step):
    if env == "envA" and step == 0:
        end = time.time() + 6   # slow, not stuck: burning a core
        while time.time() < end:
            sum(range(1000))
    else:
        time.sleep(0.2)
'''


@pytest.mark.parametrize("mode", ["lanes", "hybrid"])
def test_lanes_idle_beside_a_slow_one_raise_nothing(pytester, mode):
    """The end of an uneven run: one lane still working, its siblings out of
    work for longer than the stall limit. Nothing is stuck, so nothing is
    raised - the working lane reads its own CPU, the idle ones have no test,
    and the process holding them all has a lane in flight."""
    pytester.makepyfile(test_busy=BUSY_SUITE)
    result = _lanes_run(pytester, MODES[mode], ini="failure_stall_seconds = 2\n")
    assert result.ret == 0, result.stdout.str()
    stalls = [incident for incident in _incidents(pytester) if incident["kind"] == "worker_stall"]
    assert stalls == []


DEATH_SUITE = '''
import os, time
import pytest


@pytest.mark.parametrize("step", range(2))
@pytest.mark.parametrize("env", ["envA", "envB", "envC"])
def test_d(env, step, tmp_path_factory):
    flag = tmp_path_factory.getbasetemp().parent / "died-once"
    if env == "envB" and step == 0 and not flag.exists():
        time.sleep(float(os.environ["DEATH_AFTER"]))
        flag.write_text("x")
        os._exit(1)
    time.sleep(float(os.environ["SIBLINGS_TAKE"]))
'''


@pytest.mark.parametrize(
    ("siblings_take", "death_after", "in_flight"),
    [(2.0, 0.5, 3), (0.1, 2.0, 1)],
    ids=["siblings-in-flight", "culprit-alone"],
)
def test_a_death_names_the_lanes_it_took(pytester, monkeypatch, siblings_take, death_after, in_flight):
    monkeypatch.setenv("SIBLINGS_TAKE", str(siblings_take))
    monkeypatch.setenv("DEATH_AFTER", str(death_after))
    pytester.makepyfile(test_death=DEATH_SUITE)
    result = _lanes_run(pytester, ["-n", "1", "--lanes", "3"])

    deaths = [incident for incident in _incidents(pytester) if incident["kind"] == "worker_death"]
    assert len(deaths) == 1, deaths
    (incident,) = deaths
    assert incident["worker"] == "gw0"
    assert incident["verdict"] == "SELF_EXIT"
    assert len(incident["lanes_in_flight"]) == in_flight
    culprit = "test_death.py::test_d[envB-0]"
    if in_flight == 1:
        assert incident["test_in_flight"] == culprit
        assert incident["lanes_in_flight"][0]["nodeid"] == culprit
    else:
        assert incident["test_in_flight"] is None
        assert culprit in {lane["nodeid"] for lane in incident["lanes_in_flight"]}
    # xdist replaced the worker and the run went on to the end.
    assert "passed" in result.stdout.str()


ADJUSTED_SUITE = '''
import os
import pytest


@pytest.mark.parametrize("env", ["envA", "envB"])
def test_writes_to_fd_2(env):
    os.write(2, b"a line from native code\\n")
'''


def test_what_is_one_per_process_is_adjusted_and_says_so(pytester):
    pytester.makepyfile(test_adjusted=ADJUSTED_SUITE)
    result = _lanes_run(
        pytester, ["--lanes", "2", "--dist", "loadscope"],
        ini="failure_capture_output = true\nfailure_profile = true\n",
    )
    assert result.ret == 0, result.stdout.str()

    # --dist without -n is still one process, and it records itself.
    (directory,) = [path for path in (pytester.path / ".pytest-failures").iterdir() if path.is_dir()]
    records = {path.stem: read_state(path) for path in directory.glob("*.state")}
    assert records["main"]["lanes"] is True
    assert any(record.get("process") == "main" for record in records.values())

    events = [json.loads(line) for line in (directory / "main.events").read_text().splitlines()]
    adjusted = {event["mechanism"]: event["action"] for event in events
                if event["event"] == "lanes_adjusted"}
    assert adjusted["profiler"] == "disabled"
    assert adjusted["stderr_tee"] == "taken once for the session"
    assert adjusted["slow_test_watchdog"] == "one clock per lane"
    assert "heartbeat" in adjusted
    assert not list(directory.glob("*.profile.jsonl"))
    # faulthandler's C timer dumps without the GIL: with lanes running, missed
    # beats do not mean nothing is executing, so it is never armed.
    assert adjusted["frozen_fallback"] == "off"
    assert not list(directory.glob("*.frozen"))
    # The process's beat names no test: its lanes are running several.
    assert {event["nodeid"] for event in events if event["event"] == "heartbeat"} == {None}

    # fd 2 was the capture file for the whole session, and what reached it
    # was passed on to the terminal as well.
    assert "a line from native code" in (directory / "main.output").read_text()
    assert "a line from native code" in result.stderr.str()


#: A hang in the setup of each lane's first test - provisioning, say - before
#: any lane has reported anything.
FIRST_SETUP_SUITE = '''
import os, time
import pytest


@pytest.fixture
def env_setup(env, step):
    if step == 0 and env in os.environ["HANG_ENVS"].split(","):
        time.sleep(6)
    yield


@pytest.mark.parametrize("step", range(3))
@pytest.mark.parametrize("env", ["envA", "envB"])
def test_f(env, step, env_setup):
    time.sleep(0.2)
'''


@pytest.mark.parametrize("hang", ["envA", "envA,envB"], ids=["one-lane", "every-lane"])
def test_a_hang_before_any_report_is_still_a_lanes_stall(pytester, monkeypatch, hang):
    """With -n, the controller hears of a lane only from its reports; the
    lane's own record is what times a test that never sent one."""
    monkeypatch.setenv("HANG_ENVS", hang)
    pytester.makepyfile(test_first=FIRST_SETUP_SUITE)
    _lanes_run(pytester, ["-n", "1", "--lanes", "2"], ini="failure_stall_seconds = 2\n")
    stalls = [i for i in _incidents(pytester) if i["kind"] == "worker_stall"]
    assert stalls, _incidents(pytester)
    assert {i["worker"] for i in stalls} <= {"gw0.ln0", "gw0.ln1"}
    assert all(i["phase"] == "setup" and "[env" in i["test_in_flight"] for i in stalls)


FEW_SUITE = '''
import time
import pytest


@pytest.mark.parametrize("step", range(4))
@pytest.mark.parametrize("env", ["envA", "envB"])
def test_few(env, step):
    for _ in range(12):
        sum(range(20000))
        time.sleep(0.1)
'''


def test_a_lane_given_no_work_is_idle_not_silent(pytester):
    """Three lanes, two environments: one lane never gets a test."""
    pytester.makepyfile(test_few=FEW_SUITE)
    result = _lanes_run(pytester, ["--lanes", "3"], ini="failure_stall_seconds = 2\n")
    assert result.ret == 0, result.stdout.str()
    assert [i for i in _incidents(pytester) if i["kind"] == "worker_stall"] == []


EXCLUSIVE_SUITE = '''
import time
import pytest


@pytest.fixture
def provision():
    time.sleep(2.5)
    yield


@pytest.mark.lanes_exclusive
@pytest.mark.parametrize("env", ["envX"])
def test_exclusive(env, provision):
    time.sleep(2.5)


@pytest.mark.parametrize("step", range(2))
@pytest.mark.parametrize("env", ["envA", "envB"])
def test_plain(env, step):
    time.sleep(0.2)
'''


@pytest.mark.parametrize("mode", ["lanes", "hybrid"])
def test_an_exclusive_tests_held_reports_are_not_a_stall(pytester, mode):
    """pytest-threadlanes holds an exclusive test's reports until it ends; its
    phases, each under the limit, are timed by the lane's own record."""
    pytester.makepyfile(test_ex=EXCLUSIVE_SUITE)
    result = _lanes_run(
        pytester, ["--lanes", "2"] if mode == "lanes" else ["-n", "1", "--lanes", "2"],
        ini="failure_stall_seconds = 4\nmarkers = lanes_exclusive\n",
    )
    assert result.ret == 0, result.stdout.str()
    assert [i for i in _incidents(pytester) if i["kind"] == "worker_stall"] == []


INTERNAL_ERROR_CONFTEST = LANES_CONFTEST + '''
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    yield
    if "envB-1" in item.nodeid and call.when == "call":
        raise RuntimeError("plugin bug in makereport")
'''


@pytest.mark.parametrize("mode", ["lanes", "hybrid"])
def test_an_internal_error_names_the_lane_and_its_test(pytester, mode):
    _needs_lanes()
    pytester.makepyfile(test_ie="""
import time, pytest
@pytest.mark.parametrize("step", range(3))
@pytest.mark.parametrize("env", ["envA", "envB"])
def test_i(env, step):
    time.sleep(0.3)
""")
    _lanes_run(
        pytester, ["--lanes", "2"] if mode == "lanes" else ["-n", "1", "--lanes", "2"],
        conftest=INTERNAL_ERROR_CONFTEST,
    )
    (incident,) = [i for i in _incidents(pytester) if i["kind"] == "internal_error"]
    assert incident["test_in_flight"] == "test_ie.py::test_i[envB-1]"
    assert incident["worker"] in ("ln1", "gw0.ln1")


SEGFAULT_SUITE = '''
import ctypes, time, pytest


@pytest.mark.parametrize("step", range(2))
@pytest.mark.parametrize("env", ["envA", "envB", "envC"])
def test_d(env, step, tmp_path_factory):
    flag = tmp_path_factory.getbasetemp().parent / "died-once"
    if env == "envB" and step == 0 and not flag.exists():
        time.sleep(0.5)
        flag.write_text("x")
        ctypes.string_at(0)
    time.sleep(2)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="a fault is an OSError there")
def test_a_native_crash_on_one_lane_blames_that_lanes_test(pytester):
    pytester.makepyfile(test_seg=SEGFAULT_SUITE)
    _lanes_run(pytester, ["-n", "1", "--lanes", "3"])
    (incident,) = [i for i in _incidents(pytester) if i["kind"] == "worker_death"]
    assert incident["verdict"] == "NATIVE_CRASH"
    assert incident["test_in_flight"] == "test_seg.py::test_d[envB-0]"
    assert len(incident["lanes_in_flight"]) == 3



APPEND_SCRIPT = r"""
import fcntl, os, sys
from pathlib import Path
from pytest_failure_instrumentation.capture import output

def appends(descriptor):
    return bool(fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_APPEND)

directory = Path(sys.argv[1])
per_phase = output.StderrTee(directory / "gw0.output", limit=4096)
per_phase.start()
session = output.StderrTee(directory / "main.output", limit=4096, append=True)
session.start()
flags = [appends(per_phase._file), appends(session._file)]
output._punch_hole = lambda descriptor, length: False
output.ROTATES = True  # no thread writes fd 2 while it is swapped here
session.take()
os.write(2, b"x" * 4096 * 3 + b"\n")
session.drain()                  # rotates
flags += [appends(session._file), appends(2)]
session.hand_back()
print(flags)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX only")
def test_a_session_long_tee_appends_so_concurrent_writers_never_share_an_offset(tmp_path):
    """Under lanes many writers share fd 2 - lane threads, and the children
    their tests start, which inherit it. Without ``O_APPEND`` each write lands
    at the description's shared offset, which Linux serializes and macOS does
    not: there a child's writes overwrote a lane's (CI lost the last 233 lines
    of one writer). The session's file, and every file a rotation starts, is
    opened to append; the per-phase tee without lanes is left as it was."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", APPEND_SCRIPT, str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    assert finished.stdout.split("\n")[0] == "[False, True, True, True]"


TORN_READ_SCRIPT = r"""
import os, sys
from pathlib import Path
from pytest_failure_instrumentation.capture import output

output._punch_hole = lambda descriptor, length: False
output.ROTATES = True  # no thread writes fd 2 while it is swapped here
tee = output.StderrTee(Path(sys.argv[1]), limit=4096, append=True)
tee.start()
tee.take()
retired = os.dup(2)              # the old file, as a child that inherited it holds it
os.write(2, b"x" * 4096 * 3 + b"\n")
tee.drain()                      # rotates: fd 2 is a fresh file now
os.write(2, b"w")                # a drain reads a line the kernel is still copying in
tee.drain()
os.write(retired, b"child\n")    # the old file gains a line meanwhile
os.write(2, b"1-00420\n")        # and the torn line is completed
tee.drain()
os.close(retired)
tee.hand_back()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX only")
def test_a_drain_passes_on_only_whole_lines_so_two_files_never_splice_one(tmp_path):
    """A drain can read a line the kernel is still copying in - its first part
    is in the file before its last. Passed on as it stood, the next drain put
    the rotated file's new line between its halves: stress runs showed
    ``w`` + ``c-00000`` + ``1-00420``. Only whole lines are passed on until
    fd 2 is handed back, which passes on whatever is left."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", TORN_READ_SCRIPT, str(tmp_path / "main.output")],
        capture_output=True, text=True, timeout=60,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    lines = finished.stderr.splitlines()
    assert "w1-00420" in lines
    assert "child" in lines


REORDER_SCRIPT = r"""
import os, sys
from pathlib import Path
from pytest_failure_instrumentation.capture import output

output._punch_hole = lambda descriptor, length: False
output.ROTATES = True  # no thread writes fd 2 while it is swapped here
tee = output.StderrTee(Path(sys.argv[1]), limit=4096, append=True)
tee.start()
tee.take()
late = os.dup(2)                 # a write already under way when fd 2 is switched
os.write(2, b"x" * 4096 * 3 + b"\n")
tee.drain()                      # rotates: the old file is followed from now on
follow = tee._follow_retired

def a_writer_finishes_meanwhile():
    follow()
    # Between reading the old file and the new one, the stalled write lands
    # in the old file and the same writer's next line in the new one.
    os.write(late, b"k-00001\n")
    os.write(2, b"k-00002\n")

tee._follow_retired = a_writer_finishes_meanwhile
tee.drain()
tee._follow_retired = follow
tee.drain()
os.close(late)
tee.hand_back()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX only")
def test_a_writers_lines_keep_their_order_across_a_rotation(tmp_path):
    """One writer's lines reach the terminal in the order it wrote them, even
    when one landed late in the rotated file and the next in the fresh one
    while a drain was between the two: stress runs showed the later line
    first. A drain notes how far the fresh file goes before it reads the old
    one, and passes the fresh one on no further than that."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", REORDER_SCRIPT, str(tmp_path / "main.output")],
        capture_output=True, text=True, timeout=60,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    ours = [line for line in finished.stderr.splitlines() if line.startswith("k-")]
    assert ours == ["k-00001", "k-00002"]


CUT_LINE_SCRIPT = r"""
import os, sys
from pathlib import Path
from pytest_failure_instrumentation.capture import output

output._punch_hole = lambda descriptor, length: False
output.ROTATES = True  # no thread writes fd 2 while it is swapped here
tee = output.StderrTee(Path(sys.argv[1]), limit=4096, append=True)
tee.start()
tee.take()
child = os.dup(2)                # a child's inherited fd 2, kept past rotations
os.write(2, b"x" * 4096 * 3 + b"\n")
tee.drain()                      # rotates: the child's file is followed now
os.write(child, b"c-01")         # the child is part way through a line
tee.drain()
os.write(2, b"y" * 4096 * 3 + b"\n")
tee.drain()                      # rotates again: the child's file is closed
os.write(2, b"w1-00500\n")
tee.drain()
os.close(child)
tee.hand_back()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the tee is POSIX only")
def test_a_line_cut_off_when_a_rotated_file_is_closed_is_not_joined_to_the_next(tmp_path):
    """A child that outlives two rotations writes into a file this process has
    stopped reading, and loses what it writes after that - the documented cost
    of rotating. What it had written of a line by then is passed on, ended
    with a newline, so the line that follows it is still a line of its own."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", CUT_LINE_SCRIPT, str(tmp_path / "main.output")],
        capture_output=True, text=True, timeout=60,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    lines = finished.stderr.splitlines()
    assert "w1-00500" in lines
    assert "c-01" in lines
