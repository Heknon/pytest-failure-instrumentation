"""A small pytest plugin that ships one elastic case report per test phase.

    pytest -p elastic_reporter --elastic-url=https://reports.example/case-reports

Standalone: a single module, no package, no dependency beyond pytest. Drop it
next to your ``conftest.py`` and load it with ``-p elastic_reporter``, or put
``pytest_plugins = ["elastic_reporter"]`` in the rootdir conftest.

What it does
------------
* One :class:`CaseReport` per pytest phase - setup, call, teardown - of every
  test, whatever the outcome, plus one per collection error.
* Every case ends with a terminal report. A test that started and never reached
  teardown (the session was interrupted, something called ``pytest.exit``, the
  xdist worker holding it died) gets a synthetic ``crashed`` teardown report,
  so a consumer never waits on a phase that will not arrive.
* Exactly one report in the run carries ``last_report=True``. The newest report
  is held back until the next one arrives, so the flag lands on a report that
  has not been shipped yet - see :meth:`ElasticCaseReporter._submit`.
* Reports are queued and shipped in batches from a background thread; a test
  never waits on the sender. The queue is drained at session finish.
* Shipping is a mock. :class:`MockElasticSender` records the POST it would have
  made to the forwarding API - the endpoint that takes a body of case reports
  and forwards them to elastic. Swap it in :func:`pytest_configure` for the
  real one; anything with a ``send(list[dict])`` method fits.

The vocabulary
--------------
:data:`OUTCOMES` and :data:`PHASE_STEPS` are the only place the strings that
land in elastic are decided. Remap a value there, or reassign either dict from
a conftest, if your index speaks differently - ``OUTCOMES["error"] = "failed"``
folds setup and teardown failures back into plain failures, for instance.

Updating state from anywhere
----------------------------
``vc`` and the other run-wide fields live on the plugin instance, which is
registered under the name ``elastic-case-reporter``, and so reachable from
anywhere that can see a pytest ``config``::

    def test_something(request):
        reporter = request.config.pluginmanager.getplugin("elastic-case-reporter")
        reporter.set_vc("vc-4.2.1")          # or reporter.update(owner="qa")

Every report built after that call carries the new value, the remaining phases
of the test making the call included. The state is mutated under a lock, so a
fixture running on a thread of its own is safe.

Under xdist
-----------
The controller owns the stream. Every worker's reports reach it through
``pytest_runtest_logreport``, so a run has one ordered stream and one
``last_report=True`` however many workers it used. What only a worker can know
- the machine for a live item, the exception that was raised, the run-wide
state as the phase ended - is read on the worker by :class:`MachineAnnotator`
and attached to the report, which pytest serialises across for us. So
``set_vc`` from a test works the same under xdist as without it: the value
rides along on that worker's reports rather than having to reach the
controller. It changes that worker's reports only, each worker being its own
process, so a switch meant for the whole run belongs where every process runs
it - ``--elastic-vc``, or a hook in the rootdir conftest. A worker that dies
mid-test is a crashed teardown report like any other unfinished case.
"""

from __future__ import annotations

import json
import queue
import threading
import time as _clock
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

# The name the stream owner is registered under, and the one test code reaches
# for. Deliberately not the module name: loading the module with
# `-p elastic_reporter` already claims that.
PLUGIN_NAME = "elastic-case-reporter"
ANNOTATOR_NAME = "elastic-case-reporter-annotator"

# Set by the annotator on every test report, read by the reporter - possibly in
# another process. pytest round-trips unknown report attributes through its own
# serialiser, which is what carries this from an xdist worker.
META_ATTR = "elastic_meta"

# Elastic will take a larger string than any of these, but a 200k traceback in
# an alerting index helps nobody. Both are cut with a marker, never silently.
MAX_MESSAGE_CHARS = 2_000
MAX_TRACEBACK_CHARS = 8_000

