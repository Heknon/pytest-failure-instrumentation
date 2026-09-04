"""The sampler's platform-facing parts, exercised on whatever this machine is.

The per-thread clock has one reader per platform and CI runs all three, so
each reader is checked here against a thread that is known to be busy and one
that is known to be idle. The psutil reader is Windows's, and its ids are
native thread ids on Linux too - so it is checked here whichever platform this
is, which is the only way the Windows code path is exercised on the machine
where most of the development happens.
"""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import threading
import time
import tracemalloc
import weakref
from pathlib import Path
from typing import Any

import pytest

from pytest_failure_instrumentation.profile import sampler as sampling
from pytest_failure_instrumentation.profile.analysis import FrameRef, _without_the_instrumentation
from pytest_failure_instrumentation.profile.sampler import Sampler, ThreadClock


def spin_for(seconds: float) -> None:
    end = time.perf_counter() + seconds
    count = 0
    while time.perf_counter() < end:
        count += 1


class Spinner:
    """A thread that burns CPU until told to stop, beside one that sleeps."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.busy = threading.Thread(target=self._spin, name="busy", daemon=True)
        self.idle = threading.Thread(target=self.stop.wait, name="idle", daemon=True)

    def _spin(self) -> None:
        while not self.stop.is_set():
            spin_for(0.01)

    def __enter__(self) -> Spinner:
        self.busy.start()
        self.idle.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop.set()
        self.busy.join(timeout=2)
        self.idle.join(timeout=2)


def readers() -> list[str]:
    """Every clock source this machine can answer."""
    found = [ThreadClock().source]
    if found[0] != "psutil":
        found.append("psutil")  # Windows's reader; its ids match native_id on Linux too
    return [source for source in found if source != "unavailable"]


def clock_with(source: str) -> ThreadClock:
    clock = ThreadClock()
    if source == "psutil":
        import psutil

        clock.source = "psutil"
        clock._process = psutil.Process()
    return clock


@pytest.mark.parametrize("source", readers())
def test_the_clock_tells_a_busy_thread_from_an_idle_one(source: str) -> None:
    clock = clock_with(source)
    if sys.platform == "darwin" and source == "psutil":
        pytest.skip("psutil's thread ids on macOS are indexes, not native ids")
    with Spinner() as threads:
        time.sleep(0.1)
        before = clock.read(clock.discover())
        time.sleep(0.4)
        after = clock.read(clock.discover())

    busy, idle = threads.busy.native_id, threads.idle.native_id
    assert busy in before and busy in after, f"{source} did not list the busy thread"
    assert idle in after, f"{source} did not list the idle thread"
    burnt = after[busy] - before[busy]
    waited = after[idle] - before.get(idle, 0)
    # Roughly the interval on the busy thread - the scheduler's tick on
    # Windows makes this lumpy, so the bound is loose - and next to nothing
    # on the idle one.
    assert burnt > 150_000_000, f"{source}: busy thread burnt {burnt} ns in 0.4 s"
    assert waited < 50_000_000, f"{source}: idle thread burnt {waited} ns"


@pytest.mark.skipif(not ThreadClock().available, reason="no per-thread clock here")
@pytest.mark.parametrize("source", readers())
def test_the_sampler_charges_cpu_to_the_thread_that_burnt_it(source: str) -> None:
    if sys.platform == "darwin" and source == "psutil":
        pytest.skip("psutil's thread ids on macOS are indexes, not native ids")
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.01, worker="x")
    sampler.clock = clock_with(source)
    sampler.start()
    sampler.begin_phase("t::a", "call")
    with Spinner():
        # The psutil reader pays a GIL round trip per thread beside a busy
        # one, so its ticks land a quarter of a second apart here and the
        # first sighting of a thread charges nothing: half a second holds
        # one usable tick on a good day and none on a loaded box.
        time.sleep(0.5 if source != "psutil" else 1.5)
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()

    (record,) = [entry for entry in records if entry["record"] == "test"]
    by_thread: dict[str, int] = {}
    for stack in record["stacks"]:
        by_thread[stack["thread"]] = by_thread.get(stack["thread"], 0) + stack["cpu_ns"]
    assert record["per_thread"] is True
    # Attribution is the claim, not completeness: the psutil reader opens a
    # file per thread on Linux and pays a GIL round trip for each beside a
    # busy thread, so its ticks are a tenth of a second apart and a loaded
    # CI box sees only a few of them in this window. (Windows reads one
    # snapshot per tick and has no such cost.)
    busy, idle, main = by_thread.get("busy", 0), by_thread.get("idle", 0), by_thread.get("MainThread", 0)
    assert busy > 50_000_000, by_thread
    assert idle < busy / 5, by_thread
    # The sleeping test thread earned next to nothing: this is a CPU profile.
    assert main < busy / 2, by_thread


def test_without_a_per_thread_clock_the_process_cpu_goes_to_the_test_thread() -> None:
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.01, worker="x")
    sampler.clock.source = "unavailable"
    sampler.start()
    sampler.begin_phase("t::a", "call")
    spin_for(0.3)
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()

    (record,) = [entry for entry in records if entry["record"] == "test"]
    assert record["per_thread"] is False
    assert record["thread_clock"] == "process"
    charged = {stack["thread"] for stack in record["stacks"]}
    assert charged == {"MainThread"}
    assert sum(stack["cpu_ns"] for stack in record["stacks"]) > 150_000_000


def test_a_tight_loop_gets_its_line_even_where_the_frame_reports_none() -> None:
    """On 3.11 a thread made to give up the GIL does so at a loop's back
    edge, an instruction with no line of its own, so f_lineno answers None
    for exactly the frames a profiler wants. The offset still says."""
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.01, worker="x")
    sampler.start()
    sampler.begin_phase("t::a", "call")

    def hot() -> int:
        total = 0
        for value in range(3_000_000):
            total += value & 3
        return total

    hot()
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()

    (record,) = [entry for entry in records if entry["record"] == "test"]
    lines = {
        int(record["frames"][stack["frames"][0]].split("|")[1])
        for stack in record["stacks"]
        if record["frames"][stack["frames"][0]].split("|")[2].endswith("hot")
    }
    assert lines, "the loop was never sampled"
    assert 0 not in lines
    first = hot.__code__.co_firstlineno
    assert all(first < line <= first + 4 for line in lines), lines


def test_sampling_does_not_keep_a_functions_locals_alive() -> None:
    """The frames dict from sys._current_frames holds the sampler's own
    frame, whose locals hold the dict: a cycle that kept every sampled
    frame - and the test's locals with it - until a full collection."""

    class Blob(list):
        pass

    holder: list[Any] = []
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.005, worker="x")
    sampler.start()
    sampler.begin_phase("t::a", "call")

    def allocate() -> None:
        blob = Blob(bytearray(100_000) for _ in range(50))
        holder.append(weakref.ref(blob))
        time.sleep(0.2)

    # Off before the allocation, not after: the sampler allocates on every
    # tick, and a collection between the return and the check would clear a
    # cycle the sampler had created, and pass this test with the bug present.
    gc.disable()
    try:
        allocate()
        alive = holder[0]() is not None
    finally:
        gc.enable()
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()
    assert not alive, "the sampled function's locals outlived its return"


