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
import tracemalloc
import weakref
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

#: Resident memory is read every this many samples. It is a file read that
#: releases the GIL, so not every tick; but every climb it sees is charged to
#: the stack running at that moment, and a fixture that builds 150 MB in one
#: comprehension is done in fifty milliseconds - so every other tick, which
#: is a fortieth of a second and costs the sampler a GIL round trip each time
#: rather than any CPU.
RSS_EVERY = 2

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

#: Ticks per timeline window. The timeline is the process's CPU and the
#: machine's, window by window, which is what a burst is read from: a test
#: that is idle for ten seconds and then burns a core for two is a flat
#: line with a step in it, and the step has a start, a length and a height.
#: Five ticks is a tenth of a second at the default interval.
WINDOW_TICKS = 5

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

    On macOS the kernel answers ``thread_info`` for each thread of the task
    with its CPU times and, separately, the system-wide id that
    ``pthread_threadid_np`` reports - which is what Python's ``native_id``
    is there. Called through ctypes without releasing the GIL, for the same
    reason. It is self-checked at start: the calling thread's own id has to
    come back in the answer, or the layout is not what this expects and the
    reader stands down rather than weight samples by somebody else's counter.

    On Windows psutil's per-thread times are used; their ids are the Win32
    thread ids ``native_id`` reports. They move in the scheduler's sixteen
    millisecond ticks, which is coarse against a twenty millisecond sampling
    interval but sums correctly over a test.

    Where none of these answers, ``available`` is False and the sampler
    charges the *process's* CPU to the test's thread - see ``Sampler._sample``.
    """

    def __init__(self) -> None:
        self.source = "unavailable"
        self._mach: Any = None
        if IS_LINUX and hasattr(time, "clock_gettime_ns"):
            try:
                time.clock_gettime_ns(_thread_clock_id(os.getpid()))
                self.source = "thread-clock"
                return
            except (OSError, OverflowError, ValueError):
                pass
        if sys.platform == "darwin":
            try:
                mach = _MachThreads()
                own = threading.get_native_id()
                if own in mach.read():
                    self._mach = mach
                    self.source = "mach"
                    return
            except Exception:
                pass
        try:
            import psutil

            self._process = psutil.Process()
            self._process.threads()
            # macOS answers, but with ids that are indexes rather than
            # native_id: a profile weighted by a counter that belongs to some
            # other thread is worse than one honestly weighted otherwise.
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
        if self.source == "mach":
            try:
                return self._mach.read()
            except Exception:
                return {}
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

        On Linux the one procfs read left, and it releases the GIL - so it
        is called once a second rather than once a tick. A native thread that
        starts and dies within that second goes unseen, which is the trade.
        The other readers list every thread on every read anyway.
        """
        if self.source == "thread-clock":
            try:
                return [int(name) for name in os.listdir("/proc/self/task")]
            except (OSError, ValueError):
                return []
        if self.source in ("mach", "psutil"):
            return list(self.read(()))
        return []


