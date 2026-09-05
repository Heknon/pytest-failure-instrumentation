"""Collects incidents from every source and raises each one once.

Four of the five sources never reach ``pytest_testnodedown`` or
``pytest_internalerror``, which is why a product hooking only those two sees a
fraction of what goes wrong:

* a worker process dies            - pytest_testnodedown          (xdist)
* workers collect different tests  - pytest_xdist_node_collection_finished
* a process stops reporting        - polled here, because the absence of
                                     anything being said fires no hook. A
                                     worker, or this process itself when the
                                     run has no workers        (any run)
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

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from .. import probes
from ..analysis.attribution import Attributor
from ..analysis.collection import CollectionTracker
from ..capture.events import CONTROLLER_EVENTS, EventLog
from ..config import SOLE_WORKER, Settings, advise
from ..probes import signal_trace
from ..registration import RECORDER_NAME
from ..schedule import ScheduleTracker, worker_of
from . import leftovers, reporter
from .leftovers import OWNER_FILE, prune_finished_runs

if TYPE_CHECKING:
    from . import killer
    from .base import Incident

#: The environment variable that names this run's evidence directory, and the
#: only value in this package that a person hands over and the filesystem then
#: obeys. What it names is one component under ``failure_directory``, and
#: ``Path.__truediv__`` will not hold it to that on its own: an absolute
#: right-hand side *replaces* the left entirely, so ``PYTEST_RUN_ID=/tmp/x`` is
#: not a subdirectory of the configured directory but a different directory,
#: and this run's ``owner.json``, every worker's ``.state``, ``.events``,
#: ``.crash`` and ``.frozen``, and the live view's discovery file are all
#: written there instead. ``../..`` walks upward just as quietly, because the
#: ``mkdir(parents=True)`` that follows is happy to create whatever it is
#: handed. Neither says anything at the time: the run looks like it worked, and
#: its evidence is somewhere nobody looks, or on top of somebody else's.
RUN_NAME_ENV = "PYTEST_RUN_ID"

#: The shape a value has to have before it is used as that component. Letters,
#: digits, dot, dash and underscore cover what this variable is documented to
#: carry - a CI build number, a git SHA, a matrix cell like
#: ``ubuntu-22.04-py3.11``, a slugified branch name - and exclude every way one
#: component turns into something else: ``/`` and ``\`` are separators (both of
#: them, on Windows), ``:`` makes ``C:foo`` a drive-relative path rather than a
#: name, and a NUL is the one that does not even fail like the others -
#: ``mkdir`` answers it with ``ValueError``, which is not an ``OSError`` and so
#: is not caught by the guard that exists around it, turning an environment
#: variable into an INTERNALERROR out of session start.
#:
#: 128 characters is not a filesystem limit; ``NAME_MAX`` is 255 wherever this
#: runs. It leaves the rest of the budget to the files written *inside* the
#: directory on the platform with the least of it, Windows still bounding a
#: whole path at 260 characters unless long paths are switched on. The longest
#: identifier a build system realistically hands over is a 64-character SHA-256
#: digest.
RUN_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")

#: Names Windows resolves to devices rather than to files, in any directory and
#: whatever suffix follows them. Refused everywhere rather than only where they
#: bite, because the failure they cause is the quiet one: on Windows the
#: ``mkdir`` raises, the guard in :meth:`IncidentEngine._prepare_directory`
#: swallows it as it must, and the run reports nothing and says nothing about
#: why. A build id that gives a Linux runner a directory and a Windows
#: developer silence is worse than refusing a name nobody uses as a build id on
#: either.
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{digit}" for digit in "123456789"]
    + [f"LPT{digit}" for digit in "123456789"]
)

#: How long session finish waits for the stall watcher to notice the run is
#: over. Every wait inside it is against the stop event, so this is slack for a
#: loaded runner rather than a real deadline.
WATCHER_JOIN_SECONDS = 10.0

#: Written once at the top of the evidence directory, so that the directory a
#: run makes in somebody's checkout does not become a commit. What is under it
#: is one directory per run of scratch that a later run deletes: it is evidence
#: of a process that is over, not source, and the ``git status`` it would
#: otherwise fill is the one a developer reads to see what they changed.
#:
#: ``*`` covers the ignore file itself, which is deliberate and is what
#: pytest's own ``.pytest_cache/.gitignore`` does: a file whose whole job is to
#: keep a directory out of the repository has no business being the one thing
#: in it that ends up committed.
GITIGNORE_FILE = ".gitignore"
GITIGNORE_BODY = (
    "# Created automatically by pytest-failure-instrumentation.\n"
    "# One directory per run of evidence, pruned by later runs - not source.\n"
    "*\n"
)


def usable_as_a_run_name(value: str) -> bool:
    """Whether ``value`` is one directory component that means only itself."""
    if not RUN_NAME.match(value):
        return False
    if value.strip(".") == "":
        # "." is the configured directory itself and ".." is its parent, so
        # both are a run writing on top of something rather than into a
        # directory of its own - and "." in particular passes every other check
        # here while making ``prune_finished_runs`` sweep the directory the run
        # is at that moment using. Longer runs of dots are legal names on POSIX
        # and go with them, because nothing that reads back as a build id is
        # spelled that way.
        return False
    # Windows matches a device on the stem, so "NUL.evidence" is the device
    # too; the suffix does not rescue it.
    return value.split(".")[0].upper() not in RESERVED_NAMES


def name_this_run(fallback: str) -> str:
    """``PYTEST_RUN_ID`` if it can name a directory, otherwise ``fallback``.

    Read once, here, rather than where the directory is built - because the
    name is not only a directory name. It is stamped into ``owner.json``, it is
    what :class:`..stack_server.LiveStackServer` reports as its session, and it
    is the key a product joins incidents on until xdist has a run id of its
    own. Sanitising at the point of use would leave those three agreeing on a
    name that the filesystem never saw, which is the same correlation failure
    in a place that is harder to notice.

    A value that cannot be used falls back rather than ending the run: this
    plugin does not get to stop a suite over its own settings, and that promise
    is older than this check. But it is never dropped in silence. Somebody sets
    this variable precisely so that a build's incidents carry the build's id;
    an ignored value leaves that correlation broken with nothing to read that
    says why, which is exactly the kind of quiet absence the plugin exists to
    stop people misreading.
    """
    raw = os.environ.get(RUN_NAME_ENV, "")
    # Surrounding space is not part of anybody's build id, and a trailing one
    # is worse than useless: Windows strips it off a directory name, so two
    # values that differ would name one directory. Folded rather than reported,
    # the way ``config.resolve`` folds the space around every other setting a
    # person types.
    value = raw.strip()
    if not value:
        return fallback  # unset, or exported empty, both of which say nothing
    if usable_as_a_run_name(value):
        return value
    advise(
        f"{RUN_NAME_ENV}={raw!r} cannot name a directory, so it is ignored and "
        f"this run's evidence goes to {fallback!r} instead - nothing in it will "
        f"correlate with that id. A run name is 1-128 characters of letters, "
        f"digits, '.', '-' and '_', and is neither '.' nor '..'; anything else "
        f"is a path rather than a name and would write this run's evidence "
        f"outside failure_directory"
    )
    return fallback


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
        #: id: see the ``directory`` property. An operator may name it instead,
        #: and :func:`name_this_run` is where that name has to earn the right
        #: to be a directory component - see there.
        self.session_id = name_this_run(f"run-{uuid.uuid4().hex[:12]}")
        #: Filled by the first read that finds a real id, and never
        #: recomputed after that - see the property for why not the first read
        #: of any kind.
        self._run_id: str | None = None
        self.collections = CollectionTracker()
        self.reported_mismatch = False
        #: Workers that went down before registering a collection. They are
        #: never going to, so they are subtracted from what is waited for.
        self.workers_lost: set[str] = set()
        #: Workers xdist has reported down, which is final: a replacement gets
        #: a new gateway id, so an id here will never be live again. Kept
        #: because a dead worker's *last* report arrives after its death -
        #: see _touch.
        self.workers_down: set[str] = set()
        #: How long each live worker has been silent, on the *monotonic* clock,
        #: and which are wedged already - shared with the watcher thread below.
        #: Monotonic because this is one process measuring an interval against
        #: itself: a wall clock that steps forward - an NTP correction on a
        #: freshly booted CI machine is the ordinary way that happens - would
        #: make every worker look silent for the length of the step at once,
        #: and report the whole fleet as stalled. The heartbeat ages in
        #: analysis.stall have to stay on the wall clock, because those are
        #: two processes comparing notes; the verdict there does not rest on
        #: them (see ``confirm``, which asks whether the beat *advanced*).
        self.activity: dict[str, float] = {}
        self.stalled: set[str] = set()
        #: The live node behind each worker id, so a pid read out of a file can
        #: be checked against the process the gateway is actually running
        #: before anybody signals it. See _live_pid.
        self.nodes: dict[str, Any] = {}
        self.tests_seen = 0
        #: How many tests each worker has been handed, which no single
        #: process but this one can say - see :mod:`..schedule`. Written
        #: into the run's directory so that a live view assembles it from
        #: files like everything else, including for a run some other
        #: session's stack server is reporting on.
        self.schedule = ScheduleTracker(self._dist_mode())
        self.lock = threading.Lock()
        self.stop = threading.Event()
        #: Set once the run summary has been raised. After that there is
        #: nobody left to tell: the terminal summary is written, a consumer's
        #: hook may have closed whatever it was writing to, and an incident
        #: arriving during interpreter shutdown is a traceback rather than a
        #: report. The watcher is joined first, so this is the backstop and
        #: not the mechanism.
        self.closed = False
        self.watcher: threading.Thread | None = None
        self.sampler: threading.Thread | None = None
        self.seen: dict[str, int] = {}
        self.raised = 0
        self.suppressed = 0
        self.run_ending = 0
        #: When this process started, stamped into the marker and kept so a
        #: rewrite of it at session finish does not report a second start.
        self.started_at = time.time()
        #: Whether this process is the one running the tests, which is so
        #: exactly when the run has no workers. Settled at session start from
        #: :attr:`recorder` and kept, because a report arriving from a worker
        #: must never be mistaken for one of ours - a phantom entry in
        #: ``activity`` is a stall watcher assessing a worker that does not
        #: exist, and reporting one.
        self.records_here = False
        #: The host's live-stack server, if this session ended up hosting it.
        #: Not an incident source - it answers questions rather than raising
        #: anything - but session start and finish are here, and giving it a
        #: plugin of its own would be two objects with one lifetime.
        self.stacks: Any = None
        # Kernel tracing observes kills without altering signal delivery.
        #: Built once, on the first death - see _kill_sources.
        self._sources: killer.Sources | None = None
        self.witness_status = "off"
        self.tracer: signal_trace.SignalTracer | None = None
        self.controller_events: EventLog | None = None
        #: Whether the witnesses' status line has been written to the
        #: controller's log. Written once the run id is authoritative - see
        #: :meth:`_announce_witnesses` - so the controller's own log agrees
        #: with the workers' about which run it belongs to.
        self.witnesses_announced = False
        #: Whether the sidecar will report this run's death itself, and if
        #: not, why - see :mod:`.reporter` and :meth:`_reporter_payload`.
        self.reporter_status = "off: failure_on_run_death is not set"
        # Do not intercept SIGTERM: masks escape into subprocesses and a Python
        # handler delays termination while the GIL is held. Kernel witnesses
        # identify senders without changing the process being observed.
        #: What the profiler found, kept for the terminal summary, which runs
        #: after session finish and is the one place a person asked to see it.
        self.profile_report: Any = None
        self.profile_incidents: list[Incident] = []

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

        The join below is only ever one component deep because
        :func:`name_this_run` has already refused anything else: a name that
        reached here as ``/tmp/x`` or ``../..`` would take the whole run's
        evidence with it, quietly, and the operator who exported it would not
        find out from anything the run printed.
        """
        return self.settings.directory / self.session_id

    @property
    def recorder(self) -> Any:
        """This process's own recorder, or None when it is not running tests.

        A run with no workers records itself: the process that would be asked
        what it was doing is this one, so the recorder is registered here
        rather than in a worker, and this is how the engine reaches it.
        Registered by :func:`..registration.install`, and absent whenever it
        could not be built - a read-only directory, a psutil that will not
        import - which is exactly the case each caller has to handle anyway.
        """
        return self.config.pluginmanager.get_plugin(RECORDER_NAME)

    def _prepare_directory(self) -> None:
        """Make this run's directory, and clear out the runs that are over.

        Made when something is actually going to write there - workers, this
        process recording its own tests, or the stack server publishing its
        address. A run with none of the three has no reason to leave an empty
        directory in somebody's repository, and a run that skips the marker
        would leave a directory nothing ever prunes.

        What is pruned is *whole directories of finished runs*, which is a much
        safer thing to delete than a list of file suffixes: a directory is only
        touched if it holds this plugin's own marker naming a process that is
        no longer running. ``failure_directory`` is a natural thing to point at
        an existing artifacts directory, and a green run that deletes somebody's
        coverage report has done more damage than any failure it might have
        explained.

        The directory is also made to ignore itself in git, under the same
        rule about whose directory it is - see
        :meth:`_keep_the_evidence_out_of_git`.
        """
        if not self.distributed and not self.settings.stack_server and not self.records_here:
            return
        self._warn_if_a_live_session_already_owns_this_directory()
        # Read before it is swept: the sweep is what removes this evidence,
        # and the two orders differ by whether a killed run is reported once
        # or never.
        self._report_runs_that_never_came_back()
        # Swept *before* this run's marker is written, not after. Written
        # first, our own directory names a live pid - so a directory left
        # behind by a finished run that shared this PYTEST_RUN_ID is skipped
        # by the very sweep that exists to remove it, and this run silently
        # inherits its worker files.
        #
        # Measured: a four-worker run followed by a one-worker run under one
        # build id left the second reporting four workers, three of them the
        # first attempt's corpses, out of a directory holding two distinct
        # xdist run ids. Sequential reuse of a directory is supported on
        # purpose; inheriting the previous attempt's evidence is not part of
        # what it was supposed to mean.
        prune_finished_runs(self.settings.directory)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # The run id is deliberately absent: reading it here would settle
            # it before xdist has one, and the id this run reports would then
            # be a name nothing else agrees with. The events files inside carry
            # it, and they are written from the start.
            self._write_marker()
        except OSError:
            return  # bookkeeping must never break a run
        # After the marker, not before: the test of whether this directory is
        # ours to write an ignore file into is that everything in it is ours,
        # and until the line above ran, our own run directory was not.
        self._keep_the_evidence_out_of_git()

    def _keep_the_evidence_out_of_git(self) -> None:
        """Ignore this directory in git, when it is ours to say so.

        The default ``failure_directory`` is a directory in the checkout, made
        by a plugin the developer did not ask to think about, and what lands in
        it is scratch: a run's own state slots, event logs and stacks, which
        the next run over the same directory deletes. Untracked, that is a
        ``git status`` full of files nobody will ever commit, and the first
        thing an unlucky ``git add -A`` commits.

        So the directory carries the ignore file rather than the repository's
        own ``.gitignore`` carrying a line about us: a directory that ignores
        itself needs no change to a file the developer maintains, and it works
        the same for the second checkout, the CI image and the colleague who
        just installed the plugin.

        *When it is ours to say so* is the whole of the care here.
        ``failure_directory`` is documented as a natural thing to point at an
        existing artifacts directory, shared with whatever else writes there -
        and dropping ``*`` into somebody else's directory would quietly stop
        git from seeing their files too, which is a change to their repository
        that this plugin has no business making. So the file is written only
        into a directory holding nothing but run directories of ours: the one
        this run just made, the ones still going, and nothing else. A directory
        with a stranger's file in it keeps whatever ignore rules it already
        had, and a ``.gitignore`` that is already there is never rewritten -
        it may be the developer's, and it says what they meant.

        That rule is what makes the worst case harmless rather than merely
        unlikely: ``failure_directory = .`` is the checkout itself, and the
        checkout is full of files that are not ours, so it gets no ignore file
        at all rather than one that hides the whole repository.
        """
        root = self.settings.directory
        try:
            ignore = root / GITIGNORE_FILE
            if ignore.exists():
                return
            if any(leftovers.marker(path) is None for path in root.iterdir()
                   if path.name != leftovers.LOCK_FILE):
                return  # something here is not ours; see the docstring
            ignore.write_text(GITIGNORE_BODY, encoding="utf-8")
        except OSError:
            return  # bookkeeping must never break a run

    def _write_marker(self, finished: bool = False) -> None:
        """Say that this directory is a run of ours, and whether it ended.

        Rewritten whole at session finish rather than appended to, because it
        is one small document and the read that matters most happens when
        nobody is left to have finished writing it. What that reader takes
        from a marker it cannot parse is "this run did not reach its end",
        which is the safe way round: a run that did reach it is still here to
        be asked, and one that did not is the case this exists for.
        """
        record: dict[str, Any] = {
            "pid": os.getpid(),
            "session_id": self.session_id,
            "started_at": self.started_at,
        }
        if finished:
            # The absence of this is what makes a directory worth reporting
            # rather than merely deleting - see :mod:`.leftovers`.
            record[leftovers.FINISHED_KEY] = time.time()
        (self.directory / OWNER_FILE).write_text(json.dumps(record), encoding="utf-8")

    def _report_runs_that_never_came_back(self) -> None:
        """Raise the incidents of runs that were killed before they could.

        Every other source is something a live process notices. This is the
        one that nothing in the run it describes was alive to notice, so it is
        found rather than reported - by the next run over the same directory,
        which was about to delete it.

        Ordered before the sweep on purpose: the sweep is what removes this
        evidence, and doing it first is the difference between a killed run
        being reported once and never. Ordered after nothing else, because a
        run reports what it finds before it has anything of its own to say.
        """
        try:
            leftovers.deliver_left_behind(
                self.settings.directory, self.directory, self._deliver_recovered,
                elevate=self.settings.elevate,
            )
        except Exception as failure:  # noqa: BLE001 - never break a starting run
            advise(f"the previous runs' evidence could not be read: {failure!r}")


    def _deliver_recovered(self, incident: Incident) -> None:
        """Let callback failure escape to recovery so its evidence remains retryable."""
        self._enrich(incident)
        self.config.hook.pytest_failure_incident(incident=incident)

    def _warn_if_a_live_session_already_owns_this_directory(self) -> None:
        """Two runs may share a directory on purpose - but not at once.

        Naming a directory with PYTEST_RUN_ID is the documented way to make
        two runs share one, and for runs that follow one another that is
        exactly what it does. Concurrently it is something else: every worker
        is gw0, so the second session's gw0.state overwrites the first's and
        owner.json names whichever wrote last. Both stay separable, because the events carry the run id on every line and
        the state slot is stamped with it too - a reader that passes the id it
        expects gets nothing back rather than the other session's answer. What
        does not survive is the file itself: the loser's slot is overwritten,
        so its evidence is gone rather than merely rejected.

        A build system that exports one PYTEST_RUN_ID for the whole job and
        then runs two suites in parallel gets this without choosing it, which
        is why it is worth saying out loud rather than leaving in the README.
        """
        marker = self.directory / OWNER_FILE
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # no marker, or not one of ours: nothing to say
        other = record.get("pid")
        if not other or int(other) == os.getpid() or not probes.is_running(int(other)):
            return  # ours, or a finished run whose directory is free to reuse
        advise(
            f"this run's evidence directory ({self.directory}) is already owned "
            f"by process {other}, which is still running: two live sessions "
            "sharing one directory overwrite each other's per-worker state. "
            "Unset PYTEST_RUN_ID, or give each session its own value, to keep "
            "them apart"
        )

    # -- raising ---------------------------------------------------------

    def raise_incident(self, incident: Incident) -> bool:
        """Enrich, dedupe and deliver. True when the hook was handed it;
        False when it was suppressed as a recurrence, or the run had closed."""
        try:
            self._enrich(incident)
        except Exception as failure:  # noqa: BLE001 - a partial incident beats none
            incident.evidence.append(f"Enrichment failed: {failure!r}.")

        # The stall watcher raises from its own thread, so the counters and
        # the dedupe table are shared state.
        with self.lock:
            if self.closed:
                return False
            count = self.seen.get(incident.fingerprint, 0) + 1
            self.seen[incident.fingerprint] = count
            if count == 1:
                self.raised += 1
                self.run_ending += bool(incident.run_ending)
            else:
                self.suppressed += 1
        if count > 1:
            return False
        try:
            self.config.hook.pytest_failure_incident(incident=incident)
        except Exception as failure:  # noqa: BLE001
            print(f"[failure-instrumentation] incident hook raised: {failure!r}", flush=True)
        return True

    def _enrich(self, incident: Incident) -> None:
        """Everything that is the same whatever kind this is - see :mod:`.enrich`,
        which the sidecar's reporter shares with this."""
        from .enrich import enrich

        enrich(incident, self.attributor, self.settings.product_version, self.run_id)

    # -- liveness --------------------------------------------------------

    def _touch(self, worker: str | None) -> None:
        """Note that a worker is alive, unless it is already known not to be.

        The exception is the whole reason this is a method. When a worker
        crashes, xdist writes the test it abandoned up as a failure and
        attributes that report to the dead node - so the last report a worker
        ever produces arrives *after* ``pytest_testnodedown`` has said it is
        gone. Re-arming its liveness clock there put a corpse back in the set
        of workers being watched, where nothing could ever remove it again,
        and every crashed worker was reported a second time as STALLED_FROZEN
        one ``failure_stall_seconds`` later - "the process is stopped", which
        was true, and useless, and counted as a second run-ending incident.
        """
        if not worker:
            return
        with self.lock:
            if worker in self.workers_down:
                return
            self.activity[worker] = time.monotonic()
            # A worker that speaks again was not wedged, or is no longer. Left
            # in the set, a worker reported once could never be reported again
            # however badly it went on to hang.
            self.stalled.discard(worker)

    def _watch_for_stalls(self) -> None:
        """Poll, because a wedged worker fires no hook at all.

        Every other source is something pytest tells us. This one is the
        absence of anything being said, which nothing can deliver.
        """
        limit = self.settings.stall_seconds
        while not self.stop.wait(min(limit / 4, 15.0)):
            now = time.monotonic()
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
                    from . import stall

                    with self.lock:
                        self.stalled.add(worker)
                    self.raise_incident(
                        stall.WorkerStallIncident.degraded(worker, failure)
                    )

    def _assess_stall(self, worker: str, silent_for: float) -> None:
        from . import stall

        incident = stall.build(
            worker,
            self.directory,
            silent_for,
            self.settings.heartbeat_interval,
            self.settings.stack_probe,
            run_id=self.run_id,
            live_pid=self._live_pid(worker),
            cancel=self.stop,
        )
        if incident is None:
            # Slow, not stuck - or the run ended under us. Re-arm rather than
            # asking again immediately.
            self._touch(worker)
            return
        with self.lock:
            self.stalled.add(worker)
        self.raise_incident(incident)

    def _live_pid(self, worker: str) -> int | None:
        """The pid this worker's gateway is running, if it still is.

        The pid in the state file is what the worker wrote about itself, and a
        file is not a process: by the time a stall is assessed that worker may
        have exited and the kernel handed its number to something unrelated.
        SIGUSR1's default disposition is to terminate, so a stack probe aimed
        at a recycled pid kills a stranger's process rather than producing a
        bad report. This is the controller's own answer to "what is running
        there", and it is what licenses the signal.

        None is "cannot say", not "no": a gateway that is not a local
        subprocess - ssh, socket - has no pid here to compare against, and the
        caller falls back to asking the machine instead.
        """
        with self.lock:
            node = self.nodes.get(worker)
        popen = getattr(getattr(getattr(node, "gateway", None), "_io", None), "popen", None)
        if popen is None:
            return None
        try:
            if popen.poll() is not None:
                return None  # it has already exited; its pid is anybody's now
            return int(popen.pid)
        except Exception:  # noqa: BLE001 - nothing here is worth a failed run
            return None

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
        # First, because everything below asks it. The recorder is registered
        # alongside this object at configure time and nothing can add one
        # later, so one read settles it for the run.
        self.records_here = self.recorder is not None
        self._prepare_directory()
        self._start_kill_witnesses()

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
                # Supplied by whoever started the run, never minted here, and
                # never written down - see the stack server's module docstring
                # for why the address is published and the secret is not.
                self.settings.stack_server_token,
                # Whether ?locals may be answered. A frame's variables are the
                # data a test is working on, so this is the one thing a server
                # serves that a deployment might want withheld from a reader it
                # otherwise trusts with the frames.
                self.settings.stack_server_locals,
            )

        if self.settings.sample_seconds > 0 and not self._anything_records():
            # The sampler reads state files, so a run where nothing writes one
            # would poll an empty directory for its whole length and push
            # nothing - which from the outside is indistinguishable from a
            # product whose hook is never called. Say so instead.
            #
            # That used to be every run without xdist, and is now only a run
            # whose recorder could not be built: the process running the tests
            # records itself, so a plain pytest samples one worker called
            # "main" the same way a distributed one samples sixty-four.
            advise(
                "failure_sample_seconds is set but nothing in this run is "
                "recording what it does, so there is nothing to sample and no "
                "samples will be pushed"
            )
        elif self.settings.sample_seconds > 0:
            self.sampler = threading.Thread(
                target=self._sample_workers,
                name="failure-instrumentation-sample",
                daemon=True,
            )
            self.sampler.start()

        if self._anything_records() and self.settings.stall_seconds > 0:
            # A run with no workers watches itself, and can: the watcher is a
            # thread, and a main thread blocked on a lock or a socket does not
            # stop the others running. So a plain pytest that wedges says so
            # while it is still wedged - which is the whole value of the kind,
            # and was previously the one case that produced nothing at all
            # because the run simply never ended.
            #
            # The exception is the process frozen by native code holding the
            # GIL. Nothing in Python runs there, this thread included, so
            # nothing is reported now; the fallback timer's dump is what is
            # left, for whoever reads the directory afterwards.
            if self.records_here:
                # Before the first test, so a run whose *first* test hangs is
                # watched. The clock has to be running before there is
                # anything to report against it.
                self._touch(SOLE_WORKER)
            self.watcher = threading.Thread(
                target=self._watch_for_stalls,
                name="failure-instrumentation-stall",
                daemon=True,
            )
            self.watcher.start()

    # -- who killed it -----------------------------------------------------

    def _start_kill_witnesses(self) -> None:
        """Start what will say who kills a process of this run.

        Kernel tracing never changes signal masks or handlers. Unavailable
        tracing is recorded on incidents rather than affecting cancellation.
        """
        try:
            self._start_kill_witnesses_or_fail()
        except Exception as failure:  # noqa: BLE001 - see the docstring
            self.witness_status = f"off: failed ({failure!r})"

    def _start_kill_witnesses_or_fail(self) -> None:
        if not self.settings.kill_trace:
            self.witness_status = "off: failure_kill_trace is off"
            # No witnesses - but a reporter, if one is configured, is a
            # separate promise, and needs only a sidecar that watches.
            self._start_reporter_only()
            return
        if not self._anything_records() or not self.directory.is_dir():
            self.witness_status = "off: nothing in this run records, so there is nowhere to write"
            return
        try:
            self.controller_events = EventLog(self.directory / CONTROLLER_EVENTS)
        except OSError as failure:
            self.controller_events = None
            self.witness_status = f"off: the controller's log could not be opened ({failure!r})"
        if self.controller_events is not None:
            self.witness_status = (
                "off: signal delivery is preserved; sender attribution requires kernel tracing"
            )
        payload = self._reporter_payload()
        self.tracer = signal_trace.SignalTracer(
            self.directory / signal_trace.TRACE_FILE,
            elevate=self.settings.elevate,
            reporter=payload,
        )
        self.tracer.start()
        if payload is not None:
            self.reporter_status = (
                "armed" if self.tracer.active else f"off: the sidecar is not running ({self.tracer.how})"
            )
        if not self.distributed:
            # No xdist id is ever coming, so this process's own name is the
            # run id already; a distributed run announces from
            # pytest_configure_node, once xdist has minted the real one.
            self._announce_witnesses()

    def _announce_witnesses(self) -> None:
        """One line in the controller's log saying what was witnessing.

        Written once, and not before the run id is authoritative: at session
        start a distributed run's id is still this process's stand-in, and a
        line stamped with it makes the controller's log disagree with every
        worker's about which run they all belong to - which is precisely the
        confusion the id exists to prevent.
        """
        if self.witnesses_announced or self.controller_events is None:
            return
        self.witnesses_announced = True
        try:
            self._record(
                "kill_witnesses",
                controller_witness=self.witness_status,
                signal_trace=self.tracer.how if self.tracer is not None else "off: not started",
                reporter=self.reporter_status,
                elevate=self.settings.elevate,
            )
        except Exception:  # noqa: BLE001 - a line that cannot be written
            pass  # is not a run that cannot proceed

    def _start_reporter_only(self) -> None:
        payload = self._reporter_payload()
        if payload is None or not self._anything_records() or not self.directory.is_dir():
            return
        self.tracer = signal_trace.SignalTracer(
            self.directory / signal_trace.TRACE_FILE, reporter=payload, trace=False
        )
        self.tracer.start()
        self.reporter_status = (
            "armed" if self.tracer.active else f"off: the sidecar is not running ({self.tracer.how})"
        )
        if not self.distributed:
            self._announce_witnesses()  # a distributed run announces from configure_node

    def _reporter_payload(self) -> dict[str, Any] | None:
        """What the sidecar needs to report this run's death without us.

        The callable as a pickle or a dotted path, this process's
        environment and import path, where the evidence is, and the settings
        the incident is enriched with - see :mod:`.reporter`. Built once at
        session start, so a callable that cannot travel is said here, while
        somebody is listening, rather than discovered after the run is dead.
        """
        target = self.settings.on_run_death
        if not target:
            return None
        try:
            spec = reporter.describe_callable(target)
        except Exception as failure:  # noqa: BLE001 - a pickle that fails is a warning
            self.reporter_status = f"off: failure_on_run_death cannot travel to the sidecar ({failure!r})"
            advise(
                f"failure_on_run_death={target!r} cannot be handed to the sidecar: "
                f"{failure!r}. It has to be a module-level function, or a "
                "functools.partial of one with picklable arguments, or a dotted "
                "path 'package.module:attribute'; a killed run will not be reported"
            )
            return None
        rootdir = getattr(self.config, "rootpath", None) or getattr(self.config, "rootdir", None)
        return {
            "callable": spec,
            "env": dict(os.environ),
            "rootdir": str(rootdir) if rootdir else None,
            "sys_path": list(sys.path),
            "python": sys.executable,
            "directory": str(self.directory),
            "session": self.session_id,
            "controller_pid": os.getpid(),
            "packages": list(self.settings.packages),
            "product_version": self.settings.product_version,
            "elevate": self.settings.elevate,
        }

    def _stop_kill_witnesses(self) -> None:
        for step in (
            lambda: self.tracer.stop() if self.tracer is not None else None,
            lambda: self.controller_events.close() if self.controller_events is not None else None,
        ):
            try:
                step()
            except Exception:  # noqa: BLE001 - session finish must not fail here
                continue

    def _record(self, event: str, **fields: Any) -> None:
        """A line in the controller's own log, stamped with the run id as it
        is known *now* - which at configure time it was not."""
        log = self.controller_events
        if log is None:
            return
        try:
            log.run_id = self.run_id
        except Exception:  # noqa: BLE001 - an id that cannot be settled yet
            pass  # leaves the line unstamped rather than unwritten
        log.record(event, **fields)

    def _kill_sources(self) -> killer.Sources:
        """One object for the whole run, not one per death.

        It carries the kernel-log reading between deaths - see
        ``killer.Sources.kernel_log_reading`` - and deaths arrive together
        precisely when that reading is expensive and identical: an OOM kill
        takes one worker and then the next. Everything on it is settled by
        the time the first death can happen; the pids are read afresh on each
        call, through the callable.
        """
        if self._sources is None:
            from . import killer

            self._sources = killer.Sources(
                directory=self.directory,
                elevate=self.settings.elevate,
                trace_status=self.tracer.how if self.tracer is not None else "off: not started",
                witness_status=self.witness_status,
                run_pids=lambda: {
                    os.getpid(): killer.CONTROLLER,
                    **killer.roles_in(self.directory),
                },
            )
        return self._sources

    def _anything_records(self) -> bool:
        """Whether any process in this run is writing down what it is doing.

        The workers, when there are workers. This process, when there are not.
        Neither, when the recorder could not be built at all - a read-only
        evidence directory, a psutil that will not import - which is warned
        about where it happens and is the one case with nothing to read.
        """
        return self.distributed or self.records_here

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

        sampler = WorkerSampler(self.directory, session_id=self.session_id)
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
        # By now xdist has built its node manager, so the id is the real one -
        # which is what makes this the moment for the controller's own log to
        # start, not session start.
        self._announce_witnesses()
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
        worker = getattr(getattr(node, "gateway", None), "id", None)
        if worker:
            with self.lock:
                self.nodes[worker] = node
        self._touch(worker)

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        """Where the schedule is written down, once a test is starting.

        The start of a test rather than the end of one, on purpose. A worker
        reports a finished test to the controller *before* it tells the
        scheduler, and in between the test is counted as finished and still
        outstanding both - so a record written from the end of a test would
        land inside that window every time, and the total would read one high
        every time. At the start of a test that worker is never inside it.

        It matters that this is the *first* one too: xdist hands the work out
        inside the same call that fires
        ``pytest_xdist_node_collection_finished`` and *after* that hook, so
        the write from there is taken before anything has been assigned, and
        without this the totals would read zero until the first test finished.
        """
        self._record_schedule()

    def pytest_runtest_logreport(self, report: Any) -> None:
        node = getattr(report, "node", None)
        if node is None:
            # No node means the report was produced here rather than relayed
            # from a worker, which in a run this process is recording means
            # this process is the one still going. It is the same signal a
            # worker's report is: something completed, so the silence clock
            # starts again. There is no schedule to keep either way: the
            # counts below come from xdist's scheduler, and a run with no
            # workers has none.
            if self.records_here:
                self._touch(SOLE_WORKER)
            return
        self.tests_seen += 1
        worker = worker_of(node)
        self._touch(worker)
        # Teardown is once per test whatever happened in it - a test whose
        # setup failed still has one, and a test that passed has no other
        # phase that is guaranteed. This is the whole per-test cost of the
        # schedule: what a worker still owes is read off the scheduler when
        # the record is written, and the two added together are its total.
        # Once per *attempt*, strictly: a rerun plugin tears the same test
        # down again, and the node id is what lets the tracker tell that from
        # the next test - see ScheduleTracker.saw_a_test_finish.
        if getattr(report, "when", None) == "teardown":
            self.schedule.saw_a_test_finish(worker, getattr(report, "nodeid", None))

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: Any) -> None:
        worker = getattr(getattr(node, "gateway", None), "id", "unknown")
        self._touch(worker)
        # A worker registering a collection once tests are already running is a
        # replacement for one that died. xdist drops it silently if what it
        # collected differs, so the run continues a worker short.
        self.collections.record(worker, list(ids), replacement=self.tests_seen > 0)
        self._report_mismatch(partial=False)
        # Which workers exist, which is the first thing about a run that can
        # be said at all. Not yet how much each has: xdist schedules inside
        # this same call and after this hook, so what is written here is a
        # collection and no assignment - the first test starting is where the
        # totals arrive, and pytest_runtest_logstart forces that one too.
        self._record_schedule()

    # -- how much work each worker has -----------------------------------

    def _dist_mode(self) -> str:
        """What ``--dist`` was asked for, or "" if nothing was.

        Read once at construction. It is reported rather than acted on, with
        one exception: ``worksteal`` is the only mode that moves work between
        workers, and a total that can still shrink is a different promise from
        one that cannot - see :meth:`..schedule.ScheduleTracker._settled`.
        """
        try:
            return str(self.config.getoption("dist", "") or "")
        except (ValueError, AttributeError):
            return ""

    def _scheduler(self) -> Any:
        """xdist's scheduler, or None when there is not one.

        There is not one on a single-process run, and there is not one yet
        before the workers have collected - both of which are ordinary, and
        neither of which is worth a line of output. The first is settled
        without a lookup, because this is asked once per test and a run with
        no workers is the one that can least afford to be asked anything.
        """
        if not self.distributed:
            return None
        session = self.config.pluginmanager.getplugin("dsession")
        return getattr(session, "sched", None)

    def _record_schedule(self) -> None:
        """Publish where the scheduler has got to.

        Every time, on every test. It is one small write at a fixed offset -
        see :mod:`..schedule` for why a timer here made a worker's row able to
        say it had finished more tests than it was given.
        """
        scheduler = self._scheduler()
        if scheduler is None:
            return
        try:
            self.schedule.write(scheduler, self.directory, self.run_id)
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a run
            pass

    # -- sources, continued ----------------------------------------------

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
        from . import collection

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
            self.nodes.pop(worker, None)
            # Final, and it has to be: the report xdist writes for the test
            # this worker abandoned is still to come, and it names this node.
            self.workers_down.add(worker)
            if worker not in self.collections.digest_by_worker:
                self.workers_lost.add(worker)
        # One fewer collection to wait for, which may be the one that was
        # holding a mismatch back.
        self._report_mismatch(partial=False)
        # The last reading anybody gets of this worker, and it is an accurate
        # one: xdist fires this hook *before* it takes the node out of the
        # scheduler, so the queue is still there to be read. Forced, and
        # before the incident below - what a worker was still owed when it
        # died is the interesting half of a death.
        self._record_schedule()
        if not error:
            return  # a clean shutdown is not an incident
        from . import death

        try:
            incident: Incident = death.build(
                node,
                error,
                self.directory,
                self.baseline_oom_kills,
                self.run_id,
                sources=self._kill_sources(),
            )
        except Exception as failure:  # noqa: BLE001
            incident = death.WorkerDeathIncident.degraded(
                worker, failure, context=f"xdist reported: {error}"
            )
        self.raise_incident(incident)

    def pytest_internalerror(self, excrepr: object) -> None:
        from . import internal_error

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

    # -- the profile ---------------------------------------------------------

    def _report_profile(self) -> None:
        """Fold every worker's profile records together and raise what crosses
        a threshold. See :mod:`..profile.analysis` for the rules."""
        from ..profile import analysis
        from ..profile.sampler import TEST_RECORD, read_profile_log
        from . import profile as profile_incident

        records = []
        for path in sorted(self.directory.glob("*.profile.jsonl")):
            records.extend(read_profile_log(path))
        if not records:
            self.profile_report = None
            return
        thresholds = analysis.Thresholds(
            cpu_share_percent=self.settings.profile_cpu_share,
            cpu_floor_seconds=self.settings.profile_cpu_floor_seconds,
            retained_mb=self.settings.profile_retained_mb,
            peak_mb=self.settings.profile_peak_mb,
            burst_cores=self.settings.profile_burst_cores,
            burst_seconds=self.settings.profile_burst_seconds,
        )
        report = analysis.analyse(records, self.attributor, thresholds)
        self.profile_report = report
        worker = "controller" if self.distributed else SOLE_WORKER
        for finding in report.findings:
            incident = profile_incident.build(finding, worker)
            # Enriched in place by raise_incident, so the terminal prints the
            # owner and severity the hook was handed - and only what the hook
            # was handed: two parametrisations with one fingerprint are one
            # finding to the hook and the run summary, so one here too.
            if self.raise_incident(incident):
                self.profile_incidents.append(incident)

        # A flame graph for every test a finding names, and for the gaps
        # between tests, so the flag comes with the picture behind it. With
        # allocation tracing on, a test that climbed also gets one of its
        # live allocations at the peak, weighted in bytes.
        named = {nodeid for finding in report.findings for nodeid in finding.tests}
        named.update(finding.nodeid for finding in report.findings if finding.nodeid)
        wanted = [
            record
            for record in records
            if record.get("record") != TEST_RECORD
            or record.get("nodeid") in named
            or record.get("memory_stacks")
        ]
        if not wanted:
            return
        folder = self.directory / "profiles"
        folder.mkdir(exist_ok=True)
        for record in wanted:
            nodeid = record.get("nodeid") or f"background-{record.get('worker') or 'main'}"
            # Readable, and unique: sanitising alone maps test_x[a/b] and
            # test_x[a_b] to one name, and the second would overwrite the
            # first. A hash of the full name and the worker tells them apart.
            digest = hashlib.sha1(f"{record.get('worker')}|{nodeid}".encode()).hexdigest()[:8]
            name = f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', str(nodeid))[:110]}-{digest}"
            documents = {
                f"{name}.speedscope.json": analysis.speedscope(record, str(nodeid)),
                f"{name}.memory.speedscope.json": analysis.memory_speedscope(record, str(nodeid)),
            }
            for filename, document in documents.items():
                if document is None:
                    continue
                try:
                    (folder / filename).write_text(json.dumps(document), encoding="utf-8")
                except OSError:
                    continue

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        """What the profiler found, where a reader is already looking.

        Every other incident reaches a reader through the hook and the
        evidence directory. A profile is asked for by somebody at a terminal,
        and it is the one report that says nothing when nothing is wrong -
        which is worth a line, since silence otherwise reads as "did not run".
        """
        report = getattr(self, "profile_report", None)
        if not self.settings.profile:
            return
        write = terminalreporter.write_line
        terminalreporter.section("failure-instrumentation profile", sep="=")
        if report is None:
            write("No profile records were written: nothing in this run was sampling.")
            return
        cores = report.process_cpu_s / report.wall_s if report.wall_s else 0.0
        several = len(report.workers) > 1

        def seconds(value: float) -> str:
            return f"{value:.1f} s" if value < 10 else f"{value:.0f} s"

        write(
            f"Profile: {report.tests} test{'s' if report.tests != 1 else ''}, {seconds(report.wall_s)} of "
            f"{'worker time (summed across workers)' if several else 'wall time'}, "
            f"{seconds(report.process_cpu_s)} CPU ({cores:.2f} cores on average)"
            + (f", {report.gc_s:.1f} s of it in garbage collection" if report.gc_s >= 0.1 else "")
            + (f", {report.native_cpu_s:.1f} s of it in threads with no Python stack" if report.native_cpu_s >= 0.1 else "")
        )
        if not report.cpu_weighted:
            write("  CPU could not be read at all on this platform: samples are weighted by wall time instead.")
        elif not report.per_thread:
            write(
                "  CPU could not be read per thread on this platform: all CPU is attributed to the "
                "thread running the test."
            )
        if report.allocations:
            write(
                "  Allocation tracing was on: CPU figures include the tracer's cost, so no CPU "
                "findings are raised."
            )
        for worker, facts in sorted(report.workers.items()):
            peak = f"peak {facts['peak_mb']} MB" if facts["peak_mb"] is not None else "peak unknown"
            end = f", {facts['end_mb']} MB at the end" if facts["end_mb"] is not None else ""
            write(
                f"  worker {worker}: {facts['tests']} test{'s' if facts['tests'] != 1 else ''}, "
                f"{seconds(facts['cpu_s'])} CPU, {peak}{end}"
            )
        # With tracing on the table is the tracer's own cost, not the tests'.
        if report.functions and not report.allocations:
            write("Functions using the most CPU:")
            total = report.sampled_cpu_s + report.native_cpu_s
            for cost in report.functions[:8]:
                share = 100.0 * (cost.cpu_ns / 1e9) / total if total else 0.0
                count = len(cost.tests)
                where = f"in {count} test{'s' if count != 1 else ''}"
                if cost.gap_cpu_ns:
                    where = f"{where} and between tests" if cost.tests else "between tests"
                write(
                    f"  {share:5.1f}%  {cost.cpu_ns / 1e9:6.2f} s  {cost.function}  {Path(cost.file).name}"
                    f"  [{cost.owner}]  {where}"
                )
        findings = list(self.profile_incidents)
        if not findings:
            write("No findings: nothing crossed the thresholds.")
            return
        write(f"{len(findings)} finding{'s' if len(findings) != 1 else ''}:")
        for incident in findings:
            write("")
            write(str(incident))

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # Every wait the watcher can be inside is against this event, so it
        # returns within a poll of here rather than waiting out an interval it
        # would only throw away. The join is generous because what it is
        # preventing is an incident raised *after* the run summary - into a
        # consumer that has finished writing, or into interpreter shutdown.
        self.stop.set()
        if self.watcher is not None:
            self.watcher.join(timeout=WATCHER_JOIN_SECONDS)
        if self.sampler is not None:
            # Bounded like the watcher: a sample hook that will not return
            # must not be what keeps a finished run from exiting.
            self.sampler.join(timeout=2.0)
        # A stack service that never got the port reports that from its own
        # thread, and establishing it takes an identify round-trip - which on a
        # short run lands right about here. Winding it down now lets a verdict
        # already in flight arrive while the summary can still count it; the
        # guard at the end of this method would otherwise drop it, and the run
        # would be told nothing at all about why it has no live view. Only when
        # it is not serving: one that is has no verdict pending and is
        # deliberately left up for the whole teardown.
        if self.stacks is not None and not getattr(self.stacks, "serving", False):
            self.stacks.stop()
        # A worker that died still owing a collection means the full set never
        # arrives. Report what was seen rather than nothing at all, flagged as
        # incomplete so the worker counts are not read as the whole picture.
        self._report_mismatch(partial=True)
        # Before the summary, so the summary counts what the profiler raised.
        # The workers' records are on disk by now: a worker writes its last
        # one in its own session finish, which xdist waits for before this.
        if self.settings.profile:
            try:
                self._report_profile()
            except Exception as failure:  # noqa: BLE001 - a lost profile beats a lost run
                print(f"[failure-instrumentation] profile analysis failed: {failure!r}", flush=True)
        # With no receivers, a summary has no observable work to do. Keep
        # model validation imports off clean recording-only sessions.
        if self.config.hook.pytest_failure_incident.get_hookimpls():
            from . import summary

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
        self.schedule.close()
        # After the summary: nothing of this run's dies after it that anybody
        # would report, and a witness still up while the summary was being
        # raised is a witness for a death the summary could still count.
        self._stop_kill_witnesses()
        # Said after the summary rather than before it, and it is the last
        # thing this run does that a later one can read: from here on, this
        # directory says it reached its end and has nothing left to report.
        try:
            self._write_marker(finished=True)
        except OSError:
            pass  # the marker is bookkeeping, and this run is over anyway
        # The summary is the last word by definition - it says how many
        # incidents this run raised. Anything the watcher or the sampler still
        # manages to produce after it would contradict a number already
        # reported.
        with self.lock:
            self.closed = True
        # Last, so that a UI watching a long teardown keeps its answers for as
        # long as this session exists. Whoever is waiting for the port takes it
        # over within a few seconds of this returning.
        if self.stacks is not None:
            self.stacks.stop()