def test_the_samplers_own_frames_are_stripped_whatever_the_separator() -> None:
    windows = "C:\\env\\Lib\\site-packages\\pytest_failure_instrumentation\\profile\\sampler.py"
    posix = "/env/lib/site-packages/pytest_failure_instrumentation/profile/sampler.py"
    product = FrameRef("/srv/product/thing.py", 3, "build", "product")
    for own in (windows, posix):
        frames = [FrameRef(own, 1, "Sampler._on_gc", "runtime"), product]
        assert _without_the_instrumentation(frames) == [product]


def test_the_late_tick_factor_is_a_number_of_intervals() -> None:
    assert sampling.LATE_TICK_FACTOR > 1


def test_watchdog_tracing_never_replaces_somebody_elses_tracer():
    import tracemalloc

    from pytest_failure_instrumentation.capture.memory import enable_tracemalloc

    was_tracing = tracemalloc.is_tracing()
    if was_tracing:
        tracemalloc.stop()
    try:
        assert enable_tracemalloc(1)
        assert tracemalloc.get_traceback_limit() == 1
        assert enable_tracemalloc(12)
        assert tracemalloc.get_traceback_limit() == 1
        assert enable_tracemalloc(3)
        assert tracemalloc.get_traceback_limit() == 1
        assert enable_tracemalloc(0)
        assert tracemalloc.get_traceback_limit() == 1
    finally:
        tracemalloc.stop()
        if was_tracing:
            tracemalloc.start()


