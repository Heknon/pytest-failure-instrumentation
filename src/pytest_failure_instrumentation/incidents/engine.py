"""Collects incidents from every source and raises each one once.

Four of the five sources never reach ``pytest_testnodedown`` or
``pytest_internalerror``, which is why a product hooking only those two sees a
fraction of what goes wrong:

* a worker process dies            - pytest_testnodedown          (xdist)
* workers collect different tests  - pytest_xdist_node_collection_finished
* a worker stops reporting         - polled here, because the absence of
                                     anything being said fires no hook (xdist)
* pytest raises an internal error  - pytest_internalerror         (any run)
* the run ends                     - a summary whose absence means the
                                     process died                 (any run)

The last two are not distributed problems and this engine does not assume it
is running under xdist: an internal error ends a single-process run just as
finally, and pytest reports it through a path that produces no terminal summary
at all. The xdist-only hookimpl below is declared optionalhook so that its spec
being absent is not a registration error.

Each source builds its own model (one module per kind); everything after that
is identical for all of them and lives in ``raise_incident``: blame, severity,
fingerprint, and one hook call per distinct fingerprint - so one defect on
twelve workers is one incident with a count rather than twelve rows.

Nothing in this file may raise. An exception in a reporting hook becomes an
INTERNALERROR that ends the customer's run, which would mean the
instrumentation had done more damage than the failure it came to explain.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from .. import probes
from ..analysis import fingerprint as fingerprint_of, severity as severity_of
from ..analysis.attribution import Attributor
from ..analysis.collection import CollectionTracker
from . import collection, death, internal_error, stall, summary
from .base import Capabilities, Incident, frame_from


class IncidentEngine:
    def __init__(self, config: pytest.Config, settings: Any) -> None:
        self.config = config
        self.settings = settings
        self.directory = settings.directory
        self.attributor = Attributor(settings.packages)
        self.baseline_oom_kills = probes.cgroup_oom_kills()
        self.distributed = bool(
            config.pluginmanager.hasplugin("xdist")
            and config.getoption("dist", "no") != "no"
        )

        self.run_id = "unknown"
        self.collections = CollectionTracker()
        self.reported_mismatch = False
        #: Last time each live worker said anything, and which are wedged
        #: already - shared with the watcher thread below.
        self.activity: dict[str, float] = {}
        self.stalled: set[str] = set()
        self.tests_seen = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.watcher: threading.Thread | None = None
        self.seen: dict[str, int] = {}
        self.raised = 0
        self.suppressed = 0
        self.run_ending = 0
        self._prepare_directory()

    def _prepare_directory(self) -> None:
        # Only workers write evidence here, so a single-process run has no
        # reason to leave an empty directory in somebody's repository.
        if not self.distributed:
            return
        # Stale files from an earlier run would be read as this one's evidence.
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in self.directory.iterdir():
                if path.suffix in {".events", ".state", ".crash", ".txt", ".json"}:
                    path.unlink()
        except OSError:
            pass

    # -- raising ---------------------------------------------------------

    def raise_incident(self, incident: Incident) -> None:
        try:
            self._enrich(incident)
        except Exception as failure:  # noqa: BLE001 - a partial incident beats none
            incident.evidence.append(f"enrichment failed: {failure!r}")

        # The stall watcher raises from its own thread, so the counters and
        # the dedupe table are shared state.
        with self.lock:
            count = self.seen.get(incident.fingerprint, 0) + 1
            self.seen[incident.fingerprint] = count
            if count == 1:
                self.raised += 1
                self.run_ending += bool(incident.run_ending)
            else:
                self.suppressed += 1
        if count > 1:
            return
        try:
            self.config.hook.pytest_failure_incident(incident=incident)
        except Exception as failure:  # noqa: BLE001
            print(f"[failure-instrumentation] incident hook raised: {failure!r}", flush=True)

    def _enrich(self, incident: Incident) -> None:
        """Everything that is the same whatever kind this is."""
        lines, reverse = incident.stack_lines()
        blame = self.attributor.blame(lines, reverse=reverse)
        incident.top_frame = frame_from(blame["top_frame"])
        incident.blamed_frame = frame_from(blame["blamed_frame"])
        incident.owner = blame["owner"]

        nodeid = incident.suspect_nodeid()
        if incident.owner == "unknown" and nodeid:
            path = str(nodeid).split("::")[0]
            if path:
                incident.suspect_owner = self.attributor.owner_of(str(Path(path).resolve()))
                incident.suspect_basis = incident.suspect_basis_for(path)

        incident.run_ending = incident.ends_this_run()
        severity, why = severity_of.of(
            incident.kind, incident.owner, incident.verdict,
            incident.confidence, incident.run_ending,
        )
        incident.severity = severity
        if why:
            incident.evidence.append(why)

        incident.fingerprint = fingerprint_of.of(incident, incident.blamed_frame)
        incident.run_id = self.run_id
        incident.raised_at = time.time()
        incident.capabilities = Capabilities(**probes.capabilities())
        incident.product_version = self.settings.product_version

    # -- liveness --------------------------------------------------------

    def _touch(self, worker: str | None) -> None:
        if worker:
            with self.lock:
                self.activity[worker] = time.time()

    def _watch_for_stalls(self) -> None:
        """Poll, because a wedged worker fires no hook at all.

        Every other source is something pytest tells us. This one is the
        absence of anything being said, which nothing can deliver.
        """
        limit = self.settings.stall_seconds
        while not self.stop.wait(min(limit / 4, 15.0)):
            now = time.time()
            with self.lock:
                candidates = [
                    (worker, now - seen)
                    for worker, seen in self.activity.items()
                    if now - seen > limit and worker not in self.stalled
                ]
            for worker, silent_for in candidates:
                try:
                    self._assess_stall(worker, silent_for)
                except Exception as failure:  # noqa: BLE001
                    with self.lock:
                        self.stalled.add(worker)
                    self.raise_incident(
                        stall.WorkerStallIncident.degraded(worker, failure)
                    )

    def _assess_stall(self, worker: str, silent_for: float) -> None:
        incident = stall.build(
            worker,
            self.directory,
            silent_for,
            self.settings.heartbeat_interval,
            self.settings.stack_probe,
        )
        if incident is None:
            # Slow, not stuck. Re-arm rather than asking again immediately.
            self._touch(worker)
            return
        with self.lock:
            self.stalled.add(worker)
        self.raise_incident(incident)

    # -- sources ---------------------------------------------------------

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        manager = getattr(session.config, "_xdist_nodemanager", None)
        self.run_id = getattr(manager, "testrunuid", None) or os.environ.get(
            "PYTEST_RUN_ID", f"run-{int(time.time())}"
        )
        # Only distributed runs can strand a worker. A single process that
        # wedges takes this detector down with it.
        if self.distributed and self.settings.stall_seconds > 0:
            self.watcher = threading.Thread(
                target=self._watch_for_stalls,
                name="failure-instrumentation-stall",
                daemon=True,
            )
            self.watcher.start()

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodeready(self, node: Any) -> None:
        self._touch(getattr(getattr(node, "gateway", None), "id", None))

    def pytest_runtest_logreport(self, report: Any) -> None:
        node = getattr(report, "node", None)
        if node is not None:
            self.tests_seen += 1
            self._touch(getattr(getattr(node, "gateway", None), "id", None))

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: Any) -> None:
        worker = getattr(getattr(node, "gateway", None), "id", "unknown")
        self._touch(worker)
        # A worker registering a collection once tests are already running is a
        # replacement for one that died. xdist drops it silently if what it
        # collected differs, so the run continues a worker short.
        self.collections.record(worker, list(ids), replacement=self.tests_seen > 0)

        if not self.collections.has_mismatch or self.reported_mismatch:
            return
        self.reported_mismatch = True
        try:
            incident: Incident = collection.build(self.collections, self.directory)
        except Exception as failure:  # noqa: BLE001
            incident = collection.CollectionMismatchIncident.degraded(
                "controller", failure
            )
        self.raise_incident(incident)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: object) -> None:
        worker = getattr(getattr(node, "gateway", None), "id", "unknown")
        with self.lock:
            self.activity.pop(worker, None)
        if not error:
            return  # a clean shutdown is not an incident
        try:
            incident: Incident = death.build(
                node, error, self.directory, self.baseline_oom_kills
            )
        except Exception as failure:  # noqa: BLE001
            incident = death.WorkerDeathIncident.degraded(
                worker, failure, context=f"xdist reported: {error}"
            )
        self.raise_incident(incident)

    def pytest_internalerror(self, excrepr: object) -> None:
        try:
            incident: Incident = internal_error.build(excrepr, self.directory)
        except Exception as failure:  # noqa: BLE001
            incident = internal_error.InternalErrorIncident.degraded(
                "controller", failure, context=str(excrepr)[-2000:]
            )
        self.raise_incident(incident)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.stop.set()
        if self.watcher is not None:
            self.watcher.join(timeout=2.0)
        self.raise_incident(
            summary.build(
                exitstatus,
                self.seen,
                self.raised,
                self.suppressed,
                self.run_ending,
                self.distributed,
            )
        )
