# Copyright (c) 2026 Heknon. Swap in whatever notice your project uses;
# ruff's CPY001 only checks that there is one.
"""Turn every pytest step into a case report, and hand them to your hook in bulk.

    pytest -p elastic_reporter

Standalone: a single module, no package, nothing to install beyond pytest 8
and pydantic 2 on Python 3.12 or newer. Drop it next to your ``conftest.py`` and load it
with ``-p elastic_reporter``, or name it in ``pytest_plugins`` in the rootdir
conftest.

The hook
--------
The plugin builds the reports and calls `pytest_case_reports` with a batch of
them. Sending is yours::

    # conftest.py
    import httpx


    def pytest_case_reports(reports):
        httpx.post(URL, json=[report.to_dict() for report in reports])

It is called from a background thread in the process that owns the stream -
the xdist controller, or the session itself - when a batch fills up or when
`FLUSH_INTERVAL` has passed, whichever comes first, and once more at the end of
the run to drain what is left. So a thousand tests are a handful of calls
rather than three thousand.

Two things follow from it being a background thread. Your implementation
should send data and nothing else, because pytest's own objects are not yours
to touch from there. And if your endpoint stops answering, the queue has a
ceiling, after which reports are dropped and counted rather than growing until
the machine runs out of memory. The count is in the end-of-run summary, and an
implementation that raises is counted the same way rather than failing the run.

Outcome and step status
-----------------------
``step_status`` is what one step did: the setup passed, the call failed, the
teardown passed. Every report has one.

``outcome`` is the verdict of the whole test, and only the last report of a
test carries it. Everything before it leaves outcome empty. So a test that
fails in its call step produces three reports: setup with step status passed
and no outcome, call with step status failed and no outcome, teardown with
step status passed and outcome failed.

That is what makes a run that died mid-test readable. The reports that did get
out have no outcome, so a test with reports but no verdict is a test that
started and was never heard from again.

When reports leave
------------------
The setup and call reports are queued the moment they reach the controller, so
the evidence is out before anything can go wrong. The teardown report waits
until pytest says the test is finished, a fraction of a second later, because
that is when the verdict is known and when it is certain that no rerun is
coming.

Each report carries the attributes as they stood when its step ended. So a
machine set by a fixture during setup is on all three reports, while one set
inside the test body is only on the call and teardown reports.

Tests that never ran at all
---------------------------
This plugin only reports tests that started. A test the run never reached
leaves nothing behind, so nothing can be drawn for it. If you want those too,
write them yourself after collection, where you know what was planned::

    def pytest_collection_modifyitems(config, items):
        worker = getattr(config, "workerinput", {}).get("workerid")
        if worker not in (None, "gw0"):   # every worker collects; send once
            return
        send([CaseReport(test_suite=..., test_case=..., machine=...) for item in items])

Leave outcome empty on those, and a test that never runs keeps no verdict.
`CaseReport` and `CaseReport.to_dict` are exported so your documents match
the plugin's exactly, and the model checks what you built before it leaves.

Setting a test's attributes
---------------------------
`machine`, `vc`, `cycle_id`, `owner` and the rest describe the test rather than
the step, and they come from everywhere - a conftest hook, a fixture that
connects to the machine, the test body itself. So there is one way to set any
of them, from anywhere that can see a pytest ``config``::

    plugin = request.config.pluginmanager.getplugin("elastic-reporter")
    plugin.set(vc="fw-4.2.1", machine="rack1-dut7")

or, with no ``config`` to hand, `reporter`::

    from elastic_reporter import reporter

    def test_upgrade():
        reporter().set(vc="fw-5.0.0")

It takes any of `SETTABLE` - every field of `CaseReport` except the ones a step
answers for itself - and whatever is never set keeps the default the model
gives it. The plugin adds no options of its own: a value for the whole run is a
line in a conftest hook, reading it from wherever you keep it::

    def pytest_sessionstart(session):
        # runs in every process, controller and xdist worker alike
        session.config.pluginmanager.getplugin("elastic-reporter").set(
            vc=os.environ["FIRMWARE"],
            cycle_id=int(os.environ["CYCLE"]),
        )

Values stick until changed, so a process can set one and forget it.

Reruns
------
A test can be attempted more than once: pytest-rerunfailures retries a failure
in place, and under xdist it also re-queues a test whose worker died. Every
attempt reports its own steps, and only the final attempt's last report carries
a verdict. What says a test is done is ``pytest_runtest_logfinish`` without a
rerun pending, where "pending" means this attempt logged a report that
pytest-rerunfailures marked ``rerun`` (it marks the failing report of an
attempt it is about to retry, and rewrites the crashed-worker report the same
way before the controller sees it).

The vocabulary
--------------
`OUTCOMES` and `PHASE_STEPS` are the only place the strings that land in
elastic are decided, and `VERDICT_ORDER` is the only place it is decided which
step status wins when a test's steps disagree. Remap or reorder them from a
conftest if your index speaks differently.

Under xdist
-----------
The controller owns the stream, so your hook is called there and nowhere else:
one process sending, one ordered stream, one verdict per test however many
workers ran it. What only a worker can know - the test's attributes as it ran,
the exception that was raised - is read on the worker by `ReportAnnotator` and
attached to the report, which pytest serialises across for us. So setting an
attribute on a worker needs nothing of the controller. It describes that
worker's tests only, each worker being its own process, so a value meant for
the whole run belongs in a hook every process runs, like the
``pytest_sessionstart`` above in the rootdir conftest.
"""

