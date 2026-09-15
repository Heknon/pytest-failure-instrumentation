# Copyright (c) 2026 Heknon. Swap in whatever notice your project uses;
# ruff's CPY001 only checks that there is one.
"""Ship one elastic case report per pytest phase, and mark the last of each test.

    pytest -p elastic_reporter --elastic-url=https://reports.example/case-reports

Standalone: a single module, no package, nothing to install beyond pytest 8 or
newer on Python 3.12 or newer. Drop it next to your ``conftest.py`` and load it
with ``-p elastic_reporter``, or name it in ``pytest_plugins`` in the rootdir
conftest.

What it does
------------
* One :class:`CaseReport` per pytest phase - setup, call, teardown - of every
  attempt at every test, whatever the outcome, plus one per collection error.
* ``last_report=True`` marks the end of a *test*, not the end of the run: each
  test contributes exactly one, on the final report of its final attempt, so a
  consumer can close the case out and decide its verdict. See `_hold`.
* Every case ends with one. A test that started and never reached teardown -
  the session was interrupted, the xdist worker holding it died - gets a
  synthetic ``crashed`` teardown report instead.
* Reports are queued and shipped in batches from a background thread; a test
  never waits on the sender. The queue is drained at session finish.
* Shipping is a mock. :class:`MockElasticSender` records the POST it would have
  made to the forwarding API - the endpoint that takes a body of case reports
  and forwards them to elastic. Swap it in `pytest_configure` for the real
  one; anything with a ``send(list[dict])`` method fits.

Reruns
------
A test can be attempted more than once: pytest-rerunfailures retries a failure
in place, and under xdist it also re-queues a test whose worker died. Only the
last attempt may carry the flag, and no attempt knows at the time whether it is
the last one - so each test's newest report is held back rather than shipped,
and released when the next report for that test displaces it. What releases it
*flagged* is ``pytest_runtest_logfinish`` without a rerun pending, where
"pending" means this attempt logged a report that pytest-rerunfailures marked
``rerun`` (it marks the failing report of an attempt it is about to retry, and
rewrites the crashed-worker report the same way before the controller sees it).
Anything still held when the session ends is flagged there, so a retry that
never happened cannot leave a case open.

The vocabulary
--------------
`OUTCOMES` and `PHASE_STEPS` are the only place the strings that land in
elastic are decided. Remap a value there, or reassign either dict from a
conftest, if your index speaks differently - ``OUTCOMES["error"] = "failed"``
folds setup and teardown failures back into plain failures, for instance.

Updating state from anywhere
----------------------------
``vc`` and the other run-wide fields live on the plugin instance, which is
registered under the name ``elastic-case-reporter``, and so reachable from
anywhere that can see a pytest ``config``::

    def test_something(request):
        reporter = request.config.pluginmanager.getplugin("elastic-case-reporter")
        reporter.set_vc("vc-4.2.1")          # or reporter.update(owner="qa")

Set once and forget: every report built afterwards carries it, the remaining
phases of the test that set it included. The state is mutated under a lock, so
a fixture running on a thread of its own is safe.

Under xdist
-----------
The controller owns the stream. Every worker's reports reach it through
``pytest_runtest_logreport``, so a run has one ordered stream however many
workers it used, and one ``last_report`` per test rather than per worker. What
only a worker can know - the machine for a live item, the exception that was
raised, the run-wide state as the phase ended - is read on the worker by
`MachineAnnotator` and attached to the report, which pytest serialises across
for us. So a worker's ``set_vc`` describes that worker's later reports without
having to reach the controller. It describes that worker's only, each being
its own process, so a switch meant for the whole run belongs where every
process runs it: ``--elastic-vc``, or a hook in the rootdir conftest.
"""

import json
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, Self

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

#: A test report's outcome when pytest-rerunfailures is about to retry it.
RERUN_OUTCOME = "rerun"

#: The name the stream owner is registered under, and the one test code reaches
#: for. Deliberately not the module name: loading the module with
#: ``-p elastic_reporter`` already claims that.
PLUGIN_NAME = "elastic-case-reporter"
ANNOTATOR_NAME = "elastic-case-reporter-annotator"

#: Set by the annotator on every test report, read by the reporter - possibly
#: in another process. pytest round-trips unknown report attributes through its
#: own serialiser, which is what carries this from an xdist worker.
META_ATTR = "elastic_meta"

