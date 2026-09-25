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
                **fields,
                "process": process,
                "thread_name": f"lane-{name}",
                "thread_id": 4000 + len(name),
                "thread_ident": 0x7F0000000000 + len(name),
                "lane": True,
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
        ``process_step`` a beat and each lane's by its own step."""
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
                "threads": {lane: index * step for lane, step in lanes.items()},
                "run_id": RUN_ID,
            })
        if finish:
            lines.append({"event": "worker_finish", "exitstatus": 0, "time": now, "run_id": RUN_ID})
        (self.run / f"{process}.events").write_text(
            "".join(json.dumps(line) + "\n" for line in lines)
        )


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
        assert row["thread_id"] == 4000 + len(name)
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
    described = topology.snapshot(lanes.base, only=["gw0.ln1", "gw0"])
    rows = described["runs"][0]["workers"]
    assert [row["worker"] for row in rows] == ["gw0.ln1"]
    # The process is a container rather than a row, and asking for it by name
    # finds nothing to list.
    assert described["filter"]["unmatched"] == ["gw0"]


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


def test_lane_beats_fall_back_to_the_process_where_a_lane_is_not_measured():
    beats = [
        {"time": 1.0, "cpu_seconds": 1.0, "threads": {"ln0": 0.0}},
        {"time": 2.0, "cpu_seconds": 2.0, "threads": {"ln0": 0.0}},
        {"time": 3.0, "cpu_seconds": 3.0},
    ]
    # The latest beat does not measure the lane: the process's figure stands.
    assert stall_analysis.lane_beats(beats, "ln0") is beats
    # It does: the lane's own, from the beats that measured it.
    measured = stall_analysis.lane_beats(beats[:2], "ln0")
    assert stall_analysis.cpu_rate(measured) == 0.0
    # Measured once: no rate of its own yet, so the process's reading stands.
    early = [{"time": 1.0, "cpu_seconds": 5.0},
             {"time": 2.0, "cpu_seconds": 9.0, "threads": {"ln0": 1.0}}]
    assert stall_analysis.lane_beats(early, "ln0") is early


def test_a_heartbeat_measures_each_lanes_thread_only_when_asked():
    written = []
    native = threading.get_native_id()
    Heartbeat(lambda event, **fields: written.append(fields),
              threads=lambda: {"ln0": native})._beat()
    Heartbeat(lambda event, **fields: written.append(fields))._beat()
    with_lanes, without = written
    assert set(with_lanes["threads"]) == {"ln0"}
    assert with_lanes["threads"]["ln0"] >= 0.0
    assert "threads" not in without
    assert list(without) == ["cpu_seconds", "rss_mb", "nodeid", "nodeid_hash", "phase"]


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
    assert incident.tests_started == 3
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


def _lanes_run(pytester, mode_args, *extra, ini=""):
    _needs_lanes()
    pytester.makeconftest(LANES_CONFTEST)
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
    else:
        assert workers == {"ln0", "ln1", "ln2"}


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
    # The process's beat names no test: its lanes are running several.
    assert {event["nodeid"] for event in events if event["event"] == "heartbeat"} == {None}

    # fd 2 was the capture file for the whole session, and what reached it
    # was passed on to the terminal as well.
    assert "a line from native code" in (directory / "main.output").read_text()
    assert "a line from native code" in result.stderr.str()