import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Generator

    from pluggy import HookRelay

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

#: A batch goes out when it reaches BATCH_SIZE reports or when FLUSH_INTERVAL
#: seconds have passed, whichever comes first. The interval is also the loss
#: window: if the machine is killed, what is lost is at most one interval of
#: reports. A thousand reports is a few megabytes at the very worst, which is
#: within what a bulk endpoint wants in one request.
BATCH_SIZE = 1_000
FLUSH_INTERVAL = 5.0

#: How long the end of the run waits for the queue to drain before giving up
#: and saying so, rather than hanging the run on a dead endpoint.
SHUTDOWN_TIMEOUT = 15.0

#: How long the thread ever blocks in one go. It is not the flush interval: a
#: thread blocked for FLUSH_INTERVAL would not notice the end of the run until
#: it expired, and every run would pay that on the way out.
POLL_INTERVAL = 0.1

#: How many reports may be waiting, and how many failing hook calls are worth
#: describing. Both bound what an endpoint that has stopped answering can cost
#: the run: a test never waits on the hook, so without a ceiling the queue
#: behind it is the run's memory.
MAX_QUEUED = 10_000
MAX_ERRORS = 20

#: The outcome and step status strings that land in elastic, and the step name
#: that goes with each phase. Remap a value here - or reassign either dict from
#: a conftest - if your index speaks a different vocabulary.
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

#: Which step status becomes the test's verdict when the steps disagree, worst
#: first. A test that passed but whose teardown blew up is an error; a test
#: that was skipped in setup is skipped, not passed.
VERDICT_ORDER = ("crashed", "error", "failed", "xfailed", "xpassed", "skipped", "passed")

#: pytest's own names for the phases, and for the collection that precedes
#: them. Remapped like `OUTCOMES`, if elastic wants them said differently.
PHASE_STEPS = {"setup": "setup", "call": "call", "teardown": "teardown"}

COLLECTION_STEP = "collect"

#: What the annotator sends with a report for the reporter to build from.
type Meta = dict[str, Any]

#: One case report, as elastic wants it.
type Document = dict[str, Any]

