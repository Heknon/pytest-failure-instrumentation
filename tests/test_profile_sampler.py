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
import sys
import threading
import time
import weakref
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
        time.sleep(0.5)
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

    allocate()
    gc.disable()
    try:
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
