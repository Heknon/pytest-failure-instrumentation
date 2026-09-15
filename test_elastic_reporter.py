"""Tests for the elastic case-report plugin.

    pytest -p pytester test_elastic_reporter.py

`-p pytester` is what the end-to-end half needs: it runs a real pytest in a
subprocess, plugin and all, and reads back what `pytest_case_reports` was
handed. pytest-xdist and pytest-rerunfailures have to be installed for the two
tests that exercise them.
"""

import collections
import json
import queue
import shutil
import threading
from time import monotonic

import pytest

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


def test_to_dict_is_json_serialisable_with_json_default():
    report = er.CaseReport(test_suite="t.py", test_case="test_a")
    document = json.loads(json.dumps(report.to_dict(), default=er.json_default))
    assert document["@timestamp"] == report.time.isoformat()
    assert document["test_case"] == "test_a"
    assert document["outcome"] is None  # no verdict unless it is the last report


def test_attributes_reject_what_is_not_settable():
    attributes = er.CaseAttributes()
    attributes.set(vc="fw-1", machine="rack1")
    assert attributes.snapshot()["vc"] == "fw-1"
    with pytest.raises(ValueError, match="not a case attribute: outcome"):
        attributes.set(outcome="passed")
    # The identity comes from the nodeid; a typo must not rewrite every report.
    with pytest.raises(ValueError, match="not a case attribute: test_case"):
        attributes.set(test_case="upgrade-suite")


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


def test_queue_is_bounded_by_the_constant():
    reports = er.ReportQueue(FakeHook())
    assert reports._queue.maxsize == er.MAX_QUEUED
    with pytest.raises(queue.Full):
        for _ in range(er.MAX_QUEUED + 1):
            reports._queue.put_nowait(a_report("t"))


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

import elastic_reporter as er

RECEIVED = []


def pytest_case_reports(reports):
    RECEIVED.extend(json.loads(json.dumps(r.to_dict(), default=er.json_default))
                    for r in reports)


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
