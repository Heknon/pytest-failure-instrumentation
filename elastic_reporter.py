# Copyright (c) 2026 Heknon. Swap in whatever notice your project uses;
# ruff's CPY001 only checks that there is one.
"""Turn every pytest phase into a case report, and hand it to your hook.

    pytest -p elastic_reporter

Standalone: a single module, no package, nothing to install beyond pytest 8 or
newer on Python 3.12 or newer. Drop it next to your ``conftest.py`` and load it
with ``-p elastic_reporter``, or name it in ``pytest_plugins`` in the rootdir
conftest.

The hook
--------
The plugin builds the reports and calls `pytest_case_report` with each one.
Sending them is yours::

    # conftest.py
    import json

    import httpx

    from elastic_reporter import json_default


    def pytest_case_report(report):
        httpx.post(URL, content=json.dumps(report.to_dict(), default=json_default))

It is called once per report, in order, in the process that owns the stream -
the xdist controller, or the session itself. What you do there is up to you:
POST each one as it arrives, buffer them and POST on ``report.last_report``,
write them to a file. An implementation that raises is counted and reported at
the end of the run rather than failing the run.

What it reports
---------------
* One `CaseReport` per pytest phase - setup, call, teardown - of every attempt
  at every test, whatever the outcome, plus one per collection error.
* ``last_report=True`` marks the end of a *test*, not the end of the run: each
  test contributes exactly one, on the last report it produces, so a consumer
  can close the case out and decide its verdict.
* Every case ends with one. A test that started and never reached teardown -
  the session was interrupted, the xdist worker holding it died - gets a
  synthetic ``crashed`` teardown report instead.

Setting a test's attributes
---------------------------
`machine`, `vc`, `cycle_id`, `owner` and the rest describe the test rather than
the phase, and they come from everywhere - a conftest hook, a fixture that
connects to the machine, the test body itself. So there is one way to set any
of them, from anywhere that can see a pytest ``config``::

    plugin = request.config.pluginmanager.getplugin("elastic-reporter")
    plugin.set(vc="fw-4.2.1", machine="rack1-dut7")

or, with no ``config`` to hand, `reporter`::

    from elastic_reporter import reporter

    def test_upgrade():
        reporter().set(vc="fw-5.0.0")

That is the whole API. It takes any of `SETTABLE` - every field of `CaseReport`
except the ones a phase answers for itself - and whatever is never set keeps
the default the model gives it. The plugin adds no options of its own: a value
for the whole run is a line in a conftest hook, reading it from wherever you
keep it::

    def pytest_sessionstart(session):
        # runs in every process, controller and xdist worker alike
        session.config.pluginmanager.getplugin("elastic-reporter").set(
            vc=os.environ["FIRMWARE"],
            cycle_id=int(os.environ["CYCLE"]),
        )

A value set anywhere in a test describes the whole test: its setup, call and
teardown reports all carry it, whichever phase set it, because a test's reports
are stamped when the test ends rather than as each phase finishes. Values stick
until changed, so a process can set one and forget it.

Where a test's machine is the test's own, set it where you know it - the
fixture that allocates it, or one that reads it off the item::

    @pytest.fixture(autouse=True)
    def record_machine(request):
        plugin = request.config.pluginmanager.getplugin("elastic-reporter")
        plugin.set(machine=machine_for(request.node))

Reruns
------
A test can be attempted more than once: pytest-rerunfailures retries a failure
in place, and under xdist it also re-queues a test whose worker died. Every
attempt belongs to the same case, so they are all held together and emitted
when the test is done, with the flag on the final report of the final attempt.
What says a test is done is ``pytest_runtest_logfinish`` without a rerun
pending, where "pending" means this attempt logged a report that
pytest-rerunfailures marked ``rerun`` (it marks the failing report of an
attempt it is about to retry, and rewrites the crashed-worker report the same
way before the controller sees it). Anything still held when the session ends
is emitted there, so a retry that never happened cannot leave a case open.

The vocabulary
--------------
`OUTCOMES` and `PHASE_STEPS` are the only place the strings that land in
elastic are decided. Remap a value there, or reassign either dict from a
conftest, if your index speaks differently - ``OUTCOMES["error"] = "failed"``
folds setup and teardown failures back into plain failures, for instance.

Under xdist
-----------
The controller owns the stream, so your hook is called there and nowhere else:
one process sending, one ordered stream, one ``last_report`` per test however
many workers ran it. What only a worker can know - the test's attributes as it
ran, the exception that was raised - is read on the worker by `ReportAnnotator`
and attached to the report, which pytest serialises across for us. So setting
an attribute on a worker needs nothing of the controller. It describes that
worker's tests only, each worker being its own process, so a value meant for
the whole run belongs in a hook every process runs, like the
``pytest_sessionstart`` above in the rootdir conftest.
"""

import threading
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

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

#: How many failing hook calls are worth describing. An endpoint that is down
#: says the same thing every time, and the count is the part that is news.
MAX_ERRORS = 20

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

#: What the annotator sends with a report for the reporter to build from.
type Meta = dict[str, Any]

#: One case report, as elastic wants it.
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


