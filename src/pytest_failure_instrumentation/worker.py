"""Worker-side recording: cheap in the common path, useful when it matters.

Everything here is sized by one rule - a test that passes must cost as close to
nothing as possible, because that is the overwhelming majority of what runs.
So the per-test path writes one fixed-size record per phase and nothing else:
no appends, no ``/proc`` reads, no allocation tracking.

The expensive things (memory sampling, allocation snapshots) live on a
background thread that runs on a timer, not per test, and the truly expensive
one (walking the heap) is off unless asked for.
"""

from __future__ import annotations

import faulthandler
import json
import os
import platform
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from . import probes
from .state import WorkerState


class WorkerRecorder:
    """Records worker state and rare events; started once per worker."""

    def __init__(self, config: pytest.Config, directory: Path, worker_id: str) -> None:
        self.worker_id = worker_id
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

        self.state = WorkerState(directory / f"{worker_id}.state", os.getpid())
        # Rare events only: startup, memory high-water marks, internal errors.
        # A passing test appends nothing here.
        self._events = (directory / f"{worker_id}.events").open(
            "w", buffering=1, encoding="utf-8"
        )
        # faulthandler holds this for the process lifetime, so it is never
        # closed - a crash during interpreter shutdown still gets written.
        self._crash_stream = (directory / f"{worker_id}.crash").open(
            "w", buffering=1, encoding="utf-8"
        )
        self.watchdog: Any = None

        self.record(
            "worker_start",
            pid=os.getpid(),
            python=sys.version.split()[0],
            platform=platform.platform(),
            executable=sys.executable,
        )
        self.state.update()

    # -- recording -------------------------------------------------------

    def record(self, event: str, **fields: Any) -> None:
        fields["event"] = event
        fields.setdefault("time", round(time.time(), 3))
        try:
            self._events.write(json.dumps(fields) + "\n")
            self._events.flush()
        except (ValueError, OSError):
            pass  # a closed stream must never break a run

    # -- hooks -----------------------------------------------------------

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        # pytest's faulthandler plugin enables at configure time with
        # ``trylast``, pointing at shared stderr where every worker's output
        # interleaves. Claim it here so a crash stack names one worker.
        faulthandler.enable(file=self._crash_stream, all_threads=True)
        if probes.can_request_stack():
            # Lets the controller ask a stalled worker for a stack without
            # killing it. The handler is async-signal-safe C, so it answers
            # even while native code holds the GIL.
            faulthandler.register(
                signal.SIGUSR1, file=self._crash_stream, all_threads=True, chain=False
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
        """One fixed-size write in, one out. No log, no /proc, no allocation.

        Recording the phase is what separates "died in teardown" from "died
        mid-call" later; pytest's own logfinish only fires after the whole
        protocol, so it cannot tell them apart.
        """
        state = self.state
        if phase == "setup":
            state.tests_started += 1
        if self.watchdog is not None:
            self.watchdog.nodeid = nodeid
            self.watchdog.phase = phase
        state.update(nodeid=nodeid, phase=phase)
        yield
        if phase == "teardown":
            state.tests_finished += 1
        state.update(phase=None)

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self, excrepr: object) -> None:
        # xdist ships this to the controller as a flat string and re-raises it
        # there, so the resulting INTERNALERROR shows xdist's own frame. Record
        # the real one here, attributed to this worker and this test.
        self.record("internal_error", detail=str(excrepr), nodeid=self.state.nodeid)

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        if self.watchdog is not None:
            self.watchdog.stop()
        self.record("worker_finish", exitstatus=int(exitstatus))
        self.state.update(phase=None, nodeid=None)


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read a worker's event log. Tolerates a truncated final line."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a hard kill can truncate the last line
    return events


def read_crash_stack(path: Path, limit: int = 12, offset: int = 0) -> list[str]:
    """The head of a faulthandler dump - most recent call first.

    ``offset`` reads only what was appended after a known point, so a stack
    requested now is not confused with one written earlier.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            lines = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return []
    if not lines:
        return []
    banner = lines[0] if lines[0].startswith("Fatal Python error") else None
    for index, line in enumerate(lines):
        if line.startswith("Current thread"):
            section = lines[index : index + limit]
            return ([banner] + section) if banner else section
    return lines[:limit]
