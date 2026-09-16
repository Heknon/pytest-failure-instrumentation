"""Tests for the elastic case-report plugin.

    pytest -p pytester test_elastic_reporter.py

`-p pytester` is what the end-to-end half needs: it runs a real pytest in a
subprocess, plugin and all, and reads back what `pytest_case_reports` was
handed. pytest-xdist and pytest-rerunfailures have to be installed for the two
tests that exercise them.
"""

import collections
import json
import shutil
import threading
from datetime import datetime
from time import monotonic

import pytest
from pydantic import ValidationError

import elastic_reporter as er

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        ("t.py::test_a", ("t.py", "test_a", None)),
        ("t.py::TestB::test_c", ("t.py::TestB", "test_c", None)),
        ("a/b/t.py::TestB::test_c[1-2]", ("a/b/t.py::TestB", "test_c", "1-2")),
        ("t.py", ("t.py", "t.py", None)),
        ("t.py::test_a[x[y]]", ("t.py", "test_a", "x[y]")),
    ],
)
def test_split_nodeid(nodeid, expected):
    assert er.split_nodeid(nodeid) == expected


def test_truncate():
    assert er.truncate(None, 10) is None
    assert er.truncate("short", 10) == "short"
    cut = er.truncate("x" * 50, 10)
    assert cut.startswith("x" * 10)
    assert "40 more characters" in cut


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (["passed", "passed", "passed"], "passed"),
        (["passed", "failed", "passed"], "failed"),
        (["error", "passed"], "error"),
        (["skipped", "passed"], "skipped"),
        (["passed", "xfailed", "passed"], "xfailed"),
        (["passed", "xpassed", "passed"], "xpassed"),
        (["passed", "passed", "error"], "error"),  # a teardown that blew up
        (["passed", "failed", "crashed"], "crashed"),
        ([], "crashed"),  # nothing we can describe means it never finished
    ],
)
def test_verdict_of(statuses, verdict):
    assert er.verdict_of(statuses) == verdict


def test_stamp_only_writes_what_a_step_does_not_own():
    report = er.CaseReport(test_suite="t.py", test_case="test_a", step_status="passed")
    er.stamp(report, {"vc": "fw-1", "machine": "rack1", "step_status": "TAMPERED", "nonsense": 1})
    assert (report.vc, report.machine) == ("fw-1", "rack1")
    assert report.step_status == "passed"
    assert not hasattr(report, "nonsense")


def test_to_dict_is_ready_for_json_as_it_stands():
    report = er.CaseReport(test_suite="t.py", test_case="test_a")
    document = json.loads(json.dumps(report.to_dict()))  # no default= needed
    # Pydantic writes UTC as a trailing Z rather than +00:00. Both are ISO 8601
    # and elastic reads either, so check the instant, not the spelling.
    assert document["@timestamp"] == document["time"]
    assert datetime.fromisoformat(document["@timestamp"]) == report.time
    assert document["test_case"] == "test_a"
    assert document["outcome"] is None  # no verdict unless it is the last report


def test_the_model_refuses_what_elastic_should_not_be_asked_to_index():
    with pytest.raises(ValidationError):
        er.CaseReport(test_suite="t.py", test_case="test_a", macihne="typo")
    with pytest.raises(ValidationError):
        er.CaseReport(test_suite="t.py", test_case="test_a", cycle_id="seventy")
    with pytest.raises(ValidationError):
        er.CaseReport(test_case="test_a")  # a report belongs to a suite


def test_attributes_reject_what_is_not_settable():
    attributes = er.CaseAttributes()
    attributes.set(vc="fw-1", machine="rack1")
    assert attributes.snapshot()["vc"] == "fw-1"
    with pytest.raises(ValueError, match="not a case attribute: outcome"):
        attributes.set(outcome="passed")
    # The identity comes from the nodeid; a typo must not rewrite every report.
    with pytest.raises(ValueError, match="not a case attribute: test_case"):
        attributes.set(test_case="upgrade-suite")