#: Elastic will take a larger string than either of these, but a 200k traceback
#: in an alerting index helps nobody. Both are cut with a marker, never
#: silently.
MAX_MESSAGE_CHARS = 2_000
MAX_TRACEBACK_CHARS = 8_000

#: The outcome strings that land in elastic, and the step name that goes with
#: each phase. Remap a value here - or reassign either dict from a conftest -
#: if your index speaks a different vocabulary.
OUTCOMES = {
    "passed": "passed",
    "failed": "failed",
    "error": "error",  # a setup or teardown that failed: the test got no verdict
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "rerun": "rerun",  # this attempt failed and another one is coming
    "crashed": "crashed",  # started, never finished
}

PHASE_STEPS = {"setup": "Setup", "call": "Test Case", "teardown": "Teardown"}

COLLECTION_STEP = "Collection"

#: What the annotator reads off a live item for the reporter to build with.
type Meta = dict[str, Any]

#: One case report on its way to elastic.
type Document = dict[str, Any]

#: An old-style makereport wrapper: hand the report on, having read it.
type ReportWrapper = Generator[None, pytest.TestReport, pytest.TestReport]


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
    time: datetime = field(default_factory=lambda: datetime.now(UTC))
    exception: str | None = None
    exception_message: str | None = None
    exception_traceback: str | None = None
    step_name: str = "Test Case"
    last_report: bool = False  # Whether its the last report to be written.
    labs3: bool = True

    def to_dict(self) -> Document:
        """Convert to dict for elastic."""
        result = self.__dict__.copy()
        result["@timestamp"] = self.time
        return result


@dataclass(frozen=True)
class Failure:
    """What went wrong in one phase."""

    exception: str | None = None
    message: str | None = None
    traceback: str | None = None


#: The fields `StateAccess.update` will let you rewrite at runtime. Everything
#: else is either per-report (the outcome, the exception) or identity (the test
#: names).
MUTABLE_FIELDS = frozenset({"vc", "owner", "cycle_id", "machine", "labs3"})


# ---------------------------------------------------------------------------
# Where `machine` comes from
# ---------------------------------------------------------------------------


def resolve_machine(item: pytest.Item) -> str | None:
    """Return the machine a test ran against, or None.

    PLACEHOLDER - replace the body with your own lookup. It is called once per
    phase with the live item (a ``pytest.Function`` for an ordinary test), so
    it can read markers, ``item.funcargs``, ``item.callspec.params`` or
    anything else the item carries. It must not raise; see `safe_machine`.
    """
    marker = item.get_closest_marker("machine")
    if marker is not None and marker.args:
        return str(marker.args[0])
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def setting(config: pytest.Config, name: str) -> str:
    """Return one setting as given: on the command line, else in the ini file."""
    value = getattr(config.option, name, None) or config.getini(name)
    return str(value or "")


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
    def from_pytest(cls, config: pytest.Config) -> Self:
        """Read the run's settings off the command line, then the ini file."""
        return cls(
            url=setting(config, "elastic_url") or cls.url,
            cycle_id=int(setting(config, "elastic_cycle_id") or cls.cycle_id),
            owner=setting(config, "elastic_owner") or cls.owner,
            vc=setting(config, "elastic_vc") or cls.vc,
            batch_size=int(setting(config, "elastic_batch_size") or cls.batch_size),
            flush_interval=float(setting(config, "elastic_flush_interval") or cls.flush_interval),
            enabled=not config.getoption("elastic_off", default=False),
        )


#: Every setting, as ``(option, ini, help)``. Both interfaces, one table.
SETTINGS = [
    ("--elastic-url", "elastic_url", "forwarding API endpoint"),
    ("--elastic-cycle-id", "elastic_cycle_id", "cycle id for this run"),
    ("--elastic-owner", "elastic_owner", "owner recorded on every report"),
    ("--elastic-vc", "elastic_vc", "starting vc for this run"),
    ("--elastic-batch-size", "elastic_batch_size", "reports per POST"),
    (
        "--elastic-flush-interval",
        "elastic_flush_interval",
        "seconds before a part-full batch is sent anyway",
    ),
]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the plugin's command line options and ini keys."""
    group = parser.getgroup("elastic-reporter", "ship case reports to elastic")
    for option, name, help_text in SETTINGS:
        group.addoption(option, dest=name, help=help_text)
        parser.addini(name, help_text, default="")
    group.addoption(
        "--elastic-off",
        dest="elastic_off",
        action="store_true",
        help="collect nothing and ship nothing",
    )


