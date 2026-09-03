"""The in-process sampler: what every thread is doing, weighted by the CPU it
actually burnt.

A sampling profiler usually counts wall time - every sample is one tick, and a
thread asleep in ``recv`` for ten seconds is ten seconds of ``recv``. That
profile answers "where did the time go" and says nothing about CPU, which is
the one question a worker sitting at 30% raises. So every sample here is
weighted by how far the sampled thread's own CPU counter moved since the last
sample. A blocked thread moves it by nothing and vanishes from the profile; a
thread spinning on a non-blocking read moves it fully and becomes the widest
thing in it.

The weighting also fixes the sampler's own blind spot. Sampling from inside
the process needs the GIL, so a native call that holds it for half a second
delays the next sample by half a second. Counting samples would then miss
most of that call; weighting by the counter charges the whole half second to
the frame that was in it. The profile can be late by one sample, never wrong
about the total.

What the kernel counts and Python cannot see is kept too. A thread pool a C
extension started has no Python stack to sample, but it has a CPU counter and
a name in procfs, and a run where most of the CPU is in such threads is a run
whose answer is outside Python - which is worth saying rather than quietly
attributing the remainder to nothing.

The per-sample path allocates as little as it can: a stack is a tuple of code
objects and line numbers, keyed straight into a dict, and names are resolved
once when a test's aggregate is written out.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from ..probes.platform_flags import IS_LINUX

#: Seconds between samples. Fifty a second is enough to see a two-second
#: spike as a hundred samples and cheap enough - a few microseconds per thread
#: per sample - to leave on for a whole run.
DEFAULT_INTERVAL = 0.02

#: Frames kept per stack. Deeper than any pytest stack that matters, and a
#: bound on what a runaway recursion costs the sampler.
MAX_DEPTH = 80

#: Resident memory is read every this many samples: it is a file read on
#: Linux and a syscall elsewhere, and the peak it feeds needs no better than
#: half-second resolution.
RSS_EVERY = 25

#: The kernel's thread list is re-read every this many samples, to find the
#: threads Python does not know about. See ThreadClock.discover.
DISCOVER_EVERY = 50

#: A tick that arrives this many intervals late was held up - by native code
#: holding the GIL, nearly always. The CPU it carries was burnt *between* the
#: two samples, and the previous sample is a point inside that span where the
#: current one is at its end: after a long native call has returned, the
#: current frame is the caller and the previous frame is the call. So a late
#: tick's CPU is charged to the stack seen before it. Late by one sample,
#: never charged to the frame that happened to come next.
LATE_TICK_FACTOR = 3.0

#: What a test's record is called on disk, and the process-wide leftovers'.
TEST_RECORD = "test"
BACKGROUND_RECORD = "background"


# -- per-thread CPU -----------------------------------------------------------


class ThreadClock:
    """CPU time per OS thread, from whatever this platform offers.

    On Linux every thread has a POSIX clock, and its id can be built from the
    thread id alone - it is what ``pthread_getcpuclockid`` returns - so one
    ``clock_gettime`` per thread reads the counter in a few hundred
    nanoseconds **without releasing the GIL**. That last part is the reason
    procfs is not read here even though it has the same number. A sampler
    thread runs beside a test thread that is busy, and every file it opens
    releases the GIL and then waits a whole switch interval to get it back;
    four threads' ``schedstat`` files cost sixty milliseconds a tick that
    way, and a fifty-hertz sampler ran at eight. Measured.

    Everywhere else psutil's per-thread times are used; their ids match
    Python's ``native_id`` on Windows and do not on macOS, where the profile
    falls back to wall weighting and says so.
    """

    def __init__(self) -> None:
        self.source = "unavailable"
        if IS_LINUX and hasattr(time, "clock_gettime_ns"):
            try:
                time.clock_gettime_ns(_thread_clock_id(os.getpid()))
                self.source = "thread-clock"
                return
            except (OSError, OverflowError, ValueError):
                pass
        try:
            import psutil

            self._process = psutil.Process()
            self._process.threads()
            # macOS answers, but with ids that are not native_id: a profile
            # weighted by a counter that belongs to some other thread is
            # worse than one honestly weighted by wall time.
            self.source = "psutil" if sys.platform != "darwin" else "unavailable"
        except Exception:
            self.source = "unavailable"

    @property
    def available(self) -> bool:
        return self.source != "unavailable"

    def read(self, tids: Any) -> dict[int, int]:
        """``native_id -> nanoseconds on CPU`` for the threads asked about.

        A thread that ended between being listed and being read is left out
        rather than reported at zero.
        """
        if self.source == "thread-clock":
            clocks: dict[int, int] = {}
            for tid in tids:
                try:
                    clocks[tid] = time.clock_gettime_ns(_thread_clock_id(tid))
                except (OSError, OverflowError, ValueError):
                    continue
            return clocks
        if self.source == "psutil":
            try:
                return {
                    int(entry.id): int((entry.user_time + entry.system_time) * 1e9)
                    for entry in self._process.threads()
                }
            except Exception:
                return {}
        return {}

    def discover(self) -> list[int]:
        """Every thread the kernel knows in this process, Python's or not.

        The one procfs read left, and it releases the GIL - so it is called
        once a second rather than once a tick. A native thread that starts
        and dies within that second goes unseen, which is the trade.
        """
        if self.source == "thread-clock":
            try:
                return [int(name) for name in os.listdir("/proc/self/task")]
            except (OSError, ValueError):
                return []
        if self.source == "psutil":
            return list(self.read(()))
        return []


def _thread_clock_id(tid: int) -> int:
    """The clockid_t of a thread's CPU clock, as glibc builds it.

    ``MAKE_THREAD_CPUCLOCK(tid, CPUCLOCK_SCHED)`` from the kernel's
    ``posix-cpu-timers``: the inverted tid shifted up three bits, with the
    low bits naming the scheduler clock (2) for a thread (4).
    """
    return ((~tid) << 3) | 6


def _thread_name(tid: int) -> str:
    """The kernel's name for a thread Python does not know about."""
    try:
        with open(f"/proc/self/task/{tid}/comm", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return f"tid {tid}"


# -- the sampler ---------------------------------------------------------------


#: (code, line, instruction offset) per frame, innermost first. The line is
#: what the frame reported and can be None - see _line_of - so the offset is
#: kept to resolve it from at flush.
StackKey = tuple[tuple[Any, Any, int], ...]


class _Window:
    """What is being accumulated for one test, or for the gaps between them."""

    def __init__(self, nodeid: Optional[str], rss_mb: Optional[int]) -> None:
        self.nodeid = nodeid
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.rss_before = rss_mb
        self.rss_peak = rss_mb
        #: The live heap at the start: what the allocator has handed out, and
        #: how many small-object blocks Python holds. Against the same two at
        #: the end they say whether memory the test left behind is still in
        #: use or merely still mapped.
        self.heap_before = _heap_in_use()
        self.blocks_before = sys.getallocatedblocks()
        #: Resident memory at each phase boundary, so a step can be placed in
        #: setup (a fixture) rather than charged to the test's own body.
        self.rss_at: dict[str, Optional[int]] = {}
        #: The thread the runtest protocol runs on, which is the thread a
        #: test's own work is on. Anything else is background.
        self.test_thread: Optional[int] = None
        #: (phase, thread name, background, stack) -> [cpu_ns, wall_ns, samples]
        self.stacks: dict[tuple[Optional[str], str, bool, StackKey], list[int]] = defaultdict(
            lambda: [0, 0, 0]
        )
        #: Threads with a CPU counter and no Python frames: tid -> [name, cpu_ns]
        self.native: dict[int, list[Any]] = {}
        self.gc_seconds = 0.0
        self.gc_collections = [0, 0, 0]
        self.phase_cpu: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.phase_started: Optional[tuple[str, float, float]] = None


class Sampler:
    def __init__(
        self,
        record: Callable[[dict[str, Any]], None],
        resident_megabytes: Callable[[], Optional[int]],
        *,
        interval: float = DEFAULT_INTERVAL,
        worker: str = "",
    ) -> None:
        self.record = record
        self.resident_megabytes = resident_megabytes
        self.interval = max(0.001, float(interval))
        self.worker = worker
        self.clock = ThreadClock()
        self.nodeid: Optional[str] = None
        self.phase: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="failure-profile", daemon=True)
        self._own_ident: Optional[int] = None
        self._last_clock: dict[int, int] = {}
        self._last_tick = time.monotonic()
        self._threads: dict[int, tuple[int, str]] = {}
        self._known_idents: frozenset[int] = frozenset()
        #: Every tid the kernel listed at the last discovery, and the names
        #: of the ones Python has no thread object for.
        self._all_tids: list[int] = []
        self._native_names: dict[int, str] = {}
        #: The stack each thread was in at the previous tick - see LATE_TICK_FACTOR.
        self._previous: dict[int, tuple[str, StackKey]] = {}
        #: When the background window was set aside for a test, so the time
        #: the test took is not also counted as the background's.
        self._paused: Optional[tuple[float, float]] = None
        self._ticks = 0
        self._window = _Window(None, self._rss())
        self._background = self._window
        self._gc_started = 0.0
        self.samples_taken = 0
        self.session_started = time.monotonic()
        self.session_cpu_started = time.process_time()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._refresh_threads()
        self._all_tids = self.clock.discover()
        self._last_clock = self.clock.read(self._tids_to_read())
        self._last_tick = time.monotonic()
        gc.callbacks.append(self._on_gc)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            gc.callbacks.remove(self._on_gc)
        except ValueError:
            pass
        with self._lock:
            if self._window.nodeid is not None:
                # A test the run died inside: written as far as it got.
                self._flush(self._window, TEST_RECORD)
            self._flush(self._background, BACKGROUND_RECORD)

    def describe(self) -> dict[str, Any]:
        return {"interval": self.interval, "thread_clock": self.clock.source}

    # -- what the recorder tells it ------------------------------------------

    def begin_phase(self, nodeid: str, phase: str) -> None:
        """Called on the test's own thread, which is how that thread is known."""
        with self._lock:
            if self._window.nodeid != nodeid:
                self._window = _Window(nodeid, self._rss())
                self._paused = (time.monotonic(), time.process_time())
            window = self._window
            window.test_thread = threading.get_ident()
            window.rss_at[f"{phase}_start"] = self._rss()
            window.phase_started = (phase, time.monotonic(), time.process_time())
            self.nodeid = nodeid
            self.phase = phase

    def end_phase(self, phase: str) -> None:
        with self._lock:
            window = self._window
            if window.phase_started is not None and window.phase_started[0] == phase:
                _, wall, cpu = window.phase_started
                window.phase_cpu[phase][0] += time.process_time() - cpu
                window.phase_cpu[phase][1] += time.monotonic() - wall
                window.phase_started = None
            window.rss_at[f"{phase}_end"] = self._rss()
            self.phase = None

    def end_test(self, nodeid: str) -> None:
        with self._lock:
            window = self._window
            if window.nodeid != nodeid:
                return
            self._flush(window, TEST_RECORD)
            self._window = self._background
            if self._paused is not None:
                # The background's clocks skip the test, so its record says
                # what the gaps cost and not what the tests did as well.
                wall, cpu = self._paused
                self._background.started += time.monotonic() - wall
                self._background.cpu_started += time.process_time() - cpu
                self._paused = None
            self.nodeid = None
            self.phase = None

    # -- the sampling thread -------------------------------------------------

    def _run(self) -> None:
        self._own_ident = threading.get_ident()
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except Exception:  # noqa: BLE001 - a profiler must never end a run
                continue

    def _tids_to_read(self) -> set[int]:
        tids = {tid for tid, _ in self._threads.values() if tid}
        tids.update(self._all_tids)
        return tids

    def _sample(self) -> None:
        now = time.monotonic()
        wall_ns = int((now - self._last_tick) * 1e9)
        self._last_tick = now
        frames = sys._current_frames()
        # Our own frame is in that dict, and the dict is in our own frame's
        # locals: left there, the two form a cycle that outlives this call,
        # and every frame in the dict - the test's included, with everything
        # it holds - lives on until a full collection notices. Measured as a
        # 300 MB test whose memory was "kept" until long after it returned.
        if self._own_ident is not None:
            frames.pop(self._own_ident, None)
        idents = frozenset(frames)
        if idents != self._known_idents:
            self._refresh_threads()
            self._known_idents = idents
        if self._ticks % DISCOVER_EVERY == 0:
            self._all_tids = self.clock.discover()
        clocks = self.clock.read(self._tids_to_read())

        late = wall_ns > LATE_TICK_FACTOR * self.interval * 1e9
        with self._lock:
            window = self._window
            phase = self.phase
            seen_tids: set[int] = set()
            for ident, frame in frames.items():
                if ident == self._own_ident:
                    continue
                tid, name = self._threads.get(ident, (0, f"thread {ident}"))
                seen_tids.add(tid)
                cpu_ns = self._cpu_delta(tid, clocks)
                if cpu_ns is None:
                    # No counter for this thread: weight by wall time so the
                    # sample is not lost, and let the record say the profile
                    # was not CPU weighted.
                    cpu_ns = wall_ns if not self.clock.available else 0
                if cpu_ns <= 0 and self.clock.available:
                    continue  # idle: the whole point of the weighting
                stack = _stack_of(frame)
                if not stack:
                    continue
                previous = self._previous.get(ident)
                self._previous[ident] = (name, stack)
                if late and previous is not None:
                    name, stack = previous
                background = window.test_thread is not None and ident != window.test_thread
                entry = window.stacks[(phase, name, background, stack)]
                entry[0] += cpu_ns
                entry[1] += wall_ns
                entry[2] += 1
                self.samples_taken += 1

            # Threads the kernel knows and Python does not.
            for tid in clocks:
                if tid in seen_tids:
                    continue
                delta = self._cpu_delta(tid, clocks)
                if delta:
                    native_name = self._native_names.get(tid)
                    if native_name is None:
                        native_name = self._native_names[tid] = _thread_name(tid)
                    entry = window.native.setdefault(tid, [native_name, 0])
                    entry[1] += delta

            self._ticks += 1
            if self._ticks % RSS_EVERY == 0:
                rss = self._rss()
                if rss is not None and (window.rss_peak is None or rss > window.rss_peak):
                    window.rss_peak = rss

        self._last_clock = clocks
        del frames

    def _cpu_delta(self, tid: int, clocks: dict[int, int]) -> Optional[int]:
        if tid not in clocks:
            return None
        previous = self._last_clock.get(tid)
        if previous is None:
            return 0  # first sighting: nothing to difference against
        return max(0, clocks[tid] - previous)

    def _refresh_threads(self) -> None:
        threads: dict[int, tuple[int, str]] = {}
        for thread in threading.enumerate():
            if thread.ident is None:
                continue
            native = getattr(thread, "native_id", None) or 0
            threads[thread.ident] = (int(native), thread.name)
        self._threads = threads

    def _rss(self) -> Optional[int]:
        try:
            return self.resident_megabytes()
        except Exception:
            return None

    # -- garbage collection ----------------------------------------------------

    def _on_gc(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._gc_started = time.perf_counter()
            return
        elapsed = time.perf_counter() - self._gc_started
        # No lock here, on purpose. A collection can start inside the sampler
        # thread while it holds the lock - it allocates - and a callback that
        # then waited for the same lock would deadlock the process. The two
        # counters are read once, at flush, and a race there costs one
        # collection's worth of accuracy rather than the run.
        window = self._window
        window.gc_seconds += elapsed
        generation = int(info.get("generation", 0))
        if 0 <= generation < 3:
            window.gc_collections[generation] += 1

    # -- writing a record ------------------------------------------------------

    def _flush(self, window: _Window, kind: str) -> None:
        """Resolve names once and hand the aggregate to whoever records it.

        Called with the lock held. Frames are written as a table and stacks
        as index lists, so a test with a thousand distinct stacks over the
        same forty frames does not spell each frame a thousand times.
        """
        now = time.monotonic()
        rss_after = self._rss()
        heap_after = _heap_in_use()
        blocks_after = sys.getallocatedblocks()
        frames: dict[tuple[str, int, str], int] = {}
        stacks = []
        for (phase, thread, background, stack), (cpu_ns, wall_ns, samples) in window.stacks.items():
            indexes = []
            for code, line, offset in stack:
                if not line:
                    line = _line_of(code, offset)
                key = (code.co_filename, line, getattr(code, "co_qualname", code.co_name))
                index = frames.get(key)
                if index is None:
                    index = frames[key] = len(frames)
                indexes.append(index)
            stacks.append(
                {
                    "phase": phase,
                    "thread": thread,
                    "background": background,
                    "frames": indexes,
                    "cpu_ns": cpu_ns,
                    "wall_ns": wall_ns,
                    "samples": samples,
                }
            )
        record = {
            "record": kind,
            "worker": self.worker,
            "nodeid": window.nodeid,
            "wall_s": round(now - window.started, 4),
            "cpu_s": round(time.process_time() - window.cpu_started, 4),
            "rss_before_mb": window.rss_before,
            "rss_after_mb": rss_after,
            "rss_peak_mb": max(
                value for value in (window.rss_peak, rss_after, window.rss_before) if value is not None
            )
            if any(value is not None for value in (window.rss_peak, rss_after, window.rss_before))
            else None,
            "rss_at": window.rss_at,
            "heap_before_mb": window.heap_before,
            "heap_after_mb": heap_after,
            "blocks_before": window.blocks_before,
            "blocks_after": blocks_after,
            "phases": {
                phase: {"cpu_s": round(cpu, 4), "wall_s": round(wall, 4)}
                for phase, (cpu, wall) in window.phase_cpu.items()
            },
            "gc": {
                "seconds": round(window.gc_seconds, 4),
                "collections": sum(window.gc_collections),
                "by_generation": list(window.gc_collections),
            },
            "cpu_weighted": self.clock.available,
            "thread_clock": self.clock.source,
            "frames": [f"{file}|{line}|{function}" for (file, line, function) in frames],
            "stacks": stacks,
            "native_threads": [
                {"tid": tid, "name": name, "cpu_ns": cpu_ns}
                for tid, (name, cpu_ns) in window.native.items()
                if cpu_ns > 0
            ],
        }
        try:
            self.record(record)
        except Exception:  # noqa: BLE001
            pass
        # Whatever this window was, its aggregates are written now. The
        # background window keeps accumulating between tests.
        window.stacks.clear()
        window.native.clear()
        window.gc_seconds = 0.0
        window.gc_collections = [0, 0, 0]
        window.started = now
        window.cpu_started = time.process_time()
        window.rss_before = rss_after
        window.rss_peak = rss_after
        window.rss_at = {}
        window.heap_before = heap_after
        window.blocks_before = blocks_after
        window.phase_cpu.clear()


def _heap_in_use() -> Optional[int]:
    from .. import probes

    try:
        return probes.heap_in_use_megabytes()[0]
    except Exception:
        return None


def _stack_of(frame: Any) -> StackKey:
    """Innermost first, as a tuple of (code, line) pairs.

    Code objects are kept rather than names because they are what the frame
    already holds; naming them costs string work on every sample and is done
    once per distinct stack at flush instead.
    """
    stack = []
    depth = 0
    while frame is not None and depth < MAX_DEPTH:
        try:
            stack.append((frame.f_code, frame.f_lineno, frame.f_lasti))
            frame = frame.f_back
        except Exception:  # noqa: BLE001 - a frame torn down under us
            break
        depth += 1
    return tuple(stack)


#: Per code object, the (start offset, line) table its lines resolve from.
_line_tables: dict[Any, list[tuple[int, int]]] = {}


def _line_of(code: Any, offset: int) -> int:
    """The line a frame is on when it would not say.

    A thread made to give up the GIL does so at an eval-breaker check, and
    on 3.11 that is the backward jump of a loop - an instruction the compiler
    gives no line to, so ``f_lineno`` answers None for exactly the frames a
    profiler is most interested in: tight loops. Measured on the per-pixel
    comparison, where every sample landed there. The instruction offset is
    still known, and the nearest earlier instruction with a line is the
    line of the loop body.
    """
    table = _line_tables.get(code)
    if table is None:
        table = []
        try:
            for start, _end, line in code.co_lines():
                if line is not None:
                    table.append((start, line))
        except AttributeError:  # before 3.10
            import dis

            table = [(start, line) for start, line in dis.findlinestarts(code)]
        table.sort()
        _line_tables[code] = table
    line = code.co_firstlineno
    for start, candidate in table:
        if start > offset:
            break
        line = candidate
    return line


# -- the file ----------------------------------------------------------------------


class ProfileLog:
    """One JSON line per test, appended as each finishes and flushed at once,
    so a run that dies keeps the profiles of every test that completed."""

    def __init__(self, path: Path, run_id: Optional[str] = None) -> None:
        self.path = path
        self.run_id = run_id
        self._stream = path.open("a", buffering=1, encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("run_id", self.run_id)
        record.setdefault("time", round(time.time(), 3))
        try:
            self._stream.write(json.dumps(record) + "\n")
            self._stream.flush()
        except (OSError, ValueError, TypeError):
            pass

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:
            pass


def read_profile_log(path: Path) -> list[dict[str, Any]]:
    """Tolerates a truncated final line, like the event log does."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
