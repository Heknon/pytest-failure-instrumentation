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

When something goes wrong
-------------------------
Nothing here is worth a test run, so nothing here can fail one. Every hook of
this plugin does its work inside a ``try``: a bug in the plugin, a report that
is not what it claims to be, a vocabulary remapped with a word missing - each
is caught, kept, and shown at the end of the run, and the next test is reported
as though it had not happened.

You hear about it three ways: a warning, which is also how trouble on an xdist
worker reaches you; a red line under the end-of-run summary; and the sent over
built count in that summary, which is the number to alert on.

The same holds for your endpoint. A hook that raises loses its batch and says
so. A hook that hangs is given `SHUTDOWN_TIMEOUT` at the end of the run and
then left behind. A hook that cannot keep up fills the queue, and the reports
past `MAX_QUEUED` are dropped and counted rather than grown into a run that
reports for longer than it tested.

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
`machine`, `vc`, `cycle_id`, `cycle_start_time`, `owner` and the rest describe
the test rather than the step, and they come from everywhere - a conftest hook, a fixture that
connects to the machine, the test body itself. There are two ways to set one,
and which you want is the question of how long it is true for.

`ElasticPlugin.set_global` is for what is true of the whole run. Set it once,
in a hook every process runs::

    def pytest_sessionstart(session):
        # runs in every process, controller and xdist worker alike
        session.config.pluginmanager.getplugin("elastic-reporter").set_global(
            vc=os.environ["FIRMWARE"],
            cycle_id=int(os.environ["CYCLE"]),
        )

`ElasticPlugin.set_test` is for what one test answers for. It applies to the
test running now and to no other: the next test starts without it, whatever
this one did - passed, failed, or killed the worker it was running on. Nothing
has to be put back by hand::

    @pytest.fixture
    def dut(request):
        device = lab.allocate(...)
        request.config.pluginmanager.getplugin("elastic-reporter").set_test(
            machine=device.name, hostname=socket.gethostname()
        )
        yield device
        device.close()

Either one, with no ``config`` to hand, through `reporter`::

    from elastic_reporter import reporter

    def test_upgrade():
        reporter().set_test(vc="fw-5.0.0")

Both take any of `SETTABLE` - every field of `CaseReport` except the ones a
step answers for itself - and whatever is never set keeps the default the model
gives it. Where both scopes name the same attribute, the test wins. The plugin
adds no options of its own: a value for the whole run is the line above,
reading it from wherever you keep it.

When the cycle began
--------------------
`cycle_start_time` is filled in for you: the moment the run began, as one UTC
string, the same one in every process of that run. The controller decides it
and tells each xdist worker, so a worker that started a minute later - or was
started to replace one that died - reports the cycle that began, not the moment
it personally woke up. Without that, three workers are three cycle start times
in one run, which is what a dashboard grouping by cycle cannot have.

If your cycle began before pytest did - a CI job that built something first -
say so on the controller, before the workers are made, and they are told rather
than asked::

    def pytest_sessionstart(session):
        if hasattr(session.config, "workerinput"):
            return                                    # the controller decides
        reporter().set_global(cycle_start_time=os.environ["CYCLE_START"])

`pytest_configure` works the same way. A value every process computes
identically - an environment variable, not ``datetime.now()`` - needs no guard.

Adding an attribute of your own
-------------------------------
One line. A field on `CaseReport`::

    hostname: str | None = None

and `set_global` and `set_test` take it, checked against the type you gave it,
and it is in every document from then on. `SETTABLE` is derived from the model,
so there is no second list to keep up to date.

Text, a number or a boolean, though - because an attribute set on an xdist
worker travels to the controller on pytest's own report, and that wire carries
nothing else. For a time, use `Timestamp`, which takes a datetime or an ISO
string and stores one UTC string::

    cycle_start_time: Timestamp | None = None

Anything else is refused where it is set, naming the field, rather than inside
xdist with no tests run.

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

import contextlib
import queue
import threading
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