# The outcome strings that land in elastic, and the step name that goes with
# each phase. Remap a value here - or reassign either dict from a conftest - if
# your index speaks a different vocabulary.
OUTCOMES = {
    "passed": "passed",
    "failed": "failed",
    "error": "error",  # a setup or teardown that failed: the test got no verdict
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "crashed": "crashed",  # started, never finished
}

PHASE_STEPS = {"setup": "Setup", "call": "Test Case", "teardown": "Teardown"}

COLLECTION_STEP = "Collection"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class CaseReport:
    """Minimal case report model for elastic."""

    outcome: str
    test_suite: str
    test_case: str
    arguments: str | None = None
    machine: str | None = None  # resolved from the `pytest.Function`
    vc: str = "mock-vc"  # run-wide state, settable through `getplugin`
    cycle_id: int = 1  # comes off the config object
    owner: str = "mock-owner"
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exception: str | None = None
    exception_message: str | None = None
    exception_traceback: str | None = None
    step_name: str = "Test Case"
    last_report: bool = False  # Whether its the last report to be written.
    labs3: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for elastic."""
        result = self.__dict__.copy()
        result["@timestamp"] = self.time
        return result


# The fields `update()` will let you rewrite at runtime. Everything else is
# either per-report (the outcome, the exception) or identity (the test names).
MUTABLE_FIELDS = frozenset({"vc", "owner", "cycle_id", "machine", "labs3"})


# ---------------------------------------------------------------------------
# Where `machine` comes from
# ---------------------------------------------------------------------------


def resolve_machine(item: pytest.Item) -> str | None:
    """Return the machine a test ran against, or None.

    PLACEHOLDER - replace the body with your own lookup. It is called once per
    phase with the live item (a ``pytest.Function`` for an ordinary test), so
    it can read markers, ``item.funcargs``, ``item.callspec.params`` or
    anything else the item carries. It must not raise; see ``safe_machine``.
    """
    marker = item.get_closest_marker("machine")
    if marker is not None and marker.args:
        return str(marker.args[0])
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ReporterConfig:
    """Everything the reporter reads once, at configure time."""

    url: str = "https://elastic-forwarder.example/case-reports"
    cycle_id: int = 1
    owner: str = "mock-owner"
    vc: str = "mock-vc"
    labs3: bool = True
    batch_size: int = 25
    flush_interval: float = 2.0
    shutdown_timeout: float = 15.0
    enabled: bool = True

    @classmethod
    def from_pytest(cls, config: pytest.Config) -> ReporterConfig:
        def opt(name: str, ini: str, default: Any) -> Any:
            value = getattr(config.option, name, None)
            if value is None or value == "":
                value = config.getini(ini) or None
            return default if value is None else value

        return cls(
            url=str(opt("elastic_url", "elastic_url", cls.url)),
            cycle_id=int(opt("elastic_cycle_id", "elastic_cycle_id", cls.cycle_id)),
            owner=str(opt("elastic_owner", "elastic_owner", cls.owner)),
            vc=str(opt("elastic_vc", "elastic_vc", cls.vc)),
            batch_size=int(opt("elastic_batch_size", "elastic_batch_size", cls.batch_size)),
            flush_interval=float(
                opt("elastic_flush_interval", "elastic_flush_interval", cls.flush_interval)
            ),
            enabled=not getattr(config.option, "elastic_off", False),
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("elastic-reporter", "ship case reports to elastic")
    group.addoption("--elastic-url", dest="elastic_url", help="forwarding API endpoint")
    group.addoption("--elastic-cycle-id", dest="elastic_cycle_id", help="cycle id for this run")
    group.addoption("--elastic-owner", dest="elastic_owner", help="owner recorded on every report")
    group.addoption("--elastic-vc", dest="elastic_vc", help="starting vc for this run")
    group.addoption("--elastic-batch-size", dest="elastic_batch_size", help="reports per POST")
    group.addoption(
        "--elastic-flush-interval",
        dest="elastic_flush_interval",
        help="seconds before a part-full batch is sent anyway",
    )
    group.addoption(
        "--elastic-off",
        dest="elastic_off",
        action="store_true",
        help="collect nothing and ship nothing",
    )
    for name, help_text in (
        ("elastic_url", "forwarding API endpoint"),
        ("elastic_cycle_id", "cycle id for this run"),
        ("elastic_owner", "owner recorded on every report"),
        ("elastic_vc", "starting vc for this run"),
        ("elastic_batch_size", "reports per POST"),
        ("elastic_flush_interval", "seconds before a part-full batch is sent anyway"),
    ):
        parser.addini(name, help_text, default="")


# ---------------------------------------------------------------------------
# The sender
# ---------------------------------------------------------------------------


def json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class CaseReportSender:
    """What the reporter needs from a sender. A real one POSTs and returns."""

    def send(self, body: list[dict[str, Any]]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MockElasticSender(CaseReportSender):
    """Stands in for the API that forwards case reports to elastic.

    The real thing is one POST of ``body`` - a JSON array of case reports, the
    output of :meth:`CaseReport.to_dict` - to ``url``, which writes them to
    elastic. This records the request instead of making it, so the whole path
    up to the socket is exercised: batching, serialisation, ordering, the
    ``last_report`` flag. ``requests`` and ``documents`` are what a test of the
    plugin asserts against.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.requests: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, body: list[dict[str, Any]]) -> None:
        # Serialised here rather than in the caller so that an unserialisable
        # field fails in the mock exactly where it would fail for real.
        payload = json.dumps(body, default=json_default)
        with self._lock:
            self.requests.append({"url": self.url, "count": len(body), "bytes": len(payload)})
            self.documents.extend(body)

    def describe(self) -> list[str]:
        with self._lock:
            return [
                "POST {url} <- {count} report(s), {bytes} bytes".format(**request)
                for request in self.requests
            ]


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------