#: A makereport wrapper: hand the report on, having read it.
type ReportWrapper = Generator[None, pytest.TestReport, pytest.TestReport]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class CaseReport(BaseModel):
    """Minimal case report model for elastic."""

    # A misspelt field is a document elastic will happily index under a
    # mapping you did not mean, so refuse it here instead.
    model_config = ConfigDict(extra="forbid")

    test_suite: str
    test_case: str
    outcome: str | None = None  # the verdict, and only on a test's last report
    step_name: str | None = None
    step_status: str | None = None  # what this one step did
    arguments: str | None = None
    machine: str | None = None  # set per test, see `CaseAttributes`
    vc: str = "mock-vc"
    cycle_id: int = 1
    owner: str = "mock-owner"
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    exception: str | None = None
    exception_message: str | None = None
    exception_traceback: str | None = None
    last_report: bool = False  # Whether its the last report to be written.
    labs3: bool = True

    def to_dict(self) -> Document:
        """Convert to dict for elastic.

        Ready for ``json.dumps`` as it stands: the timestamps come out as ISO
        8601 strings rather than datetimes.
        """
        result = self.model_dump(mode="json")
        result["@timestamp"] = result["time"]
        return result


@dataclass(frozen=True)
class Failure:
    """What went wrong in one step.

    A dataclass and not a model: it never leaves the process, so there is
    nothing here for validation to protect. The same goes for `Case`.
    """

    exception: str | None = None
    message: str | None = None
    traceback: str | None = None


#: A step that went fine. Frozen, so one of it is enough.
NO_FAILURE = Failure()


#: What the plugin fills in itself: what a step did, and which test it was.
#: Everything else describes the test and belongs to `CaseAttributes`, which
#: must not be able to rewrite a report's identity - `set(test_case=...)`, one
#: keystroke from `set(test_suite=...)`, would collapse a run into one case.
PLUGIN_FIELDS = frozenset(
    {
        "test_suite",
        "test_case",
        "arguments",
        "outcome",
        "step_name",
        "step_status",
        "exception",
        "exception_message",
        "exception_traceback",
        "time",
        "last_report",
    },
)

#: Every attribute `CaseAttributes.set` will take, derived from the model, so a
#: new field on `CaseReport` is settable without being named twice.
SETTABLE = frozenset(CaseReport.model_fields) - PLUGIN_FIELDS


#: One validator per settable field, so `CaseAttributes.set` can check a value
#: against the model without an instance to assign it to.
FIELD_TYPES = {
    name: TypeAdapter(field.annotation)
    for name, field in CaseReport.model_fields.items()
    if name in SETTABLE
}


def stamp(report: CaseReport, attributes: dict[str, Any]) -> None:
    """Write a test's attributes onto one of its reports."""
    for name, value in attributes.items():
        if name in SETTABLE:
            setattr(report, name, value)


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


class CaseReportHooks:
    """The hook this plugin adds. Implement it in a conftest or a plugin."""

    @pytest.hookspec
    def pytest_case_reports(self, reports: list[CaseReport]) -> None:
        """Receive a batch of case reports, built and ready to send.

        Called from a background thread in the process that owns the stream -
        the xdist controller, or the session itself - when a batch fills up,
        when `FLUSH_INTERVAL` has passed, and once at the end of the run. The
        reports are in the order they were made.

        Nothing is retried for you: this is the whole of the plugin's delivery,
        and what to do with it is yours::

            def pytest_case_reports(reports):
                httpx.post(URL, json=[report.to_dict() for report in reports])

        Send data and nothing else from here, because it is not pytest's own
        thread. Raising is not fatal - the run does not exist to serve the
        reporting - but the batch is lost and the failure is shown at the end
        of the run.

        One catch, and it is pytest's rather than this plugin's: implementing
        a hook nobody has declared is an error, so a conftest with this in it
        cannot run without the plugin loaded - which is also how the reporting
        is switched off, there being no option for it. Where that can happen,
        say so, and pytest leaves the implementation alone instead::

            @pytest.hookimpl(optionalhook=True)
            def pytest_case_reports(reports):
                ...
        """


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """Add `pytest_case_reports` to the hooks a conftest may implement."""
    pluginmanager.add_hookspecs(CaseReportHooks)


# ---------------------------------------------------------------------------
# The attributes of a test
# ---------------------------------------------------------------------------