import pytest
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Protocol

    from pluggy import HookRelay

    class WorkerNode(Protocol):
        """The part of an xdist worker node this plugin touches.

        Spelt out rather than imported: xdist is optional, and this is the
        whole of what a controller needs from a node it is setting up.
        """

        workerinput: dict[str, Any]

#: A test report's outcome when pytest-rerunfailures is about to retry it.
RERUN_OUTCOME = "rerun"

#: The name the stream owner is registered under, and the one test code reaches
#: for. Deliberately not the module name: loading the module with
#: ``-p elastic_reporter`` already claims that.
PLUGIN_NAME = "elastic-reporter"
ANNOTATOR_NAME = "elastic-reporter-annotator"

#: The key the controller puts its own start time under in each xdist
#: worker's ``workerinput``. One run, one cycle start time, however many
#: processes it takes and however many of them are started late.
WORKER_START = "elastic_cycle_start_time"

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

#: How many reports may be waiting, and how many failing hook calls are worth
#: describing. Both bound what an endpoint that has stopped answering can cost
#: the run: a test never waits on the hook, so without a ceiling the queue
#: behind it is the run's memory. What waits is whole batches, so this is
#: rounded down to BATCH_SIZE of them, and never below one.
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


def now_text() -> str:
    """Return this moment, the way a `Timestamp` field stores one."""
    return datetime.now(UTC).astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_started(config: pytest.Config) -> str | None:
    """Return when the run began: the same answer in every process of it.

    An xdist worker is told by the controller, through ``workerinput``, and
    takes that answer even when it is None - a worker that started a minute
    later, or was started to replace one that died, must not report a cycle
    that began a minute later. Any other process began when it began.
    """
    inherited = getattr(config, "workerinput", {})
    if WORKER_START in inherited:
        value = inherited[WORKER_START]
        return value if isinstance(value, str) else None
    return now_text()


def as_utc_text(value: object) -> object:
    """Turn a time into one unambiguous string, or say why it is not a time.

    A datetime or an ISO 8601 string in, ``2026-04-01T09:00:00Z`` out: UTC,
    with the same `Z` that the model's own timestamps carry, so two fields of
    one document never disagree about what a time looks like. A datetime with
    no timezone is taken as UTC, because guessing the machine's offset is how
    a cycle ends up an hour wide in elastic.

    A string, and not a `datetime`, because an attribute crosses from an xdist
    worker to the controller on pytest's report - and that wire carries
    strings and numbers, nothing else. See `check_attributes`.
    """
    if isinstance(value, datetime):
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            message = f"not a time: {value!r} (try 2026-04-01T09:00:00Z)"
            raise ValueError(message) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    else:
        return value  # let the model say what it thinks of it
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: A time on a case report: given a datetime or an ISO string, stored as one
#: UTC string. `as_utc_text` says why it is text and not a `datetime`.
Timestamp = Annotated[str, BeforeValidator(as_utc_text)]


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
    cycle_start_time: Timestamp | None = None  # when the cycle this ran in began
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

#: Every attribute `CaseAttributes` will take, derived from the model, so a new
#: field on `CaseReport` is settable without being named anywhere a second time.
SETTABLE = frozenset(CaseReport.model_fields) - PLUGIN_FIELDS


#: What an attribute may be by the time it is stored: what pytest's report
#: serialiser and xdist's wire will both carry, and nothing else.
WIRE_TYPES = (str, int, float, bool, type(None))

#: One validator per settable field, so `check_attributes` can check a value
#: against the model without an instance to assign it to.
FIELD_TYPES = {
    name: TypeAdapter(field.annotation)
    for name, field in CaseReport.model_fields.items()
    if name in SETTABLE
}


