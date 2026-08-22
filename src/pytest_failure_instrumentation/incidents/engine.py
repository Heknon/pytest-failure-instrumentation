"""Collects incidents from every source and raises each one once.

Four of the five sources never reach ``pytest_testnodedown`` or
``pytest_internalerror``, which is why a product hooking only those two sees a
fraction of what goes wrong:

* a worker process dies            - pytest_testnodedown
* pytest raises an internal error  - pytest_internalerror
* workers collect different tests  - pytest_xdist_node_collection_finished
* a worker stops reporting         - polled here, since a hang fires no hook
* the run ends                     - a summary whose absence means the
                                     controller died too

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
import time
from pathlib import Path
from typing import Any

import pytest

from .. import probes
from ..analysis import fingerprint as fingerprint_of, severity as severity_of
from ..analysis.attribution import Attributor
from . import death, internal_error, summary
from .base import Capabilities, Incident, frame_from


class IncidentEngine:
    def __init__(self, config: pytest.Config, settings: Any) -> None:
        self.config = config
        self.settings = settings
        self.directory = settings.directory
        self.attributor = Attributor(settings.packages)
        self.baseline_oom_kills = probes.cgroup_oom_kills()

        self.run_id = "unknown"
        self.seen: dict[str, int] = {}
        self.raised = 0
        self.suppressed = 0
        self.run_ending = 0
        self._prepare_directory()

    def _prepare_directory(self) -> None:
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

        count = self.seen.get(incident.fingerprint, 0) + 1
        self.seen[incident.fingerprint] = count
        if count > 1:
            self.suppressed += 1
            return
        self.raised += 1
        self.run_ending += bool(incident.run_ending)
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
                incident.suspect_basis = f"owner of the test in flight ({path})"

        incident.run_ending = type(incident).ends_run
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

    # -- sources ---------------------------------------------------------

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        manager = getattr(session.config, "_xdist_nodemanager", None)
        self.run_id = getattr(manager, "testrunuid", None) or os.environ.get(
            "PYTEST_RUN_ID", f"run-{int(time.time())}"
        )

    def pytest_testnodedown(self, node: Any, error: object) -> None:
        if not error:
            return  # a clean shutdown is not an incident
        worker = getattr(getattr(node, "gateway", None), "id", "unknown")
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
        self.raise_incident(
            summary.build(
                exitstatus, self.seen, self.raised, self.suppressed, self.run_ending
            )
        )