_STOP = object()
_TICK = object()


class BackgroundShipper:
    """A queue, a thread, and batches out the other end.

    Order is preserved: one producer-facing queue, one consumer thread. A
    failed batch is dropped rather than retried - a run must not end up
    shipping reports for longer than it spent running tests - and the failure
    is kept for the end-of-run summary.
    """

    def __init__(self, sender: CaseReportSender, batch_size: int, flush_interval: float) -> None:
        self._sender = sender
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.05, flush_interval)
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="elastic-reporter", daemon=True)
        self.shipped = 0
        self.errors: list[str] = []

    def start(self) -> None:
        self._thread.start()

    def submit(self, report: CaseReport) -> None:
        self._queue.put(report)

    def close(self, timeout: float) -> None:
        if not self._thread.is_alive():
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.errors.append(f"sender did not drain within {timeout:g}s")

    def _run(self) -> None:
        batch: list[CaseReport] = []
        deadline = _clock.monotonic() + self._flush_interval
        while True:
            try:
                item = self._queue.get(timeout=max(0.0, deadline - _clock.monotonic()))
            except queue.Empty:
                item = _TICK
            if item is _STOP:
                self._flush(batch)
                return
            if item is not _TICK:
                batch.append(item)
                if len(batch) < self._batch_size:
                    continue
            self._flush(batch)
            batch = []
            deadline = _clock.monotonic() + self._flush_interval

    def _flush(self, batch: list[CaseReport]) -> None:
        if not batch:
            return
        try:
            self._sender.send([report.to_dict() for report in batch])
        except Exception as exc:  # never fail a run over telemetry
            self.errors.append(f"{len(batch)} report(s) dropped: {type(exc).__name__}: {exc}")
        else:
            self.shipped += len(batch)


# ---------------------------------------------------------------------------
# Run-wide state
# ---------------------------------------------------------------------------


