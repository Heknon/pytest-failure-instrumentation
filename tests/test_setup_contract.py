"""What this plugin promises never to do to a run it is only watching.

Every other test asks whether an incident is right. These ask whether the
instrumentation is safe to install at all, which is the prior question: a
reporting tool that ends a run, or eats a file, has done more damage than any
failure it might have explained. Both were real - an evidence directory that
could not be created raised out of ``pytest_configure`` before a single test
ran, and a green run deleted a coverage report that happened to be sitting in
the directory it was pointed at.
"""

from __future__ import annotations

from .conftest import needs_xdist

SUITE = """
def test_one():
    assert True


def test_two():
    assert True
"""


def test_a_directory_that_cannot_be_created_does_not_end_the_run(runner):
    """A read-only image, a vanished mount, a name already taken by a file.

    The plugin turns itself off and says so. It does not raise, because an
    exception in pytest_configure is an INTERNALERROR and the customer's suite
    never runs.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    (runner.pytester.path / "taken").write_text("not a directory", encoding="utf-8")

    result = runner.pytester.runpytest_subprocess(
        "-p", "no:xdist", "-o", "failure_directory=taken/evidence", "test_suite.py"
    )
    result.assert_outcomes(passed=2)


@needs_xdist
def test_a_worker_that_cannot_write_evidence_does_not_end_the_run(runner):
    """The worker path is the one that broke: the controller creates nothing
    until a worker dies, so only the workers reach mkdir on a normal run."""
    runner.pytester.makepyfile(test_suite=SUITE)
    (runner.pytester.path / "taken").write_text("not a directory", encoding="utf-8")

    result = runner.pytester.runpytest_subprocess(
        "-n", "2", "-o", "failure_directory=taken/evidence", "test_suite.py", timeout=180
    )
    result.assert_outcomes(passed=2)
    # A warning, on stderr, from each worker - not an error and not silence.
    result.stderr.fnmatch_lines(["*failure instrumentation is off for this worker*"])


@needs_xdist
def test_nothing_in_the_evidence_directory_that_is_not_ours_is_deleted(runner):
    """failure_directory is a natural thing to point at an existing artifacts
    directory. Clearing stale evidence used to take every .txt and .json with
    it, on a run where nothing failed at all."""
    runner.pytester.makepyfile(test_suite=SUITE)
    evidence = runner.pytester.path / "artifacts"
    evidence.mkdir()
    (evidence / "coverage.json").write_text('{"covered": 91}', encoding="utf-8")
    (evidence / "report.txt").write_text("somebody's build log", encoding="utf-8")
    # Ours, from a previous run, and named the way this plugin names them.
    (evidence / "collection-deadbeef.txt").write_text("stale", encoding="utf-8")
    (evidence / "gw9.events").write_text("stale\n", encoding="utf-8")

    runner.run("-n", "2", "-o", "failure_directory=artifacts", "test_suite.py", timeout=180)

    assert (evidence / "coverage.json").read_text(encoding="utf-8") == '{"covered": 91}'
    assert (evidence / "report.txt").exists()
    # Stale evidence still goes, or it would be read as this run's.
    assert not (evidence / "collection-deadbeef.txt").exists()
    assert not (evidence / "gw9.events").exists()


@needs_xdist
def test_a_worker_death_is_still_reported_from_a_relocated_directory(runner):
    """The narrowed cleanup must not have narrowed the plugin's own reach: a
    directory it shares with somebody else still has to work."""
    runner.pytester.makepyfile(
        test_crash="""
        import victim


        def test_filler():
            assert True


        def test_crashes():
            victim.native_call(1)
        """
    )
    (runner.pytester.path / "artifacts").mkdir()
    incidents = runner.run(
        "-n", "2", "-o", "failure_directory=artifacts", "test_crash.py", timeout=180
    )
    assert runner.only(incidents, "worker_death").verdict == "NATIVE_CRASH"
