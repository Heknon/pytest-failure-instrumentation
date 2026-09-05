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
import inspect
import json
import os
import signal
import sys
import textwrap
import threading
import time
import tracemalloc
import weakref
from pathlib import Path
from typing import Any

import pytest

from pytest_failure_instrumentation.profile import analysis as burst_analysis
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
    # The assertion is about CPU, so time the workload by CPU as well.
    # A wall-time burn can receive arbitrarily little CPU on a loaded host.
    deadline = time.thread_time() + 0.3
    wall_deadline = time.monotonic() + 10.0
    while time.thread_time() < deadline:
        if time.monotonic() >= wall_deadline:
            pytest.fail("runner could not provide 0.3 seconds of CPU within 10 seconds")
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
        # Half a second of this thread's CPU, however fast the interpreter and
        # however busy the machine: a fixed iteration count is a fixed amount
        # of work, and on a fast 3.13 it was over before enough ticks had
        # landed in it to prove anything.
        total = 0
        clock = getattr(time, "thread_time", time.process_time)
        deadline = clock() + 0.5
        while clock() < deadline:
            for value in range(200_000):
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
    # Inside `hot`, and not its `def` line. Bounded by the function's own
    # length rather than by a number written here, which went stale the first
    # time the body grew.
    first = hot.__code__.co_firstlineno
    body = len(textwrap.dedent(inspect.getsource(hot)).splitlines())
    assert all(first < line <= first + body for line in lines), lines


