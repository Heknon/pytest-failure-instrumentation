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
import platform
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ..config import Settings
from ..probes import tracing
from . import crash_stack
from . import memory as memory_capture
from . import output as output_capture
from .events import EventLog
from .heartbeat import Heartbeat
from .state import WorkerState


class WorkerRecorder:
    def __init__(
        self,
        directory: Path,
        worker_id: str,
        settings: Settings,
        *,
        faulthandler_timeout: float = 0.0,
        claims_fatal_dumps: bool = True,
    ) -> None:
        self.worker_id = worker_id
        self.directory = directory
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
        self._frozen_stream = None
        self.frozen = crash_stack.FrozenInterpreterFallback(None, 0.0)
        if settings.watchdog and self.faulthandler_timeout <= 0:
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
            tee = output_capture.StderrTee(directory / f"{worker_id}.output")
            tee.start()
            if tee.active:
                self._track(tee)
                self.stderr_tee = tee
            self.events.record("output_capture", status=tee.reason)

        self._apply_memory_limit(settings)
        self._start_monitors(settings)

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
            platform=platform.platform(),
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
        self.heartbeat = Heartbeat(
            self.events.record,
            interval=settings.heartbeat_interval,
            observers=[self.monitor],
            # Every wake rather than every beat: one is watching a deadline
            # and the other is pushing one out, and both are wrong if they
            # only happen every fifth second.
            tickers=[self.slow_test, self.frozen],
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

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Any:
        yield from self._phase("setup", item.nodeid)

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Any:
        yield from self._phase("call", item.nodeid)

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Any:
        yield from self._phase("teardown", item.nodeid)

    def _tee_take(self) -> None:
        """Point fd 2 at the capture file, if the tee is on. pytest points fd 2
        at its own file at the start of every phase; this takes it just after,
        and _tee_hand_back gives it back at the phase's end with the phase's
        bytes copied into pytest's file, so both keep the output."""
        if self.stderr_tee is not None:
            self.stderr_tee.take()

    def _tee_hand_back(self) -> None:
        if self.stderr_tee is not None:
            self.stderr_tee.hand_back()

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

    def _phase(self, phase: str, nodeid: str) -> Any:
        """Which phase is open is what separates "died in teardown" from
        "died mid-call": pytest's own logfinish fires only after the whole
        protocol, so it cannot tell them apart."""
        self._tee_take()
        now = time.time()
        if phase == "setup":
            self.state.tests_started += 1
            # The whole test's clock, not the phase's: pytest-timeout and
            # faulthandler_timeout both time the item from its setup, so a
            # death is matched against a timeout by how long the *test* ran.
            self.state.test_started = now
        if self.heartbeat is not None:
            self.heartbeat.nodeid = nodeid
            self.heartbeat.phase = phase
        self.state.update(nodeid=nodeid, phase=phase, phase_started=now)
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
            self.slow_test.start_test()
        try:
            yield
        finally:
            # Give fd 2 back to pytest, with this phase's stderr copied into
            # pytest's own capture, whatever the phase did.
            self._tee_hand_back()
        if self.heartbeat is not None:
            self.heartbeat.phase = None
        if phase != "teardown":
            self.state.update(phase=None)
            return
        self.slow_test.end_test()
        self.state.tests_finished += 1
        # The node id is cleared with the *test*, not with each phase. A worker
        # that dies or wedges in the gap between two tests has no test in
        # flight, and saying it had one names a test that already passed - to
        # which the incident is then attributed, with an owner and a severity.
        # What that test was is still worth knowing, so the slot keeps it in
        # `last_nodeid`, where nothing can mistake it for the present.
        if self.heartbeat is not None:
            self.heartbeat.nodeid = None
        self.state.update(phase=None, nodeid=None)

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self, excrepr: object) -> None:
        # xdist relays this to the controller as a flat string and re-raises it
        # there, so the INTERNALERROR block shows xdist's frame rather than
        # this failure. Record the real one, attributed to this worker.
        self.events.record(
            "internal_error", detail=str(excrepr), nodeid=self.state.nodeid
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
        self.events.record("worker_finish", exitstatus=int(exitstatus))
        self.state.update(phase=None, nodeid=None)
