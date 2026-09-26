"""What this run finds, as OpenTelemetry metrics.

An incident is a record, and ``pytest_failure_incident`` is where a product
stores it. What a record cannot give anybody is a *rate* across a fleet - how
many workers were killed this week, whose tests they were, whether a runner
pool's stalls are rising - and a product that already has a metrics pipeline
should not have to rebuild one from rows. So with ``failure_otel`` on, the
controller reports:

========================================  ==========================  =============================================
``pytest.failure.incidents``              counter, ``{incident}``      kind, verdict, owner, severity, run_ending, tags
``pytest.failure.runs``                   counter, ``{run}``           exit status, distributed
``pytest.failure.workers``                up-down counter             status
``pytest.failure.worker.tests``           up-down counter, ``{test}``  worker, state (finished, running, queued)
``pytest.failure.run.tests.unassigned``   up-down counter, ``{test}``  -
``pytest.failure.worker.memory.rss``      gauge, ``By``                worker, tags
``pytest.failure.worker.cpu.rate``        gauge, ``{cpu}``             worker, tags
``pytest.failure.worker.heartbeat.age``   gauge, ``s``                 worker, tags
========================================  ==========================  =============================================

Attributes are named ``pytest.failure.*``, and a tag - see
:mod:`.attributes` - is ``test.tag.<key>``, apart from them so that a tag
called ``owner`` cannot overwrite the finding of the same name. None of them
is a node id, a fingerprint, a run id or a pid: each of those is a new series
per test or per run, which is a table and not a metric. They are in the
incident, and ``OTEL_RESOURCE_ATTRIBUTES`` is where a build id belongs.

**Every instrument is observable**, including the two counters. A counter's
``add`` made before a provider exists is dropped without a word, and one of
these is made early: the incidents of a killed run are recovered in
``pytest_sessionstart``, before any conftest below the rootdir has been loaded
to set a provider. So a hook only adds to a tally, and the reader collects the
tally whenever it collects - a provider that arrives late loses nothing. The
instruments are made at configure time, from the global meter provider as it
stands; OpenTelemetry's API stands in for one that has not been set yet and
hands every instrument to it once it is.

**The controller only.** Incidents are raised there, and a worker never
imports OpenTelemetry. A provider set in a conftest *below* the rootdir is
never loaded by an xdist controller at all - only its workers collect - so
this says so at the end of a run whose metrics went nowhere, rather than
leaving an empty dashboard to be misread as a quiet week.

**Never the provider's owner.** It is not set here and not shut down here; a
product that set it shuts it down, in ``pytest_unconfigure`` or at exit. What
this does do is flush it as the session finishes, bounded by
:data:`FLUSH_MILLIS`, because a periodic reader's first export is a minute
away and most runs are over before that.

**Killed runs** are the limit. A metric is held in memory until it is
exported, so a controller killed outright takes what it had not exported with
it - the one failure this package exists for. Its incidents still reach the
next run over the same directory, or the sidecar's reporter, and the next run
counts them when it recovers them. The metrics are a dashboard; the incidents
are the record.
"""

from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Optional

import pytest

from .config import Settings, advise

METER = "pytest_failure_instrumentation"
#: How long the session may wait on the provider's exporters as it finishes.
FLUSH_MILLIS = 5000
#: One read of the run's files serves every gauge of one collection: the
#: reader calls each instrument's callback in turn, and each would otherwise
#: walk the directory again for the same answer.
SAMPLE_REUSE_SECONDS = 0.5
#: The prefix a tag's key is given, so it can never be read as a finding.
TAG_PREFIX = "test.tag."
PREFIX = "pytest.failure."


def _observed(callback: Callable[[MetricsAdapter, Any], Iterable[Any]]) -> Callable[..., list[Any]]:
    """A callback that answers nothing, rather than raising, when it fails."""

    @functools.wraps(callback)
    def guarded(self: MetricsAdapter, options: Any) -> list[Any]:
        try:
            return list(callback(self, options))
        except Exception:  # noqa: BLE001 - see "what the reader collects" below
            return []

    return guarded


