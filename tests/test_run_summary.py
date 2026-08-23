"""One per run, whose absence is the finding."""

from __future__ import annotations

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


def test_no_evidence_directory_is_left_behind_by_a_plain_run(runner):
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.run("-p", "no:xdist", "test_suite.py")
    assert not (runner.pytester.path / ".pytest-failures").exists()


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
