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
from pathlib import Path
from typing import Any

import pytest

from ..config import Settings
from . import crash_stack
from . import memory as memory_capture
from .events import EventLog
from .heartbeat import Heartbeat
from .state import WorkerState


class WorkerRecorder:
    def __init__(self, directory: Path, worker_id: str, settings: Settings) -> None:
        self.worker_id = worker_id
        self.directory = directory
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
            WorkerState(directory / f"{worker_id}.state", os.getpid())
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
        self._slow_stream = self._track(
            (directory / f"{worker_id}.slow").open("w", buffering=1, encoding="utf-8")
        )
        # A test that outlives this dumps its own stack - no signal, so it
        # works on Windows and interrupts no syscall.
        self.slow_test = crash_stack.SlowTestWatchdog(
            self._slow_stream, settings.slow_test_seconds
        )

        self._apply_memory_limit(settings)
        self._start_monitors(settings)

        self.events.record(
            "worker_start",
            pid=os.getpid(),
            python=sys.version.split()[0],
            platform=platform.platform(),
            executable=sys.executable,
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
        )
        self.heartbeat.start()

    # -- hooks -----------------------------------------------------------

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        # After configure, so this wins over pytest's own faulthandler plugin,
        # which registers trylast and points the handler at shared stderr.
        on_demand = crash_stack.arm_fatal_handler(self._crash_stream)
        self.events.record(
            "faulthandler_armed",
            on_demand_stack=on_demand,
            slow_test_seconds=self.slow_test.timeout if self.slow_test.enabled else None,
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Any:
        yield from self._phase("setup", item.nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Any:
        yield from self._phase("call", item.nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Any:
        yield from self._phase("teardown", item.nodeid)

    def _phase(self, phase: str, nodeid: str) -> Any:
        """Which phase is open is what separates "died in teardown" from
        "died mid-call": pytest's own logfinish fires only after the whole
        protocol, so it cannot tell them apart."""
        if phase == "setup":
            self.state.tests_started += 1
        if self.heartbeat is not None:
            self.heartbeat.nodeid = nodeid
            self.heartbeat.phase = phase
        self.state.update(nodeid=nodeid, phase=phase)
        if phase == "call":
            self.slow_test.start_test()
        yield
        if phase == "call":
            self.slow_test.end_test()
        if phase == "teardown":
            self.state.tests_finished += 1
        if self.heartbeat is not None:
            self.heartbeat.phase = None
        self.state.update(phase=None)

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