def json_default(value: object) -> str:
    """Render what `json` cannot: the timestamps, as ISO 8601.

    ``json.dumps(report.to_dict(), default=json_default)`` is the payload.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


class CaseReportHooks:
    """The hook this plugin adds. Implement it in a conftest or a plugin."""

    @pytest.hookspec
    def pytest_case_report(self, report: CaseReport) -> None:
        """Receive one case report, built and ready to send.

        Called once per report, in the order the reports were made, in the
        process that owns the stream - the xdist controller, or the session
        itself. A test's reports arrive together when the test is done, the
        last of them with ``report.last_report`` set.

        Nothing is buffered for you and nothing is retried: this is the whole
        of the plugin's delivery, and what to do with it is yours::

            def pytest_case_report(report):
                httpx.post(URL, content=json.dumps(report.to_dict(), default=json_default))

        Raising is not fatal - the run does not exist to serve the reporting -
        but it is counted and shown at the end of the run.

        One catch, and it is pytest's rather than this plugin's: implementing
        a hook nobody has declared is an error, so a conftest with this in it
        cannot run without the plugin loaded - which is also how the reporting
        is switched off, there being no option for it. Where that can happen,
        say so, and pytest leaves the implementation alone instead::

            @pytest.hookimpl(optionalhook=True)
            def pytest_case_report(report):
                ...
        """


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """Add `pytest_case_report` to the hooks a conftest may implement."""
    pluginmanager.add_hookspecs(CaseReportHooks)


# ---------------------------------------------------------------------------
# The attributes of a test
# ---------------------------------------------------------------------------


class CaseAttributes:
    """Every report field that describes the test rather than the phase.

    One store for all of them, because they come from everywhere: a conftest
    hook, a fixture that connects to the machine, the test body. `set` takes
    any of `SETTABLE` from any of those places, the values describe the whole
    test - its reports are stamped when it ends, not as each phase finishes -
    and they stick until changed, so a process can set one and forget it.

    What is never set keeps the default `CaseReport` gives it.
    """

    def __init__(self) -> None:
        """Start with nothing set: `CaseReport`'s own defaults stand in."""
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}

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
# The worker half
# ---------------------------------------------------------------------------


class ReportAnnotator(ElasticPlugin):
    """Pins what this process knows about the test to its phase reports.

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
        """Attach this process's answers to the phase report."""
        report = yield
        exception, message = describe_exception(call)
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
    """Turns pytest reports into case reports and calls the hook with them.

    Registered where the reports all come together: the xdist controller, or
    the session itself without xdist.
    """

    def __init__(self, hook: "HookRelay", attributes: CaseAttributes) -> None:
        """Report through ``hook``, stamped from ``attributes``."""
        super().__init__(attributes)
        self.hook = hook
        self._cases: dict[str, Case] = {}
        self._closed = False
        self.emitted = 0
        self.failures = 0
        self.errors: list[str] = []

    # -- the stream ----------------------------------------------------------

    def _build(self, nodeid: str, outcome: str, step_name: str, failure: Failure) -> CaseReport:
        """Build what the phase itself answers for. `stamp` adds the rest."""
        suite, case, arguments = split_nodeid(nodeid)
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

    def _emit(self, nodeid: str) -> None:
        """Hand everything one test produced to the hook, stamped and closed out.

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
            self._call(report)

    def _call(self, report: CaseReport) -> None:
        """Call the hook, and survive an implementation that does not."""
        self.emitted += 1
        try:
            self.hook.pytest_case_report(report=report)
        except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
            self.failures += 1
            if len(self.errors) < MAX_ERRORS:
                self.errors.append(f"{report.test_case}: {type(exc).__name__}: {exc}")

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Note that a fresh attempt at this test has begun."""
        self._case(nodeid).rerun_pending = False

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Turn one phase report into a case report."""
        meta: Meta = getattr(report, META_ATTR, None) or {}
        # pytest keeps every report for the whole session, so what we hung on
        # this one would be kept too. It has been read; let it go.
        if hasattr(report, META_ATTR):
            delattr(report, META_ATTR)
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
            self._emit(nodeid)

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
            self._emit(report.nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Close every case the run left open, then say what was reported."""
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
            self._emit(report.nodeid)

    def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        for nodeid, case in list(self._cases.items()):
            if case.running:
                # It started and never reached teardown, so nothing else will
                # close this case out. The step is teardown on purpose: every
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
            self._emit(nodeid)


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


def summarise(config: pytest.Config, plugin: ElasticCaseReporter) -> None:
    """Write what was reported, and anything the hook made of it."""
    terminal = config.pluginmanager.get_plugin("terminalreporter")
    if terminal is None:
        return
    terminal.write_sep("-", f"elastic-reporter: {plugin.emitted} case report(s)")
    for error in plugin.errors:
        terminal.write_line("  elastic-reporter: pytest_case_report raised " + error, red=True)
    if plugin.failures > len(plugin.errors):
        terminal.write_line(
            f"  elastic-reporter: and {plugin.failures - len(plugin.errors)} more like it",
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


def pytest_configure(config: pytest.Config) -> None:
    """Register the half of the plugin this process is responsible for."""
    attributes = CaseAttributes()
    worker, controller = is_xdist_worker(config), is_xdist_controller(config)
    if not controller:
        # Reports are made here, so this is where they can be annotated - and,
        # on a worker, what test code reaching for `getplugin` finds.
        annotator = ReportAnnotator(attributes)
        config.pluginmanager.register(annotator, PLUGIN_NAME if worker else ANNOTATOR_NAME)
    if not worker:
        config.pluginmanager.register(ElasticCaseReporter(config.hook, attributes), PLUGIN_NAME)