class CaseAttributes:
    """Every report field that describes the test rather than the step.

    One store for all of them, because they come from everywhere: a conftest
    hook, a fixture that connects to the machine, the test body. `set` takes
    any of `SETTABLE` from any of those places, and the values stick until
    changed, so a process can set one and forget it.

    Each report carries what was set by the time its step ended. What is never
    set keeps the default `CaseReport` gives it.
    """

    def __init__(self) -> None:
        """Start with nothing set: `CaseReport`'s own defaults stand in."""
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}

    def set(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for this test and the tests after it.

        Each value is checked against the model here, where whoever wrote it
        can see the error, rather than in the middle of a run or, worse, in
        elastic. A value the model can convert is converted: ``cycle_id="77"``
        is stored as the integer 77.
        """
        unknown = sorted(set(attributes) - SETTABLE)
        if unknown:
            message = (
                f"not a case attribute: {', '.join(unknown)} "
                f"(settable: {', '.join(sorted(SETTABLE))})"
            )
            raise ValueError(message)
        checked = {
            name: FIELD_TYPES[name].validate_python(value) for name, value in attributes.items()
        }
        with self._lock:
            self._values.update(checked)

    def snapshot(self) -> dict[str, Any]:
        """Return the attributes as they stand, to stamp a report with."""
        with self._lock:
            return dict(self._values)


class ElasticPlugin:
    """What ``getplugin("elastic-reporter")`` hands you, in any process.

    A session registers one under that name whichever half it needs: the
    reporter where the stream is built, the annotator on an xdist worker. This
    plain one is what `reporter` answers with outside a session, where there is
    nobody to report to. All three set attributes the same way, so nothing that
    sets one needs to know which it got.
    """

    #: The one this process is using, for code with no ``config`` to hand.
    #: Read it through `reporter`, which is there even before a session is.
    current: ClassVar["ElasticPlugin | None"] = None

    def __init__(self, attributes: CaseAttributes) -> None:
        """Answer for ``attributes``, which every half of a session shares."""
        self.attributes = attributes
        ElasticPlugin.current = self

    def set(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for this test and the tests after it."""
        self.attributes.set(**attributes)


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class ReportQueue:
    """A queue, a thread, and batches handed to the hook.

    Order is preserved: one producer-facing queue, one consumer thread. A batch
    the hook could not take is dropped rather than retried - a run must not end
    up reporting for longer than it spent running tests - and the failure is
    kept for the end-of-run summary.
    """

    def __init__(self, hook: "HookRelay") -> None:
        """Hand batches to ``hook`` from a thread of its own."""
        self._hook = hook
        self._queue: queue.Queue[CaseReport] = queue.Queue(MAX_QUEUED)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="elastic-reporter", daemon=True)
        self.sent = 0
        self.failures = 0
        self.errors: list[str] = []
        # One counter per thread: `+=` is a read, an add and a store, so a
        # single one shared between the producer and the consumer would lose
        # updates - and this is the number a degraded run exists to report.
        self._dropped_full = 0
        self._dropped_failed = 0

    @property
    def dropped(self) -> int:
        """How many reports never reached the hook, for whatever reason."""
        return self._dropped_full + self._dropped_failed

    def start(self) -> None:
        """Start the reporting thread."""
        self._thread.start()

    def submit(self, report: CaseReport) -> None:
        """Queue one report for the next batch, unless the queue is full.

        Full means the hook is not keeping up with the run. A test must not
        wait on the reporting and the run must not be failed over it, so what
        is left is to drop the report and say how many went that way.
        """
        try:
            self._queue.put_nowait(report)
        except queue.Full:
            self._dropped_full += 1

    def close(self, timeout: float | None = None) -> None:
        """Drain the queue and stop the thread, or record that it would not."""
        # Read now rather than bound as a default, so that reassigning the
        # constant from a conftest works the way it does for the others.
        timeout = SHUTDOWN_TIMEOUT if timeout is None else timeout
        self._stop.set()
        if not self._thread.is_alive():
            if not self._queue.empty():
                self._record(f"the thread was gone with {self._queue.qsize()} report(s) left")
            return
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._record(f"the hook did not drain the queue within {timeout:g}s")

    def _run(self) -> None:
        batch: list[CaseReport] = []
        deadline = monotonic() + FLUSH_INTERVAL
        while True:
            # Never block for longer than a poll: stopping has to be noticed
            # promptly, and the flush interval is kept by the deadline below
            # rather than by how long this waits.
            stopping = self._stop.is_set()
            wait = 0.0 if stopping else min(POLL_INTERVAL, max(0.0, deadline - monotonic()))
            try:
                report = self._queue.get(timeout=wait)
            except queue.Empty:
                if stopping:
                    # Told to stop with nothing left to take: send and finish.
                    self._flush(batch)
                    return
                if monotonic() >= deadline:
                    batch, deadline = self._flush(batch), monotonic() + FLUSH_INTERVAL
                continue
            batch.append(report)
            if len(batch) >= BATCH_SIZE or monotonic() >= deadline:
                batch, deadline = self._flush(batch), monotonic() + FLUSH_INTERVAL

    def _flush(self, batch: list[CaseReport]) -> list[CaseReport]:
        if not batch:
            return []
        try:
            self._hook.pytest_case_reports(reports=batch)
        # BaseException rather than Exception: a SystemExit out of an
        # implementation would otherwise end this thread without a word, and
        # every report after it would pile up in a queue nobody is draining.
        except BaseException as exc:  # noqa: BLE001 - never fail a run over telemetry
            self._dropped_failed += len(batch)
            self._record(f"{len(batch)} report(s) dropped: {type(exc).__name__}: {exc}")
        else:
            self.sent += len(batch)
        return []

    def _record(self, error: str) -> None:
        """Keep the first few failures, and count the rest.

        An endpoint that is down says the same thing every batch, so the count
        is the part that is news.
        """
        self.failures += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(error)


