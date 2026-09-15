"""Tests for the elastic case-report plugin.

    pytest -p pytester test_elastic_reporter.py

`-p pytester` is what the end-to-end half needs: it runs a real pytest in a
subprocess, plugin and all, and reads back what the mock sender was handed.
pytest-xdist and pytest-rerunfailures have to be installed for the two tests
that exercise them.
"""

import collections
import json
import queue
import shutil
import threading

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


def test_stamp_only_writes_settable_fields():
    report = er.CaseReport(outcome="passed", test_suite="t.py", test_case="test_a")
    er.stamp(report, {"vc": "fw-1", "machine": "rack1", "outcome": "TAMPERED", "nonsense": 1})
    assert (report.vc, report.machine) == ("fw-1", "rack1")
    assert report.outcome == "passed"  # a phase answers for its own
    assert not hasattr(report, "nonsense")


def test_to_dict_carries_the_elastic_timestamp():
    report = er.CaseReport(outcome="passed", test_suite="t.py", test_case="test_a")
    document = report.to_dict()
    assert document["@timestamp"] == report.time
    assert document["test_case"] == "test_a"


def test_attributes_reject_what_is_not_settable():
    attributes = er.CaseAttributes(er.ReporterConfig())
    attributes.set(vc="fw-1", machine="rack1")
    assert attributes.snapshot()["vc"] == "fw-1"
    with pytest.raises(ValueError, match="not a case attribute: outcome"):
        attributes.set(outcome="passed")