def test_attributes_are_checked_against_the_model_where_they_are_set():
    attributes = er.CaseAttributes()
    with pytest.raises(ValidationError):
        attributes.set(cycle_id="seventy-seven")
    attributes.set(cycle_id="77")  # but a value the model can convert is converted
    assert attributes.snapshot()["cycle_id"] == 77


def test_a_vocabulary_that_is_not_strings_is_refused_at_startup():
    er.check_vocabulary(("OUTCOMES", er.OUTCOMES), ("PHASE_STEPS", er.PHASE_STEPS))
    with pytest.raises(TypeError, match=r"OUTCOMES\['passed'\]"):
        er.check_vocabulary(("OUTCOMES", {**er.OUTCOMES, "passed": 1}))


def test_unset_attributes_keep_the_models_defaults():
    attributes = er.CaseAttributes()
    assert attributes.snapshot() == {}
    report = er.CaseReport(test_suite="t.py", test_case="test_a")
    er.stamp(report, attributes.snapshot())
    assert (report.vc, report.owner, report.cycle_id, report.labs3) == (
        "mock-vc",
        "mock-owner",
        1,
        True,
    )


def test_attributes_survive_concurrent_writers():
    attributes = er.CaseAttributes()
    threads = [threading.Thread(target=attributes.set, kwargs={"vc": f"fw-{n}"}) for n in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert attributes.snapshot()["vc"].startswith("fw-")


def test_reporter_works_outside_a_session(monkeypatch):
    monkeypatch.setattr(er.ElasticPlugin, "current", None)  # restored afterwards
    er.reporter().set(vc="fw-nowhere")  # must not raise, must not need a config
    assert er.reporter().attributes.snapshot()["vc"] == "fw-nowhere"


# ---------------------------------------------------------------------------
# the queue behind the hook
# ---------------------------------------------------------------------------


class FakeHook:
    """Stands in for pytest's hook relay, recording the batches it is given."""

    def __init__(self, error=None):
        """Record batches, or raise ``error`` instead of taking them."""
        self.batches = []
        self.error = error

    def pytest_case_reports(self, reports):
        """Take one batch, the way a conftest implementation would."""
        if self.error is not None:
            raise self.error
        self.batches.append(list(reports))


def a_report(name):
    return er.CaseReport(test_suite="t.py", test_case=name, step_status="passed")


def test_queue_batches_in_order_and_drains_on_close(monkeypatch):
    monkeypatch.setattr(er, "BATCH_SIZE", 3)
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 60.0)
    hook = FakeHook()
    reports = er.ReportQueue(hook)
    reports.start()
    for n in range(7):
        reports.submit(a_report(f"test_{n}"))
    reports.close(timeout=5)
    assert [len(batch) for batch in hook.batches] == [3, 3, 1]
    assert [r.test_case for batch in hook.batches for r in batch] == [f"test_{n}" for n in range(7)]
    assert reports.sent == 7
    assert reports.dropped == 0


def test_queue_sends_on_the_interval_without_a_full_batch(monkeypatch):
    monkeypatch.setattr(er, "BATCH_SIZE", 1000)
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 0.05)
    hook = FakeHook()
    reports = er.ReportQueue(hook)
    reports.start()
    reports.submit(a_report("test_alone"))
    threading.Event().wait(0.5)  # longer than the interval, without a sleep
    assert hook.batches, "the interval should have sent a part-full batch"
    reports.close(timeout=5)


def test_queue_shuts_down_without_waiting_for_the_interval(monkeypatch):
    """A thread blocked for the whole interval would tax every run on the way out."""
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 30.0)
    reports = er.ReportQueue(FakeHook())
    reports.start()
    reports.submit(a_report("test_a"))
    started = monotonic()
    reports.close(timeout=5)
    assert monotonic() - started < 1.0
    assert reports.sent == 1


