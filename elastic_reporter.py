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
* One `CaseReport` per pytest phase - setup, call, teardown - of every attempt
  at every test, whatever the outcome, plus one per collection error.
* ``last_report=True`` marks the end of a *test*, not the end of the run: each
  test contributes exactly one, on the last report it produces, so a consumer
  can close the case out and decide its verdict.
* Every case ends with one. A test that started and never reached teardown -
  the session was interrupted, the xdist worker holding it died - gets a
  synthetic ``crashed`` teardown report instead.
* Reports are queued and shipped in batches from a background thread; a test
  never waits on the sender. The queue is drained at session finish.
* Shipping is a mock. `MockElasticSender` records the POST it would have made
  to the forwarding API - the endpoint that takes a body of case reports and
  forwards them to elastic. Swap it in `pytest_configure` for the real one;
  anything with a ``send(list[dict])`` method fits.

Setting a test's attributes
---------------------------
`machine`, `vc`, `cycle_id`, `owner` and the rest describe the test rather than
the phase, and they come from everywhere - an option, a fixture that connects
to the machine, the test body itself. So there is one way to set any of them,
from anywhere that can see a pytest ``config``::

    reporter = request.config.pluginmanager.getplugin("elastic-reporter")
    reporter.set(vc="fw-4.2.1", machine="rack1-dut7")

That is the whole API. It takes any of `SETTABLE` - every field of `CaseReport`
except the ones a phase answers for itself - and it is there in every process,
whether or not the plugin is switched on, so nothing has to be guarded.

A value set anywhere in a test describes the whole test: its setup, call and
teardown reports all carry it, whichever phase set it, because a test's reports
are stamped when the test ends rather than as each phase finishes. Values stick
until changed, so a process can set one and forget it.

Where a test's machine is the test's own, set it where you know it - the
fixture that allocates it, or one that reads it off the item::

    @pytest.fixture(autouse=True)
    def record_machine(request):
        reporter = request.config.pluginmanager.getplugin("elastic-reporter")
        reporter.set(machine=machine_for(request.node))

Reruns
------
A test can be attempted more than once: pytest-rerunfailures retries a failure
in place, and under xdist it also re-queues a test whose worker died. Every
attempt belongs to the same case, so they are all held together and shipped
when the test is done, with the flag on the final report of the final attempt.
What says a test is done is ``pytest_runtest_logfinish`` without a rerun
pending, where "pending" means this attempt logged a report that
pytest-rerunfailures marked ``rerun`` (it marks the failing report of an
attempt it is about to retry, and rewrites the crashed-worker report the same
way before the controller sees it). Anything still held when the session ends
is shipped there, so a retry that never happened cannot leave a case open.

The vocabulary
--------------
`OUTCOMES` and `PHASE_STEPS` are the only place the strings that land in
elastic are decided. Remap a value there, or reassign either dict from a
conftest, if your index speaks differently - ``OUTCOMES["error"] = "failed"``
folds setup and teardown failures back into plain failures, for instance.

Under xdist
-----------
The controller owns the stream. Every worker's reports reach it through
``pytest_runtest_logreport``, so a run has one ordered stream however many
workers it used, and one ``last_report`` per test rather than per worker. What
only a worker can know - the test's attributes as it ran, the exception that
was raised - is read on the worker by `ReportAnnotator` and attached to the
report, which pytest serialises across for us. So setting an attribute on a
worker needs nothing of the controller. It describes that worker's tests only,
each worker being its own process, so a value meant for the whole run belongs
where every process picks it up: an option, an ini key, or a hook in the
rootdir conftest.
"""

import json
import queue
import threading
from dataclasses import dataclass, field, fields
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
PLUGIN_NAME = "elastic-reporter"
ANNOTATOR_NAME = "elastic-reporter-annotator"

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

#: pytest's own names for the phases, and for the collection that precedes
#: them. Remapped like `OUTCOMES`, if elastic wants them said differently.
PHASE_STEPS = {"setup": "setup", "call": "call", "teardown": "teardown"}

COLLECTION_STEP = "collect"

#: What the annotator reads off a live item for the reporter to build with.
type Meta = dict[str, Any]

#: One case report on its way to elastic.
type Document = dict[str, Any]

#: A makereport wrapper: hand the report on, having read it.
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
    machine: str | None = None  # set per test, see `CaseAttributes`
    vc: str = "mock-vc"
    cycle_id: int = 1
    owner: str = "mock-owner"
    time: datetime = field(default_factory=lambda: datetime.now(UTC))
    exception: str | None = None
    exception_message: str | None = None
    exception_traceback: str | None = None
    step_name: str = "call"
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


#: What a phase answers for itself. Everything else describes the test, and so
#: belongs to `CaseAttributes`.
PHASE_FIELDS = frozenset(
    {
        "outcome",
        "step_name",
        "exception",
        "exception_message",
        "exception_traceback",
        "time",
        "last_report",
    },
)

#: Every attribute `CaseAttributes.set` will take, derived from the model, so a
#: new field on `CaseReport` is settable without being named twice.
SETTABLE = frozenset(f.name for f in fields(CaseReport)) - PHASE_FIELDS


def stamp(report: CaseReport, attributes: dict[str, Any]) -> None:
    """Write a test's attributes onto one of its reports."""
    for name, value in attributes.items():
        if name in SETTABLE:
            setattr(report, name, value)