def test_a_timeline_window_is_a_tenth_of_a_second_however_slowly_it_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise elapsed-time bucketing without coupling CPU burn to scheduling.

    The old test burnt real thread CPU then asserted an upper wall-time bound.
    A descheduled runner could exceed that bound with no sampler regression.
    Real clock/attribution behavior is covered by the busy/idle tests above.
    """
    for gap_ns in (20_000_000, 80_000_000, 200_000_000):
        with monkeypatch.context() as patch:
            now = [10_000_000_000]
            cpu = [0]
            patch.setattr(sampling.time, "monotonic", lambda now=now: now[0] / 1e9)
            patch.setattr(sampling.time, "process_time", lambda cpu=cpu: cpu[0] / 1e9)
            patch.setattr(sampling.time, "process_time_ns", lambda cpu=cpu: cpu[0])
            records: list[dict[str, Any]] = []
            sampler = Sampler(records.append, lambda: 1, interval=0.02, worker="x")
            sampler.clock.source = "thread-clock"
            patch.setattr(sampler.clock, "read", lambda tids, cpu=cpu: {threading.get_native_id(): cpu[0]})
            patch.setattr(sampler.clock, "discover", lambda known=None: [threading.get_native_id()])
            patch.setattr(sampler._machine, "busy_permille", lambda: 0)
            sampler.begin_phase("t::a", "setup")
            for _ in range((600_000_000 + gap_ns - 1) // gap_ns):
                now[0] += gap_ns
                cpu[0] += gap_ns * 9 // 10
                sampler._sample()
            sampler.end_phase("setup")
            sampler.end_test("t::a")

            (record,) = [entry for entry in records if entry["record"] == "test"]
            timeline = record["timeline"]
            assert len(timeline) >= 3, (gap_ns, timeline)
            longest = max(after[0] - before[0] for before, after in zip(timeline, timeline[1:]))
            # First tick at/after the 100 ms boundary, allowing one tick of
            # rounding, rather than a fixed number of sampling ticks.
            assert longest <= max(gap_ns / 1e6, sampling.WINDOW_SECONDS * 1000) + gap_ns / 1e6 + 1
            bursts = burst_analysis._bursts(record, burst_analysis.Thresholds(burst_cores=0.5))
            assert len(bursts) == 1, (gap_ns, timeline, bursts)



def test_a_tick_keeps_nothing_it_read_once_it_is_over() -> None:
    """The frames dict from sys._current_frames holds the sampler's own
    frame, whose locals hold the dict: a cycle that would keep every sampled
    frame - and the test's locals with it - until a full collection.

    The tick is taken by hand, on this thread, rather than by the sampling
    thread. Against a running sampler what this asserts is partly *when* the
    check happened: a tick holds the frames it read for as long as it is in
    `_tick`, and one of those frames belongs to a function that has since
    returned - so a check that lands mid-tick sees the blob alive and reports
    the bug. Windows makes that window wide enough to hit, because psutil's
    per-thread read there enumerates every thread on the machine, and the 3.9
    cell failed on it. Synchronously there is no window and no weather: what
    is left after a tick is over is the whole question.
    """

    class Blob(list):
        pass

    holder: list[Any] = []
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=1.0, worker="x")
    # What `start` would set, without starting the thread that would race this.
    sampler._own_ident = threading.get_ident()
    sampler.begin_phase("t::a", "call")

    def allocate() -> None:
        blob = Blob(bytearray(100_000) for _ in range(50))
        holder.append(weakref.ref(blob))
        sampler._sample()  # a tick, taken while this frame is live

    # Off before the allocation, not after: a collection between the return
    # and the check would clear a cycle the tick had created, and pass this
    # with the bug present.
    gc.disable()
    try:
        allocate()
        alive = holder[0]() is not None
    finally:
        gc.enable()
    sampler.end_phase("call")
    sampler.end_test("t::a")
    sampler.stop()
    assert not alive, "a tick kept the frames it read past its own return"


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


def test_a_threads_first_sighting_charges_the_whole_counter_it_arrived_with() -> None:
    """A thread's CPU clock starts at zero when the thread does, so the first
    reading of it is not a baseline to subtract from later ones - it is
    everything that thread has burnt. Treated as a baseline, a thread that
    lived less than a sampling interval was charged nothing at all, which is
    every worker of a pool that runs one thread per task.
    """
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=1.0, worker="x")

    # Never seen before: all of it.
    assert sampler._cpu_delta(4242, {4242: 7_000_000}) == 7_000_000
    # Seen before: the difference, and never a negative one.
    sampler._last_clock = {4242: 7_000_000}
    assert sampler._cpu_delta(4242, {4242: 9_500_000}) == 2_500_000
    assert sampler._cpu_delta(4242, {4242: 6_000_000}) == 0
    # Not in this reading at all: nothing to charge.
    assert sampler._cpu_delta(4242, {}) is None


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


def test_what_the_sampler_remembers_is_bounded_by_the_threads_that_exist() -> None:
    """A thread's ident is a pthread handle and is handed to the next thread
    the moment this one ends, so a pool running a task per thread cycles a
    handful of idents through hundreds of threads - and everything the sampler
    keys by ident would otherwise be a note about a thread that is gone.

    Size is the smaller half. ``_linked`` is what a generator caught between
    yields is relinked onto, guarded against a stale entry by "same window" -
    and the background window is one object for the whole run, so an entry
    made between tests passes that guard for ever. A generator on a new thread
    would then be relinked onto a dead thread's last stack, and the profile
    would say so.

    Written with the notes planted rather than by outliving real threads: a
    thread that lives less than a sampling interval is never seen, and one
    that is seen leaves an entry only until its ident is handed on - so a test
    that made threads and looked at what was left would pass for the wrong
    reason as often as the right one.
    """
    records: list[dict[str, Any]] = []
    sampler = Sampler(records.append, lambda: 1, interval=0.002, worker="x")
    ghost = -1  # no thread has this ident, so it is a thread that is gone
    sampler.start()
    try:
        with sampler._lock:
            sampler._previous[ghost] = (sampler._ticks, "gone", (), sampler._background)
            sampler._linked[ghost] = ((), sampler._background)
        sampler._native_names[-2] = "a kernel thread that has exited"
        alive = threading.get_ident()
        deadline = time.monotonic() + 5.0
        while sampler._ticks < sampling.DISCOVER_EVERY * 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sampler._ticks >= sampling.DISCOVER_EVERY, "the sampler never reached a discovery"
    finally:
        sampler.stop()

    assert ghost not in sampler._previous
    assert ghost not in sampler._linked
    assert -2 not in sampler._native_names
    # The thread that was actually running is still remembered.
    assert alive in sampler._previous or not sampler._previous


class Widget:
    """A class whose methods are what a hotspot in one would be named after."""

    def render(self) -> int:
        return sum(character for character in [1, 2, 3])

    @staticmethod
    def measure() -> int:
        return 1

    @classmethod
    def build(cls) -> Widget:
        return cls()

    @property
    def size(self) -> int:
        return 2

    class Inner:
        def deep(self) -> int:
            return 3


def a_module_level_function() -> list:
    return [value for value in range(3)]


def test_a_method_is_named_with_its_class_on_every_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run``, ``close``, ``__init__`` and ``send`` are the method names this
    reports most often and the ones a bare name says least about, and 3.9 and
    3.10 - both supported - have no ``co_qualname`` to read one from. The name
    is worked out from the defining module there instead.

    Checked against ``co_qualname`` itself rather than against a literal: this
    runs the worked-out path on every interpreter, and on the ones that have
    the real answer it asserts the two agree. Where they cannot be compared -
    3.9 and 3.10 - the shapes below are what the later ones produce.
    """
    from pytest_failure_instrumentation.profile.sampler import _qualified_name

    subjects = [
        Widget.render,
        Widget.measure,
        Widget.build.__func__,  # type: ignore[attr-defined]
        Widget.__dict__["size"].fget,
        Widget.Inner.deep,
        a_module_level_function,
    ]
    for function in subjects:
        code = function.__code__
        monkeypatch.setattr(sampling, "HAS_QUALNAME", False)
        monkeypatch.setattr(sampling, "_qualnames", {})
        monkeypatch.setattr(sampling, "_walked", set())
        worked_out = _qualified_name(code)
        if sampling.HAS_QUALNAME or hasattr(code, "co_qualname"):
            assert worked_out == code.co_qualname, function
        assert worked_out.startswith(("Widget.", "a_module_level_function")), function

    # The comprehension inside a method is folded back into the method, which
    # is what the analysis reads and what 3.12 produces without being asked.
    monkeypatch.setattr(sampling, "HAS_QUALNAME", False)
    monkeypatch.setattr(sampling, "_qualnames", {})
    monkeypatch.setattr(sampling, "_walked", set())
    inner = [
        const
        for const in Widget.render.__code__.co_consts
        if isinstance(const, type(Widget.render.__code__))
    ]
    if inner:  # 3.12 inlines comprehensions, so there is no code object to find
        assert _qualified_name(inner[0]) == "Widget.render.<locals>.<genexpr>"