def test_queue_drops_rather_than_growing_without_limit(monkeypatch):
    monkeypatch.setattr(er, "MAX_QUEUED", 10)
    monkeypatch.setattr(er, "BATCH_SIZE", 1)
    blocked = threading.Event()

    class Blocked:
        def pytest_case_reports(self, reports):
            blocked.wait(timeout=5)

    reports = er.ReportQueue(Blocked())
    reports.start()
    for n in range(200):
        reports.submit(a_report(f"test_{n}"))
    assert reports._queue.qsize() <= 10
    assert reports.dropped >= 180  # the rest never reached elastic, and said so
    blocked.set()
    reports.close(timeout=5)


def test_queue_survives_a_hook_that_raises(monkeypatch):
    monkeypatch.setattr(er, "BATCH_SIZE", 2)
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 60.0)
    reports = er.ReportQueue(FakeHook(error=RuntimeError("the api is down")))
    reports.start()
    for n in range(6):
        reports.submit(a_report(f"test_{n}"))
    reports.close(timeout=5)
    assert reports.sent == 0
    assert reports.dropped == 6
    assert reports.failures == 3
    assert "the api is down" in reports.errors[0]


def test_queue_survives_a_hook_that_raises_a_base_exception(monkeypatch):
    """A SystemExit would otherwise end the thread and silently strand the rest."""
    monkeypatch.setattr(er, "BATCH_SIZE", 1)
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 60.0)
    reports = er.ReportQueue(FakeHook(error=SystemExit("a config error")))
    reports.start()
    for n in range(3):
        reports.submit(a_report(f"test_{n}"))
    reports.close(timeout=5)
    assert reports.dropped == 3
    assert reports.failures == 3  # it stayed alive for all three, not just the first


def test_queue_keeps_only_the_first_errors(monkeypatch):
    monkeypatch.setattr(er, "BATCH_SIZE", 1)
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 60.0)
    reports = er.ReportQueue(FakeHook(error=RuntimeError("503")))
    reports.start()
    for n in range(er.MAX_ERRORS + 15):
        reports.submit(a_report(f"test_{n}"))
    reports.close(timeout=5)
    assert len(reports.errors) == er.MAX_ERRORS
    assert reports.failures == er.MAX_ERRORS + 15


def test_queue_close_is_safe_twice_and_before_start():
    reports = er.ReportQueue(FakeHook())
    reports.close(timeout=1)  # never started
    reports.start()
    reports.close(timeout=5)
    reports.close(timeout=5)


def test_queue_is_bounded_by_the_constant(monkeypatch):
    monkeypatch.setattr(er, "MAX_QUEUED", 5)
    monkeypatch.setattr(er, "BATCH_SIZE", 1)
    reports = er.ReportQueue(FakeHook())  # never started, so nothing drains
    for _ in range(12):
        reports.submit(a_report("t"))
    assert reports._queue.qsize() == 5  # the ceiling is the queue's own
    assert reports.dropped == 7


def test_a_batch_is_never_handed_over_outside_the_lock(monkeypatch):
    """The end of a run takes the lock to look; a batch handed over without it is lost.

    The thread stops when it finds the queue empty and the part-full batch
    taken. A batch put on the queue after that - swapped out under the lock,
    put on the queue after releasing it - is on a queue nobody reads again.
    """
    monkeypatch.setattr(er, "BATCH_SIZE", 2)
    reports = er.ReportQueue(FakeHook())  # never started: watch the hand-over
    held = []
    put = reports._queue.put_nowait

    def watched(batch):
        held.append(reports._lock.locked())
        put(batch)

    monkeypatch.setattr(reports._queue, "put_nowait", watched)
    for n in range(4):
        reports.submit(a_report(f"test_{n}"))
    assert held == [True, True]