def test_allocation_profiler_refuses_an_existing_tracer() -> None:
    from pytest_failure_instrumentation.capture.memory import TracemallocSession
    from pytest_failure_instrumentation.errors import TracemallocConflict

    was_tracing = tracemalloc.is_tracing()
    if was_tracing:
        tracemalloc.stop()
    tracemalloc.start(3)
    try:
        with pytest.raises(TracemallocConflict, match="already active with depth 3"):
            TracemallocSession(12)
        assert tracemalloc.is_tracing()
        assert tracemalloc.get_traceback_limit() == 3
    finally:
        tracemalloc.stop()
        if was_tracing:
            tracemalloc.start()


def test_allocation_profiler_stops_the_tracer_it_started() -> None:
    from pytest_failure_instrumentation.capture.memory import TracemallocSession

    was_tracing = tracemalloc.is_tracing()
    if was_tracing:
        tracemalloc.stop()
    try:
        session = TracemallocSession(7)
        assert tracemalloc.get_traceback_limit() == 7
        session.close()
        assert not tracemalloc.is_tracing()
        session.close()
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        if was_tracing:
            tracemalloc.start()


def nodeids_of(records: list[dict[str, Any]]) -> list[str]:
    return [entry["nodeid"] for entry in records if entry["record"] == "test"]


def test_a_test_that_stops_tracemalloc_does_not_break_the_ones_after_it() -> None:
    """A suite that checks its own leaks starts and stops tracemalloc inside
    a test. With allocation tracing on, that used to raise out of the next
    begin_phase - inside pytest's hook - and error every test after it."""
    was_tracing = tracemalloc.is_tracing()
    tracemalloc.start(5)
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.005, worker="x", allocations=True, retained_mb=1)
    assert sampler.allocations
    sampler.start()
    try:
        sampler.begin_phase("t::stops", "call")
        tracemalloc.stop()
        sampler.end_phase("call")
        sampler.end_test("t::stops")
        sampler.begin_phase("t::after", "call")
        sampler.end_phase("call")
        sampler.end_test("t::after")
    finally:
        sampler.stop()
        if was_tracing:
            tracemalloc.start()
    assert nodeids_of(records) == ["t::stops", "t::after"]
    assert [entry["record"] for entry in records][-1] == "background"


def test_a_test_whose_end_was_never_announced_is_still_written() -> None:
    """A plugin that owns the runtest protocol without a logfinish, or a run
    that fell over between teardown and logfinish, leaves a test window open
    when the next test begins. It is written as far as it got."""
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.005, worker="x")
    sampler.start()
    sampler.begin_phase("t::orphan", "call")
    spin_for(0.02)
    sampler.begin_phase("t::next", "call")
    sampler.end_phase("call")
    sampler.end_test("t::next")
    sampler.stop()
    assert nodeids_of(records) == ["t::orphan", "t::next"]


def test_starting_twice_is_harmless() -> None:
    sampler = Sampler(lambda record: None, lambda: 1, interval=0.005, worker="x")
    sampler.start()
    sampler.start()
    sampler.stop()