# ---------------------------------------------------------------------------
# The worker half
# ---------------------------------------------------------------------------


class ReportAnnotator(ElasticPlugin):
    """Pins what this process knows about the test to its step reports.

    Registered wherever tests actually run - an xdist worker, or the session
    itself without xdist. The report is the only thing that crosses to the
    controller, so anything the reporter needs has to leave from here.
    """

    def __init__(self, attributes: CaseAttributes) -> None:
        """Answer for ``attributes``, and remember what has been annotated."""
        super().__init__(attributes)
        self._annotated: list[pytest.TestReport] = []

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, call: pytest.CallInfo[None]) -> ReportWrapper:
        """Attach this process's answers to the step report."""
        report = yield
        # A skip and an expected failure both raise, and neither is something
        # that went wrong, so neither leaves an exception on the report. Any
        # query for "did anything go wrong" would otherwise count them all.
        exception, message = (None, None) if report.skipped else describe_exception(call)
        meta: Meta = {
            "attributes": self.attributes.snapshot(),
            "exception": exception,
            "exception_message": truncate(message, MAX_MESSAGE_CHARS),
        }
        setattr(report, META_ATTR, meta)
        self._annotated.append(report)
        return report

    def pytest_runtest_logfinish(self) -> None:
        """Drop what was annotated, now that it has been logged and sent.

        The reports have gone to the controller by now, and pytest keeps them
        for the rest of the session - without this, so would the dicts.
        """
        for report in self._annotated:
            if hasattr(report, META_ATTR):
                delattr(report, META_ATTR)
        self._annotated.clear()


# ---------------------------------------------------------------------------
# The stream owner
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """What the reporter remembers about one test while it runs.

    ``statuses`` is what the steps of the current attempt did, which decides
    the verdict; ``attributes`` is the latest answer from wherever the test
    ran; ``held`` is the teardown report, waiting for pytest to say the test is
    finished so it can carry that verdict; ``running`` says setup was seen and
    teardown was not; and ``rerun_pending`` says this attempt is to be retried,
    so the test is not done yet.
    """

    statuses: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    held: CaseReport | None = None
    running: bool = False
    rerun_pending: bool = False