class RunState:
    """The report fields that belong to the run rather than to a test."""

    def __init__(self, config: ReporterConfig) -> None:
        self._lock = threading.Lock()
        self._fields: dict[str, Any] = {
            "vc": config.vc,
            "owner": config.owner,
            "cycle_id": config.cycle_id,
            "labs3": config.labs3,
        }

    def update(self, **fields: Any) -> None:
        unknown = sorted(set(fields) - MUTABLE_FIELDS)
        if unknown:
            raise ValueError(
                f"not run-wide report state: {', '.join(unknown)} "
                f"(settable: {', '.join(sorted(MUTABLE_FIELDS))})"
            )
        with self._lock:
            self._fields.update(fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._fields)


class StateAccess:
    """The half of the plugin API that is the same wherever a test runs.

    Under xdist a test reaching for ``getplugin`` gets the worker's annotator
    and not the controller's reporter, so both have to answer to this.
    """

    def __init__(self, state: RunState) -> None:
        self.run_state = state

    def set_vc(self, vc: str) -> None:
        """Set the vc recorded on every report built from now on."""
        self.run_state.update(vc=vc)

    def update(self, **fields: Any) -> None:
        """Set any of `MUTABLE_FIELDS` for every report built from now on."""
        self.run_state.update(**fields)

    @property
    def state(self) -> dict[str, Any]:
        return self.run_state.snapshot()


# ---------------------------------------------------------------------------
# The worker half
# ---------------------------------------------------------------------------


class MachineAnnotator(StateAccess):
    """Reads what only a live item can answer for, and pins it to the report.

    Registered wherever tests actually run - an xdist worker, or the session
    itself without xdist. The report is the only thing that crosses to the
    controller, so anything the reporter needs has to leave from here.
    """

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo):
        outcome = yield
        report: pytest.TestReport = outcome.get_result()
        exc_type, exc_message = describe_exception(call)
        setattr(
            report,
            META_ATTR,
            {
                "machine": safe_machine(item),
                "exception": exc_type,
                "exception_message": truncate(exc_message, MAX_MESSAGE_CHARS),
                # Snapshotted per phase: a test that sets the vc mid-run is
                # describing the reports from that point on, and under xdist
                # this is the only way that reaches the controller.
                "state": self.state,
            },
        )


# ---------------------------------------------------------------------------
# The stream owner
# ---------------------------------------------------------------------------


