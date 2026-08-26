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

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from .. import probes
from ..analysis import fingerprint as fingerprint_of
from ..analysis import severity as severity_of
from ..analysis.attribution import Attributor
from ..analysis.collection import CollectionTracker
from ..config import Settings
from . import collection, death, internal_error, stall, summary
from .base import Capabilities, Incident, frame_from

#: Written at the top of each run's own directory, and the only thing that
#: makes a directory this plugin's to delete. Matching on file suffixes instead
#: is how a cleanup takes somebody's coverage report with it: ``failure_directory``
#: is a natural thing to point at an existing artifacts directory.
OWNER_FILE = "owner.json"


def prune_finished_runs(root: Path) -> None:
    """Delete the directories of runs that are over.

    Over, not old. The controller's pid is in the marker, so a run that is
    still going is recognisable as such however long it has been going - which
    matters, because the whole reason each run has a directory is that several
    of them happen at once.

    A directory without our marker is not ours and is left alone, whatever it
    looks like.
    """
    try:
        candidates = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return
    for path in candidates:
        owner = _owner_of(path)
        if owner is None or probes.is_running(owner):
            continue
        shutil.rmtree(path, ignore_errors=True)


def _owner_of(directory: Path) -> Optional[int]:
    """The pid that owns this run directory, or None if it is not ours."""
    try:
        record = json.loads((directory / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = record.get("pid") if isinstance(record, dict) else None
    return int(pid) if isinstance(pid, int) else None


class IncidentEngine:
    def __init__(self, config: pytest.Config, settings: Settings) -> None:
        self.config = config
        self.settings = settings
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
        #: This process's own name for itself, fixed here and dependent on
        #: nothing. It names the directory, which is why it cannot be xdist's
        #: id: see the ``directory`` property.
        self.session_id = os.environ.get("PYTEST_RUN_ID") or (
            f"run-{uuid.uuid4().hex[:12]}"
        )
        #: Filled by the first read that finds a real id, and never
        #: recomputed after that - see the property for why not the first read
        #: of any kind.
        self._run_id: str | None = None
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
        self.sampler: threading.Thread | None = None
        self.seen: dict[str, int] = {}
        self.raised = 0
        self.suppressed = 0
        self.run_ending = 0
        #: The host's live-stack server, if this session ended up hosting it.
        #: Not an incident source - it answers questions rather than raising
        #: anything - but session start and finish are here, and giving it a
        #: plugin of its own would be two objects with one lifetime.
        self.stacks: Any = None

    # -- where this run's evidence goes ----------------------------------

    @property
    def directory(self) -> Path:
        """This run's own directory, under the configured one.

        Runs used to share a directory and name their files after the worker,
        which works exactly until two runs happen at once - and on a laptop or
        a bare-metal runner that is the ordinary case, not the exotic one.
        Every worker is ``gw0``, so the second run's ``gw0.state`` is the first
        run's ``gw0.state``: one run reads the other's evidence, believes it,
        and attributes a stall to a test that a different run is running. The
        old start-of-run cleanup made it worse rather than better, because it
        deleted the files of a run that was still using them.

        A directory per run removes the class of bug rather than a symptom of
        it. Nothing inside is named for the run, because the directory already
        is, and the paths every reader builds are unchanged.

        **Named by this process, not by xdist.** The obvious name is the run id
        this run reports, and it cannot be used: xdist's id does not exist
        until xdist has built its node manager, and there is no hook order that
        reliably puts that before this. ``trylast`` does not do it, because
        xdist's own session start is *also* ``trylast`` - so which of the two
        runs first comes down to which plugin registered first, and that
        differs between installing from the entry point and installing from a
        framework's ``pytest_configure``. Ordering had already been got wrong
        here once (see :mod:`..registration`); a name that depends on nothing
        cannot be got wrong again.

        The reported run id is unaffected and still prefers xdist's, so an
        incident still lines up with xdist's logs. Every ``.events`` line in
        here carries it, which is how a directory is matched to a run.
        """
        return self.settings.directory / self.session_id

    def _prepare_directory(self) -> None:
        """Make this run's directory, and clear out the runs that are over.

        Made when something is actually going to write there - workers, or the
        stack server publishing its address. A single-process run with neither
        has no reason to leave an empty directory in somebody's repository, and
        a run that skips the marker would leave a directory nothing ever prunes.

        What is pruned is *whole directories of finished runs*, which is a much
        safer thing to delete than a list of file suffixes: a directory is only
        touched if it holds this plugin's own marker naming a process that is
        no longer running. ``failure_directory`` is a natural thing to point at
        an existing artifacts directory, and a green run that deletes somebody's
        coverage report has done more damage than any failure it might have
        explained.
        """
        if not self.distributed and not self.settings.stack_server:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # The run id is deliberately absent: reading it here would settle
            # it before xdist has one, and the id this run reports would then
            # be a name nothing else agrees with. The events files inside carry
            # it, and they are written from the start.
            (self.directory / OWNER_FILE).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "session_id": self.session_id,
                        "started_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            return  # bookkeeping must never break a run
        prune_finished_runs(self.settings.directory)

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
        incident.owner = blame["owner"] or "unknown"
        if incident.owner == "unknown":
            # A kind that fails before anybody's code runs knows its own owner;
            # attribution had no frames to find it from.
            incident.owner = incident.owner_when_unattributable() or "unknown"

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

        Kept once it is *authoritative*, and only then. The distinction is
        load-bearing: this process's own name for itself is a stand-in that
        exists from the start, and xdist's real one does not exist until xdist
        has built its node manager. Caching whichever came first froze the
        stand-in and every incident in the run then carried a name that
        appears in nobody's logs.

        And "whichever came first" is not under this plugin's control at all.
        pytest's fixture collection walks every attribute of every registered
        plugin object looking for fixtures, with a plain ``getattr`` - so
        reading a property is something *pytest* does, at plugin registration
        time, before any hook here has run. Measured on pytest 7.0.1, where it
        happens through ``FixtureManager.parsefactories``; newer pytest
        happened not to, which is the kind of difference that makes an
        ordering assumption a bug waiting for a version bump.

        So a property on a plugin object must have no lasting side effect, and
        this one no longer does: an early read answers with the stand-in and
        keeps nothing, and the first read once the real id exists is the one
        that sticks.

        A framework that installed this by hand and named an id outranks both -
        correlating incidents with a build id is a reason to install by hand in
        the first place, and it is the only id that means anything outside this
        process. Then xdist's own, so an incident lines up with its logs.
        """
        if self._run_id is not None:
            return self._run_id
        resolved = self._resolve_run_id()
        if resolved != self.session_id:
            # An answer rather than a stand-in, so it is safe to keep.
            self._run_id = resolved
        return resolved

    def _resolve_run_id(self) -> str:
        if self.settings.run_id:
            return self.settings.run_id
        session = self.config.pluginmanager.getplugin("dsession")
        manager = getattr(session, "nodemanager", None)
        return getattr(manager, "testrunuid", None) or self.session_id

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Deliberately not ordered against xdist's own session start.

        Nothing here reads the run id, so nothing here cares whether xdist has
        built its node manager yet - see the ``directory`` property for why
        that independence had to be designed in rather than assumed.
        """
        self._prepare_directory()

        # Whether or not this is distributed: a single-process run has a stack
        # worth serving too, and it is the one this process can read for free.
        if self.settings.stack_server:
            from ..stack_server import start as start_stack_server

            self.stacks = start_stack_server(
                self.settings.stack_server_port,
                self.settings.stack_server_host,
                # The evidence directory, so a drawn port is written down
                # beside the worker state a UI reads to know which pid is
                # running which test.
                self.directory,
                self._stack_server_gave_up,
                self._stack_server_ready,
                self.session_id,
            )

        if self.settings.sample_seconds > 0:
            self.sampler = threading.Thread(
                target=self._sample_workers,
                name="failure-instrumentation-sample",
                daemon=True,
            )
            self.sampler.start()

        # Only distributed runs can strand a worker. A single process that
        # wedges takes this detector down with it.
        if self.distributed and self.settings.stall_seconds > 0:
            self.watcher = threading.Thread(
                target=self._watch_for_stalls,
                name="failure-instrumentation-stall",
                daemon=True,
            )
            self.watcher.start()

    def _sample_workers(self) -> None:
        """Push what every worker is doing, on a cadence, until the run ends.

        Its own thread rather than the stall watcher's, though both poll the
        same files. The watcher's cadence is derived from the stall threshold
        and is a detection deadline; this one is a reporting rate somebody
        picked to suit their dashboard, and tying a diagnosis to a display
        setting is how a stall starts being detected late because a UI wanted
        fewer rows.
        """
        from ..sampling import WorkerSampler

        sampler = WorkerSampler(
            self.directory,
            session_id=self.session_id,
            want_stacks=self.settings.sample_stacks,
        )
        while not self.stop.wait(self.settings.sample_seconds):
            try:
                sample = sampler.sample()
            except Exception as failure:  # noqa: BLE001 - never break a run
                print(
                    f"[failure-instrumentation] could not sample: {failure!r}",
                    flush=True,
                )
                continue
            if not sample.workers:
                # Nothing has started writing yet, or the run is over. An
                # empty sample is a row that says nothing, every interval.
                continue
            try:
                self.config.hook.pytest_failure_worker_sample(sample=sample)
            except Exception as failure:  # noqa: BLE001 - never break a run
                print(
                    f"[failure-instrumentation] sample hook raised: {failure!r}",
                    flush=True,
                )

    def _stack_server_ready(self, server: Any) -> None:
        """The live view is up, and only this run knows where.

        A drawn port is the case that makes this necessary rather than
        convenient: nobody can configure an address that did not exist until a
        moment ago, so without this a product's only route to it is to read
        this package's discovery file and parse it - which makes a private file
        into a public interface, and one that cannot then be changed.

        Called from the server's own announcing thread. The hook is wrapped
        exactly as the incident hook is: a product's reporting is never allowed
        to become an INTERNALERROR in somebody's test run.
        """
        try:
            self.config.hook.pytest_failure_server_ready(server=server)
        except Exception as failure:  # noqa: BLE001 - never break a run
            print(
                f"[failure-instrumentation] server-ready hook raised: {failure!r}",
                flush=True,
            )

    def _stack_server_gave_up(self, verdict: str, detail: str) -> None:
        """Somebody switched the live view on and it is not there.

        Without this the run continues perfectly well and their UI shows
        nothing forever, with no error anywhere - because from the outside "no
        server" and "no tests running" look identical. Called from the
        server's own thread, which raise_incident is already safe for.
        """
        from . import stack_server as stack_server_incident

        try:
            self.raise_incident(
                stack_server_incident.build(
                    verdict,
                    self.settings.stack_server_host,
                    self.settings.stack_server_port,
                    detail,
                )
            )
        except Exception as failure:  # noqa: BLE001 - never break a run
            print(f"[failure-instrumentation] could not report: {failure!r}", flush=True)

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
            # The *resolved* directory, not the configured one. A worker cannot
            # work out which run it belongs to on its own, and one that guessed
            # would write its evidence where nothing is going to read it.
            node.workerinput["failure_settings"] = self.settings.with_overrides(
                directory=self.directory
            ).as_payload()
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
        if self.sampler is not None:
            # Bounded like the watcher: a sample hook that will not return
            # must not be what keeps a finished run from exiting.
            self.sampler.join(timeout=2.0)
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
