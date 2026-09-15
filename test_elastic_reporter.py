"""Tests for the elastic case-report plugin.

    pytest -p pytester test_elastic_reporter.py

`-p pytester` is what the end-to-end half needs: it runs a real pytest in a
subprocess, plugin and all, and reads back what `pytest_case_report` was
handed. pytest-xdist and pytest-rerunfailures have to be installed for the two
tests that exercise them.
"""

import collections
import json
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


def test_to_dict_is_json_serialisable_with_json_default():
    report = er.CaseReport(outcome="passed", test_suite="t.py", test_case="test_a")
    document = json.loads(json.dumps(report.to_dict(), default=er.json_default))
    assert document["@timestamp"] == report.time.isoformat()
    assert document["test_case"] == "test_a"


def test_unset_attributes_keep_the_models_defaults():
    attributes = er.CaseAttributes()
    assert attributes.snapshot() == {}
    report = er.CaseReport(outcome="passed", test_suite="t.py", test_case="test_a")
    er.stamp(report, attributes.snapshot())
    assert (report.vc, report.owner, report.cycle_id, report.labs3) == (
        "mock-vc",
        "mock-owner",
        1,
        True,
    )


def test_attributes_reject_what_is_not_settable():
    attributes = er.CaseAttributes()
    attributes.set(vc="fw-1", machine="rack1")
    assert attributes.snapshot()["vc"] == "fw-1"
    with pytest.raises(ValueError, match="not a case attribute: outcome"):
        attributes.set(outcome="passed")


def test_attributes_survive_concurrent_writers():
    attributes = er.CaseAttributes()
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
# the plugin, run by a real pytest, from the outside
# ---------------------------------------------------------------------------

#: An implementation of the hook that keeps what it was given, so a test can
#: read it back. It is also the shortest example of one.
RECORD = """
import json

import elastic_reporter as er

RECEIVED = []


def pytest_case_report(report):
    RECEIVED.append(json.loads(json.dumps(report.to_dict(), default=er.json_default)))


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


def test_the_hook_is_called_once_per_report_in_order(run):
    result, _ = run(
        """
        def test_a(): pass
        def test_b(): pass
        """,
        "-s",
        conftest="""
import elastic_reporter as er


def pytest_case_report(report):
    print(f"HOOK {report.test_case}/{report.step_name} last={report.last_report}")
""",
    )
    # pytest's own progress output shares the line, so cut from the marker.
    hooked = [line[line.index("HOOK") :] for line in result.outlines if "HOOK" in line]
    assert hooked == [
        "HOOK test_a/setup last=False",
        "HOOK test_a/call last=False",
        "HOOK test_a/teardown last=True",
        "HOOK test_b/setup last=False",
        "HOOK test_b/call last=False",
        "HOOK test_b/teardown last=True",
    ]


def test_a_hook_that_raises_is_counted_not_fatal(run):
    result, _ = run(
        "def test_a(): pass",
        conftest="""
def pytest_case_report(report):
    raise RuntimeError("the api is down")
""",
    )
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert "elastic-reporter: 3 case report(s)" in "\n".join(result.outlines)
    assert any("RuntimeError: the api is down" in line for line in result.outlines)


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


def test_a_conftest_hook_sets_the_run_wide_attributes(run):
    """The plugin has no options: this is what replaces them, workers included."""
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


def test_without_the_plugin_loaded_nothing_reports_and_set_still_works(pytester):
    """Not loading it is how the reporting is switched off."""
    shutil.copy(er.__file__, pytester.path / "elastic_reporter.py")
    pytester.makeconftest(
        """
        import pytest

        @pytest.hookimpl(optionalhook=True)  # the plugin may not be loaded
        def pytest_case_report(report):
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
    assert_one_last_report_per_case(documents)
    assert len(documents) == 75