def test_queue_loses_nothing_on_the_way_out(monkeypatch):
    """Every report is sent or counted: a hand-over at the end must not strand one."""
    monkeypatch.setattr(er, "BATCH_SIZE", 10)
    monkeypatch.setattr(er, "FLUSH_INTERVAL", 0.01)
    for _ in range(50):  # the loss was a race, so once proves nothing
        reports = er.ReportQueue(FakeHook())
        reports.start()
        for n in range(95):  # not a whole number of batches
            reports.submit(a_report(f"test_{n}"))
        reports.close(timeout=5)
        assert reports.sent + reports.dropped == 95


def test_the_thread_is_woken_once_per_batch_not_once_per_report(monkeypatch):
    """Waking it per report takes the GIL off the run twelve thousand times."""
    monkeypatch.setattr(er, "BATCH_SIZE", 100)
    reports = er.ReportQueue(FakeHook())  # never started: count what it was handed
    for n in range(1000):
        reports.submit(a_report(f"test_{n}"))
    assert reports._queue.qsize() == 10  # ten hand-overs for a thousand reports


def test_a_test_that_logged_nothing_is_still_closed_out(monkeypatch):
    """It started, so it is not a test the run never reached."""
    monkeypatch.setattr(er, "BATCH_SIZE", 1)
    monkeypatch.setattr(er.ElasticPlugin, "current", None)
    hook = FakeHook()
    plugin = er.ElasticCaseReporter(hook, er.CaseAttributes())
    plugin.queue.start()
    plugin.pytest_runtest_logstart(nodeid="t.py::test_a")  # and then the run died
    plugin._close(reason="the session ended (interrupted)")
    documents = [report for batch in hook.batches for report in batch]
    assert [(d.test_case, d.step_name, d.outcome, d.last_report) for d in documents] == [
        ("test_a", "teardown", "crashed", True),
    ]


# ---------------------------------------------------------------------------
# the plugin, run by a real pytest, from the outside
# ---------------------------------------------------------------------------

#: An implementation of the hook that keeps what it was given, so a test can
#: read it back. It is also the shortest example of one.
RECORD = """
import json

RECEIVED = []


def pytest_case_reports(reports):
    RECEIVED.extend(report.to_dict() for report in reports)


def pytest_unconfigure(config):
    if not hasattr(config, "workerinput"):  # the controller owns the stream
        (config.rootpath / "reported.json").write_text(json.dumps(RECEIVED))
"""


@pytest.fixture
def run(pytester):
    """Run an inner pytest with the plugin, and return what the hook received."""
    shutil.copy(er.__file__, pytester.path / "elastic_reporter.py")

    def _run(source, *args, conftest=RECORD):
        pytester.makeconftest(conftest)
        pytester.makepyfile(source)
        result = pytester.runpytest_subprocess("-p", "elastic_reporter", *args)
        reported = pytester.path / "reported.json"
        return result, json.loads(reported.read_text()) if reported.exists() else []

    return _run


def by_case(documents):
    """One entry per case: a parametrized test is one case per parameter set."""
    cases = collections.defaultdict(list)
    for document in documents:
        name = document["test_case"]
        if document["arguments"]:
            name += f"[{document['arguments']}]"
        cases[name].append(document)
    return cases


def assert_one_verdict_per_case(documents):
    for name, reports in by_case(documents).items():
        with_outcome = [r for r in reports if r["outcome"] is not None]
        assert len(with_outcome) == 1, f"{name}: {len(with_outcome)} reports carry a verdict"
        assert with_outcome[0] is reports[-1], f"{name}: the verdict is not on its last report"
        assert with_outcome[0]["last_report"] is True, f"{name}: the verdict is not flagged last"
        assert all(not r["last_report"] for r in reports[:-1]), f"{name}: more than one last report"


# ---------------------------------------------------------------------------