class MetricsAdapter:
    """Registered on the controller beside the engine, which it reads.

    Built only where ``failure_otel`` is on, and only once OpenTelemetry has
    imported - an ImportError out of the constructor is the caller's to
    report, because it is the one failure with a fix to name.
    """

    def __init__(self, engine: Any, settings: Settings) -> None:
        from opentelemetry import metrics
        from opentelemetry.metrics import Observation

        from . import __version__

        self._observation = Observation
        self._metrics = metrics
        self.engine = engine
        self.provider = settings.otel_meter_provider
        self._lock = threading.Lock()
        self._incidents: dict[tuple[tuple[str, Any], ...], int] = {}
        self._runs: dict[tuple[tuple[str, Any], ...], int] = {}
        self._sampler: Any = None
        self._sample: Any = None
        self._sampled_at = 0.0

        meter = (self.provider or metrics).get_meter(METER, __version__)
        meter.create_observable_counter(
            PREFIX + "incidents", callbacks=[self._count_incidents], unit="{incident}",
            description="Incidents raised, after duplicates of one fingerprint are suppressed.",
        )
        meter.create_observable_counter(
            PREFIX + "runs", callbacks=[self._count_runs], unit="{run}",
            description="Runs that reached their run summary.",
        )
        meter.create_observable_up_down_counter(
            PREFIX + "workers", callbacks=[self._statuses], unit="{worker}",
            description="Workers by status: working, blocked, frozen, gone, unmeasured, finished.",
        )
        meter.create_observable_up_down_counter(
            PREFIX + "worker.tests", callbacks=[self._tests], unit="{test}",
            description="Each worker's tests: finished, running and queued.",
        )
        meter.create_observable_up_down_counter(
            PREFIX + "run.tests.unassigned", callbacks=[self._unassigned], unit="{test}",
            description="Collected tests no worker has been given yet.",
        )
        meter.create_observable_gauge(
            PREFIX + "worker.memory.rss", callbacks=[self._rss], unit="By",
            description="Each live worker's resident memory at its last heartbeat.",
        )
        meter.create_observable_gauge(
            PREFIX + "worker.cpu.rate", callbacks=[self._cpu], unit="{cpu}",
            description="Cores each live worker burned between its last heartbeats.",
        )
        meter.create_observable_gauge(
            PREFIX + "worker.heartbeat.age", callbacks=[self._heartbeat], unit="s",
            description="Seconds since each live worker last beat.",
        )

    # -- what the run tells it --------------------------------------------

    def pytest_failure_incident(self, incident: Any) -> None:
        """Counted, never exported from here - see the module docstring.

        Never raises: this shares one hook call with the product's own
        implementation, and pluggy stops calling the rest at the first one
        that raises - so a failure here would cost the product its incident.
        """
        try:
            self._count(incident)
        except Exception as failure:  # noqa: BLE001 - see the docstring
            print(f"[failure-instrumentation] could not count an incident: {failure!r}", flush=True)

    def _count(self, incident: Any) -> None:
        if incident.kind == "run_summary":
            bucket = self._runs
            key = _key({
                PREFIX + "exit_status": int(getattr(incident, "exitstatus", 0) or 0),
                PREFIX + "distributed": bool(getattr(incident, "distributed", False)),
            })
        else:
            bucket = self._incidents
            key = _key({
                PREFIX + "kind": incident.kind,
                PREFIX + "verdict": incident.verdict,
                PREFIX + "owner": incident.owner,
                PREFIX + "severity": incident.severity,
                PREFIX + "run_ending": bool(incident.run_ending),
                **_tags(getattr(incident, "test_attributes", None)),
            })
        with self._lock:
            bucket[key] = bucket.get(key, 0) + 1

    def pytest_terminal_summary(self) -> None:
        """Said here, where the warnings summary still has it to show: by the
        time the session has finished, the provider this run was going to have
        has long been set - or never will be."""
        try:
            unset = self._flushable() is None
        except Exception:  # noqa: BLE001 - never break a run
            return
        if unset:
            advise(
                "failure_otel is on, but no OpenTelemetry MeterProvider that can "
                "export was set in this process, so this run's metrics went "
                "nowhere. Set one in a pytest_configure of the rootdir conftest or "
                "of a plugin - under xdist, the controller reports them and never "
                "loads a conftest below the rootdir - or pass one as "
                "install(config, otel_meter_provider=...)",
            )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_sessionfinish(self) -> Any:
        """Flush once everything else has finished: the engine raises the run
        summary in its own session finish, and this is after all of them."""
        yield
        try:
            flush = self._flushable()
            if flush is not None:
                flush(timeout_millis=FLUSH_MILLIS)
        except Exception as failure:  # noqa: BLE001 - never break a run
            print(f"[failure-instrumentation] metrics flush failed: {failure!r}", flush=True)

    def _flushable(self) -> Any:
        """The provider's ``force_flush``, or None where nothing can export.

        The SDK's provider has one; the API's stand-in for a provider not yet
        set, and its no-op, do not.
        """
        provider = self.provider or self._metrics.get_meter_provider()
        return getattr(provider, "force_flush", None)

    # -- what the reader collects -----------------------------------------
    #
    # Each runs on the reader's thread and must never raise into it: the SDK
    # would log and carry on, but a callback that fails on every collection
    # is a log line every interval for the rest of the run. See _observed.

    @_observed
    def _count_incidents(self, options: Any) -> Iterable[Any]:
        with self._lock:
            counted = list(self._incidents.items())
        return [self._observation(count, dict(key)) for key, count in counted]

    @_observed
    def _count_runs(self, options: Any) -> Iterable[Any]:
        with self._lock:
            counted = list(self._runs.items())
        return [self._observation(count, dict(key)) for key, count in counted]

    @_observed
    def _statuses(self, options: Any) -> Iterable[Any]:
        sample = self._read()
        if sample is None:
            return []
        statuses: dict[str, int] = {}
        for row in sample.workers:
            if row.status:
                statuses[row.status] = statuses.get(row.status, 0) + 1
        return [
            self._observation(count, {PREFIX + "worker.status": status})
            for status, count in sorted(statuses.items())
        ]

    @_observed
    def _tests(self, options: Any) -> Iterable[Any]:
        sample = self._read()
        if sample is None:
            return []
        found = []
        for row in sample.workers:
            for state, count in (
                ("finished", row.tests_finished),
                ("running", row.tests_running),
                ("queued", row.tests_queued),
            ):
                if count is not None:
                    found.append(self._observation(
                        count, {PREFIX + "worker": row.worker, PREFIX + "test.state": state}
                    ))
        return found

    @_observed
    def _unassigned(self, options: Any) -> Iterable[Any]:
        sample = self._read()
        if sample is None or sample.unassigned is None:
            return []
        return [self._observation(sample.unassigned)]

    @_observed
    def _rss(self, options: Any) -> Iterable[Any]:
        return self._per_live_worker(lambda row: None if row.rss_mb is None else row.rss_mb * 1024 * 1024)

    @_observed
    def _cpu(self, options: Any) -> Iterable[Any]:
        return self._per_live_worker(lambda row: row.cpu_rate)

    @_observed
    def _heartbeat(self, options: Any) -> Iterable[Any]:
        return self._per_live_worker(lambda row: row.heartbeat_age_s)

    def _per_live_worker(self, value: Any) -> Iterable[Any]:
        """One observation per worker whose process is still there.

        A ``gone`` worker's row keeps the figures of its last beat, and xdist
        replaces it under a new name - so reporting it would hold a dead
        process's memory on a dashboard for the rest of the run.
        """
        sample = self._read()
        if sample is None:
            return []
        found = []
        for row in sample.workers:
            measured = value(row)
            if measured is None or row.status == "gone":
                continue
            found.append(self._observation(
                measured, {PREFIX + "worker": row.worker, **_tags(row.test_attributes)}
            ))
        return found

    def _read(self) -> Any:
        """This run's workers now, or None once the session is finishing.

        The engine's stop event is the session finishing: after it, a row is
        a worker xdist is tearing down, and nothing is reported rather than
        figures from processes that are going away.
        """
        if self.engine.stop.is_set():
            return None
        now = time.monotonic()
        with self._lock:
            if self._sample is not None and now - self._sampled_at < SAMPLE_REUSE_SECONDS:
                return self._sample
        try:
            if self._sampler is None:
                from .sampling import WorkerSampler

                self._sampler = WorkerSampler(
                    self.engine.directory, session_id=self.engine.session_id
                )
            sample = self._sampler.sample()
        except Exception as failure:  # noqa: BLE001 - never break a run
            print(f"[failure-instrumentation] could not sample for metrics: {failure!r}", flush=True)
            return None
        with self._lock:
            self._sample, self._sampled_at = sample, now
        return sample


def _tags(values: Optional[Mapping[str, str]]) -> dict[str, str]:
    return {TAG_PREFIX + key: value for key, value in (values or {}).items()}


def _key(attributes: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(attributes.items()))
