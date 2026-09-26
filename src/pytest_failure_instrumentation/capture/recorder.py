"""The worker-side plugin object: wires the recorders to pytest's hooks.

Phase tracking lives here rather than in a module of its own because the hooks
*are* the phase tracking - splitting them would leave two files that can only
be read together.

The rule that sizes everything: a passing test must cost as close to nothing as
possible, because that is the overwhelming majority of what runs. So the
per-test path is two fixed-size writes per phase and nothing else - no append,
no /proc read, no allocation tracking.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from ..config import Settings
from ..lanes import LANE_CPU_SUFFIX, LaneCpu
from ..probes import tracing
from ..probes.platform_flags import platform_description
from ..probes.process import thread_cpu_seconds
from . import crash_stack
from . import memory as memory_capture
from . import output as output_capture
from .events import EventLog
from .heartbeat import Heartbeat
from .state import WorkerState


class _Slot:
    """The per-test half of a recorder: a state slot, and the test it counts.

    A worker has one, for the process. A process running pytest-threadlanes
    has one per lane as well: a lane is a worker that shares its process with
    its siblings, and everything per test here was written for one test in
    flight. The per-process half (heartbeat, event log, dumps) stays shared.

    A lane's is only ever touched from the lane's own thread while it runs,
    so it takes no lock; its CPU is written elsewhere, by the heartbeat - see
    :class:`..lanes.LaneCpu`.
    """

    def __init__(
        self,
        name: str,
        state: WorkerState,
        native_id: Optional[int] = None,
        ident: Optional[int] = None,
    ) -> None:
        self.name = name
        self.state = state
        #: The test whose protocol is open and has already been counted as
        #: started, or None between tests. What makes a rerun not count twice
        #: - see WorkerRecorder.pytest_runtest_protocol.
        self.counted: Optional[str] = None
        #: Which attempt of `counted` is running, kept here rather than read
        #: back from the state slot: the slot clears the attempt at the end of
        #: every teardown, exactly as it clears the node id, so a counter
        #: living there would start again from the cleared value and every
        #: attempt after the second would report itself as the second. Reset
        #: at the protocol boundary, where the test changes.
        self.attempt = 0
        #: A lane's thread, by native id and by the ident faulthandler prints;
        #: None for the process's own slot.
        self.native_id = native_id
        self.ident = ident


class _SessionTeeDrain:
    """A heartbeat ticker that passes a session-long tee's bytes on to the
    terminal, so fd 2 under lanes reaches it within a tick rather than at the
    end of whichever phase some lane finishes next - a phase of these suites
    can be two hours long."""

    def __init__(self, recorder: WorkerRecorder) -> None:
        self.recorder = recorder

    def tick(self) -> None:
        self.recorder._tee_drain()

    def stop(self) -> None:
        pass


class WorkerRecorder:
    def __init__(
        self,
        directory: Path,
        worker_id: str,
        settings: Settings,
        *,
        faulthandler_timeout: float = 0.0,
        claims_fatal_dumps: bool = True,
        lanes: bool = False,
    ) -> None:
        self.worker_id = worker_id
        self.directory = directory
        #: Whether this process runs its tests on pytest-threadlanes' lanes,
        #: several at once. Decided at registration from the option alone -
        #: see :mod:`..lanes` - so that everything that has to be settled
        #: before the first test (the tee, the profiler, the heartbeat's
        #: per-lane figures) is. False everywhere else, and then nothing below
        #: takes a branch it did not take before lanes existed.
        self.lanes = lanes
        #: This process's lanes, by name, each created on its first test.
        #: Inserted into under the lock, from the lanes' own threads; read
        #: without it by each lane for its own entry.
        self._lanes: dict[str, _Slot] = {}
        #: Lanes whose slot could not be opened, so that it is tried - and the
        #: failure recorded - once, rather than on every phase of every test.
        self._lanes_failed: set[str] = set()
        self._lanes_lock = threading.Lock()
        #: The lanes' CPU file, made on the first beat that has a lane to
        #: measure. Only the heartbeat's thread touches it.
        self._lane_cpu: Any = None
        #: Serializes the session-long tee's copies, which lanes' phase ends
        #: and the heartbeat's ticks make concurrently. Untouched without lanes.
        self._tee_lock = threading.Lock()
        #: Whether to point fatal-signal dumps at this process's own crash
        #: file, which means taking them off the stderr pytest aimed them at.
        #: True for a worker, where that stderr is shared with fifteen others
        #: and a dump written to it belongs to nobody. A run with no workers
        #: decides for itself, because there the stderr in question is a
        #: terminal somebody is watching - see ``failure_crash_stack``.
        self.claims_fatal_dumps = claims_fatal_dumps
        #: pytest's own ``faulthandler_timeout``. Not a setting of ours - it
        #: decides only whether the frozen-interpreter fallback may arm the
        #: one timer the two plugins share. See config.pytest_faulthandler_timeout.
        self.faulthandler_timeout = faulthandler_timeout
        self.heartbeat: Heartbeat | None = None
        self.monitor: memory_capture.MemoryMonitor | None = None
        self.profiler: Any = None
        self._allocation_tracer: memory_capture.TracemallocSession | None = None
        # Filled as each resource is opened, so close() works on a recorder
        # that never finished being built.
        self._open_resources: list[Any] = []
        try:
            self._open(directory, worker_id, settings)
        except Exception:
            # The caller turns this into "instrumentation is off for this
            # worker" and carries on; leaving descriptors open behind it would
            # make a failed setup cost more than a working one.
            self.close()
            raise

    def _open(self, directory: Path, worker_id: str, settings: Settings) -> None:
        directory.mkdir(parents=True, exist_ok=True)

        self.state = self._track(
            WorkerState(directory / f"{worker_id}.state", os.getpid(), settings.run_id)
        )
        #: This process's own slot, which every test of a process without
        #: lanes is counted in.
        self._own = _Slot(worker_id, self.state)
        self.events = self._track(
            EventLog(directory / f"{worker_id}.events", settings.run_id)
        )
        # Never closed in the happy path: faulthandler keeps these for the
        # process lifetime, so a crash during interpreter shutdown still gets
        # written.
        #
        # Two files, not one. A watchdog dump is written by tests that go on to
        # pass; a fatal dump means the process is ending. Sharing a file made
        # them indistinguishable afterwards, and a slow test that passed could
        # be read as the crash that killed the worker.
        self._crash_stream = self._track(
            (directory / f"{worker_id}.crash").open("w", buffering=1, encoding="utf-8")
        )
        # A test that outlives this has its stack written for it - no signal,
        # so it works on Windows and interrupts no syscall. The heartbeat
        # thread does the writing (see SlowTestWatchdog), so a worker with the
        # watchdog switched off has nothing to drive it and gets no dumps.
        self.slow_test = crash_stack.SlowTestWatchdog(
            directory / f"{worker_id}.slow",
            settings.slow_test_seconds if settings.watchdog else 0.0,
        )
        # And the one stack the watchdog above can never take: the worker's
        # own threads cannot run while native code holds the GIL. Never
        # closed, for the same reason as the crash stream - the C timer keeps
        # the descriptor and may write to it long after Python has stopped.
        #
        # It stands down when pytest is using that timer itself. There is one
        # per process and arming it cancels what was armed before, so the
        # fallback - which re-arms every second - would quietly take over a
        # user's faulthandler_timeout and with it the exit that was meant to
        # end a hung run. Losing a stack is a worse report; losing somebody's
        # configured timeout is a worse run.
        #
        # And it stands down under lanes, where it cannot keep its promise to
        # fire only once nothing is executing - see _record_lane_adjustments.
        self._frozen_stream = None
        self.frozen = crash_stack.FrozenInterpreterFallback(None, 0.0)
        if settings.watchdog and self.faulthandler_timeout <= 0 and not self.lanes:
            self._frozen_stream = self._track(
                (directory / f"{worker_id}.frozen").open(
                    "w", buffering=1, encoding="utf-8"
                )
            )
            self.frozen = crash_stack.FrozenInterpreterFallback(
                self._frozen_stream, settings.heartbeat_interval
            )

        # A tee of the worker's stderr into a bounded ring, so the one line a
        # native death leaves - and no stack carries - survives the kill. It
        # reads fd 2 directly because pytest's own capture reaches a report
        # only for a completed phase, missing the crashing phase and imports.
        # Started here (pipe and thread), but fd 2 is taken over later, once
        # pytest's capture is up - see _tee_fd. Its own guards inside: a tee
        # that cannot be built is a recorded reason, never a failed worker.
        self.stderr_tee: output_capture.StderrTee | None = None
        if settings.capture_output:
            tee = output_capture.StderrTee(directory / f"{worker_id}.output", append=self.lanes)
            tee.start()
            if tee.active:
                self._track(tee)
                self.stderr_tee = tee
            self.events.record("output_capture", status=tee.reason)
        #: Set when the tee did not take fd 2 for the current phase - see
        #: _tee_take. Read by _tee_hand_back, so it never restores a
        #: descriptor it did not take.
        self._tee_stood_down = False
        if self.lanes:
            self._record_lane_adjustments(settings)

        self._apply_memory_limit(settings)
        if settings.profile_allocations:
            # Allocation profiling resets the process-wide peak for every
            # test, so it cannot safely share a tracer with another consumer.
            # Start once for both profiler and watchdog, at the deeper of the
            # two requested tracebacks, before either takes a snapshot.
            self._allocation_tracer = self._track(
                memory_capture.TracemallocSession(
                    max(settings.profile_allocation_depth, settings.tracemalloc_depth)
                )
            )
        self._start_monitors(settings)
        self._start_profiler(directory, worker_id, settings)

        # Before anything can be asked to read this process. Without it a
        # live-stack read of this worker is refused wherever Yama enforces
        # ptrace_scope=1, because the reader is a *sibling* rather than an
        # ancestor - see probes.tracing.
        #
        # The policy in force, which the controller resolved and this process
        # obeys rather than judges. It is "off" for every run that reads no
        # worker stacks - which is nearly all of them, and which used to widen
        # ptrace here anyway, on every Linux machine that installed this. The
        # worker has not been told enough to reach that answer itself, on
        # purpose; see Settings.tracer_in_force.
        traceable = tracing.permit_tracing(settings.tracer_in_force)

        self.events.record(
            "worker_start",
            pid=os.getpid(),
            python=sys.version.split()[0],
            platform=platform_description(),
            executable=sys.executable,
            # Recorded because it is the difference between "no stack" and
            # "no stack, and here is the reason", and it is only knowable here.
            traceable_by_parent=traceable,
            # What was declared, not what was configured: on a run with
            # nothing reading stacks those differ, and the reader of this line
            # is asking about the process it names.
            tracer_policy=settings.tracer_in_force,
        )
        if settings.watchdog and self.faulthandler_timeout > 0:
            self.events.record(
                "frozen_fallback_stood_down",
                reason="pytest's faulthandler_timeout is set and owns the one "
                "dump_traceback_later timer this process has; re-arming it "
                "would cancel that timeout",
                faulthandler_timeout=self.faulthandler_timeout,
            )
        self.state.update()

    def _track(self, resource: Any) -> Any:
        self._open_resources.append(resource)
        return resource

    def _record_lane_adjustments(self, settings: Settings) -> None:
        """Say which process-wide machinery runs differently under lanes.

        Each of these was written for a process with one test in flight, and
        a reader looking at this process's evidence - a stderr tail with no
        phase boundaries, a watchdog dump holding every lane, no nodeid on a
        beat - is owed the reason, in the file it is reading. The profiler
        says so where it is skipped.
        """
        if self.stderr_tee is not None:
            self.events.record(
                "lanes_adjusted",
                mechanism="stderr_tee",
                action="taken once for the session",
                reason="lanes run their phases at once, so handing fd 2 back at "
                "one lane's phase end would take it from the others; pytest does "
                "not swap fd 2 per test under lanes either. What reaches it is "
                "passed on to stderr as it arrives, and the disk under what was "
                "passed on is given back as it goes on Linux - holes punched in "
                "the file, or the file rotated to .prev where holes cannot be "
                "punched. Elsewhere the file keeps the session's stderr: fd 2 "
                "cannot be swapped safely under writing threads there",
            )
        if self.slow_test.enabled:
            self.events.record(
                "lanes_adjusted",
                mechanism="slow_test_watchdog",
                action="one clock per lane",
                reason="several tests are in flight at once; the dump is of "
                "every thread, so it holds whichever lane is overdue",
            )
        if settings.watchdog and self.faulthandler_timeout <= 0:
            # Its dump is taken without the GIL, and is safe only once no
            # Python thread is executing. Three missed beats meant that in a
            # process running one test; under lanes they do not. Twenty lanes
            # running Python on four cores kept the heartbeat from the GIL for
            # longer than that, and a stopped process resumes every lane at
            # the instant the overdue timer fires: either way the dump walked
            # frames being torn down, and the worker died of SIGSEGV in 3 runs
            # of 4 and 5 of 5. A stack for a frozen process of lanes is read
            # from outside it instead, by py-spy, with the process's incident.
            self.events.record(
                "lanes_adjusted",
                mechanism="frozen_fallback",
                action="off",
                reason="its dump is taken without the GIL and is safe only once no "
                "Python thread is executing, which missed beats cannot show when "
                "many lanes contend for the GIL or a stopped process resumes. A "
                "frozen process's stack is read by py-spy from outside it instead",
            )
        if settings.watchdog:
            self.events.record(
                "lanes_adjusted",
                mechanism="heartbeat",
                action="no test on the process's beat; each lane's CPU in the "
                "process's .lanecpu file",
                reason="the process is running one test per lane, and its beat "
                "cannot name one of them. Memory is per process and is not "
                "attributed to any test",
            )

    # -- setup -----------------------------------------------------------

    def _apply_memory_limit(self, settings: Settings) -> None:
        """Turn a silent OOM kill into a MemoryError attributed to a test.

        A ceiling that cannot be applied is recorded and shrugged off. Refusing
        to start over it would mean this plugin ended a run that was going to
        work, which is a worse outcome than any report it could have produced.
        """
        if settings.memory_limit_mb <= 0:
            return
        try:
            import resource
        except ImportError:
            return  # Windows has no RLIMIT
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            limit = settings.memory_limit_mb * 1024 * 1024
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        except (OSError, ValueError) as failure:
            self.events.record("memory_limit_refused", detail=repr(failure))
            return
        self.events.record("memory_limit_applied", limit_mb=settings.memory_limit_mb)

    def _start_profiler(self, directory: Path, worker_id: str, settings: Settings) -> None:
        """The CPU and memory sampler, when this run asked for one.

        A thread of its own rather than a tick of the heartbeat: the beat is
        once a second at its fastest, and a profile needs fifty a second to
        see a two-second spike as anything but one sample.
        """
        if not settings.profile:
            return
        if self.lanes:
            # Every sample is attributed to the one test in flight, and a
            # process of lanes has as many in flight as it has lanes: each of
            # them would be charged for all the others' CPU and memory.
            self.events.record(
                "lanes_adjusted",
                mechanism="profiler",
                action="disabled",
                reason="it attributes samples to one test at a time, and this "
                "process runs one test per lane at once",
            )
            return
        from .. import probes
        from ..profile.sampler import ProfileLog, Sampler

        log = self._track(ProfileLog(directory / f"{worker_id}.profile.jsonl", settings.run_id))
        # Tracked after the log, so a setup that fails past this point stops
        # the sampling thread and unhooks its collector callback before the
        # log it writes to is closed - rather than leaving both running for
        # the whole run under a plugin that has said it is off.
        self.profiler = self._track(
            Sampler(
                log.write,
                lambda: probes.resident_megabytes()[0],
                resident_kilobytes=lambda: probes.resident_kilobytes()[0],
                interval=settings.profile_interval,
                worker=worker_id,
                allocations=settings.profile_allocations,
                retained_mb=settings.profile_retained_mb,
            )
        )
        self.profiler.start()
        self.events.record("profiler_started", **self.profiler.describe())

    def _profile(self, method: str, *args: Any) -> None:
        """Hand the profiler a boundary. It is a diagnostic and this is a
        pytest hook: whatever it raises is recorded against the worker and
        is not the test's failure, and never the run's."""
        if self.profiler is None:
            return
        try:
            getattr(self.profiler, method)(*args)
        except Exception as failure:  # noqa: BLE001 - a profiler must never end a run
            self.events.record("profiler_failed", method=method, detail=repr(failure))

    def _start_monitors(self, settings: Settings) -> None:
        if not settings.watchdog:
            return
        memory_capture.enable_tracemalloc(settings.tracemalloc_depth)
        self.monitor = memory_capture.MemoryMonitor(
            self.events.record,
            threshold_mb=settings.high_water_mb or None,
            share_count=settings.worker_count,
            object_census=settings.object_census,
        )
        from .. import probes

        self.events.record(
            "watchdog_started",
            interval=settings.heartbeat_interval,
            capabilities=probes.capabilities(),
            **self.monitor.describe(),
        )
        tickers: list[Any] = [self.slow_test, self.frozen]
        if self.lanes and self.stderr_tee is not None:
            tickers.append(_SessionTeeDrain(self))
        self.heartbeat = Heartbeat(
            self.events.record,
            interval=settings.heartbeat_interval,
            observers=[self.monitor],
            # Every wake rather than every beat: one is watching a deadline
            # and the other is pushing one out, and both are wrong if they
            # only happen every fifth second.
            tickers=tickers,
            lane_cpu=self._record_lane_cpu if self._lane_cpu_readable() else None,
        )
        self.heartbeat.start()

    # -- hooks -----------------------------------------------------------

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        # After configure, so this runs against pytest's own faulthandler
        # plugin rather than before it: that one registers trylast and points
        # the fatal dump at stderr.
        #
        # A worker takes it over, because the stderr in question is shared
        # with every other worker and a dump written there belongs to nobody.
        # A run with no workers only takes it if it was asked to - see
        # crash_stack.arm_fatal_handler for both halves of that.
        if self.claims_fatal_dumps:
            on_demand = crash_stack.arm_fatal_handler(self._crash_stream)
        else:
            on_demand = crash_stack.arm_on_demand_handler(self._crash_stream)
        self.events.record(
            "faulthandler_armed",
            # Which of the two. A death recovered from this evidence afterwards
            # reads it to tell "the process wrote no dump" from "the dump went
            # to the terminal, and was never this file's to hold".
            fatal_stack="this file" if self.claims_fatal_dumps else "stderr",
            on_demand_stack=on_demand,
            slow_test_seconds=self.slow_test.timeout if self.slow_test.enabled else None,
        )
        if self.lanes:
            self._tee_session()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: Any) -> Any:
        """One test, however many times its phases run inside this.

        ``tests_started`` and ``tests_finished`` count tests, and the phases
        are not a count of tests. A rerun plugin - pytest-rerunfailures,
        flaky, and every one written the same way - implements this hook and
        runs ``runtestprotocol`` again inside it when the test fails, so setup
        and teardown run once per attempt while the test, its node id and its
        slot in the scheduler's queue are one. Counting it at setup counted
        every attempt, and a suite of 368 tests with six of them rerun once
        was reported as 374: the controller's total is floored at this
        counter, so an attempt counted here became a test nobody was given.

        A hookwrapper is around every implementation, the rerun plugin's
        included, so this opens and closes exactly once per test whatever
        happens in between - which is all it is here for. The counting stays
        in the phases, where the slot is written anyway: what this adds is
        the boundary, so that a setup for the id already counted in *this*
        protocol is a rerun, and the same id in the next protocol is the next
        test (``--keep-duplicates`` collects a file twice, and both copies
        run). The test is closed at the end of teardown, as it always was,
        and not here: pytest's ``logfinish`` fires between the two, and a
        worker that dies there died between tests, not in one.

        The *profile* record is the exception, and closes here. It has to be
        written after pytest lets go of the item's fixture values - otherwise
        every function-scoped fixture's value counts as memory the test kept,
        and its release as the next gap's - and ``runtestprotocol`` clears
        ``funcargs`` before it returns, so anything after that will do. It
        used to be ``logfinish``, which is the next thing to fire and is
        inside the protocol: a rerun plugin runs the whole protocol again,
        ``logfinish`` and all, so a test rerun twice wrote three records and
        the profile summary reported a run of two tests as four. Here is
        after the last attempt, whichever attempt that turns out to be.
        """
        slot = self._slot(item)
        if slot is not None:
            slot.counted = None
            slot.attempt = 0
        yield
        if slot is not None:
            slot.counted = None
            slot.attempt = 0
        self._profile("end_test", item.nodeid)

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Any:
        yield from self._phase("setup", item.nodeid, item)

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Any:
        yield from self._phase("call", item.nodeid, item)

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Any:
        yield from self._phase("teardown", item.nodeid, item)

    #: Fixtures that take fd 1/2 over for the test themselves. Taking fd 2 out
    #: from under one of them makes its readouterr() miss what the test wrote,
    #: which is a change to a passing test's behaviour - the one thing this may
    #: never do. A test that captures its own fd output is also watching its
    #: own crash, so the tee stands down for it and loses nothing worth having.
    #: capsys / capsysbinary are sys-level and untouched, so they are not here.
    _FD_CAPTURE_FIXTURES = frozenset({"capfd", "capfdbinary"})

    def _tee_take(self, item: pytest.Item | None = None) -> None:
        """Point fd 2 at the capture file, if the tee is on and no fd-capture
        fixture owns it for this test. pytest points fd 2 at its own file at
        the start of every phase; this takes it just after, and _tee_hand_back
        gives it back at the phase's end with the phase's bytes copied into
        pytest's file, so both keep the output."""
        if self.stderr_tee is None or self.lanes:
            # Under lanes fd 2 is taken once, for the session - _tee_session.
            return
        if item is not None and self._FD_CAPTURE_FIXTURES.intersection(
            getattr(item, "fixturenames", ())
        ):
            self._tee_stood_down = True
            return
        self._tee_stood_down = False
        self.stderr_tee.take()

    def _tee_hand_back(self) -> None:
        # Only hand back what was taken: a phase the tee stood down for never
        # touched fd 2, and calling hand_back then would restore a descriptor
        # it does not own.
        if self.lanes:
            self._tee_drain()
            return
        if self.stderr_tee is not None and not self._tee_stood_down:
            self.stderr_tee.hand_back()

    def _tee_session(self) -> None:
        """Take fd 2 once, for a process running lanes.

        Per phase is what the tee does everywhere else, and it cannot here:
        lanes run their phases at the same time, and a lane handing fd 2 back
        at the end of its phase would take it away from every sibling still
        inside one - the next C-level write of theirs lost from the ring, or
        fd 2 left pointing at whichever file was restored last. pytest does
        not take fd 2 per test under lanes either (pytest-threadlanes runs it
        with ``--capture=no`` and captures per lane itself), so there is
        nothing to coexist with. What arrives is passed on as it arrives -
        _tee_drain - and fd 2 is given back at session finish.
        """
        if self.stderr_tee is None:
            return
        with self._tee_lock:
            self.stderr_tee.take()

    def _tee_drain(self) -> None:
        if self.stderr_tee is None:
            return
        with self._tee_lock:
            self.stderr_tee.drain()

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_collection(self, session: Any) -> Any:
        # Collection is where imports run, and an import is where a native
        # thread pool is first built and first fails. Take fd 2 over the whole
        # of it, and again at each collector below - a module's import is where
        # the write actually happens, and pytest re-points fd 2 at its own
        # capture around each one.
        self._tee_take()
        try:
            yield
        finally:
            self._tee_hand_back()

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_make_collect_report(self, collector: Any) -> Any:
        # Wraps the collection of one node - a module's import among them - so
        # fd 2 is the capture file at the moment the import runs, after pytest
        # has taken it for its own collection capture.
        self._tee_take()
        try:
            yield
        finally:
            self._tee_hand_back()

    def _phase(self, phase: str, nodeid: str, item: pytest.Item | None = None) -> Any:
        """Which phase is open is what separates "died in teardown" from
        "died mid-call": pytest's own logfinish fires only after the whole
        protocol, so it cannot tell them apart."""
        self._tee_take(item)
        slot = self._slot(item)
        if slot is None:
            # A lane whose slot could not be opened: recorded once, and the
            # test runs unrecorded rather than written into another's slot.
            try:
                yield
            finally:
                self._tee_hand_back()
            return
        own = slot is self._own
        now = time.time()
        if phase == "setup":
            if slot.counted != nodeid:
                # The first setup of the protocol is the test starting.
                slot.counted = nodeid
                slot.state.tests_started += 1
                slot.attempt = 1
            else:
                # A second one is a rerun of the same test, which is not a
                # test starting - see pytest_runtest_protocol. The attempt is
                # counted whatever the counters are doing, because it is the
                # only field that says a rerun is happening at all; everything
                # else here is about tests, of which this is still the one.
                slot.attempt += 1
                if slot.state.tests_finished > 0:
                    # And the finish counted at the end of the last attempt
                    # was not a finish either. It is taken back rather than
                    # never counted, because at the end of a teardown nobody
                    # yet knows whether an attempt was the last; and taken
                    # back rather than left, because the row is read as
                    # ``started - finished`` running, and that read zero
                    # beside a call phase in the slot.
                    slot.state.tests_finished -= 1
            # The whole test's clock, not the phase's: pytest-timeout and
            # faulthandler_timeout both time the item from its setup, so a
            # death is matched against a timeout by how long the *test* ran.
            # Set on every attempt, rerun or not: an enforcer gives each
            # attempt its own deadline, measured from that attempt's setup.
            slot.state.attempt = slot.attempt
            slot.state.test_started = now
            from .timeouts import effective

            slot.state.timeout_settings = effective(item) if item is not None else []
        # The process's beat names the test it is running, which a process of
        # lanes cannot: it runs one per lane. Their CPU is on the beat per
        # lane instead - see Heartbeat.threads.
        if own and self.heartbeat is not None:
            self.heartbeat.nodeid = nodeid
            self.heartbeat.phase = phase
        slot.state.update(nodeid=nodeid, phase=phase, phase_started=now)
        # Started once for the whole test rather than per phase, and from
        # setup rather than from the call.
        #
        # A fixture that blocks on a container, a connection or a service is
        # one of the commonest real hangs there is, and a finalizer closing
        # any of them is the next - the state slot has always distinguished
        # "died in teardown" from "died mid-call" for exactly that reason.
        # Covering only the call left both of those with no stack at all.
        #
        # Once, not per phase, because the clock is what the timeout is
        # measured against: restarting it at each phase would mean a test that
        # spent most of the interval in setup and the rest in the call never
        # reached it.
        if phase == "setup":
            if own:
                self.slow_test.start_test()
            else:
                self.slow_test.start_lane(slot.name)
        self._profile("begin_phase", nodeid, phase)
        try:
            yield
        finally:
            # Give fd 2 back to pytest, with this phase's stderr copied into
            # pytest's own capture, whatever the phase did.
            self._tee_hand_back()
        self._profile("end_phase", phase)
        if own and self.heartbeat is not None:
            self.heartbeat.phase = None
        if phase != "teardown":
            slot.state.update(phase=None)
            return
        if own:
            self.slow_test.end_test()
        else:
            self.slow_test.end_lane(slot.name)
        slot.state.tests_finished += 1
        # Cleared with the test rather than with the phase, exactly as the
        # node id below is and for the same reason: between two tests there is
        # no attempt in flight, and a row saying there is names one that is
        # over. A rerun sets it again at its next setup, above.
        slot.state.attempt = None
        # The node id is cleared with the *test*, not with each phase. A worker
        # that dies or wedges in the gap between two tests has no test in
        # flight, and saying it had one names a test that already passed - to
        # which the incident is then attributed, with an owner and a severity.
        # What that test was is still worth knowing, so the slot keeps it in
        # `last_nodeid`, where nothing can mistake it for the present.
        #
        # The two clocks go with it, and for the same reason. They are read on
        # the controller as "how long the test had been running when this
        # worker died" and matched against the run's timeouts - so a clock
        # left running between tests makes an idle worker's `os._exit(1)`
        # reach any timeout you like, and the death is reported as TIMED_OUT
        # against a test that had already passed.
        if own and self.heartbeat is not None:
            self.heartbeat.nodeid = None
        slot.state.update(phase=None, nodeid=None, phase_started=None, test_started=None)

    # -- lanes -----------------------------------------------------------

    def _slot(self, item: Any) -> Optional[_Slot]:
        """Where this test's bookkeeping goes: this process's slot, or its lane's.

        The process's own everywhere but a lane. None only for a lane whose
        slot could not be opened.
        """
        if not self.lanes or item is None:
            return self._own
        lane = self._lane_of(item)
        if lane is None:
            return self._own
        slot = self._lanes.get(lane)
        if slot is None and lane not in self._lanes_failed:
            slot = self._open_lane(lane)
        return slot

    def _lane_of(self, item: Any) -> Optional[str]:
        """The lane running ``item``, or None when it is this process itself.

        pytest-threadlanes makes ``config.workerinput`` name the lane on the
        lane's own thread, exactly as xdist makes it name the worker in the
        worker's process: ``ln3`` in a run with no ``-n``, ``gw0.ln3`` in one
        with. Off a lane it is what it always was - absent in a run with no
        workers, and this worker's own id in an xdist worker - and either of
        those is this process, not a lane.
        """
        workerinput = getattr(getattr(item, "config", None), "workerinput", None)
        if not isinstance(workerinput, dict):
            return None
        lane = workerinput.get("workerid")
        if not lane or lane == self.worker_id:
            return None
        return str(lane)

    def _open_lane(self, lane: str) -> Optional[_Slot]:
        """A lane's slot, made on its first test, on its own thread.

        On its own thread because the slot records which thread that is - by
        name for a person, by native id for the per-thread CPU figures and a
        UI matching a stack to it, and by the id faulthandler prints for the
        stall and death readers picking its section out of a dump.

        The first one also marks this process's own record as the container of
        its lanes, and leaves its node id alone from then on: a reader then
        lists the lanes rather than a process that is running all of them.
        """
        current = threading.current_thread()
        native, ident = threading.get_native_id(), threading.get_ident()
        with self._lanes_lock:
            existing = self._lanes.get(lane)
            if existing is not None:
                return existing
            try:
                state = WorkerState(
                    self.directory / f"{lane}.state",
                    os.getpid(),
                    self.state.run_id,
                    lane={
                        "process": self.worker_id,
                        "thread_name": current.name,
                        "thread_id": native,
                        "thread_ident": ident,
                        "lane": True,
                    },
                )
            except OSError as failure:
                self._lanes_failed.add(lane)
                self.events.record("lane_state_failed", lane=lane, detail=repr(failure))
                return None
            self._track(state)
            slot = _Slot(lane, state, native, ident)
            first = not self._lanes
            self._lanes[lane] = slot
            state.update()
            if first:
                self.state.update(lanes=True)
        return slot

    def _lane_cpu_readable(self) -> bool:
        """Whether each lane's own CPU can be read here, said once if not.

        It is matched on the native thread id: Linux reads each thread's CPU
        clock by it, and psutil numbers a thread by it on Windows. On macOS
        psutil numbers them 1, 2, 3... in the order the kernel lists them,
        which is no id at all, so no ``.lanecpu`` file is written there: a
        lane's row reads no CPU rate of its own, its stall is judged on its
        process's CPU and says so, and the evidence says why.
        """
        if not self.lanes:
            return False
        if sys.platform == "darwin":
            self.events.record(
                "lanes_adjusted",
                mechanism="per_lane_cpu",
                action="unavailable: each lane reads its process's CPU",
                reason="psutil numbers a thread on macOS by its position, not by "
                "the native id a lane records, so no lane's thread can be found",
            )
            return False
        return True

    def _record_lane_cpu(self) -> None:
        """Every lane's thread CPU, read at one instant, into one file for the
        process - see :class:`..lanes.LaneCpu` - after every beat.

        The slots are copied without the lock. A lane opening its slot holds
        it across the slot's file I/O, and every one of those calls lets go of
        the GIL and has to win it back from the running lanes: with twenty
        lanes starting at once the heartbeat waited 6.7 s behind them. A slot
        is only ever added, and copying a dict's values is one step for the
        interpreter, so the copy is whole whether or not a lane is being
        added; one added meanwhile is read at the next beat.
        """
        slots = list(self._lanes.values())
        if not slots:
            return
        stamp = time.time()
        used = thread_cpu_seconds(
            slot.native_id for slot in slots if slot.native_id is not None
        )
        if self._lane_cpu is None:
            self._lane_cpu = LaneCpu(
                self.directory / f"{self.worker_id}{LANE_CPU_SUFFIX}", self.state.run_id
            )
        self._lane_cpu.record(stamp, {
            slot.name: used[slot.native_id]
            for slot in slots
            if slot.native_id is not None and slot.native_id in used
        })

    def _current_lane(self) -> Optional[_Slot]:
        """The lane whose thread this is, if it is one."""
        ident = threading.get_ident()
        with self._lanes_lock:
            slots = list(self._lanes.values())
        return next((slot for slot in slots if slot.ident == ident), None)

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self, excrepr: object) -> None:
        # xdist relays this to the controller as a flat string and re-raises it
        # there, so the INTERNALERROR block shows xdist's frame rather than
        # this failure. Record the real one, attributed to this worker.
        if self.lanes:
            self._lanes_internal_error(excrepr)
            return
        self.events.record(
            "internal_error",
            detail=str(excrepr),
            nodeid=self.state.nodeid,
            nodeid_hash=self.state.nodeid_hash,
        )

    def _lanes_internal_error(self, excrepr: object) -> None:
        """The same record, for a process of lanes, whose own slot names no test.

        pytest-threadlanes hands an error out of a lane to the main thread and
        raises it there, so this is rarely called on the lane that failed. The
        test is then found the way a death finds it: a lane still naming a
        test is one the error interrupted - it never reached the teardown
        that clears it. One such lane is named, as a worker's test always was;
        several are all listed, and none is named, since nothing says which of
        them raised it.
        """
        current = self._current_lane()
        with self._lanes_lock:
            slots = list(self._lanes.values())
        flying = [current] if current is not None else [
            slot for slot in slots if slot.state.nodeid
        ]
        named = flying[0] if len(flying) == 1 else None
        self.events.record(
            "internal_error",
            detail=str(excrepr),
            nodeid=named.state.nodeid if named is not None else None,
            nodeid_hash=named.state.nodeid_hash if named is not None else None,
            **({"lane": named.name} if named is not None else {}),
            **(
                {
                    "lanes_in_flight": [
                        {
                            "lane": slot.name,
                            "nodeid": slot.state.nodeid,
                            "nodeid_hash": slot.state.nodeid_hash,
                            "phase": slot.state.phase,
                        }
                        for slot in flying
                    ]
                }
                if len(flying) > 1
                else {}
            ),
        )

    def close(self) -> None:
        """Undo whatever was already set up. Called when construction failed
        part-way, so a half-built recorder does not leak the descriptors it
        managed to open before the failure."""
        while self._open_resources:
            try:
                self._open_resources.pop().close()
            except Exception:  # noqa: BLE001 - cleanup must not raise either
                pass

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()
        # Writes the background record, which is the CPU spent with no test
        # in flight: what the controller reads a moment later.
        self._profile("stop")
        self.events.record("worker_finish", exitstatus=int(exitstatus))
        # The attempt goes with the node id, and for the same reason: a
        # session torn down inside a test - pytest.exit() in a call phase -
        # reaches here without the teardown that would have cleared either,
        # and an attempt beside a null node id names a test the same record
        # says is not running.
        self.state.update(phase=None, nodeid=None, attempt=None)
        if self.lanes:
            # Every lane, for the same reason: a session torn down inside a
            # test leaves its lane's slot naming it. The lanes' threads have
            # all returned by now, so nothing else is writing these.
            with self._lanes_lock:
                slots = list(self._lanes.values())
            for slot in slots:
                slot.state.update(phase=None, nodeid=None, attempt=None)
            # And fd 2 back where it was, with what is left of the session's
            # stderr passed on - see _tee_session.
            if self.stderr_tee is not None:
                with self._tee_lock:
                    self.stderr_tee.hand_back()
        if self._allocation_tracer is not None:
            self._allocation_tracer.close()