def test_attributes_survive_concurrent_writers():
    attributes = er.CaseAttributes(er.ReporterConfig())
    threads = [threading.Thread(target=attributes.set, kwargs={"vc": f"fw-{n}"}) for n in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert attributes.snapshot()["vc"].startswith("fw-")


def test_reporter_works_outside_a_session():
    er.ElasticPlugin.current = None
    er.reporter().set(vc="fw-nowhere")  # must not raise, must not need a config
    assert er.reporter().attributes.snapshot()["vc"] == "fw-nowhere"


# ---------------------------------------------------------------------------
# the shipper
# ---------------------------------------------------------------------------


def a_report(name):
    return er.CaseReport(outcome="passed", test_suite="t.py", test_case=name)


def test_shipper_batches_in_order_and_drains_on_close():
    sender = er.MockElasticSender("https://example.invalid")
    shipper = er.BackgroundShipper(sender, batch_size=3, flush_interval=60)
    shipper.start()
    for n in range(7):
        shipper.submit(a_report(f"test_{n}"))
    shipper.close(timeout=5)
    assert [d["test_case"] for d in sender.documents] == [f"test_{n}" for n in range(7)]
    assert [r["count"] for r in sender.requests] == [3, 3, 1]
    assert shipper.shipped == 7
    assert shipper.dropped == 0


def test_shipper_drops_rather_than_growing_without_limit(monkeypatch):
    monkeypatch.setattr(er, "MAX_QUEUED", 10)
    blocked = threading.Event()

    class Blocked:
        def send(self, body):
            blocked.wait(timeout=5)

    shipper = er.BackgroundShipper(Blocked(), batch_size=1, flush_interval=60)
    shipper.start()
    for n in range(200):
        shipper.submit(a_report(f"test_{n}"))
    assert shipper._queue.qsize() <= 10
    assert shipper.dropped >= 180  # the rest never reached elastic, and said so
    blocked.set()
    shipper.close(timeout=5)


def test_shipper_survives_a_sender_that_raises():
    class Broken:
        def send(self, body):
            raise RuntimeError("503")

    shipper = er.BackgroundShipper(Broken(), batch_size=2, flush_interval=60)
    shipper.start()
    for n in range(6):
        shipper.submit(a_report(f"test_{n}"))
    shipper.close(timeout=5)
    assert shipper.shipped == 0
    assert shipper.dropped == 6
    assert shipper.failures == 3
    assert "503" in shipper.errors[0]


def test_shipper_keeps_only_the_first_errors():
    class Broken:
        def send(self, body):
            raise RuntimeError("503")

    shipper = er.BackgroundShipper(Broken(), batch_size=1, flush_interval=60)
    shipper.start()
    for n in range(er.MAX_ERRORS + 15):
        shipper.submit(a_report(f"test_{n}"))
    shipper.close(timeout=5)
    assert len(shipper.errors) == er.MAX_ERRORS
    assert shipper.failures == er.MAX_ERRORS + 15


def test_shipper_close_is_safe_twice_and_before_start():
    shipper = er.BackgroundShipper(er.MockElasticSender("u"), batch_size=1, flush_interval=60)
    shipper.close(timeout=1)  # never started
    shipper.start()
    shipper.close(timeout=5)
    shipper.close(timeout=5)


def test_queue_is_bounded_by_the_constant():
    shipper = er.BackgroundShipper(er.MockElasticSender("u"), batch_size=1, flush_interval=60)
    assert shipper._queue.maxsize == er.MAX_QUEUED
    with pytest.raises(queue.Full):
        for _ in range(er.MAX_QUEUED + 1):
            shipper._queue.put_nowait(a_report("t"))


# ---------------------------------------------------------------------------
# the plugin, run by a real pytest, from the outside
# ---------------------------------------------------------------------------


DUMP = """
import json

import elastic_reporter as er


def pytest_unconfigure(config):
    reporter = config.pluginmanager.getplugin(er.PLUGIN_NAME)
    if not isinstance(reporter, er.ElasticCaseReporter):
        return
    documents = [json.loads(json.dumps(d, default=er.json_default))
                 for d in reporter.sender.documents]
    (config.rootpath / "shipped.json").write_text(json.dumps(documents))
"""


@pytest.fixture
def run(pytester):
    """Run an inner pytest with the plugin, and return what it shipped."""
    shutil.copy(er.__file__, pytester.path / "elastic_reporter.py")

    def _run(source, *args, conftest=""):
        pytester.makeconftest(DUMP + conftest)
        pytester.makepyfile(source)
        result = pytester.runpytest_subprocess("-p", "elastic_reporter", *args)
        shipped = pytester.path / "shipped.json"
        return result, json.loads(shipped.read_text()) if shipped.exists() else []

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


def assert_one_last_report_per_case(documents):
    for name, reports in by_case(documents).items():
        flagged = [r for r in reports if r["last_report"]]
        assert len(flagged) == 1, f"{name}: {len(flagged)} last_report flags"
        assert flagged[0] is reports[-1], f"{name}: the flag is not on its last report"


# ---------------------------------------------------------------------------


def test_every_phase_of_every_outcome(run):
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
    assert_one_last_report_per_case(documents)
    cases = by_case(documents)
    assert [r["step_name"] for r in cases["test_passes"]] == ["setup", "call", "teardown"]
    assert [r["outcome"] for r in cases["test_passes"]] == ["passed"] * 3
    assert [r["outcome"] for r in cases["test_fails"]] == ["passed", "failed", "passed"]
    assert cases["test_fails"][1]["exception"] == "AssertionError"
    assert cases["test_fails"][1]["exception_traceback"]
    # A skipped test never runs its call phase, so it has no call report.
    assert [r["step_name"] for r in cases["test_skipped"]] == ["setup", "teardown"]
    assert cases["test_xfails"][1]["outcome"] == "xfailed"
    assert cases["test_setup_error"][0]["outcome"] == "error"


def test_attributes_describe_the_whole_test(run):
    _, documents = run(
        """
        import elastic_reporter as er

        def test_sets_in_call():
            er.reporter().set(vc="fw-5.0.0", machine="rack1")

        def test_inherits_the_vc():
            pass
        """,
    )
    assert_one_last_report_per_case(documents)
    cases = by_case(documents)
    # Set in the call phase, but the setup report carries it too.
    assert {r["vc"] for r in cases["test_sets_in_call"]} == {"fw-5.0.0"}
    assert {r["machine"] for r in cases["test_sets_in_call"]} == {"rack1"}
    # And it sticks for the test after it.
    assert {r["vc"] for r in cases["test_inherits_the_vc"]} == {"fw-5.0.0"}


def test_options_and_ini_set_the_run_wide_attributes(run, pytester):
    pytester.makeini("[pytest]\nelastic_owner = ini-owner\nelastic_cycle_id = 99\n")
    _, documents = run("def test_a(): pass", "--elastic-vc=fw-from-cli")
    assert {d["vc"] for d in documents} == {"fw-from-cli"}
    assert {d["owner"] for d in documents} == {"ini-owner"}
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
    assert_one_last_report_per_case(documents)
    cases = by_case(documents)
    assert [r["step_name"] for r in cases["test_exits"]] == ["setup", "teardown"]
    assert cases["test_exits"][-1]["outcome"] == "crashed"
    assert cases["test_exits"][-1]["exception"] == "TestIncomplete"
    assert "test_never_runs" not in cases


def test_collection_error_is_reported_once(run):
    _, documents = run("import definitely_not_a_real_module")
    assert len(documents) == 1
    assert documents[0]["step_name"] == "collect"
    assert documents[0]["outcome"] == "error"
    assert documents[0]["last_report"] is True


def test_every_attempt_of_a_rerun_is_one_case(run):
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
    assert_one_last_report_per_case(documents)
    reports = by_case(documents)["test_flaky"]
    assert len(reports) == 9  # three attempts of three phases
    assert [r["outcome"] for r in reports] == (
        ["passed", "rerun", "passed"] * 2 + ["passed", "passed", "passed"]
    )
    assert reports[-1]["last_report"] is True


def test_the_controller_owns_the_stream_under_xdist(run):
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
    assert_one_last_report_per_case(documents)
    assert len(documents) == 24  # 8 tests, 3 phases, one stream
    assert sum(d["last_report"] for d in documents) == 8


def test_switched_off_ships_nothing_and_still_takes_attributes(run):
    result, documents = run(
        """
        import elastic_reporter as er

        def test_a():
            er.reporter().set(vc="fw-1")  # must not raise with the plugin off
        """,
        "--elastic-off",
    )
    result.assert_outcomes(passed=1)
    assert documents == []


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
    assert_one_last_report_per_case(documents)
    assert len(documents) == 75