# ---------------------------------------------------------------------------
# The sender
# ---------------------------------------------------------------------------


def json_default(value: object) -> str:
    """Render what `json` cannot: the timestamps, as ISO 8601."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class CaseReportSender:
    """What the reporter needs from a sender. A real one POSTs and returns."""

    def send(self, body: list[Document]) -> None:
        """Hand one batch of case reports to the forwarding API."""
        raise NotImplementedError


class MockElasticSender(CaseReportSender):
    """Stands in for the API that forwards case reports to elastic.

    The real thing is one POST of ``body`` - a JSON array of case reports, the
    output of `CaseReport.to_dict` - to ``url``, which writes them to elastic.
    This records the request instead of making it, so the whole path up to the
    socket is exercised: batching, serialisation, ordering, the ``last_report``
    flag. ``requests`` and ``documents`` are what a test of the plugin asserts
    against.
    """

    def __init__(self, url: str) -> None:
        """Record, rather than send, what would be POSTed to ``url``."""
        self.url = url
        self.requests: list[dict[str, Any]] = []
        self.documents: list[Document] = []
        self._lock = threading.Lock()

    def send(self, body: list[Document]) -> None:
        """Record the request the real sender would have made."""
        # Serialised here rather than in the caller so that an unserialisable
        # field fails in the mock exactly where it would fail for real.
        payload = json.dumps(body, default=json_default)
        with self._lock:
            self.requests.append({"url": self.url, "count": len(body), "bytes": len(payload)})
            self.documents.extend(body)

    def describe(self) -> list[str]:
        """Return one line per request, for the end-of-run summary."""
        with self._lock:
            return [
                "POST {url} <- {count} report(s), {bytes} bytes".format(**request)
                for request in self.requests
            ]


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------


class BackgroundShipper:
    """A queue, a thread, and batches out the other end.

    Order is preserved: one producer-facing queue, one consumer thread. A
    failed batch is dropped rather than retried - a run must not end up
    shipping reports for longer than it spent running tests - and the failure
    is kept for the end-of-run summary.
    """

    def __init__(self, sender: CaseReportSender, batch_size: int, flush_interval: float) -> None:
        """Ship through ``sender``, in batches, on a thread of its own."""
        self._sender = sender
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.05, flush_interval)
        # None is the only thing put on the queue that is not a report: it says
        # drain what is left and stop.
        self._queue: queue.Queue[CaseReport | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="elastic-reporter", daemon=True)
        self.shipped = 0
        self.errors: list[str] = []

    def start(self) -> None:
        """Start the shipping thread."""
        self._thread.start()

    def submit(self, report: CaseReport) -> None:
        """Queue one report for the next batch."""
        self._queue.put(report)

    def close(self, timeout: float) -> None:
        """Drain the queue and stop the thread, or record that it would not."""
        if not self._thread.is_alive():
            return
        self._queue.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.errors.append(f"sender did not drain within {timeout:g}s")

    def _run(self) -> None:
        batch: list[CaseReport] = []
        deadline = monotonic() + self._flush_interval
        while True:
            try:
                report = self._queue.get(timeout=max(0.0, deadline - monotonic()))
            except queue.Empty:
                batch, deadline = self._flush(batch), monotonic() + self._flush_interval
                continue
            if report is None:
                self._flush(batch)
                return
            batch.append(report)
            if len(batch) >= self._batch_size:
                batch, deadline = self._flush(batch), monotonic() + self._flush_interval

    def _flush(self, batch: list[CaseReport]) -> list[CaseReport]:
        if not batch:
            return []
        try:
            self._sender.send([report.to_dict() for report in batch])
        except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
            self.errors.append(f"{len(batch)} report(s) dropped: {type(exc).__name__}: {exc}")
        else:
            self.shipped += len(batch)
        return []


# ---------------------------------------------------------------------------
# Run-wide state
# ---------------------------------------------------------------------------


class RunState:
    """The report fields that belong to the run rather than to a test."""

    def __init__(self, config: ReporterConfig) -> None:
        """Start from what the config object says."""
        self._lock = threading.Lock()
        self._fields: dict[str, Any] = {
            "vc": config.vc,
            "owner": config.owner,
            "cycle_id": config.cycle_id,
            "labs3": config.labs3,
        }

    def update(self, **fields: object) -> None:
        """Set any of `MUTABLE_FIELDS` for every report built from now on."""
        unknown = sorted(set(fields) - MUTABLE_FIELDS)
        if unknown:
            message = (
                f"not run-wide report state: {', '.join(unknown)} "
                f"(settable: {', '.join(sorted(MUTABLE_FIELDS))})"
            )
            raise ValueError(message)
        with self._lock:
            self._fields.update(fields)

    def snapshot(self) -> dict[str, Any]:
        """Return the state as it stands, to build one report from."""
        with self._lock:
            return dict(self._fields)


class StateAccess:
    """The half of the plugin API that is the same wherever a test runs.

    Under xdist a test reaching for ``getplugin`` gets the worker's annotator
    and not the controller's reporter, so both have to answer to this.
    """

    def __init__(self, state: RunState) -> None:
        """Answer for ``state``, which the reporter and annotator may share."""
        self.run_state = state

    def set_vc(self, vc: str) -> None:
        """Set the vc recorded on every report built from now on."""
        self.run_state.update(vc=vc)

    def update(self, **fields: object) -> None:
        """Set any of `MUTABLE_FIELDS` for every report built from now on."""
        self.run_state.update(**fields)

    @property
    def state(self) -> dict[str, Any]:
        """The run-wide fields as they stand."""
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

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self,
        item: pytest.Item,
        call: pytest.CallInfo[None],
    ) -> ReportWrapper:
        """Attach this process's answers to the phase report."""
        report = yield
        exception, message = describe_exception(call)
        meta: Meta = {
            "machine": safe_machine(item),
            "exception": exception,
            "exception_message": truncate(message, MAX_MESSAGE_CHARS),
            # Snapshotted per phase: a test that sets the vc mid-run is
            # describing the reports from that point on, and under xdist this
            # is the only way that reaches the controller.
            "state": self.state,
        }
        setattr(report, META_ATTR, meta)
        return report