class _MachThreads:
    """``thread_info`` over every thread of this task, through ctypes.

    Layouts from ``<mach/thread_info.h>``: ``thread_basic_info`` is two
    ``time_value_t`` (seconds and microseconds, both 32-bit) followed by six
    32-bit integers, forty bytes; ``thread_identifier_info`` is three
    64-bit fields, of which the first is the thread id. The counts passed
    are those sizes in 32-bit words, as the kernel expects.
    """

    THREAD_BASIC_INFO = 3
    THREAD_IDENTIFIER_INFO = 4
    KERN_SUCCESS = 0

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        name = ctypes.util.find_library("System") or ctypes.util.find_library("c")
        if not name:
            raise OSError("no system library")
        # PyDLL: none of these calls block, and a release of the GIL here
        # costs the sampler a switch interval per call beside a busy thread.
        self.lib = lib = ctypes.PyDLL(name, use_errno=True)
        self.ctypes = ctypes
        mach_port_t = ctypes.c_uint32
        lib.mach_task_self.restype = mach_port_t
        lib.mach_task_self.argtypes = ()
        lib.task_threads.restype = ctypes.c_int
        lib.task_threads.argtypes = (
            mach_port_t,
            ctypes.POINTER(ctypes.POINTER(mach_port_t)),
            ctypes.POINTER(ctypes.c_uint32),
        )
        lib.thread_info.restype = ctypes.c_int
        lib.thread_info.argtypes = (
            mach_port_t,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        )
        lib.mach_port_deallocate.restype = ctypes.c_int
        lib.mach_port_deallocate.argtypes = (mach_port_t, mach_port_t)
        lib.vm_deallocate.restype = ctypes.c_int
        lib.vm_deallocate.argtypes = (mach_port_t, ctypes.c_void_p, ctypes.c_size_t)
        self.mach_port_t = mach_port_t

        class BasicInfo(ctypes.Structure):
            _fields_ = [
                ("user_seconds", ctypes.c_int32),
                ("user_microseconds", ctypes.c_int32),
                ("system_seconds", ctypes.c_int32),
                ("system_microseconds", ctypes.c_int32),
                ("cpu_usage", ctypes.c_int32),
                ("policy", ctypes.c_int32),
                ("run_state", ctypes.c_int32),
                ("flags", ctypes.c_int32),
                ("suspend_count", ctypes.c_int32),
                ("sleep_time", ctypes.c_int32),
            ]

        class IdentifierInfo(ctypes.Structure):
            _fields_ = [
                ("thread_id", ctypes.c_uint64),
                ("thread_handle", ctypes.c_uint64),
                ("dispatch_qaddr", ctypes.c_uint64),
            ]

        self.BasicInfo = BasicInfo
        self.IdentifierInfo = IdentifierInfo

    def read(self) -> dict[int, int]:
        ctypes = self.ctypes
        task = self.lib.mach_task_self()
        threads = ctypes.POINTER(self.mach_port_t)()
        count = ctypes.c_uint32(0)
        if self.lib.task_threads(task, ctypes.byref(threads), ctypes.byref(count)) != self.KERN_SUCCESS:
            return {}
        clocks: dict[int, int] = {}
        try:
            for index in range(count.value):
                port = threads[index]
                try:
                    identity = self.IdentifierInfo()
                    size = ctypes.c_uint32(ctypes.sizeof(identity) // 4)
                    if (
                        self.lib.thread_info(
                            port, self.THREAD_IDENTIFIER_INFO, ctypes.byref(identity), ctypes.byref(size)
                        )
                        != self.KERN_SUCCESS
                    ):
                        continue
                    basic = self.BasicInfo()
                    size = ctypes.c_uint32(ctypes.sizeof(basic) // 4)
                    if (
                        self.lib.thread_info(port, self.THREAD_BASIC_INFO, ctypes.byref(basic), ctypes.byref(size))
                        != self.KERN_SUCCESS
                    ):
                        continue
                    nanoseconds = (
                        (basic.user_seconds + basic.system_seconds) * 1_000_000_000
                        + (basic.user_microseconds + basic.system_microseconds) * 1_000
                    )
                    clocks[int(identity.thread_id)] = nanoseconds
                finally:
                    self.lib.mach_port_deallocate(task, port)
        finally:
            self.lib.vm_deallocate(
                task, ctypes.cast(threads, ctypes.c_void_p), count.value * ctypes.sizeof(self.mach_port_t)
            )
        return clocks


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
        #: (thread name, stack, stack a tick earlier) -> megabytes resident
        #: memory climbed while that stack was running. The memory profile's
        #: answer to "who". The earlier stack is there for the analysis to
        #: fall back on: a climb read just after a test body returned lands
        #: on pytest's own frames, and the tick before it is the body.
        self.growth: dict[tuple[str, StackKey, StackKey], int] = defaultdict(int)
        self.gc_seconds = 0.0
        self.gc_collections = [0, 0, 0]
        self.phase_cpu: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.phase_started: Optional[tuple[str, float, float]] = None
        #: The timeline: one entry per closed window, as
        #: [offset_ms, cpu_ns, machine_permille, phase, (thread, stack) or None].
        self.windows: list[list[Any]] = []
        #: Allocation tracing, when it is on: the snapshots this window took.
        self.snapshot_before: Any = None
        self.snapshot_peak: Any = None
        self.snapshot_at = 0.0
        self.snapshot_rss = 0
        self.traced_before = 0
        self.traced_peak_reset = False
        #: For the background window at the end of the session: what held
        #: the memory the worker accumulated. See Sampler._session_holders.
        self.holders_session: list[tuple[float, Any]] = []


class Sampler:
    def __init__(
        self,
        record: Callable[[dict[str, Any]], None],
        resident_megabytes: Callable[[], Optional[int]],
        *,
        interval: float = DEFAULT_INTERVAL,
        worker: str = "",
        allocations: bool = False,
        retained_mb: int = 100,
    ) -> None:
        self.record = record
        self.resident_megabytes = resident_megabytes
        self.interval = max(0.001, float(interval))
        self.worker = worker
        #: Whether tracemalloc is on and snapshots are taken around a test
        #: that climbs or keeps more than retained_mb. See _snapshot_if_climbing.
        self.allocations = allocations and tracemalloc.is_tracing()
        self.retained_mb = max(1, int(retained_mb))
        #: With tracing on, a snapshot from before the first test, so what
        #: the worker accumulated over the whole session can be diffed at
        #: the end: the holders of a leak no single test shows.
        self._session_snapshot: Any = None
        self._machine = _MachineClock()
        self._window_cpu_ns = 0
        self._window_stacks: dict[tuple[str, StackKey], int] = {}
        self._window_ticks = 0
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
        #: The stack each thread was last seen busy in, stamped with the tick
        #: it was seen on - see LATE_TICK_FACTOR. The stamp is what keeps a
        #: thread that idled for a minute from having its next work charged
        #: to whatever it was doing a minute ago.
        self._previous: dict[int, tuple[int, str, StackKey]] = {}
        #: When the background window was set aside for a test, so the time
        #: the test took is not also counted as the background's.
        self._paused: Optional[tuple[float, float]] = None
        self._ticks = 0
        self._last_process_cpu = time.process_time_ns()
        self._last_rss: Optional[int] = None
        #: The live heap at the last reading, for the climbs resident memory
        #: cannot show: a test that fills pages an earlier test freed grows
        #: the heap and not the process.
        self._last_heap: Optional[int] = None
        self._window = _Window(None, self._rss())
        self._background = self._window
        self._gc_started = 0.0
        self._stopped = False
        self.samples_taken = 0
        self.session_started = time.monotonic()
        self.session_cpu_started = time.process_time()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._refresh_threads()
        self._all_tids = self.clock.discover()
        self._last_clock = self.clock.read(self._tids_to_read())
        self._last_tick = time.monotonic()
        self._last_rss = self._rss()
        self._last_heap = _heap_in_use()
        gc.callbacks.append(self._on_gc)
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._thread.ident is not None:  # started; a half-built worker's may not be
            self._thread.join(timeout=2.0)
        try:
            gc.callbacks.remove(self._on_gc)
        except ValueError:
            pass
        with self._lock:
            if self._window.nodeid is not None:
                # A test the run died inside: written as far as it got.
                self._flush(self._window, TEST_RECORD)
            self._background.holders_session = self._session_holders()
            self._flush(self._background, BACKGROUND_RECORD)

    def close(self) -> None:
        """What the recorder's resource list calls when a worker's setup
        fails part-way: the sampling thread and the collector callback must
        not outlive the plugin that started them."""
        self.stop()

    def _session_holders(self) -> list[tuple[float, Any]]:
        """What tracemalloc sees holding memory now that it did not before the
        first test: (megabytes, traceback) for the three biggest, from a
        megabyte up. Empty when tracing was off or nothing accumulated."""
        if not self.allocations or self._session_snapshot is None:
            return []
        try:
            now = _snapshot()
            return [
                (round(stat.size_diff / 1048576, 1), stat.traceback)
                for stat in now.compare_to(self._session_snapshot, "traceback")[:3]
                if stat.size_diff >= 1048576
            ]
        except Exception:  # noqa: BLE001 - a lost diff beats a lost record
            return []

    def describe(self) -> dict[str, Any]:
        return {"interval": self.interval, "thread_clock": self.clock.source}

    # -- what the recorder tells it ------------------------------------------

    def begin_phase(self, nodeid: str, phase: str) -> None:
        """Called on the test's own thread, which is how that thread is known."""
        with self._lock:
            if self._window.nodeid != nodeid:
                self._close_window(self._window)
                self._window = _Window(nodeid, self._rss())
                self._paused = (time.monotonic(), time.process_time())
                if self.allocations:
                    self._window.snapshot_before = _snapshot()
                    self._window.traced_before = tracemalloc.get_traced_memory()[0]
                    tracemalloc.reset_peak()
                    if self._session_snapshot is None:
                        self._session_snapshot = self._window.snapshot_before
            window = self._window
            window.test_thread = threading.get_ident()
            window.rss_at[f"{phase}_start"] = self._rss()
            self._settle_climb(window, window.rss_at[f"{phase}_start"])
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
            self._settle_climb(window, window.rss_at[f"{phase}_end"])
            self.phase = None

    def _settle_climb(self, window: _Window, rss: Optional[int]) -> None:
        """A phase boundary is a reading too, and a climb it finds that the
        sampler never saw is still charged - to no stack, because the frames
        that made it are gone by now. The record then says how much of the
        climb was too quick to be placed, rather than losing it or, worse,
        resetting the baseline over it so it was never counted at all.

        Called with the lock held, on the test's thread.
        """
        climb = 0
        if rss is not None and self._last_rss is not None:
            climb = rss - self._last_rss
        heap = _heap_in_use()
        if heap is not None and self._last_heap is not None:
            climb = max(climb, heap - self._last_heap)
        if climb > 0:
            window.growth[("", (), ())] += climb
        if rss is not None:
            self._last_rss = rss
        if heap is not None:
            self._last_heap = heap

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
        process_cpu = time.process_time_ns()
        process_delta = max(0, process_cpu - self._last_process_cpu)
        self._last_process_cpu = process_cpu
        with self._lock:
            window = self._window
            phase = self.phase
            seen_tids: set[int] = set()
            if not self.clock.available:
                # No per-thread counter here. The process's own CPU clock is
                # still exact, and the test's thread is where the CPU went
                # in every case that has a test to blame - so it is charged
                # all of it, and the other threads nothing. Wall weighting was
                # the alternative and was worse: every idle thread earned the
                # whole interval, and Condition.wait topped every profile.
                culprit = window.test_thread if window.test_thread in frames else None
                if culprit is None:
                    culprit = next((ident for ident in frames if ident != self._own_ident), None)
                frames = {culprit: frames[culprit]} if culprit is not None else {}
            # The sampler's own thread is nobody's cost: not a stack, and
            # not a native thread either.
            seen_tids.add(self._threads.get(self._own_ident or 0, (0, ""))[0])
            #: What each busy thread was in before this tick overwrote it,
            #: for the memory charge below.
            prior: dict[int, Optional[tuple[int, str, StackKey]]] = {}
            for ident, frame in frames.items():
                if ident == self._own_ident:
                    continue
                tid, name = self._threads.get(ident, (0, f"thread {ident}"))
                seen_tids.add(tid)
                if self.clock.available:
                    cpu_ns = self._cpu_delta(tid, clocks)
                    if cpu_ns is None:
                        cpu_ns = 0
                    if cpu_ns <= 0:
                        continue  # idle: the whole point of the weighting
                else:
                    cpu_ns = process_delta
                    if cpu_ns <= 0:
                        continue
                stack = _stack_of(frame)
                if not stack:
                    continue
                previous = prior[ident] = self._previous.get(ident)
                self._previous[ident] = (self._ticks, name, stack)
                if late and previous is not None and previous[0] == self._ticks - 1:
                    _, name, stack = previous
                background = window.test_thread is not None and ident != window.test_thread
                entry = window.stacks[(phase, name, background, stack)]
                entry[0] += cpu_ns
                entry[1] += wall_ns
                entry[2] += 1
                self.samples_taken += 1
                key = (name, stack)
                self._window_stacks[key] = self._window_stacks.get(key, 0) + cpu_ns

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
            self._window_cpu_ns += process_delta
            self._window_ticks += 1
            if self._window_ticks >= WINDOW_TICKS:
                self._close_window(window)
            if self._ticks % RSS_EVERY == 0:
                rss = self._rss()
                if rss is not None and (window.rss_peak is None or rss > window.rss_peak):
                    window.rss_peak = rss
                climb = 0
                if rss is not None and self._last_rss is not None:
                    climb = rss - self._last_rss
                heap = _heap_in_use()
                if heap is not None and self._last_heap is not None:
                    climb = max(climb, heap - self._last_heap)
                if heap is not None:
                    self._last_heap = heap
                if rss is not None and self.allocations:
                    self._snapshot_if_climbing(window, rss)
                if climb > 0:
                    # Charged to the test's own thread, or to whichever thread
                    # is running when there is no test: the one allocating is
                    # not knowable from outside, and the test thread is the
                    # answer in every case that has a test to blame.
                    culprit = window.test_thread
                    if culprit is None or culprit not in frames:
                        culprit = next(iter(frames), None)
                    if culprit is not None:
                        stack = _stack_of(frames[culprit])
                        if stack:
                            _, name = self._threads.get(culprit, (0, f"thread {culprit}"))
                            # The climb happened since the last reading, and
                            # the stack now is the end of that span. A test
                            # body that allocated and returned within it has
                            # pytest's frames on the stack now and its own a
                            # tick earlier, so the earlier stack goes with the
                            # charge, for the analysis to prefer when the
                            # current one is nobody's code.
                            earlier = prior[culprit] if culprit in prior else self._previous.get(culprit)
                            fallback: StackKey = ()
                            if earlier is not None and earlier[0] >= self._ticks - RSS_EVERY and earlier[2] != stack:
                                fallback = earlier[2]
                            window.growth[(name, stack, fallback)] += climb
                if rss is not None:
                    self._last_rss = rss

        self._last_clock = clocks
        del frames

    def _close_window(self, window: _Window) -> None:
        """One timeline entry from what the last few ticks accumulated.

        Called with the lock held. The stack kept is the one that burnt the
        most CPU in the window, on whichever thread; a burst's blame comes
        from the stacks of its windows, and the thread name says whether it
        was the test's own.
        """
        if self._window_ticks == 0:
            return
        top = max(self._window_stacks.items(), key=lambda item: item[1])[0] if self._window_stacks else None
        window.windows.append(
            [
                int((time.monotonic() - window.started) * 1000),
                self._window_cpu_ns,
                self._machine.busy_permille(),
                self.phase,
                top,
            ]
        )
        self._window_cpu_ns = 0
        self._window_ticks = 0
        self._window_stacks = {}

    def _snapshot_if_climbing(self, window: _Window, rss: int) -> None:
        """A tracemalloc snapshot as a test's memory climbs, throttled.

        A snapshot copies every live trace - seconds, on a process holding
        millions of objects - so one is taken when the climb since the test
        began is worth reporting, has grown by another threshold since the
        last snapshot, and at least a second has passed. The last one taken
        is what held the memory nearest the peak.
        """
        if window.nodeid is None or window.rss_before is None:
            return
        climbed = rss - window.rss_before
        now = time.monotonic()
        if (
            climbed >= self.retained_mb
            and rss - window.snapshot_rss >= self.retained_mb
            and now - window.snapshot_at >= 1.0
        ):
            window.snapshot_peak = _snapshot()
            window.snapshot_rss = rss
            window.snapshot_at = now

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
        self._close_window(window)
        rss_after = self._rss()
        heap_after = _heap_in_use()
        blocks_after = sys.getallocatedblocks()
        frames: dict[tuple[str, int, str], int] = {}

        def index_of(file: str, line: int, function: str) -> int:
            key = (file, line, function)
            index = frames.get(key)
            if index is None:
                index = frames[key] = len(frames)
            return index

        def indexes_of(stack: StackKey) -> list[int]:
            return [
                index_of(code.co_filename, line or _line_of(code, offset), getattr(code, "co_qualname", code.co_name))
                for code, line, offset in stack
            ]

        timeline = []
        for offset_ms, cpu_ns, machine, phase, top in window.windows:
            indexes = None
            thread = None
            if top is not None:
                thread, stack = top
                indexes = indexes_of(stack)
            timeline.append([offset_ms, cpu_ns, machine, phase, thread, indexes])

        allocations = self._allocation_report(window, kind, index_of)
        if window.holders_session:
            allocations["holders_session"] = [
                {
                    "mb": megabytes,
                    "frames": [index_of(frame.filename, frame.lineno, "") for frame in traceback],
                }
                for megabytes, traceback in window.holders_session
            ]
        stacks = []
        for (phase, thread, background, stack), (cpu_ns, wall_ns, samples) in window.stacks.items():
            stacks.append(
                {
                    "phase": phase,
                    "thread": thread,
                    "background": background,
                    "frames": indexes_of(stack),
                    "cpu_ns": cpu_ns,
                    "wall_ns": wall_ns,
                    "samples": samples,
                }
            )
        growth = []
        for (thread, stack, fallback), megabytes in window.growth.items():
            entry: dict[str, Any] = {"thread": thread, "frames": indexes_of(stack), "mb": megabytes}
            if fallback:
                entry["fallback"] = indexes_of(fallback)
            growth.append(entry)
        record = {
            "record": kind,
            "worker": self.worker,
            "nodeid": window.nodeid,
            "cpus": os.cpu_count(),
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
            # Weighted by CPU either way; what differs is whether the CPU
            # could be told apart per thread. See ThreadClock.
            "cpu_weighted": True,
            "per_thread": self.clock.available,
            # With allocation tracing on, every CPU figure here carries the
            # tracer's cost as well; the analysis raises no CPU findings
            # from such a record.
            "allocations": self.allocations,
            "thread_clock": self.clock.source if self.clock.available else "process",
            "frames": [f"{file}|{line}|{function}" for (file, line, function) in frames],
            "stacks": stacks,
            "growth": growth,
            "timeline": timeline,
            **allocations,
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
        window.growth.clear()
        window.gc_seconds = 0.0
        window.gc_collections = [0, 0, 0]
        window.started = now
        window.cpu_started = time.process_time()
        window.rss_before = rss_after
        window.rss_peak = rss_after
        window.rss_at = {}
        window.heap_before = heap_after
        window.blocks_before = blocks_after
        window.windows = []
        window.snapshot_before = window.snapshot_peak = None
        window.phase_cpu.clear()


    def _allocation_report(self, window: _Window, kind: str, index_of: Any) -> dict[str, Any]:
        """What tracemalloc says about this test, when it was tracing.

        Three things: the tracebacks holding the most at the peak, the ones
        holding the most of what the test kept, and the peak's live
        allocations as stacks weighted by bytes - a memory flame graph.
        The tracer's own memory is reported so the analysis can discount it:
        its tables grow with every allocation it records and churn the
        allocator enough to leave resident memory up after the test freed
        everything.
        """
        if not self.allocations or kind != TEST_RECORD or window.snapshot_before is None:
            return {}
        report: dict[str, Any] = {}
        current, peak = tracemalloc.get_traced_memory()
        report["traced"] = {
            "before_mb": round(window.traced_before / 1048576),
            "after_mb": round(current / 1048576),
            "peak_mb": round(peak / 1048576),
            "tracer_mb": round(tracemalloc.get_tracemalloc_memory() / 1048576),
        }
        before = window.snapshot_before
        after = None
        kept = current - window.traced_before
        if kept >= self.retained_mb * 1048576:
            after = _snapshot()

        def holders(snapshot: Any) -> list[dict[str, Any]]:
            found = []
            for stat in snapshot.compare_to(before, "traceback")[:3]:
                if stat.size_diff < 1048576:
                    break
                found.append(
                    {
                        "mb": round(stat.size_diff / 1048576, 1),
                        "frames": [index_of(frame.filename, frame.lineno, "") for frame in stat.traceback],
                    }
                )
            return found

        top = window.snapshot_peak or after
        if window.snapshot_peak is not None:
            report["holders_peak"] = holders(window.snapshot_peak)
        if after is not None:
            report["holders_kept"] = holders(after)
        if top is not None:
            stacks = []
            for statistic in top.statistics("traceback")[:400]:
                stacks.append(
                    {
                        "frames": [index_of(frame.filename, frame.lineno, "") for frame in statistic.traceback],
                        "bytes": statistic.size,
                    }
                )
            report["memory_stacks"] = stacks
        return report


def _snapshot() -> Any:
    """A tracemalloc snapshot without the tracer's or the sampler's own
    allocations, which are what a naive diff blames first."""
    return tracemalloc.take_snapshot().filter_traces(
        (tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, __file__))
    )


class _MachineClock:
    """How busy the whole machine is, per window, as a per-mille figure.

    A burst that ran at a third of a core while the machine was pinned is
    twenty workers sharing four cores, not a slow fixture; without this the
    two are the same finding. psutil's cpu_times is one call, read every
    window, and the busy share is everything but idle and iowait.
    """

    def __init__(self) -> None:
        self._last: Any = None
        try:
            import psutil

            self._psutil: Any = psutil
            self._last = psutil.cpu_times()
        except Exception:
            self._psutil = None

    def busy_permille(self) -> Optional[int]:
        if self._psutil is None:
            return None
        try:
            now = self._psutil.cpu_times()
        except Exception:
            return None
        last, self._last = self._last, now
        if last is None:
            return None
        total = sum(now) - sum(last)
        if total <= 0:
            return None
        idle = (now.idle - last.idle) + (getattr(now, "iowait", 0.0) - getattr(last, "iowait", 0.0))
        return max(0, min(1000, int(1000 * (total - idle) / total)))


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
#: Weakly keyed: a suite that builds code objects as it runs would otherwise
#: pin every one of them for the life of the process, and the profiler is
#: the thing meant to report memory that drifts up.
_line_tables: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


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