@pytest.mark.skipif(not ThreadClock().available, reason="no per-thread clock here")
def test_a_thread_that_lives_less_than_an_interval_is_still_charged() -> None:
    """A thread's clock starts at zero with the thread, so its first sighting
    already says what it burnt. Charging nothing on a first sighting lost
    every thread that lived less than an interval - a pool's workers, one per
    task - and the sampler contradicted its own process figure."""
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.002, worker="x")
    if sampler.clock.source == "psutil":
        pytest.skip("the psutil reader's ticks are too far apart to see a short thread")
    sampler.start()
    sampler.begin_phase("t::a", "call")
    started = time.process_time()
    for _ in range(30):
        thread = threading.Thread(target=spin_for, args=(0.015,), name="short")
        thread.start()
        thread.join()
    burnt = time.process_time() - started
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()

    (record,) = [entry for entry in records if entry["record"] == "test"]
    charged = sum(stack["cpu_ns"] for stack in record["stacks"] if stack["thread"] == "short") / 1e9
    # Sampling, so not all of it: a thread that comes and goes between two
    # ticks is never seen, and on a loaded box the ticks are further apart.
    # Before the fix this was exactly zero, whatever the load.
    assert charged > burnt * 0.1, (charged, burnt)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork here")
# 3.12 warns that forking a multi-threaded process may deadlock the child,
# which is the hazard this test exists to exercise.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_forked_child_neither_hangs_nor_writes_records(tmp_path: Path) -> None:
    """The sampling thread releases the GIL while it holds the lock, so a
    fork on the test thread can copy a held lock into a child that has no
    thread to release it. The child's first begin_phase then waited for
    ever - under pytest-forked, the whole run did. The child is detached:
    it does not hang, and it does not write into the parent's log."""
    log = tmp_path / "records.jsonl"

    def record(entry: dict[str, Any]) -> None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"record": entry["record"], "nodeid": entry["nodeid"]}) + "\n")

    sampler = Sampler(record, lambda: 1, interval=0.001, worker="x")
    sampler.start()
    sampler.begin_phase("t::parent", "call")
    spin_for(0.05)
    pid = os.fork()
    if pid == 0:
        # The child: what pytest-forked does next.
        try:
            sampler.begin_phase("t::child", "call")
            sampler.end_phase("call")
            sampler.end_test("t::child")
            sampler.stop()
        finally:
            os._exit(0)
    deadline = time.monotonic() + 20
    while True:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the forked child hung")
        time.sleep(0.01)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    sampler.end_phase("call")
    sampler.end_test("t::parent")
    sampler.stop()
    written = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [entry["nodeid"] for entry in written if entry["record"] == "test"] == ["t::parent"]


@pytest.mark.skipif(sys.platform == "win32", reason="resident memory is read through psutil there; not what this checks")
def test_a_climb_between_two_readings_carries_the_last_sampled_stack() -> None:
    """A body that allocates and returns between two memory readings is
    seen only by the reading at the phase boundary, where the frames that
    made it are gone. The stack the test's thread was last sampled in goes
    with the charge, for the analysis to place it on."""
    from pytest_failure_instrumentation import probes

    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: probes.resident_megabytes()[0], interval=0.01, worker="x")
    sampler.start()
    sampler.begin_phase("t::a", "call")

    def allocate() -> bytearray:
        block = bytearray(64 * 1024 * 1024)
        block[::4096] = b"x" * len(block[::4096])
        spin_for(0.2)  # long enough to be sampled inside it several times
        return block

    kept = allocate()
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()
    del kept

    (record,) = [entry for entry in records if entry["record"] == "test"]
    names = {
        record["frames"][index].split("|")[2]
        for entry in record["growth"]
        for index in (entry.get("frames") or []) + (entry.get("fallback") or [])
    }
    assert sum(entry["mb"] for entry in record["growth"]) >= 32, record["growth"]
    # Qualified where the interpreter qualifies (3.11+ writes <locals>.allocate).
    assert any(name.split(".")[-1] == "allocate" for name in names), (record["growth"], names)