def test_step_status_on_every_report_and_a_verdict_only_on_the_last(run):
    result, documents = run(
        """
        import pytest

        def test_passes(): pass
        def test_fails(): assert False
        @pytest.mark.skip(reason="no")
        def test_skipped(): pass
        @pytest.mark.xfail(reason="known")
        def test_xfails(): raise RuntimeError
        @pytest.fixture
        def broken(): raise ValueError("setup")
        def test_setup_error(broken): pass
        """,
    )
    result.assert_outcomes(passed=1, failed=1, skipped=1, xfailed=1, errors=1)
    assert_one_verdict_per_case(documents)
    cases = by_case(documents)

    steps = [(r["step_name"], r["step_status"], r["outcome"]) for r in cases["test_fails"]]
    assert steps == [
        ("setup", "passed", None),
        ("call", "failed", None),
        ("teardown", "passed", "failed"),
    ]
    assert cases["test_fails"][1]["exception"] == "AssertionError"
    assert cases["test_fails"][1]["exception_traceback"]

    assert [r["step_status"] for r in cases["test_passes"]] == ["passed"] * 3
    assert cases["test_passes"][-1]["outcome"] == "passed"
    # A skipped test never runs its call step, so it has no call report.
    assert [r["step_name"] for r in cases["test_skipped"]] == ["setup", "teardown"]
    assert cases["test_skipped"][-1]["outcome"] == "skipped"
    assert cases["test_xfails"][-1]["outcome"] == "xfailed"
    assert cases["test_setup_error"][0]["step_status"] == "error"
    assert cases["test_setup_error"][-1]["outcome"] == "error"
    # A skip and an expected failure raise, but neither went wrong, so neither
    # leaves an exception behind for a "what failed" query to trip over.
    assert cases["test_skipped"][0]["exception"] is None
    assert cases["test_xfails"][1]["exception"] is None


def test_a_long_exception_message_is_cut_once(run):
    _, documents = run(
        """
        def test_long():
            raise ValueError("x" * 50_000)
        """,
    )
    message = by_case(documents)["test_long"][1]["exception_message"]
    assert message.count("truncated") == 1
    assert f"{50_000 - er.MAX_MESSAGE_CHARS} more characters" in message


def test_a_report_carries_what_was_set_when_its_step_ended(run):
    _, documents = run(
        """
        import elastic_reporter as er
        import pytest

        @pytest.fixture
        def allocated():
            er.reporter().set(machine="rack1")

        def test_set_in_setup(allocated):
            pass

        def test_set_in_the_body():
            er.reporter().set(vc="fw-5.0.0")
        """,
    )
    assert_one_verdict_per_case(documents)
    cases = by_case(documents)
    # A fixture sets it during setup, so every report of that test has it.
    assert {r["machine"] for r in cases["test_set_in_setup"]} == {"rack1"}
    # The body sets it after the setup report was already sent, so that one
    # does not have it, and the two after it do.
    body = cases["test_set_in_the_body"]
    assert [r["vc"] for r in body] == ["mock-vc", "fw-5.0.0", "fw-5.0.0"]
    # And it sticks, so the machine from the test before is still there.
    assert {r["machine"] for r in body} == {"rack1"}


def test_a_conftest_hook_sets_the_run_wide_attributes(run):
    """The plugin has no options: this is what replaces them, workers included."""
    pytest.importorskip("xdist")
    _, documents = run(
        """
        import pytest

        @pytest.mark.parametrize("n", range(4))
        def test_spread(n):
            pass
        """,
        "-n",
        "2",
        conftest=RECORD
        + """

def pytest_sessionstart(session):
    session.config.pluginmanager.getplugin("elastic-reporter").set(
        vc="fw-from-conftest", owner="lab-team", cycle_id=99,
    )
""",
    )
    assert len(documents) == 12
    assert {d["vc"] for d in documents} == {"fw-from-conftest"}
    assert {d["owner"] for d in documents} == {"lab-team"}
    assert {d["cycle_id"] for d in documents} == {99}