class ElasticCaseReporter(StateAccess):
    """Turns pytest reports into case reports and hands them to the shipper.

    Registered where the reports all come together: the xdist controller, or
    the session itself without xdist.
    """

    def __init__(self, config: ReporterConfig, sender: CaseReportSender, state: RunState) -> None:
        super().__init__(state)
        self.config = config
        self.sender = sender
        self.shipper = BackgroundShipper(sender, config.batch_size, config.flush_interval)
        self._lock = threading.Lock()
        # Started and not yet torn down, against the metadata its setup report
        # carried. Whatever is still in here when the session ends never
        # finished, and is reported as crashed.
        self._inflight: dict[str, dict[str, Any]] = {}
        self._pending: CaseReport | None = None  # holdback, see _submit
        self._closed = False
        self.built = 0

    # -- building ------------------------------------------------------------

    def _build(
        self,
        nodeid: str,
        outcome: str,
        step_name: str,
        meta: dict[str, Any] | None = None,
        exception: str | None = None,
        exception_message: str | None = None,
        exception_traceback: str | None = None,
    ) -> CaseReport:
        meta = meta or {}
        # The state the phase ended in, which under xdist was captured on the
        # worker. Only a report built here - a collection error - falls back to
        # this process's own.
        state = dict(meta.get("state") or self.state)
        # `machine` is per-item, so the resolver wins; a value set through
        # update() is the fallback for items it cannot answer for. Popped
        # unconditionally - it is passed positionally below, not in **state.
        fallback = state.pop("machine", None)
        suite, case, arguments = split_nodeid(nodeid)
        self.built += 1
        return CaseReport(
            outcome=outcome,
            test_suite=suite,
            test_case=case,
            arguments=arguments,
            machine=meta.get("machine") or fallback,
            step_name=step_name,
            exception=exception if exception is not None else meta.get("exception"),
            exception_message=truncate(
                exception_message
                if exception_message is not None
                else meta.get("exception_message"),
                MAX_MESSAGE_CHARS,
            ),
            exception_traceback=truncate(exception_traceback, MAX_TRACEBACK_CHARS),
            **state,
        )

    def _submit(self, report: CaseReport) -> None:
        """Queue a report, holding the newest one back.

        ``last_report`` has to be set on a report that has not been sent, and
        nothing knows a report is the last until either another one turns up or
        the session ends. So the newest report waits here until one of those
        two happens: the stream stays in order and one report - the one
        released by :meth:`_close` - carries the flag.
        """
        with self._lock:
            previous, self._pending = self._pending, report
        if previous is not None:
            self.shipper.submit(previous)

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        meta = getattr(report, META_ATTR, None) or {}
        if report.when not in PHASE_STEPS:
            # Not a phase at all: xdist's stand-in report for a worker that
            # died with this test in its hands. Nothing else will close the
            # case out, so this is its terminal report - described by what its
            # setup report carried, since the stand-in knows only the nodeid.
            meta = self._inflight.pop(report.nodeid, None) or meta
            self._submit(
                self._build(
                    nodeid=report.nodeid,
                    outcome=OUTCOMES["crashed"],
                    step_name=PHASE_STEPS["teardown"],
                    meta=meta,
                    exception="WorkerCrash",
                    exception_message=report.longreprtext or "the worker running it died",
                )
            )
            return
        if report.when == "setup":
            self._inflight[report.nodeid] = meta
        elif report.when == "teardown":
            self._inflight.pop(report.nodeid, None)
        self._submit(
            self._build(
                nodeid=report.nodeid,
                outcome=outcome_of(report),
                step_name=PHASE_STEPS[report.when],
                meta=meta,
                exception_traceback=report.longreprtext or None,
            )
        )

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A collection error means no test of that module will ever produce a
        # phase report, so this is the only trace elastic would otherwise get.
        # Under xdist the controller re-fires this for the worker that failed,
        # deduplicated across workers, so it arrives here exactly once.
        if report.failed:
            self._submit(
                self._build(
                    nodeid=report.nodeid,
                    outcome=OUTCOMES["error"],
                    step_name=COLLECTION_STEP,
                    exception="CollectError",
                    exception_message=f"collection of {report.nodeid} failed",
                    exception_traceback=report.longreprtext or None,
                )
            )

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self._close(reason=exit_reason(session, exitstatus))
        summarise(session.config, self)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        # Backstop: sessionfinish is not reached if configure-time work blew up.
        self._close(reason="the session was unconfigured")

    # -- shutdown ------------------------------------------------------------

    def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        for nodeid, meta in list(self._inflight.items()):
            # It started and never reached teardown, so nothing else will close
            # this case out. The step is Teardown on purpose: every case in the
            # index then ends with one, crashed or not.
            self._inflight.pop(nodeid, None)
            self._submit(
                self._build(
                    nodeid=nodeid,
                    outcome=OUTCOMES["crashed"],
                    step_name=PHASE_STEPS["teardown"],
                    meta=meta,
                    exception="TestIncomplete",
                    exception_message=f"never reached teardown: {reason}",
                )
            )
        with self._lock:
            last, self._pending = self._pending, None
        if last is not None:
            last.last_report = True
            self.shipper.submit(last)
        self.shipper.close(self.config.shutdown_timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split_nodeid(nodeid: str) -> tuple[str, str, str | None]:
    """``tests/test_a.py::TestB::test_c[1-2]`` -> suite, case, arguments."""
    path, _, rest = nodeid.partition("::")
    parts = rest.split("::") if rest else []
    name = parts[-1] if parts else path
    case, bracket, arguments = name.partition("[")
    suite = "::".join([path] + parts[:-1]) if parts else path
    return suite, case, arguments[:-1] if bracket else None


def outcome_of(report: pytest.TestReport) -> str:
    """Phase outcome as elastic sees it.

    A failing setup or teardown is an error rather than a failure: the test
    itself never got a verdict.
    """
    if getattr(report, "wasxfail", None) is not None:
        return OUTCOMES["xpassed" if report.passed and report.when == "call" else "xfailed"]
    if report.skipped:
        return OUTCOMES["skipped"]
    if report.passed:
        return OUTCOMES["passed"]
    return OUTCOMES["failed" if report.when == "call" else "error"]


def describe_exception(call: pytest.CallInfo | None) -> tuple[str | None, str | None]:
    excinfo = getattr(call, "excinfo", None)
    if excinfo is None:
        return None, None
    return excinfo.typename, str(excinfo.value)


def safe_machine(item: pytest.Item | None) -> str | None:
    if item is None:
        return None
    try:
        return resolve_machine(item)
    except Exception:  # a lookup of someone else's is not worth a failed run
        return None


def truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated, {len(text) - limit} more characters"


def exit_reason(session: pytest.Session, exitstatus: int) -> str:
    if getattr(session, "shouldstop", False):
        return f"the session stopped early ({session.shouldstop})"
    if getattr(session, "shouldfail", False):
        return f"the session was failed early ({session.shouldfail})"
    try:
        status = pytest.ExitCode(int(exitstatus)).name.lower().replace("_", " ")
    except ValueError:
        status = f"exit status {exitstatus}"
    return f"the session ended ({status})"


def summarise(config: pytest.Config, reporter: ElasticCaseReporter) -> None:
    terminal = config.pluginmanager.get_plugin("terminalreporter")
    if terminal is None:
        return
    sender = reporter.sender
    requests = len(getattr(sender, "requests", []))
    mock = " [mock]" if isinstance(sender, MockElasticSender) else ""
    terminal.write_sep(
        "-",
        f"elastic-reporter: {reporter.shipper.shipped}/{reporter.built} report(s) "
        f"in {requests} POST(s) to {reporter.config.url}{mock}",
    )
    if config.option.verbose > 0 and hasattr(sender, "describe"):
        for line in sender.describe():
            terminal.write_line("  " + line)
    for error in reporter.shipper.errors:
        terminal.write_line("  elastic-reporter: " + error, red=True)


def get_reporter(config: pytest.Config) -> StateAccess | None:
    """Whatever this process registered, or None when the plugin is off.

    The stream owner everywhere except an xdist worker, where it is that
    worker's annotator. Both answer `set_vc`, `update` and `state`.
    """
    return config.pluginmanager.get_plugin(PLUGIN_NAME)


def is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def is_xdist_controller(config: pytest.Config) -> bool:
    if is_xdist_worker(config):
        return False
    return getattr(config.option, "dist", "no") not in ("no", None)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "machine(name): machine this test runs against")
    settings = ReporterConfig.from_pytest(config)
    if not settings.enabled:
        return
    state = RunState(settings)
    worker, controller = is_xdist_worker(config), is_xdist_controller(config)
    if not controller:
        # Items run here, so this is where a machine can be resolved - and,
        # on a worker, what test code reaching for `getplugin` finds.
        annotator = MachineAnnotator(state)
        config.pluginmanager.register(annotator, PLUGIN_NAME if worker else ANNOTATOR_NAME)
    if not worker:
        # The one line to change for a real run: anything with .send(list[dict]).
        reporter = ElasticCaseReporter(settings, MockElasticSender(settings.url), state)
        reporter.shipper.start()
        config.pluginmanager.register(reporter, PLUGIN_NAME)