class ElasticCaseReporter(ElasticPlugin):
    """Turns pytest reports into case reports and queues them for the hook.

    Registered where the reports all come together: the xdist controller, or
    the session itself without xdist.
    """

    def __init__(self, hook: "HookRelay", attributes: CaseAttributes) -> None:
        """Report through ``hook``, stamped from ``attributes``."""
        super().__init__(attributes)
        self.queue = ReportQueue(hook)
        self._cases: dict[str, Case] = {}
        self._closed = False
        self.built = 0

    # -- building ------------------------------------------------------------

    def _build(
        self,
        nodeid: str,
        step_name: str,
        status: str,
        case: Case,
        failure: Failure = NO_FAILURE,
    ) -> CaseReport:
        """Build what the step itself answers for, stamped with the test's."""
        suite, name, arguments = split_nodeid(nodeid)
        self.built += 1
        report = CaseReport(
            test_suite=suite,
            test_case=name,
            arguments=arguments,
            step_name=step_name,
            step_status=OUTCOMES[status],
            exception=failure.exception,
            # Already bounded: the annotator cuts the message before sending it
            # across, and the ones built here are a sentence. Cutting twice
            # would eat the first marker and miscount what was dropped.
            exception_message=failure.message,
            exception_traceback=truncate(failure.traceback, MAX_TRACEBACK_CHARS),
        )
        stamp(report, case.attributes)
        return report

    def _case(self, nodeid: str) -> Case:
        return self._cases.setdefault(nodeid, Case())

    def _finish(self, nodeid: str, case: Case, report: CaseReport) -> None:
        """Send a test's last report, carrying the verdict, and forget the test."""
        report.outcome = OUTCOMES[verdict_of(case.statuses)]
        report.last_report = True
        self.queue.submit(report)
        self._cases.pop(nodeid, None)

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Note that a fresh attempt at this test has begun."""
        case = self._case(nodeid)
        case.rerun_pending = False
        case.statuses.clear()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Turn one step report into a case report."""
        meta: Meta = getattr(report, META_ATTR, None) or {}
        # pytest keeps every report for the whole session, so what we hung on
        # this one would be kept too. It has been read; let it go.
        if hasattr(report, META_ATTR):
            delattr(report, META_ATTR)
        case = self._case(report.nodeid)
        case.attributes = meta.get("attributes") or case.attributes
        status = status_of(report)
        if status == "rerun":
            # pytest-rerunfailures marks the failure it is about to retry, and
            # rewrites the crashed-worker report below the same way.
            case.rerun_pending = True
        if report.when not in PHASE_STEPS:
            self._worker_crashed(report, case)
            return
        case.statuses.append(status)
        built = self._build(
            report.nodeid,
            PHASE_STEPS[report.when],
            status,
            case,
            Failure(
                exception=meta.get("exception"),
                message=meta.get("exception_message"),
                traceback=report.longreprtext or None,
            ),
        )
        if report.when == "teardown":
            # The only one held: a moment later pytest says the test is done,
            # and then this report can carry the verdict.
            case.running = False
            case.held = built
        else:
            case.running = case.running or report.when == "setup"
            self.queue.submit(built)

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        """Close the test out, unless another attempt at it is coming."""
        case = self._cases.get(nodeid)
        if case is None:
            return
        held, case.held = case.held, None
        if case.rerun_pending:
            # Not the last attempt, so this teardown gets no verdict.
            if held is not None:
                self.queue.submit(held)
            return
        if held is None:
            held = self._build(
                nodeid,
                PHASE_STEPS["teardown"],
                "crashed",
                case,
                Failure(exception="TestIncomplete", message="the test logged no teardown"),
            )
            case.statuses.append("crashed")
        self._finish(nodeid, case, held)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Report a collection error, which no step report will describe.

        Under xdist the controller re-fires this for the worker that failed,
        deduplicated across workers, so it arrives here exactly once.
        """
        if report.failed:
            case = self._case(report.nodeid)
            case.attributes = self.attributes.snapshot()
            case.statuses.append("error")
            self._finish(
                report.nodeid,
                case,
                self._build(
                    report.nodeid,
                    COLLECTION_STEP,
                    "error",
                    case,
                    Failure(
                        exception="CollectError",
                        message=f"collection of {report.nodeid} failed",
                        traceback=report.longreprtext or None,
                    ),
                ),
            )

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Close every test the run left open, then drain the queue."""
        self._close(reason=exit_reason(session, exitstatus))
        summarise(session.config, self)

    def pytest_unconfigure(self) -> None:
        """Back stop: sessionfinish is not reached if configure-time work blew up.

        Also lets go of the session: `ElasticPlugin.current` holds this, which
        holds the hook relay, which holds pytest's whole config. A process that
        runs pytest more than once would keep every one of them.
        """
        self._close(reason="the session was unconfigured")
        ElasticPlugin.current = None

    # -- the tests that do not end by themselves -----------------------------

    def _worker_crashed(self, report: pytest.TestReport, case: Case) -> None:
        """Turn xdist's stand-in for a dead worker into a last report.

        The stand-in knows only the nodeid, so the test is described by what
        the worker said before it died. It is the last unless the test is being
        re-queued, which pytest-rerunfailures says by rewriting the outcome.
        """
        case.running = False
        case.statuses.append("crashed")
        held, case.held = case.held, None
        if held is not None:
            # Its teardown was made and the worker died before pytest could say
            # the test was finished. It is not the last report any more, but it
            # is what the test did, so it still goes.
            self.queue.submit(held)
        built = self._build(
            report.nodeid,
            PHASE_STEPS["teardown"],
            "crashed",
            case,
            Failure(
                exception="WorkerCrash",
                message=report.longreprtext or "the worker running it died",
            ),
        )
        if case.rerun_pending:
            self.queue.submit(built)
        else:
            self._finish(report.nodeid, case, built)

    def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        for nodeid, case in list(self._cases.items()):
            held, case.held = case.held, None
            if held is not None:
                # Its teardown was made but the test never closed, so the run
                # ended in between. It is the last report either way.
                self._finish(nodeid, case, held)
            else:
                # It is in here at all because it started, and it is still in
                # here because it never finished: it never reached teardown, or
                # its rerun never happened, or it died before its first step
                # was reported. Nothing else will close it out. The step is
                # teardown on purpose: every test in the index then ends with
                # one, crashed or not.
                case.statuses.append("crashed")
                self._finish(
                    nodeid,
                    case,
                    self._build(
                        nodeid,
                        PHASE_STEPS["teardown"],
                        "crashed",
                        case,
                        Failure(
                            exception="TestIncomplete",
                            message=f"never reached teardown: {reason}",
                        ),
                    ),
                )
        self.queue.close()


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