def test_a_test_that_never_reaches_teardown_is_crashed(run):
    _, documents = run(
        """
        import pytest

        def test_first(): pass
        def test_exits(): pytest.exit("down")
        def test_never_runs(): pass
        """,
    )
    assert_one_verdict_per_case(documents)
    cases = by_case(documents)
    assert [r["step_name"] for r in cases["test_exits"]] == ["setup", "teardown"]
    assert cases["test_exits"][-1]["outcome"] == "crashed"
    assert cases["test_exits"][-1]["exception"] == "TestIncomplete"
    # Its setup report went out before the run died, with no verdict on it.
    assert cases["test_exits"][0]["outcome"] is None
    assert "test_never_runs" not in cases


def test_collection_error_is_reported_once(run):
    _, documents = run("import definitely_not_a_real_module")
    assert len(documents) == 1
    assert documents[0]["step_name"] == "collect"
    assert documents[0]["step_status"] == "error"
    assert documents[0]["outcome"] == "error"
    assert documents[0]["last_report"] is True


def test_only_the_final_attempt_of_a_rerun_carries_the_verdict(run):
    pytest.importorskip("pytest_rerunfailures")
    _, documents = run(
        """
        import pathlib

        COUNT = pathlib.Path(__file__).parent / "n.txt"

        def test_flaky():
            n = int(COUNT.read_text()) + 1 if COUNT.exists() else 1
            COUNT.write_text(str(n))
            assert n >= 3
        """,
        "--reruns",
        "2",
    )
    assert_one_verdict_per_case(documents)
    reports = by_case(documents)["test_flaky"]
    assert len(reports) == 9  # three attempts of three steps
    assert [r["step_status"] for r in reports] == (
        ["passed", "rerun", "passed"] * 2 + ["passed", "passed", "passed"]
    )
    assert reports[-1]["outcome"] == "passed"


def test_the_controller_owns_the_stream_under_xdist(run):
    pytest.importorskip("xdist")
    _, documents = run(
        """
        import pytest

        @pytest.mark.parametrize("n", range(8))
        def test_spread(n):
            pass
        """,
        "-n",
        "2",
    )
    assert_one_verdict_per_case(documents)
    assert len(documents) == 24  # 8 tests, 3 steps, one stream
    assert sum(d["outcome"] is not None for d in documents) == 8


def test_without_the_plugin_loaded_nothing_reports_and_set_still_works(pytester):
    """Not loading it is how the reporting is switched off."""
    shutil.copy(er.__file__, pytester.path / "elastic_reporter.py")
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(optionalhook=True)  # the plugin may not be loaded
        def pytest_case_reports(reports):
            raise AssertionError("nothing should be reporting")
        """,
    )
    pytester.makepyfile(
        """
        import elastic_reporter as er

        def test_a():
            er.reporter().set(vc="fw-1")  # no session, no config, no plugin
        """,
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_nothing_is_held_once_a_test_is_done(run):
    """The reporter's own state does not grow with the run."""
    _, documents = run(
        """
        import elastic_reporter as er
        import pytest

        @pytest.mark.parametrize("n", range(25))
        def test_many(n):
            assert len(er.reporter()._cases) <= 1  # only the test running now
        """,
    )
    assert_one_verdict_per_case(documents)
    assert len(documents) == 75


# ---------------------------------------------------------------------------
# robustness: a reporting bug is never a test failure
# ---------------------------------------------------------------------------


@pytest.fixture
def stream(monkeypatch):
    """Make a reporter with a recording hook, and put `current` back afterwards."""
    monkeypatch.setattr(er.ElasticPlugin, "current", None)
    return er.ElasticCaseReporter(FakeHook(), er.CaseAttributes())


def a_step(nodeid="t.py::test_a", when="call", outcome="passed", longrepr=None):
    """Build a real pytest report, the way the runner would hand us one."""
    return pytest.TestReport(
        nodeid=nodeid,
        location=("t.py", 1, nodeid),
        keywords={},
        outcome=outcome,
        longrepr=longrepr,
        when=when,
    )


