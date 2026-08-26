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
import uuid
from pathlib import Path
from typing import Any

import pytest

from .. import probes
from ..analysis import fingerprint as fingerprint_of
from ..analysis import severity as severity_of
from ..analysis.attribution import Attributor
from ..analysis.collection import CollectionTracker
from ..config import Settings
from . import collection, death, internal_error, stall, summary
from .base import Capabilities, Incident, frame_from

#: Suffixes this plugin writes, one file per worker. Anything else in the
#: evidence directory belongs to somebody else.
#: ``.part`` is a watchdog dump that was being written when the run ended.
OWNED_SUFFIXES = (".state", ".events", ".crash", ".slow", ".frozen", ".part")

#: The one file that is not per worker. It is a ``.txt``, and deleting every
#: ``.txt`` to catch it took a coverage report and a build log with it - so it
#: is matched by the name this plugin gave it instead.
COLLECTION_PREFIX = "collection-"


def _is_ours(path: Path) -> bool:
    if path.suffix in OWNED_SUFFIXES:
        return True
    return path.name.startswith(COLLECTION_PREFIX) and path.suffix == ".txt"


class IncidentEngine:
    def __init__(self, config: pytest.Config, settings: Settings) -> None:
        self.config = config
        self.settings = settings
        self.directory = settings.directory
        self.attributor = Attributor(settings.packages)
        self.baseline_oom_kills = probes.cgroup_oom_kills()
        self.distributed = bool(
            config.pluginmanager.hasplugin("xdist")
            and config.getoption("dist", "no") != "no"
        )

        # xdist's own id for the run is the one a reader can line up against
        # its logs, but it does not exist until xdist has started its workers -
        # so it is resolved lazily below, with a fallback fixed now. A
        # timestamp is not enough: two runs starting in the same second are
        # common on CI, and they would share an id.
        self._run_id_fallback = os.environ.get("PYTEST_RUN_ID") or (
            f"run-{uuid.uuid4().hex[:12]}"
        )
        self.collections = CollectionTracker()
        self.reported_mismatch = False
        #: Workers that went down before registering a collection. They are
        #: never going to, so they are subtracted from what is waited for.
        self.workers_lost: set[str] = set()
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
        #: The host's live-stack server, if this session ended up hosting it.
        #: Not an incident source - it answers questions rather than raising
        #: anything - but session start and finish are here, and giving it a
        #: plugin of its own would be two objects with one lifetime.
        self.stacks: Any = None
        self._prepare_directory()

    def _prepare_directory(self) -> None:
        # Only workers write evidence here, so a single-process run has no
        # reason to leave an empty directory in somebody's repository.
        if not self.distributed:
            return
        # Stale files from an earlier run would be read as this one's evidence.
        # Only files this plugin writes are removed: failure_directory is a
        # natural thing to point at an existing artifacts directory, and a
        # green run that deletes somebody's coverage report has done more
        # damage than any failure it might have explained.
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in self.directory.iterdir():
                if path.is_file() and _is_ours(path):
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
        lines, reverse = incident.blame_stack()
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

    @property
    def run_id(self) -> str:
        """The id every piece of this run's evidence is stamped with.

        xdist's own id is preferred, so an incident lines up with its logs. It
        is read from the session plugin rather than stored: the node manager
        that holds it is built inside xdist's own ``pytest_sessionstart``,
        which may run after this one.

        A framework that installed this by hand and named an id outranks both
        - correlating incidents with a build id is a reason to install by hand
        in the first place, and it is the only id that means anything outside
        this process.
        """
        if self.settings.run_id:
            return self.settings.run_id
        session = self.config.pluginmanager.getplugin("dsession")
        manager = getattr(session, "nodemanager", None)
        return getattr(manager, "testrunuid", None) or self._run_id_fallback

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        # Whether or not this is distributed: a single-process run has a stack
        # worth serving too, and it is the one this process can read for free.
        if self.settings.stack_server:
            from ..stack_server import start as start_stack_server

            self.stacks = start_stack_server(self.settings.stack_server_port)

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
    def pytest_configure_node(self, node: Any) -> None:
        """Hand each worker the run id and the settings in force.

        The run id, because without it the controller and its workers stamp
        different ids and a file left behind by an earlier run reads as part
        of this one.

        The settings, because a worker is a different process. Ini it can read
        for itself; a framework's settings, computed in Python on the
        controller, do not exist there at all - and the framework's own code
        may not even be loaded in the worker to recompute them. Sending them
        also removes the last way the two sides can disagree about a number
        they both act on.
        """
        try:
            node.workerinput["failure_run_id"] = self.run_id
            node.workerinput["failure_settings"] = self.settings.as_payload()
        except Exception:  # noqa: BLE001 - a worker falling back to ini is a
            pass  # worse answer than this one, not a broken run

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
        self._report_mismatch(partial=False)

    def _expected_collections(self) -> int:
        """How many collections this run should produce before anyone judges.

        xdist brings a worker up and lets it collect before starting the next,
        so "how many are ready" tracks "how many have reported" exactly and
        answers nothing. The number of gateway specs is the real total, and it
        is the one xdist itself waits for; it also resolves ``-n auto``, which
        no option value does at this point. Workers already lost are
        subtracted, since a dead one will never register.
        """
        expected = 0
        session = self.config.pluginmanager.get_plugin("dsession")
        specs = getattr(getattr(session, "nodemanager", None), "specs", None)
        if specs:
            expected = len(specs)
        if not expected:
            try:
                expected = int(self.config.getoption("numprocesses", 0) or 0)
            except (TypeError, ValueError):
                expected = 0
        with self.lock:
            expected -= len(self.workers_lost)
        # Never wait for fewer than have already answered: an unrecognised
        # topology should report late rather than not at all.
        return max(expected, len(self.collections.digest_by_worker))

    def _report_mismatch(self, partial: bool) -> None:
        """Report a disagreement once, and only once every opinion is in.

        Reporting on the first two differing digests describes whichever two
        workers won the race: at sixty workers that is "2 workers produced 2
        different collections", and the majority-against-minority framing this
        whole kind is built on never happens. So it waits - and session finish
        passes ``partial`` to report what it has if a worker died still owing
        a collection.
        """
        if self.reported_mismatch or not self.collections.has_mismatch:
            return
        try:
            recorded = len(self.collections.digest_by_worker)
            waiting = not partial and recorded < self._expected_collections()
        except Exception:  # noqa: BLE001 - this runs inside an xdist hook, and
            waiting = False  # an exception here is an INTERNALERROR
        if waiting:
            return
        self.reported_mismatch = True
        try:
            incident: Incident = collection.build(
                self.collections, self.directory, complete=not partial
            )
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
            if worker not in self.collections.digest_by_worker:
                self.workers_lost.add(worker)
        # One fewer collection to wait for, which may be the one that was
        # holding a mismatch back.
        self._report_mismatch(partial=False)
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
            incident: Incident = internal_error.build(
                excrepr,
                self.directory,
                run_id=self.run_id,
                distributed=self.distributed,
            )
        except Exception as failure:  # noqa: BLE001
            incident = internal_error.InternalErrorIncident.degraded(
                "controller", failure, context=str(excrepr)[-2000:]
            )
        self.raise_incident(incident)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.stop.set()
        if self.watcher is not None:
            self.watcher.join(timeout=2.0)
        # A worker that died still owing a collection means the full set never
        # arrives. Report what was seen rather than nothing at all, flagged as
        # incomplete so the worker counts are not read as the whole picture.
        self._report_mismatch(partial=True)
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
        # Last, so that a UI watching a long teardown keeps its answers for as
        # long as this session exists. Whoever is waiting for the port takes it
        # over within a few seconds of this returning.
        if self.stacks is not None:
            self.stacks.stop()