# ---------------------------------------------------------------------------
# The stream owner
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """What the reporter remembers about one test between its reports.

    ``held`` is the report that will carry ``last_report`` if nothing displaces
    it; ``meta`` is what the current attempt's setup arrived with, so a crash
    can be described without an item; ``running`` says setup was seen and
    teardown was not; ``rerun_pending`` says this attempt is to be retried.
    """

    held: CaseReport | None = None
    meta: Meta = field(default_factory=dict)
    running: bool = False
    rerun_pending: bool = False


class ElasticCaseReporter(StateAccess):
    """Turns pytest reports into case reports and hands them to the shipper.

    Registered where the reports all come together: the xdist controller, or
    the session itself without xdist.
    """

    def __init__(self, config: ReporterConfig, sender: CaseReportSender, state: RunState) -> None:
        """Ship what ``config`` says, through ``sender``, tagged from ``state``."""
        super().__init__(state)
        self.config = config
        self.sender = sender
        self.shipper = BackgroundShipper(sender, config.batch_size, config.flush_interval)
        self._cases: dict[str, Case] = {}
        self._closed = False
        self.built = 0

    # -- the stream ----------------------------------------------------------

    def _build(
        self,
        nodeid: str,
        outcome: str,
        step_name: str,
        meta: Meta | None = None,
        failure: Failure | None = None,
    ) -> CaseReport:
        meta = meta or {}
        failure = failure or Failure()
        # The state the phase ended in, which under xdist was captured on the
        # worker. Only a report built here - a collection error - falls back to
        # this process's own.
        state = dict(meta.get("state") or self.state)
        # `machine` is per-item, so the resolver wins; a value set through
        # update() is the fallback for items it cannot answer for. Popped
        # unconditionally - it is passed by name below, not in **state.
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
            exception=failure.exception or meta.get("exception"),
            exception_message=truncate(
                failure.message or meta.get("exception_message"),
                MAX_MESSAGE_CHARS,
            ),
            exception_traceback=truncate(failure.traceback, MAX_TRACEBACK_CHARS),
            **state,
        )

    def _hold(self, nodeid: str, report: CaseReport) -> None:
        """Make ``report`` the case's candidate for ``last_report``.

        Nothing can tell at the time whether a report is a test's last: another
        phase may follow, and after the last phase a rerun may follow. So the
        newest report waits here and the one it displaces ships unflagged. What
        is still held when the test is done - or when the run is - is the one
        that was last, and `_release` flags it there.
        """
        case = self._case(nodeid)
        previous, case.held = case.held, report
        if previous is not None:
            self.shipper.submit(previous)

    def _release(self, nodeid: str, *, terminal: bool) -> None:
        case = self._cases.get(nodeid)
        if case is None:
            return
        if terminal:
            del self._cases[nodeid]  # nothing left to remember about it
        report, case.held = case.held, None
        if report is not None:
            report.last_report = terminal
            self.shipper.submit(report)

    def _case(self, nodeid: str) -> Case:
        return self._cases.setdefault(nodeid, Case())

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Note that a fresh attempt at this test has begun."""
        self._case(nodeid).rerun_pending = False

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Turn one phase report into a case report."""
        meta: Meta = getattr(report, META_ATTR, None) or {}
        case = self._case(report.nodeid)
        if report.outcome == RERUN_OUTCOME:
            # pytest-rerunfailures marks the failure it is about to retry, and
            # rewrites the crashed-worker report below the same way.
            case.rerun_pending = True
        if report.when not in PHASE_STEPS:
            self._worker_crashed(report, case)
            return
        if report.when == "setup":
            case.meta, case.running = meta, True
        elif report.when == "teardown":
            case.running = False
        self._hold(
            report.nodeid,
            self._build(
                report.nodeid,
                outcome_of(report),
                PHASE_STEPS[report.when],
                meta=meta,
                failure=Failure(traceback=report.longreprtext or None),
            ),
        )

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        """Close the case out, unless another attempt at it is coming."""
        case = self._cases.get(nodeid)
        if case is not None and not case.rerun_pending:
            self._release(nodeid, terminal=True)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Report a collection error, which no phase report will describe.

        Under xdist the controller re-fires this for the worker that failed,
        deduplicated across workers, so it arrives here exactly once.
        """
        if report.failed:
            self._hold(
                report.nodeid,
                self._build(
                    report.nodeid,
                    OUTCOMES["error"],
                    COLLECTION_STEP,
                    failure=Failure(
                        exception="CollectError",
                        message=f"collection of {report.nodeid} failed",
                        traceback=report.longreprtext or None,
                    ),
                ),
            )
            self._release(report.nodeid, terminal=True)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Close every case the run left open, then drain the queue."""
        self._close(reason=exit_reason(session, exitstatus))
        summarise(session.config, self)

    def pytest_unconfigure(self) -> None:
        """Back stop: sessionfinish is not reached if configure-time work blew up."""
        self._close(reason="the session was unconfigured")

    # -- the cases that do not end by themselves -----------------------------

    def _worker_crashed(self, report: pytest.TestReport, case: Case) -> None:
        """Turn xdist's stand-in for a dead worker into a terminal report.

        The stand-in knows only the nodeid, so the case is described by what
        its setup report carried. It is terminal unless the test is being
        re-queued, which pytest-rerunfailures says by rewriting the outcome.
        """
        case.running = False
        self._hold(
            report.nodeid,
            self._build(
                report.nodeid,
                OUTCOMES["crashed"],
                PHASE_STEPS["teardown"],
                meta=case.meta,
                failure=Failure(
                    exception="WorkerCrash",
                    message=report.longreprtext or "the worker running it died",
                ),
            ),
        )
        if not case.rerun_pending:
            self._release(report.nodeid, terminal=True)

    def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        for nodeid, case in list(self._cases.items()):
            if case.running:
                # It started and never reached teardown, so nothing else will
                # close this case out. The step is Teardown on purpose: every
                # case in the index then ends with one, crashed or not.
                self._hold(
                    nodeid,
                    self._build(
                        nodeid,
                        OUTCOMES["crashed"],
                        PHASE_STEPS["teardown"],
                        meta=case.meta,
                        failure=Failure(
                            exception="TestIncomplete",
                            message=f"never reached teardown: {reason}",
                        ),
                    ),
                )
            # Held because a rerun was expected that the run never got to, or
            # because the case never finished. Either way this was its last.
            self._release(nodeid, terminal=True)
        self.shipper.close(self.config.shutdown_timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split_nodeid(nodeid: str) -> tuple[str, str, str | None]:
    """Split ``tests/test_a.py::TestB::test_c[1-2]`` into suite, case, arguments."""
    path, _, rest = nodeid.partition("::")
    parts = rest.split("::") if rest else []
    name = parts[-1] if parts else path
    case, bracket, arguments = name.partition("[")
    suite = "::".join([path, *parts[:-1]]) if parts else path
    return suite, case, arguments[:-1] if bracket else None


def outcome_of(report: pytest.TestReport) -> str:
    """Return the phase outcome as elastic sees it.

    A failing setup or teardown is an error rather than a failure: the test
    itself never got a verdict.
    """
    if report.outcome == RERUN_OUTCOME:
        return OUTCOMES["rerun"]
    if getattr(report, "wasxfail", None) is not None:
        return OUTCOMES["xpassed" if report.passed and report.when == "call" else "xfailed"]
    if report.skipped:
        return OUTCOMES["skipped"]
    if report.passed:
        return OUTCOMES["passed"]
    return OUTCOMES["failed" if report.when == "call" else "error"]


def describe_exception(call: pytest.CallInfo[None] | None) -> tuple[str | None, str | None]:
    """Return the exception's type and message, if the phase raised one."""
    excinfo = getattr(call, "excinfo", None)
    if excinfo is None:
        return None, None
    return excinfo.typename, str(excinfo.value)


def safe_machine(item: pytest.Item | None) -> str | None:
    """Return `resolve_machine`, or None if it was not in a position to answer."""
    if item is None:
        return None
    try:
        return resolve_machine(item)
    except Exception:  # noqa: BLE001 - a lookup of someone else's is not worth a failed run
        return None


def truncate(text: str | None, limit: int) -> str | None:
    """Cut ``text`` to ``limit`` characters, saying so where it was cut."""
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated, {len(text) - limit} more characters"


def exit_reason(session: pytest.Session, exitstatus: int) -> str:
    """Say why the run ended, for the cases it ended in the middle of."""
    if getattr(session, "shouldstop", False):
        return f"the session stopped early ({session.shouldstop})"
    if getattr(session, "shouldfail", False):
        return f"the session was failed early ({session.shouldfail})"
    try:
        status = pytest.ExitCode(exitstatus).name.lower().replace("_", " ")
    except ValueError:
        status = f"exit status {exitstatus}"
    return f"the session ended ({status})"


def summarise(config: pytest.Config, reporter: ElasticCaseReporter) -> None:
    """Write what was shipped, and anything that was not, to the terminal."""
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
    if config.option.verbose > 0 and isinstance(sender, MockElasticSender):
        for line in sender.describe():
            terminal.write_line("  " + line)
    for error in reporter.shipper.errors:
        terminal.write_line("  elastic-reporter: " + error, red=True)


def get_reporter(config: pytest.Config) -> StateAccess | None:
    """Return whatever this process registered, or None when the plugin is off.

    The stream owner everywhere except an xdist worker, where it is that
    worker's annotator. Both answer `StateAccess.set_vc`, `StateAccess.update`
    and `StateAccess.state`.
    """
    plugin = config.pluginmanager.get_plugin(PLUGIN_NAME)
    return plugin if isinstance(plugin, StateAccess) else None


def is_xdist_worker(config: pytest.Config) -> bool:
    """Say whether this process is an xdist worker."""
    return hasattr(config, "workerinput")


def is_xdist_controller(config: pytest.Config) -> bool:
    """Say whether this process is an xdist controller with workers to come."""
    if is_xdist_worker(config):
        return False
    return getattr(config.option, "dist", "no") not in ("no", None)


def pytest_configure(config: pytest.Config) -> None:
    """Register the half of the plugin this process is responsible for."""
    config.addinivalue_line("markers", "machine(name): machine this test runs against")
    settings = ReporterConfig.from_pytest(config)
    if not settings.enabled:
        return
    state = RunState(settings)
    worker, controller = is_xdist_worker(config), is_xdist_controller(config)
    if not controller:
        # Items run here, so this is where a machine can be resolved - and, on
        # a worker, what test code reaching for `getplugin` finds.
        annotator = MachineAnnotator(state)
        config.pluginmanager.register(annotator, PLUGIN_NAME if worker else ANNOTATOR_NAME)
    if not worker:
        # The one line to change for a real run: anything with .send(list[dict]).
        reporter = ElasticCaseReporter(settings, MockElasticSender(settings.url), state)
        reporter.shipper.start()
        config.pluginmanager.register(reporter, PLUGIN_NAME)