def test_a_function_that_cannot_be_found_keeps_its_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code compiled at runtime belongs to no module, so there is nothing to
    search. It is named once, not looked for on every flush after that."""
    from pytest_failure_instrumentation.profile.sampler import _qualified_name

    monkeypatch.setattr(sampling, "HAS_QUALNAME", False)
    monkeypatch.setattr(sampling, "_qualnames", {})
    monkeypatch.setattr(sampling, "_walked", set())
    namespace: dict = {}
    exec(compile("def made_up():\n    pass\n", "<nowhere>", "exec"), namespace)

    code = namespace["made_up"].__code__
    assert _qualified_name(code) == "made_up"
    assert code in sampling._qualnames


@pytest.mark.parametrize("source", [source for source in readers() if source in {"mach", "psutil"}])
def test_a_discovery_reuses_the_clocks_it_was_just_handed(source: str) -> None:
    """Where a reader lists every thread on every read - mach and psutil -
    the clocks read a moment ago on the same tick already *are* the thread
    list, and asking again costs what the read costs.

    It matters most where it costs most. psutil's ``Process.threads()`` on
    Windows goes through ``NtQuerySystemInformation``, which enumerates every
    thread on the machine, and a discovery tick was paying for two or three
    of those at fifty ticks a second.
    """
    clock = clock_with(source)
    if not clock.available:
        pytest.skip("no per-thread CPU clock on this platform")

    reads = 0
    real = clock.read

    def counted(tids: Any) -> dict:
        nonlocal reads
        reads += 1
        return real(tids)

    clock.read = counted  # type: ignore[method-assign]
    handed = real(())
    assert clock.discover(handed) == list(handed)
    assert reads == 0, "the clocks it was handed were enough"
    assert clock.discover() and reads == 1, "and without them it still answers"


@pytest.mark.parametrize("has_thread_objects", [True, False])
@pytest.mark.parametrize("windows", [True, False])
def test_startup_discovery_is_reused_until_the_next_scheduled_tick(monkeypatch, has_thread_objects, windows):
    sampler = Sampler(lambda row: None, lambda: 0)
    discoveries = []
    if windows:
        sampler.clock.source = "windows-thread-times"
    else:
        sampler.clock.source = "psutil"
    own = threading.get_native_id()

    def discover(known=None):
        discoveries.append(known)
        return [own]

    monkeypatch.setattr(sampler.clock, "discover", discover)
    monkeypatch.setattr(sampler.clock, "read", lambda tids: {own: 0})
    monkeypatch.setattr(sampler._thread, "start", lambda: None)
    if not has_thread_objects:
        monkeypatch.setattr(sampler, "_refresh_threads", lambda: None)
    try:
        sampler.start()
        assert len(discoveries) == (0 if windows else 1)
        sampler._sample()
        assert len(discoveries) == 1
        sampler._ticks = sampling.DISCOVER_EVERY
        sampler._sample()
        assert len(discoveries) == 2
    finally:
        sampler.stop()


def test_windows_clock_closes_handles_on_failed_reads() -> None:
    import ctypes
    from types import SimpleNamespace

    closed = []

    def read(handle, created, exited, kernel, user):
        kernel._obj.value = 11
        user._obj.value = 17
        return handle != 2

    clock = sampling._WindowsThreads.__new__(sampling._WindowsThreads)
    clock.ctypes = ctypes
    clock.kernel = SimpleNamespace(OpenThread=lambda access, inherit, tid: tid if tid != 3 else 0,
                                   GetThreadTimes=read, CloseHandle=closed.append)
    assert clock.read([0, 1, 2, 3]) == {1: 2800}
    assert closed == [1, 2]


def test_windows_clock_closes_handles_on_exception() -> None:
    import ctypes
    from types import SimpleNamespace

    closed = []

    def read(*args):
        raise OSError("thread query failed")

    clock = sampling._WindowsThreads.__new__(sampling._WindowsThreads)
    clock.ctypes = ctypes
    clock.kernel = SimpleNamespace(OpenThread=lambda *args: 123, GetThreadTimes=read, CloseHandle=closed.append)
    with pytest.raises(OSError, match="thread query failed"):
        clock.read([1])
    assert closed == [123]


def test_windows_system_clock_counts_idle_only_once():
    import ctypes
    from types import SimpleNamespace

    def read(idle, kernel, user):
        idle._obj.value, kernel._obj.value, user._obj.value = 40, 60, 20
        return 1

    clock = sampling._WindowsSystemClock.__new__(sampling._WindowsSystemClock)
    clock.ctypes = ctypes
    clock.kernel = SimpleNamespace(GetSystemTimes=read)
    assert clock.read() == (80, 40)


def test_windows_system_clock_failure_resets_portable_baseline():
    from collections import namedtuple
    from types import SimpleNamespace

    def fails():
        raise OSError("clock unavailable")

    Times = namedtuple("Times", "user system idle")
    readings = iter([Times(100, 100, 100), Times(105, 110, 105)])
    clock = sampling._MachineClock.__new__(sampling._MachineClock)
    clock._windows = SimpleNamespace(read=fails)
    clock._last = Times(0, 0, 0)
    clock._psutil = SimpleNamespace(cpu_times=lambda: next(readings))
    assert clock.busy_permille() is None
    assert clock._windows is None
    assert clock.busy_permille() == 750