def check_attributes(attributes: dict[str, object]) -> dict[str, Any]:
    """Check attributes against the model, where whoever wrote them can see it.

    Rather than in the middle of a run or, worse, in elastic. A value the model
    can convert is converted: ``cycle_id="77"`` is stored as the integer 77.
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
    # An attribute set on an xdist worker reaches the controller on pytest's
    # report, and that wire takes strings, numbers and booleans. A field typed
    # as anything else would not fail here - it would fail inside xdist's
    # dispatcher, as an INTERNALERROR with no tests run. Say it here instead,
    # naming the field, before a single test has been collected.
    unsendable = sorted(
        f"{name}={type(value).__name__}"
        for name, value in checked.items()
        if not isinstance(value, WIRE_TYPES)
    )
    if unsendable:
        message = (
            f"a case attribute has to be text, a number or a boolean, so that it can "
            f"reach the controller from an xdist worker: {', '.join(unsendable)} "
            f"(a time belongs in a `Timestamp` field, which is text)"
        )
        raise TypeError(message)
    return checked


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

    Two scopes, because these come from two kinds of place. What is true of
    the whole run - the firmware under test, the cycle, the team that owns it
    - is set once with `set_global` and stays set. What is true of one test -
    the machine it was given, the host that ran it - is set with `set_test`
    and is gone by the time the next test starts.

    Nothing leaks from one test into the next, and nothing has to be put back
    by hand: the test scope is emptied at the start of every test, in every
    process. Where both scopes name the same attribute, the test wins.

    Each report carries what was set by the time its step ended. What is never
    set keeps the default `CaseReport` gives it.
    """

    def __init__(self) -> None:
        """Start with nothing set: `CaseReport`'s own defaults stand in."""
        self._lock = threading.Lock()
        self._run: dict[str, Any] = {}
        self._test: dict[str, Any] = {}

    def set_global(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for this test and every test after it."""
        checked = check_attributes(attributes)
        with self._lock:
            self._run.update(checked)

    def set_test(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for the test running now, and no other."""
        checked = check_attributes(attributes)
        with self._lock:
            self._test.update(checked)

    def clear_test(self) -> None:
        """Forget what a test set, which is what the start of the next one does."""
        with self._lock:
            self._test.clear()

    def snapshot(self) -> dict[str, Any]:
        """Return the attributes as they stand, to stamp a report with."""
        with self._lock:
            if not self._test:
                return dict(self._run)
            return {**self._run, **self._test}


class ReportingFault(UserWarning):
    """A reporting bug, raised as a warning so it cannot fail the run.

    Warned rather than raised on purpose: a pytest hook that raises takes the
    session with it, and nothing this plugin does is worth a run. Under xdist
    a warning from a worker is carried to the controller and shown in the
    warnings summary, which is the only way a worker can say anything.
    """


def warn(message: str) -> None:
    """Say something about the reporting, without a chance of failing the run.

    A suite with ``filterwarnings = error`` turns a warning into an exception,
    which is exactly the outcome this plugin exists never to cause.
    """
    # BaseException: a warning filter can raise anything it likes, and then
    # the run hears nothing about the reporting and carries on running.
    with contextlib.suppress(BaseException):
        warnings.warn(f"elastic-reporter: {message}", ReportingFault, stacklevel=3)


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
        #: What went wrong in the reporting, first of each kind, bounded.
        self.faults: list[str] = []
        self.previous = ElasticPlugin.current
        ElasticPlugin.current = self

    def set_global(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for this test and every test after it.

        For what is true of the whole run: the firmware under test, the cycle,
        the team that owns it. Set it once, in a hook every process runs.
        """
        self.attributes.set_global(**attributes)

    def set_test(self, **attributes: object) -> None:
        """Set any of `SETTABLE` for the test running now, and no other.

        For what one test answers for: the machine it was given, the host that
        ran it. The next test starts without it, whatever this one did - it
        passed, it failed, it killed the worker it was running on.
        """
        self.attributes.set_test(**attributes)

    def fault(self, where: str, exc: BaseException) -> None:
        """Keep a reporting bug, and say it once. Never raises, whatever it is.

        Every hook of this plugin runs its work inside a `try`, and this is
        what the `except` does with it. The test run carries on reporting: one
        bad report is one bad report, not the end of the stream.
        """
        try:
            # `as_text` and not an f-string: the thing that went wrong may be
            # an exception whose own `__str__` goes wrong, and the type name
            # alone still says which hook and roughly what.
            detail = as_text(exc) or "(no message)"
            line = f"{where}: {type(exc).__name__}: {detail}"[:MAX_MESSAGE_CHARS]
            if line in self.faults or len(self.faults) >= MAX_ERRORS:
                return
            self.faults.append(line)
            warn(line)
        except BaseException:  # noqa: BLE001, S110 - saying so must not fail either
            pass

    def detach(self) -> None:
        """Let go of this session, putting back whatever was current before it.

        A process that runs pytest more than once - `pytest.main` in a loop, a
        suite that tests its own plugins - would otherwise leave `current`
        pointing at a session that is over. Each half of a session detaches
        itself, in whichever order pytest unconfigures them, so this unlinks
        from the middle of the chain as readily as from the end of it.
        """
        if ElasticPlugin.current is self:
            ElasticPlugin.current = self.previous
        else:
            plugin = ElasticPlugin.current
            while plugin is not None:
                if plugin.previous is self:
                    plugin.previous = self.previous
                    break
                plugin = plugin.previous
        self.previous = None


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
        # The batch is filled on the test's own thread and handed over whole.
        # Handing over one report at a time means waking the thread once per
        # report, and a thread woken twelve thousand times takes the GIL off
        # the run that often: measured at 0.38ms a test, against 0.03ms for
        # the work itself.
        self._lock = threading.Lock()
        self._filling: list[CaseReport] = []
        # The ceiling is the queue's own: it holds at most this many batches,
        # plus the one being filled. A count kept alongside it would be one
        # more thing to keep in step, and this cannot drift from what is held.
        self._queue: queue.Queue[list[CaseReport]] = queue.Queue(max(1, MAX_QUEUED // BATCH_SIZE))
        self._stop = threading.Event()
        self._shut = False
        self._thread = threading.Thread(target=self._run, name="elastic-reporter", daemon=True)
        self.sent = 0
        self.failures = 0
        self.dropped = 0
        self.errors: list[str] = []

    def start(self) -> None:
        """Start the reporting thread."""
        self._thread.start()

    def submit(self, report: CaseReport) -> None:
        """Add one report to the batch being filled, and hand it over when full.

        A full queue means the hook is not keeping up with the run. A test must
        not wait on the reporting and the run must not be failed over it, so
        what is left is to drop the batch and say how many went that way.
        """
        with self._lock:
            if self._shut:
                # The run is over and the thread has gone. Whatever made this
                # report, it is too late to send it - so say so rather than
                # let the summary claim everything got out.
                self.dropped += 1
                return
            self._filling.append(report)
            if len(self._filling) < BATCH_SIZE:
                return
            batch, self._filling = self._filling, []
            # Handed over under the lock `_take` holds too, so a batch cannot
            # overtake one still being filled, and the thread cannot find the
            # queue empty while a full batch is on its way to it.
            try:
                self._queue.put_nowait(batch)  # one wake-up per batch, not per report
            except queue.Full:
                self.dropped += len(batch)

    def _take(self) -> list[CaseReport]:
        """Take the batch being filled, however full it is."""
        with self._lock:
            batch, self._filling = self._filling, []
        return batch

    def close(self, timeout: float | None = None) -> None:
        """Drain the queue and stop the thread, or record that it would not."""
        # Read now rather than bound as a default, so that reassigning the
        # constant from a conftest works the way it does for the others.
        timeout = SHUTDOWN_TIMEOUT if timeout is None else timeout
        self._stop.set()
        # Everything meant for this run has been submitted by now: `_close`
        # closes every open test out before it gets here.
        with self._lock:
            self._shut = True
        if not self._thread.is_alive():
            stranded = self._unsent()
            if stranded:
                self._record(f"the thread was gone with {stranded} report(s) left", stranded)
            return
        # Nudge it: it is waiting on the queue, not on the stop flag.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait([])
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._record(f"the hook did not drain the queue within {timeout:g}s")

    def _unsent(self) -> int:
        """Empty what is held and say how many reports it was."""
        left = len(self._take())
        while True:
            try:
                left += len(self._queue.get_nowait())
            except queue.Empty:
                return left

    def _run(self) -> None:
        deadline = monotonic() + FLUSH_INTERVAL
        while True:
            # The wait is the whole interval: `close` nudges the queue, so the
            # end of a run is noticed at once without an idle run waking this
            # thread ten times a second to ask.
            stopping = self._stop.is_set()
            wait = 0.0 if stopping else max(0.0, deadline - monotonic())
            try:
                batch = self._queue.get(timeout=wait)
            except queue.Empty:
                # Nothing was handed over, so take the part-full batch: the
                # interval has passed, or the run is over.
                self._flush(self._take())
                if stopping:
                    return
            else:
                self._flush(batch)
            # After either, so that the hook is called once an interval at
            # most however the batches arrive.
            deadline = monotonic() + FLUSH_INTERVAL

    def _flush(self, batch: list[CaseReport]) -> None:
        if not batch:
            return
        # Counted before the call: the list is the implementation's now, and
        # one that empties it would otherwise make the summary count nothing.
        count = len(batch)
        try:
            self._hook.pytest_case_reports(reports=batch)
        # BaseException rather than Exception: a SystemExit out of an
        # implementation would otherwise end this thread without a word, and
        # every report after it would pile up in a queue nobody is draining.
        except BaseException as exc:  # noqa: BLE001 - never fail a run over telemetry
            self._record(f"{count} report(s) dropped: {type(exc).__name__}: {exc}", count)
        else:
            self.sent += count

    def _record(self, error: str, dropped: int = 0) -> None:
        """Keep the first few failures, count the rest, and what they lost.

        An endpoint that is down says the same thing every batch, so the count
        is the part that is news. Written from the reporting thread and from
        the end of the run, so it takes the lock: no caller may hold it.
        """
        with self._lock:
            self.failures += 1
            self.dropped += dropped
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
        """Attach this process's answers to the step report.

        The `yield` is deliberately outside the `try`: pytest's own report is
        pytest's business, and only the annotating is ours to swallow.
        """
        report = yield
        try:
            # A skip and an expected failure both raise, and neither is
            # something that went wrong, so neither leaves an exception on the
            # report. Any query for "did anything go wrong" would otherwise
            # count them all.
            exception, message = (None, None) if report.skipped else describe_exception(call)
            meta: Meta = {
                "attributes": self.attributes.snapshot(),
                "exception": exception,
                "exception_message": truncate(message, MAX_MESSAGE_CHARS),
            }
            setattr(report, META_ATTR, meta)
            self._annotated.append(report)
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_runtest_makereport", exc)
        return report

    def pytest_runtest_logstart(self) -> None:
        """Start the test with nothing the last one set still in place."""
        self.attributes.clear_test()

    def pytest_unconfigure(self) -> None:
        """Let go of the session, so the next one in this process starts clean."""
        self.detach()

    def pytest_runtest_logfinish(self) -> None:
        """Drop what was annotated, now that it has been logged and sent.

        The reports have gone to the controller by now, and pytest keeps them
        for the rest of the session - without this, so would the dicts.
        """
        try:
            for report in self._annotated:
                if hasattr(report, META_ATTR):
                    delattr(report, META_ATTR)
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_runtest_logfinish", exc)
        finally:
            # Whatever happened, do not hold pytest's reports for the session.
            self._annotated.clear()


# ---------------------------------------------------------------------------
# The stream owner
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """What the reporter remembers about one test while it runs.

    ``identity`` is its nodeid split into suite, case and arguments, done once
    rather than on each of its three reports; ``statuses`` is what the steps of
    the current attempt did, which decides the verdict; ``attributes`` is the
    latest answer from wherever the test ran; ``held`` is the teardown report,
    waiting for pytest to say the test is finished so it can carry that
    verdict; and ``rerun_pending`` says this attempt is to be retried, so the
    test is not done yet.
    """

    identity: tuple[str, str, str | None]
    statuses: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    held: CaseReport | None = None
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
        step_name: str,
        status: str,
        case: Case,
        failure: Failure = NO_FAILURE,
    ) -> CaseReport:
        """Build what the step itself answers for, stamped with the test's."""
        suite, name, arguments = case.identity
        self.built += 1
        report = CaseReport(
            test_suite=suite,
            test_case=name,
            arguments=arguments,
            step_name=step_name,
            step_status=outcome_of(status),
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
        """Return the case for ``nodeid``, made the first time its test is seen."""
        case = self._cases.get(nodeid)
        if case is None:
            case = self._cases[nodeid] = Case(identity=split_nodeid(nodeid))
        return case

    def _finish(self, nodeid: str, case: Case, report: CaseReport) -> None:
        """Send a test's last report, carrying the verdict, and forget the test."""
        report.outcome = outcome_of(verdict_of(case.statuses))
        report.last_report = True
        self.queue.submit(report)
        self._cases.pop(nodeid, None)

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Note that a fresh attempt at this test has begun."""
        try:
            # Belt and braces with the annotator, which does this too: under
            # xdist they are different processes, and without xdist they are
            # the same store cleared twice at the same moment, which is free.
            self.attributes.clear_test()
            case = self._case(nodeid)
            case.rerun_pending = False
            case.statuses.clear()
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_runtest_logstart", exc)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Turn one step report into a case report.

        Every hook here works this way: a reporting bug is caught, kept and
        shown at the end of the run, and the next test is reported as if it
        had not happened. A test run is never failed over telemetry, and that
        has to hold for this plugin's own mistakes, not only the endpoint's.
        """
        try:
            self._on_logreport(report)
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_runtest_logreport", exc)

    def _on_logreport(self, report: pytest.TestReport) -> None:
        meta = getattr(report, META_ATTR, None)
        # pytest keeps every report for the whole session, so what we hung on
        # this one would be kept too. It has been read; let it go.
        if hasattr(report, META_ATTR):
            delattr(report, META_ATTR)
        # Anything at all can be on a report by the time it gets here: another
        # plugin's attribute of the same name, a worker running a different
        # version of this module. Whatever it is, it is not trusted to be ours.
        if not isinstance(meta, dict):
            meta = {}
        case = self._case(report.nodeid)
        attributes = meta.get("attributes")
        if isinstance(attributes, dict):
            case.attributes = attributes
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
            step_of(report.when),
            status,
            case,
            Failure(
                exception=as_text(meta.get("exception")),
                message=as_text(meta.get("exception_message")),
                traceback=longrepr_of(report),
            ),
        )
        if report.when == "teardown":
            # The only one held: a moment later pytest says the test is done,
            # and then this report can carry the verdict.
            case.held = built
        else:
            self.queue.submit(built)

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        """Close the test out, unless another attempt at it is coming."""
        try:
            self._on_logfinish(nodeid)
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_runtest_logfinish", exc)
            # It failed part way through, so it may never be closed out. Drop
            # it rather than leave it to be closed twice at the end of the run.
            self._cases.pop(nodeid, None)

    def _on_logfinish(self, nodeid: str) -> None:
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
                step_of("teardown"),
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
        try:
            self._on_collectreport(report)
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_collectreport", exc)

    def _on_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            case = self._case(report.nodeid)
            case.attributes = self.attributes.snapshot()
            case.statuses.append("error")
            self._finish(
                report.nodeid,
                case,
                self._build(
                    COLLECTION_STEP,
                    "error",
                    case,
                    Failure(
                        exception="CollectError",
                        message=f"collection of {report.nodeid} failed",
                        traceback=longrepr_of(report),
                    ),
                ),
            )

    # optionalhook: this is xdist's hookspec, and most runs have no xdist.
    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: "WorkerNode") -> None:
        """Give a worker the run's start time, so every process reports the same one.

        Read from the attributes rather than remembered, so that a conftest
        that set its own `cycle_start_time` before the workers started - a
        cycle that began before pytest did - is the one they are told about.
        """
        try:
            node.workerinput[WORKER_START] = self.attributes.snapshot().get("cycle_start_time")
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_configure_node", exc)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Close every test the run left open, then drain the queue."""
        try:
            self._close(reason=exit_reason(session, exitstatus))
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_sessionfinish", exc)
            # Whatever went wrong closing the tests out, the thread still has
            # to be told to stop and the queue still has to be drained.
            self._closed = True
            self.queue.close()
        summarise(session.config, self)

    def pytest_unconfigure(self) -> None:
        """Back stop: sessionfinish is not reached if configure-time work blew up.

        Also lets go of the session: `ElasticPlugin.current` holds this, which
        holds the hook relay, which holds pytest's whole config. A process that
        runs pytest more than once would keep every one of them.
        """
        try:
            self._close(reason="the session was unconfigured")
        except Exception as exc:  # noqa: BLE001 - see `ElasticPlugin.fault`
            self.fault("pytest_unconfigure", exc)
            self._closed = True
            self.queue.close()
        finally:
            self.detach()

    # -- the tests that do not end by themselves -----------------------------

    def _worker_crashed(self, report: pytest.TestReport, case: Case) -> None:
        """Turn xdist's stand-in for a dead worker into a last report.

        The stand-in knows only the nodeid, so the test is described by what
        the worker said before it died. It is the last unless the test is being
        re-queued, which pytest-rerunfailures says by rewriting the outcome.
        """
        case.statuses.append("crashed")
        held, case.held = case.held, None
        if held is not None:
            # Its teardown was made and the worker died before pytest could say
            # the test was finished. It is not the last report any more, but it
            # is what the test did, so it still goes.
            self.queue.submit(held)
        built = self._build(
            step_of("teardown"),
            "crashed",
            case,
            Failure(
                exception="WorkerCrash",
                message=longrepr_of(report) or "the worker running it died",
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
            try:
                self._close_case(nodeid, case, reason)
            except Exception as exc:  # noqa: BLE001 - the next test still gets its say
                self.fault("closing out " + nodeid, exc)
        self.queue.close()

    def _close_case(self, nodeid: str, case: Case, reason: str) -> None:
        """Give one test the last report the run never let it have."""
        held, case.held = case.held, None
        if held is not None:
            # Its teardown was made but the test never closed, so the run
            # ended in between. It is the last report either way.
            self._finish(nodeid, case, held)
            return
        # It is in here at all because it started, and it is still in here
        # because it never finished: it never reached teardown, or its rerun
        # never happened, or it died before its first step was reported.
        # Nothing else will close it out. The step is teardown on purpose:
        # every test in the index then ends with one, crashed or not.
        case.statuses.append("crashed")
        self._finish(
            nodeid,
            case,
            self._build(
                step_of("teardown"),
                "crashed",
                case,
                Failure(
                    exception="TestIncomplete",
                    message=f"never reached teardown: {reason}",
                ),
            ),
        )

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


def longrepr_of(report: "pytest.TestReport | pytest.CollectReport") -> str | None:
    """Return the report's traceback text, or None where there is none.

    ``longreprtext`` builds a terminal writer to render into whether or not
    there is anything to render, and in a green run three reports in four have
    nothing: asking first is forty times cheaper than being told.
    """
    if report.longrepr is None:
        return None
    try:
        # Rendering is whatever built the longrepr, which may be a plugin.
        return report.longreprtext or None
    except Exception:  # noqa: BLE001 - a traceback is never worth the run
        return "<traceback unavailable>"


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


def outcome_of(status: str) -> str:
    """Return what the index calls a status, or the status if `OUTCOMES` lost it.

    A conftest is invited to remap the vocabulary, and a table that has been
    remapped with a key missing must degrade to the plain English word rather
    than raise a `KeyError` out of a hook, half way through a run.
    """
    mapped = OUTCOMES.get(status)
    return mapped if isinstance(mapped, str) else status


def step_of(when: str) -> str:
    """Return what the index calls a phase, or the phase if `PHASE_STEPS` lost it."""
    mapped = PHASE_STEPS.get(when)
    return mapped if isinstance(mapped, str) else when


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
    try:
        # Somebody else's exception, and `__str__` is somebody else's code:
        # one that raises must not take the report with it.
        return excinfo.typename, str(excinfo.value)
    except Exception:  # noqa: BLE001 - the type alone still says something
        return getattr(excinfo, "typename", None), "<exception message unavailable>"


def as_text(value: object) -> str | None:
    """Return a string or None, whatever came across claiming to be one.

    The model would refuse an integer where it says string, and refusing is a
    dropped report. Nothing here is worth a dropped report.
    """
    if value is None or isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - it said nothing usable, so say nothing
        return None


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
    hook = getattr(config.hook, "pytest_case_reports", None)
    if terminal is None or hook is None or not hook.get_hookimpls():
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
    # Bugs in the reporting itself. Kept rather than raised, because a hook
    # that raises takes the session with it - so this is the only way they are
    # ever heard about.
    for line in plugin.faults:
        terminal.write_line(f"  elastic-reporter: {line}", red=True)


def reporter() -> ElasticPlugin:
    """Return this process's plugin, for code with no ``config`` to hand.

    ::

        from elastic_reporter import reporter

        def test_upgrade():
            reporter().set_test(vc="fw-5.0.0")

    The same object ``getplugin("elastic-reporter")`` returns, with the same
    `ElasticPlugin.set_global` and `ElasticPlugin.set_test`. Outside a session
    - the module imported but no pytest configured, a unit test of your own
    helpers - it is one that accepts attributes and drops them, so this never
    returns None and never raises.
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
    missing = sorted(
        f"{name}[{key!r}]"
        for name, table in tables
        for key in REQUIRED_KEYS.get(name, frozenset())
        if key not in table
    )
    if missing:
        message = f"the vocabulary is missing what the plugin looks up: {', '.join(missing)}"
        raise KeyError(message)


#: What the plugin itself looks up in each table. A conftest that remaps one
#: and drops a key is told at configure time, before a test has run - and
#: `outcome_of` and `step_of` still degrade to the plain word if it is dropped
#: later than that.
REQUIRED_KEYS = {
    "OUTCOMES": frozenset({*VERDICT_ORDER, RERUN_OUTCOME}),
    "PHASE_STEPS": frozenset({"setup", "call", "teardown"}),
}


def pytest_configure(config: pytest.Config) -> None:
    """Register the half of the plugin this process is responsible for."""
    check_vocabulary(("OUTCOMES", OUTCOMES), ("PHASE_STEPS", PHASE_STEPS))
    attributes = CaseAttributes()
    # The run's own start time, before the detached values below and before
    # any conftest hook, so that either can say otherwise and be obeyed.
    attributes.set_global(cycle_start_time=run_started(config))
    detached = ElasticPlugin.current
    if type(detached) is ElasticPlugin:
        # Something set attributes through `reporter` before there was a
        # session - a conftest at import time, say. Those would otherwise be
        # written to a plugin nobody reports through and quietly lost.
        attributes.set_global(**detached.attributes.snapshot())
    try:
        worker, controller = is_xdist_worker(config), is_xdist_controller(config)
        if not controller:
            # Reports are made here, so this is where they can be annotated -
            # and, on a worker, what test code reaching for `getplugin` finds.
            annotator = ReportAnnotator(attributes)
            config.pluginmanager.register(annotator, PLUGIN_NAME if worker else ANNOTATOR_NAME)
        if not worker:
            plugin = ElasticCaseReporter(config.hook, attributes)
            plugin.queue.start()
            config.pluginmanager.register(plugin, PLUGIN_NAME)
    except Exception as exc:  # noqa: BLE001 - a run without reporting beats no run
        warn(f"not reporting this run: {type(exc).__name__}: {exc}")