def status_of(report: pytest.TestReport) -> str:
    """Return what one step did, as a key of `OUTCOMES`.

    A failing setup or teardown is an error rather than a failure: the test
    itself never got a verdict from it.
    """
    if report.outcome == RERUN_OUTCOME:
        return "rerun"
    if getattr(report, "wasxfail", None) is not None:
        return "xpassed" if report.passed and report.when == "call" else "xfailed"
    if report.skipped:
        return "skipped"
    if report.passed:
        return "passed"
    return "failed" if report.when == "call" else "error"


def verdict_of(statuses: list[str]) -> str:
    """Return the verdict of a test whose steps did ``statuses``."""
    for verdict in VERDICT_ORDER:
        if verdict in statuses:
            return verdict
    return "crashed"  # it did nothing we can describe, so it did not finish


def describe_exception(call: pytest.CallInfo[None] | None) -> tuple[str | None, str | None]:
    """Return the exception's type and message, if the step raised one."""
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
    """Say why the run ended, for the tests it ended in the middle of."""
    if getattr(session, "shouldstop", False):
        return f"the session stopped early ({session.shouldstop})"
    if getattr(session, "shouldfail", False):
        return f"the session was failed early ({session.shouldfail})"
    try:
        status = pytest.ExitCode(exitstatus).name.lower().replace("_", " ")
    except ValueError:
        status = f"exit status {exitstatus}"
    return f"the session ended ({status})"