# ---------------------------------------------------------------------------
# The attributes of a test
# ---------------------------------------------------------------------------


class CaseAttributes:
    """Every report field that describes the test rather than the phase.

    One store for all of them, because they come from everywhere: an option, a
    fixture that connects to the machine, the test body. `set` takes any of
    `SETTABLE` from any of those places, the values describe the whole test -
    its reports are stamped when it ends, not as each phase finishes - and they
    stick until changed, so a process can set one and forget it.
    """

    def __init__(self, config: "ReporterConfig") -> None:
        """Start from what the config object says."""
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {
            "vc": config.vc,
            "owner": config.owner,
            "cycle_id": config.cycle_id,
            "labs3": config.labs3,
        }

    def set(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for this test and the tests after it."""
        unknown = sorted(set(attributes) - SETTABLE)
        if unknown:
            message = (
                f"not a case attribute: {', '.join(unknown)} "
                f"(settable: {', '.join(sorted(SETTABLE))})"
            )
            raise ValueError(message)
        with self._lock:
            self._values.update(attributes)

    def snapshot(self) -> dict[str, Any]:
        """Return the attributes as they stand, to stamp one test's reports."""
        with self._lock:
            return dict(self._values)


class ElasticPlugin:
    """What ``getplugin("elastic-reporter")`` hands you, in any process.

    A session registers one of these under that name whatever it is doing: the
    reporter where the stream is built, the annotator on an xdist worker, and
    this plain one when the plugin is switched off. They all set attributes the
    same way, so nothing that sets one needs to know which it got, or to check
    that it got anything.
    """

    def __init__(self, attributes: CaseAttributes) -> None:
        """Answer for ``attributes``, which every half of a session shares."""
        self.attributes = attributes

    def set(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for this test and the tests after it."""
        self.attributes.set(**attributes)


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
# The worker half
# ---------------------------------------------------------------------------


class ReportAnnotator(ElasticPlugin):
    """Pins what this process knows about the test to its phase reports.

    Registered wherever tests actually run - an xdist worker, or the session
    itself without xdist. The report is the only thing that crosses to the
    controller, so anything the reporter needs has to leave from here.
    """

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, call: pytest.CallInfo[None]) -> ReportWrapper:
        """Attach this process's answers to the phase report."""
        report = yield
        exception, message = describe_exception(call)
        meta: Meta = {
            "attributes": self.attributes.snapshot(),
            "exception": exception,
            "exception_message": truncate(message, MAX_MESSAGE_CHARS),
        }
        setattr(report, META_ATTR, meta)
        return report


# ---------------------------------------------------------------------------
# The stream owner
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """One test's reports, held until the test is done.

    ``reports`` is every phase of every attempt, in order; ``attributes`` is the
    latest answer from wherever the test ran, which all of them are stamped
    with; ``running`` says setup was seen and teardown was not; and
    ``rerun_pending`` says this attempt is to be retried, so the test is not
    done yet.
    """

    reports: list[CaseReport] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    running: bool = False
    rerun_pending: bool = False


class ElasticCaseReporter(ElasticPlugin):
    """Turns pytest reports into case reports and hands them to the shipper.

    Registered where the reports all come together: the xdist controller, or
    the session itself without xdist.
    """

    def __init__(
        self,
        config: ReporterConfig,
        sender: CaseReportSender,
        attributes: CaseAttributes,
    ) -> None:
        """Ship what ``config`` says, through ``sender``, stamped from ``attributes``."""
        super().__init__(attributes)
        self.config = config
        self.sender = sender
        self.shipper = BackgroundShipper(sender, config.batch_size, config.flush_interval)
        self._cases: dict[str, Case] = {}
        self._closed = False
        self.built = 0

    # -- the stream ----------------------------------------------------------

    def _build(self, nodeid: str, outcome: str, step_name: str, failure: Failure) -> CaseReport:
        """Build what the phase itself answers for. `stamp` adds the rest."""
        suite, case, arguments = split_nodeid(nodeid)
        self.built += 1
        return CaseReport(
            outcome=outcome,
            test_suite=suite,
            test_case=case,
            arguments=arguments,
            step_name=step_name,
            exception=failure.exception,
            exception_message=truncate(failure.message, MAX_MESSAGE_CHARS),
            exception_traceback=truncate(failure.traceback, MAX_TRACEBACK_CHARS),
        )

    def _case(self, nodeid: str) -> Case:
        return self._cases.setdefault(nodeid, Case())

    def _ship(self, nodeid: str) -> None:
        """Ship everything one test produced, stamped and closed out.

        Nothing can tell as a phase ends whether it was the test's last - more
        phases may follow, and after the last phase a rerun may follow - and
        the attributes are not final until the test is. So a case is kept here
        until the test is done, and leaves in one piece: every report stamped
        with what the test finally said about itself, the last one flagged.
        """
        case = self._cases.pop(nodeid, None)
        if case is None or not case.reports:
            return
        case.reports[-1].last_report = True
        for report in case.reports:
            stamp(report, case.attributes)
            self.shipper.submit(report)

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Note that a fresh attempt at this test has begun."""
        self._case(nodeid).rerun_pending = False

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Turn one phase report into a case report."""
        meta: Meta = getattr(report, META_ATTR, None) or {}
        case = self._case(report.nodeid)
        case.attributes = meta.get("attributes") or case.attributes
        if report.outcome == RERUN_OUTCOME:
            # pytest-rerunfailures marks the failure it is about to retry, and
            # rewrites the crashed-worker report below the same way.
            case.rerun_pending = True
        if report.when not in PHASE_STEPS:
            self._worker_crashed(report, case)
            return
        if report.when == "setup":
            case.running = True
        elif report.when == "teardown":
            case.running = False
        case.reports.append(
            self._build(
                report.nodeid,
                outcome_of(report),
                PHASE_STEPS[report.when],
                Failure(
                    exception=meta.get("exception"),
                    message=meta.get("exception_message"),
                    traceback=report.longreprtext or None,
                ),
            ),
        )

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        """Close the case out, unless another attempt at it is coming."""
        case = self._cases.get(nodeid)
        if case is not None and not case.rerun_pending:
            self._ship(nodeid)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Report a collection error, which no phase report will describe.

        Under xdist the controller re-fires this for the worker that failed,
        deduplicated across workers, so it arrives here exactly once.
        """
        if report.failed:
            case = self._case(report.nodeid)
            case.attributes = self.attributes.snapshot()
            case.reports.append(
                self._build(
                    report.nodeid,
                    OUTCOMES["error"],
                    COLLECTION_STEP,
                    Failure(
                        exception="CollectError",
                        message=f"collection of {report.nodeid} failed",
                        traceback=report.longreprtext or None,
                    ),
                ),
            )
            self._ship(report.nodeid)

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
        the worker said before it died. It is terminal unless the test is being
        re-queued, which pytest-rerunfailures says by rewriting the outcome.
        """
        case.running = False
        case.reports.append(
            self._build(
                report.nodeid,
                OUTCOMES["crashed"],
                PHASE_STEPS["teardown"],
                Failure(
                    exception="WorkerCrash",
                    message=report.longreprtext or "the worker running it died",
                ),
            ),
        )
        if not case.rerun_pending:
            self._ship(report.nodeid)

    def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        for nodeid, case in list(self._cases.items()):
            if case.running:
                # It started and never reached teardown, so nothing else will
                # close this case out. The step is Teardown on purpose: every
                # case in the index then ends with one, crashed or not.
                case.reports.append(
                    self._build(
                        nodeid,
                        OUTCOMES["crashed"],
                        PHASE_STEPS["teardown"],
                        Failure(
                            exception="TestIncomplete",
                            message=f"never reached teardown: {reason}",
                        ),
                    ),
                )
            # Held because a rerun was expected that the run never got to, or
            # because the case never finished. Either way this was its last.
            self._ship(nodeid)
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
    settings = ReporterConfig.from_pytest(config)
    attributes = CaseAttributes(settings)
    if not settings.enabled:
        # Registered anyway, so that a fixture setting attributes goes on
        # working - it just has nobody listening.
        config.pluginmanager.register(ElasticPlugin(attributes), PLUGIN_NAME)
        return
    worker, controller = is_xdist_worker(config), is_xdist_controller(config)
    if not controller:
        # Reports are made here, so this is where they can be annotated - and,
        # on a worker, what test code reaching for `getplugin` finds.
        annotator = ReportAnnotator(attributes)
        config.pluginmanager.register(annotator, PLUGIN_NAME if worker else ANNOTATOR_NAME)
    if not worker:
        # The one line to change for a real run: anything with .send(list[dict]).
        reporter = ElasticCaseReporter(settings, MockElasticSender(settings.url), attributes)
        reporter.shipper.start()
        config.pluginmanager.register(reporter, PLUGIN_NAME)