def test_a_foreign_object_in_logreport_is_a_fault_not_a_crash(stream):
    """Anything at all can call this hook. None of it may end the session."""
    stream.pytest_runtest_logreport(object())  # no nodeid, no when, no outcome
    assert stream.faults
    assert "pytest_runtest_logreport" in stream.faults[0]
    # and the next test still reports
    stream.pytest_runtest_logstart("t.py::test_a")
    stream.pytest_runtest_logreport(a_step())
    assert stream.built == 1


def test_a_corrupt_meta_is_ignored(stream):
    """Another plugin's attribute of the same name, or an older worker's."""
    report = a_step()
    setattr(report, er.META_ATTR, "not a dict at all")
    stream.pytest_runtest_logstart(report.nodeid)
    stream.pytest_runtest_logreport(report)
    assert stream.built == 1
    assert not stream.faults  # not even worth mentioning: it was simply not ours


def test_meta_that_is_not_what_it_claims_still_builds_a_report(stream):
    """A string field is given something that is not a string."""
    report = a_step()
    setattr(report, er.META_ATTR, {"attributes": ["not", "a", "dict"], "exception": 17})
    stream.pytest_runtest_logstart(report.nodeid)
    stream.pytest_runtest_logreport(report)
    assert stream.built == 1
    assert not stream.faults
    [batch] = [stream.queue._filling]
    assert batch[0].exception == "17"


def test_a_failed_logfinish_does_not_let_a_test_report_twice(stream, monkeypatch):
    def broken(*_args, **_kwargs):
        message = "the bug is in here"
        raise RuntimeError(message)

    stream.pytest_runtest_logstart("t.py::test_a")
    stream.pytest_runtest_logreport(a_step(when="teardown"))
    monkeypatch.setattr(er.ElasticCaseReporter, "_finish", broken)
    stream.pytest_runtest_logfinish("t.py::test_a")
    assert stream.faults
    assert "the bug is in here" in stream.faults[0]
    assert stream._cases == {}  # dropped, so the end of the run cannot close it again


def test_faults_are_said_once_and_bounded(stream):
    for _ in range(200):
        stream.fault("somewhere", RuntimeError("the same thing again"))
    assert len(stream.faults) == 1
    for n in range(200):
        stream.fault("somewhere", RuntimeError(f"number {n}"))
    assert len(stream.faults) == er.MAX_ERRORS


def test_a_nested_session_gives_the_outer_plugin_back(monkeypatch):
    """A process that runs pytest twice must not strand the first one."""
    monkeypatch.setattr(er.ElasticPlugin, "current", None)
    outer = er.ElasticPlugin(er.CaseAttributes())
    inner = er.ElasticPlugin(er.CaseAttributes())
    assert er.ElasticPlugin.current is inner
    inner.detach()
    assert er.ElasticPlugin.current is outer
    outer.detach()
    assert er.ElasticPlugin.current is None


def test_a_vocabulary_missing_a_word_is_caught_before_the_run():
    with pytest.raises(KeyError, match="passed"):
        er.check_vocabulary(("OUTCOMES", {"failed": "FAIL"}))
    with pytest.raises(TypeError, match="strings"):
        er.check_vocabulary(("PHASE_STEPS", {"setup": 1, "call": "call", "teardown": "t"}))


def test_a_vocabulary_broken_after_configure_degrades_instead_of_raising(monkeypatch):
    """The check passed, then a conftest mutated the table anyway."""
    monkeypatch.setitem(er.OUTCOMES, "passed", "PASS")
    assert er.outcome_of("passed") == "PASS"
    monkeypatch.delitem(er.OUTCOMES, "passed")
    assert er.outcome_of("passed") == "passed"  # the plain word, not a KeyError
    monkeypatch.setitem(er.OUTCOMES, "passed", 7)
    assert er.outcome_of("passed") == "passed"  # nor a number on a string field


