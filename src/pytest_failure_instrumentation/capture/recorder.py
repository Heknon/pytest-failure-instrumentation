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

from . import crash_stack, memory as memory_capture
from .events import EventLog
from .heartbeat import Heartbeat
from .state import WorkerState


class WorkerRecorder:
    def __init__(self, directory: Path, worker_id: str, settings: Any) -> None:
        self.worker_id = worker_id
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

        self.state = WorkerState(directory / f"{worker_id}.state", os.getpid())
        self.events = EventLog(directory / f"{worker_id}.events")
        # Never closed: faulthandler keeps it for the process lifetime, so a
        # crash during interpreter shutdown still gets written.
        self._crash_stream = (directory / f"{worker_id}.crash").open(
            "w", buffering=1, encoding="utf-8"
        )
        self.heartbeat: Heartbeat | None = None
        self.monitor: memory_capture.MemoryMonitor | None = None
        # A test that outlives this dumps its own stack - no signal, so it
        # works on Windows and interrupts no syscall.
        self.slow_test = crash_stack.SlowTestWatchdog(
            self._crash_stream, settings.slow_test_seconds
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

    # -- setup -----------------------------------------------------------

    def _apply_memory_limit(self, settings: Any) -> None:
        """Turn a silent OOM kill into a MemoryError attributed to a test."""
        if settings.memory_limit_mb <= 0:
            return
        try:
            import resource
        except ImportError:
            return  # Windows has no RLIMIT
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = settings.memory_limit_mb * 1024 * 1024
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        self.events.record("memory_limit_applied", limit_mb=settings.memory_limit_mb)

    def _start_monitors(self, settings: Any) -> None:
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

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()
        self.events.record("worker_finish", exitstatus=int(exitstatus))
        self.state.update(phase=None, nodeid=None)
