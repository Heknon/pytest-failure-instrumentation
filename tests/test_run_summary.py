"""One per run, whose absence is the finding."""

from __future__ import annotations

import json

from .conftest import needs_xdist

SUITE = """
def test_one():
    assert True
"""


def test_a_plain_pytest_run_gets_a_summary(runner):
    """Two of the five kinds are not distributed problems, so the plugin
    registers whether or not xdist is in the picture."""
    runner.pytester.makepyfile(test_suite=SUITE)
    incidents = runner.run("-p", "no:xdist", "test_suite.py")

    summary = runner.only(incidents, "run_summary")
    assert summary.verdict == "RUN_FINISHED"
    assert summary.distributed is False
    assert summary.exitstatus == 0
    assert summary.raised == 0
    assert "single process" in str(summary)


def test_a_plain_run_leaves_one_directory_and_the_next_run_clears_it(runner):
    """It used to leave none, because it recorded nothing, and that absence
    was the whole assertion. Now it records itself and the evidence has to
    outlive the process - which is the point of writing it down - so what has
    to hold instead is that the directories do not pile up: every run sweeps
    the ones that are over before it makes its own."""
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.run("-p", "no:xdist", "test_suite.py")
    first, = _run_directories(runner.pytester)

    runner.run("-p", "no:xdist", "test_suite.py")
    second, = _run_directories(runner.pytester)
    assert second != first


def _run_directories(pytester):
    root = pytester.path / ".pytest-failures"
    return sorted(path for path in root.iterdir() if path.is_dir())


def test_a_run_without_xdist_still_gets_an_id_of_its_own(runner):
    """xdist's id is the good one, and there is none without xdist. What
    replaces it has to be unique per run: a timestamp is not, and two runs
    starting in the same second on CI is ordinary."""
    runner.pytester.makepyfile(test_suite=SUITE)
    first = runner.only(runner.run("-p", "no:xdist", "test_suite.py"), "run_summary")
    (runner.pytester.path / "incidents.jsonl").unlink()
    second = runner.only(runner.run("-p", "no:xdist", "test_suite.py"), "run_summary")

    assert first.run_id and first.run_id != "unknown"
    assert first.run_id != second.run_id


@needs_xdist
def test_a_distributed_run_says_so(runner):
    runner.pytester.makepyfile(test_suite=SUITE)
    incidents = runner.run("-n", "2", "test_suite.py", timeout=180)

    summary = runner.only(incidents, "run_summary")
    assert summary.distributed is True
    assert "distributed" in str(summary)


@needs_xdist
def test_one_defect_on_many_workers_is_one_incident_with_a_count(runner):
    runner.pytester.makepyfile(
        test_crash="""
        import victim


        def test_a():
            victim.native_call(1)


        def test_b():
            victim.native_call(1)


        def test_c():
            victim.native_call(1)
        """
    )
    incidents = runner.run("-n", "3", "test_crash.py", timeout=180)

    summary = runner.only(incidents, "run_summary")
    deaths = runner.of_kind(incidents, "worker_death")

    # Same defect, same fingerprint, so the hook is called once however many
    # workers hit it - and the ones that were folded in are counted, not lost.
    assert len(deaths) == 1
    assert summary.incidents[deaths[0].fingerprint] == 1 + summary.duplicates_suppressed
    assert summary.raised == len(incidents) - 1  # every incident but this one


@needs_xdist
def test_the_controller_and_its_workers_agree_on_the_run_id(runner):
    """run_id is what tells one run's evidence from another's.

    It used to be read off an xdist attribute the controller cannot reach in
    every version, so it silently fell back to a bare timestamp - and the
    workers, which never saw it at all, stamped nothing. Two ids for one run
    is the same as no id.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    incidents = runner.run("-n", "2", "test_suite.py", timeout=180)

    summary = runner.only(incidents, "run_summary")
    assert summary.run_id and summary.run_id != "unknown"

    stamped = {
        json.loads(line).get("run_id")
        for path in (runner.pytester.path / ".pytest-failures").glob("*/*.events")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert stamped == {summary.run_id}


def test_a_run_that_finished_is_not_told_it_could_not_have(runner):
    """``run_ending`` is the inference the evidence supported when an incident
    was raised: a worker silent past the threshold has handed xdist work it
    will never give back. A summary is the one thing emitted late enough to
    know whether that came true, and here it did not - the frozen worker came
    back and the run passed. Saying "N of them ended the session" over an exit
    status of 0 reports a run that finished as one that could not."""
    from pytest_failure_instrumentation.incidents import summary

    incident = summary.build(
        exitstatus=0, seen={"abc": 1}, raised=1, suppressed=0, run_ending=1,
        distributed=True,
    )

    rendered = str(incident)
    assert "raised as run-ending" in rendered
    assert "still reached session finish" in rendered
    assert "of them ended the session" not in rendered