def test_an_exception_whose_str_raises_still_reports(run):
    result, documents = run(
        """
        class Nasty(Exception):
            def __str__(self):
                raise RuntimeError("even my message is broken")

        def test_nasty():
            raise Nasty()
        """,
    )
    assert result.ret == 1  # the test failed, which is the test's business
    assert_one_verdict_per_case(documents)
    call = next(d for d in documents if d["step_name"] == "call")
    assert call["exception"] == "Nasty"
    assert call["exception_message"] == "<exception message unavailable>"


def test_a_bug_in_the_reporting_does_not_fail_the_run(run):
    """Break the plugin for one test, on purpose. Every other test still reports."""
    conftest = (
        RECORD
        + """
import elastic_reporter as er

_build = er.ElasticCaseReporter._build


def broken(self, step_name, status, case, failure=er.NO_FAILURE):
    if case.identity[1] == "test_b":
        raise RuntimeError("the reporting is broken")
    return _build(self, step_name, status, case, failure)


er.ElasticCaseReporter._build = broken
"""
    )
    result, documents = run(
        """
        def test_a(): pass
        def test_b(): pass
        def test_c(): pass
        """,
        conftest=conftest,
    )
    result.assert_outcomes(passed=3)  # the run is untouched by the reporting
    cases = by_case(documents)
    assert len(cases["test_a"]) == 3
    assert len(cases["test_c"]) == 3
    assert cases["test_a"][-1]["outcome"] == "passed"
    assert cases["test_c"][-1]["outcome"] == "passed"
    result.stdout.fnmatch_lines(["*the reporting is broken*"])


def test_warnings_as_errors_does_not_fail_the_run(run):
    """A suite that turns warnings into errors must not turn a fault into one."""
    conftest = (
        RECORD
        + """
import elastic_reporter as er

_build = er.ElasticCaseReporter._build


def broken(self, step_name, status, case, failure=er.NO_FAILURE):
    raise RuntimeError("everything is broken")


er.ElasticCaseReporter._build = broken
"""
    )
    result, _ = run(
        """
        def test_a(): pass
        """,
        "-W",
        "error",
        conftest=conftest,
    )
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize("order", ["reporter first", "annotator first"])
def test_both_halves_hand_the_session_back_in_any_order(monkeypatch, order):
    """The unconfigure order is pytest's to choose, not ours."""
    monkeypatch.setattr(er.ElasticPlugin, "current", None)
    before = er.ElasticPlugin(er.CaseAttributes())
    attributes = er.CaseAttributes()
    annotator = er.ReportAnnotator(attributes)
    reporter = er.ElasticCaseReporter(FakeHook(), attributes)
    halves = [reporter, annotator] if order == "reporter first" else [annotator, reporter]
    for half in halves:
        half.detach()
    assert er.ElasticPlugin.current is before


def test_a_report_after_the_run_is_counted_not_lost():
    reports = er.ReportQueue(FakeHook())
    reports.start()
    reports.close(timeout=5)
    reports.submit(a_report("test_too_late"))
    assert reports.dropped == 1


def test_a_hook_that_empties_the_batch_is_still_counted(monkeypatch):
    monkeypatch.setattr(er, "BATCH_SIZE", 2)

    class Greedy:
        def pytest_case_reports(self, reports):
            reports.clear()  # took them, and took the list with them

    reports = er.ReportQueue(Greedy())
    reports.start()
    for n in range(4):
        reports.submit(a_report(f"test_{n}"))
    reports.close(timeout=5)
    assert reports.sent == 4


def test_a_fault_is_recorded_even_when_the_exception_cannot_say_why(stream):
    class UnspeakableError(Exception):
        def __str__(self):
            raise RuntimeError  # not even the message works

    stream.fault("somewhere", UnspeakableError())
    assert stream.faults == ["somewhere: UnspeakableError: (no message)"]