def summarise(config: pytest.Config, plugin: ElasticCaseReporter) -> None:
    """Write what was reported, and anything the hook made of it.

    Nothing at all when nobody implements the hook, so a plugin installed for
    one suite says nothing in every other suite in that environment. By now
    every conftest has been loaded, including the ones in subdirectories,
    which is why this is decided here and not at configure time.
    """
    terminal = config.pluginmanager.get_plugin("terminalreporter")
    if terminal is None or not config.hook.pytest_case_reports.get_hookimpls():
        return
    reports = plugin.queue
    terminal.write_sep("-", f"elastic-reporter: {reports.sent}/{plugin.built} case report(s)")
    for error in reports.errors:
        terminal.write_line("  elastic-reporter: pytest_case_reports " + error, red=True)
    if reports.failures > len(reports.errors):
        terminal.write_line(
            f"  elastic-reporter: and {reports.failures - len(reports.errors)} more like it",
            red=True,
        )
    if reports.dropped:
        terminal.write_line(
            f"  elastic-reporter: {reports.dropped} report(s) never reached elastic",
            red=True,
        )


def reporter() -> ElasticPlugin:
    """Return this process's plugin, for code with no ``config`` to hand.

    ::

        from elastic_reporter import reporter

        def test_upgrade():
            reporter().set(vc="fw-5.0.0")

    The same object ``getplugin("elastic-reporter")`` returns, and the same
    `ElasticPlugin.set`. Outside a session - the module imported but no pytest
    configured, a unit test of your own helpers - it is one that accepts
    attributes and drops them, so this never returns None and never raises.
    """
    current = ElasticPlugin.current
    if current is None:
        current = ElasticPlugin(CaseAttributes())
    return current


def is_xdist_worker(config: pytest.Config) -> bool:
    """Say whether this process is an xdist worker."""
    return hasattr(config, "workerinput")


def is_xdist_controller(config: pytest.Config) -> bool:
    """Say whether this process is an xdist controller with workers to come."""
    if is_xdist_worker(config):
        return False
    return getattr(config.option, "dist", "no") not in ("no", None)


def check_vocabulary(*tables: tuple[str, dict[str, Any]]) -> None:
    """Fail now, not mid-run, if the vocabulary was remapped to non-strings.

    `OUTCOMES` and `PHASE_STEPS` end up on a model that says these fields are
    strings, so a conftest mapping one to, say, a status number would raise
    out of a pytest hook halfway through the session. Better here.
    """
    wrong = sorted(
        f"{name}[{key!r}]"
        for name, table in tables
        for key, value in table.items()
        if not isinstance(value, str)
    )
    if wrong:
        message = f"the vocabulary has to be strings: {', '.join(wrong)}"
        raise TypeError(message)


def pytest_configure(config: pytest.Config) -> None:
    """Register the half of the plugin this process is responsible for."""
    check_vocabulary(("OUTCOMES", OUTCOMES), ("PHASE_STEPS", PHASE_STEPS))
    attributes = CaseAttributes()
    detached = ElasticPlugin.current
    if type(detached) is ElasticPlugin:
        # Something set attributes through `reporter` before there was a
        # session - a conftest at import time, say. Those would otherwise be
        # written to a plugin nobody reports through and quietly lost.
        attributes.set(**detached.attributes.snapshot())
    worker, controller = is_xdist_worker(config), is_xdist_controller(config)
    if not controller:
        # Reports are made here, so this is where they can be annotated - and,
        # on a worker, what test code reaching for `getplugin` finds.
        annotator = ReportAnnotator(attributes)
        config.pluginmanager.register(annotator, PLUGIN_NAME if worker else ANNOTATOR_NAME)
    if not worker:
        plugin = ElasticCaseReporter(config.hook, attributes)
        plugin.queue.start()
        config.pluginmanager.register(plugin, PLUGIN_NAME)