def test_a_generator_caught_between_yields_keeps_its_callers() -> None:
    """A generator frame sampled between suspending and resuming has no
    f_back, so the sample was one frame deep and json's encoder was charged
    to the runtime alone, a few percent of the time and weighted by CPU.
    The thread's last stack that had the generator linked supplies the
    callers."""
    import json

    rows = [{"id": i, "name": f"n{i}", "tags": ["a", "b"], "v": i * 1.5} for i in range(20_000)]
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.002, worker="x")
    sampler.start()
    sampler.begin_phase("t::a", "call")

    def render() -> None:
        end = time.perf_counter() + 0.6
        while time.perf_counter() < end:
            json.dumps(rows, indent=2)

    render()
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()

    (record,) = [entry for entry in records if entry["record"] == "test"]
    names = [entry.split("|")[2] for entry in record["frames"]]
    in_encoder = orphaned = 0
    for stack in record["stacks"]:
        functions = [names[index] for index in stack["frames"]]
        if not any("_iterencode" in function for function in functions):
            continue
        in_encoder += stack["cpu_ns"]
        if not any(function.endswith("render") for function in functions):
            orphaned += stack["cpu_ns"]
    if in_encoder == 0:
        pytest.skip("this interpreter's json encodes with indent in C: no generator frames to catch")
    # The first sighting of a generator with nobody to relink it from stays
    # an orphan; the rest are relinked. Unfixed, a fifth to three quarters
    # of the encoder's time was orphaned, more on a loaded box.
    assert orphaned < in_encoder * 0.15, (orphaned, in_encoder)


def test_the_log_is_read_a_line_at_a_time_with_repeated_strings_shared(tmp_path) -> None:
    """The one place in this package whose cost scales with the number of
    tests: the controller reads every worker's log at session finish and the
    analysis walks the lot four times, so all of it is resident at once.

    A record spells its own frame table, which is the same absolute paths on
    every test in the run, and ``json`` shares nothing between calls - so
    twenty thousand records held twenty thousand copies of each. Sharing them
    is not an optimisation of the analysis, which never notices: it is the
    difference between 346 MB and 145 MB on a twenty-thousand-test fold. The
    keys go the same way, thirty per record and the same thirty every time.
    """
    from pytest_failure_instrumentation.profile.sampler import ProfileLog, read_profile_log

    path = tmp_path / "main.profile.jsonl"
    log = ProfileLog(path, run_id="run-1")
    frames = [f"/a/very/long/path/to/_pytest/python.py|{line}|pytest_pyfunc_call" for line in range(20)]
    for index in range(50):
        log.write({"record": "test", "nodeid": f"t::x[{index}]", "frames": list(frames), "stacks": []})
    log.close()

    records = read_profile_log(path)

    assert [record["nodeid"] for record in records] == [f"t::x[{index}]" for index in range(50)]
    assert all(record["frames"] == frames for record in records)
    # One object per distinct frame across the whole log, and one per key.
    assert {id(entry) for record in records for entry in record["frames"]} == {
        id(entry) for entry in records[0]["frames"]
    }
    assert len({id(key) for record in records for key in record}) == len(records[0])


def test_a_truncated_last_line_costs_that_record_and_no_others(tmp_path) -> None:
    """A run that was killed mid-write leaves a half-written line, and every
    test that finished before it is still on disk."""
    from pytest_failure_instrumentation.profile.sampler import read_profile_log

    path = tmp_path / "main.profile.jsonl"
    path.write_text(
        '{"record": "test", "nodeid": "t::a"}\n'
        '{"record": "test", "nodeid": "t::b"}\n'
        '{"record": "test", "nodei',
        encoding="utf-8",
    )

    assert [record["nodeid"] for record in read_profile_log(path)] == ["t::a", "t::b"]
    assert read_profile_log(tmp_path / "absent.profile.jsonl") == []
