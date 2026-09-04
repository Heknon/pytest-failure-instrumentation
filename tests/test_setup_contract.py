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

import subprocess
import sys

import pytest

from .conftest import ENABLE_FLAG, INNER_CONFTEST, needs_xdist

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
        ENABLE_FLAG, "-p", "no:xdist", "-o", "failure_directory=taken/evidence",
        "test_suite.py",
    )
    result.assert_outcomes(passed=2)


@needs_xdist
def test_a_worker_that_cannot_write_evidence_does_not_end_the_run(runner):
    """The worker path is the one that broke: the controller creates nothing
    until a worker dies, so only the workers reach mkdir on a normal run."""
    runner.pytester.makepyfile(test_suite=SUITE)
    (runner.pytester.path / "taken").write_text("not a directory", encoding="utf-8")

    result = runner.pytester.runpytest_subprocess(
        ENABLE_FLAG, "-n", "2", "-o", "failure_directory=taken/evidence", "test_suite.py",
        timeout=180,
    )
    result.assert_outcomes(passed=2)
    # A warning, on stderr, from each worker - not an error and not silence.
    result.stderr.fnmatch_lines(["*failure instrumentation is off for this worker*"])


#: A conftest that records whether the plugin was installed on this process,
#: and implements the incident hook - which is the part that has to keep
#: working when the plugin is off: the hookspec still has to be there, or a
#: consumer's conftest fails the whole session at check_pending.
REPORTS_WHETHER_INSTALLED = INNER_CONFTEST + """

from pytest_failure_instrumentation import installed_settings


def pytest_sessionstart(session):
    worker = getattr(session.config, "workerinput", None)
    name = worker["workerid"] if worker else "controller"
    with open(f"installed-{name}.txt", "w", encoding="utf-8") as handle:
        handle.write(repr(installed_settings(session.config)))
"""


def _installed_on(runner) -> dict[str, str]:
    return {
        path.stem.split("-", 1)[1]: path.read_text(encoding="utf-8")
        for path in sorted(runner.pytester.path.glob("installed-*.txt"))
    }


def test_installed_is_not_switched_on(runner):
    """A run that did not ask gets the hookspecs and the options and nothing
    else: no evidence directory, no recorder, no summary at the end.

    The switch is what makes ``pip install`` safe to leave in a shared
    environment - a plugin that starts writing to every run's working
    directory the moment it is present is one that gets uninstalled.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makeconftest(REPORTS_WHETHER_INSTALLED)

    result = runner.pytester.runpytest_subprocess("-p", "no:xdist", "test_suite.py")

    result.assert_outcomes(passed=2)
    assert "INTERNALERROR" not in result.stdout.str()
    assert _installed_on(runner) == {"controller": "None"}
    assert not (runner.pytester.path / ".pytest-failures").exists()
    assert runner.incidents() == [], "nothing was watching, so nothing reports"
    # The ini keys are still known, because the options are registered whether
    # or not the plugin is on - a project's ini must not become invalid
    # because one run left the switch off.
    assert "unknown config option" not in result.stdout.str().lower()


@needs_xdist
def test_workers_are_not_switched_on_either(runner):
    """A worker replays the controller's argv and is handed its settings, and
    with neither saying anything the worker has nothing to obey."""
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makeconftest(REPORTS_WHETHER_INSTALLED)

    result = runner.pytester.runpytest_subprocess("-n", "2", "test_suite.py", timeout=180)

    result.assert_outcomes(passed=2)
    assert _installed_on(runner) == {"controller": "None", "gw0": "None", "gw1": "None"}
    assert not (runner.pytester.path / ".pytest-failures").exists()
    assert runner.incidents() == []


@needs_xdist
def test_the_switch_on_the_controller_reaches_the_workers(runner):
    """The flag travels: xdist replays argv on every worker, and the controller
    hands its settings down as well, so a run switched on once is on
    everywhere."""
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makeconftest(REPORTS_WHETHER_INSTALLED)

    incidents = runner.run("-n", "2", "test_suite.py", timeout=180)

    installed = _installed_on(runner)
    assert set(installed) == {"controller", "gw0", "gw1"}
    assert all(record != "None" for record in installed.values()), installed
    assert runner.only(incidents, "run_summary").verdict == "RUN_FINISHED"


def test_naming_the_live_view_switches_it_on(runner):
    """``--callstack-port`` is a request for a server, and the server cannot
    run without the plugin under it - so it is a request for the plugin. An
    option accepted and then ignored for want of a second one is the failure
    the option's own docstring exists to rule out."""
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makeconftest(REPORTS_WHETHER_INSTALLED)

    result = runner.pytester.runpytest_subprocess(
        "-p", "no:xdist", "--callstack-port", "0", "test_suite.py"
    )

    result.assert_outcomes(passed=2)
    (record,) = _installed_on(runner).values()
    assert "stack_server=True" in record
    assert runner.only(runner.incidents(), "run_summary").verdict == "RUN_FINISHED"


#: A psutil that is installed and will not import, which is what a platform
#: with no wheel, a C extension built against another libc, or ``pip install
#: --no-deps`` actually leaves behind. Shadowing rather than deleting, because
#: the two are different paths through the importer and only this one is the
#: failure being reproduced.
BROKEN_PSUTIL = 'raise ImportError("no psutil wheel for this platform")\n'


def _psutil_that_will_not_import(runner, monkeypatch) -> None:
    """Put ``BROKEN_PSUTIL`` ahead of the real one for the inner run.

    pytester prepends the inner run's own directory to PYTHONPATH and keeps
    whatever is already there, so this entry still sits ahead of the
    site-packages psutil - in the controller and in every worker it spawns.
    """
    shadow = runner.pytester.path / "no-psutil"
    shadow.mkdir()
    (shadow / "psutil.py").write_text(BROKEN_PSUTIL, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shadow))


def test_a_psutil_that_will_not_import_does_not_end_the_run(runner, monkeypatch):
    """psutil is a hard dependency, and a hard dependency can still be missing.

    Every probe reads process facts through psutil, so both of the objects
    registration builds - the worker's recorder and the controller's engine -
    reach it through their imports. Those imports used to sit outside the
    try/except that exists for exactly this, one statement above the call it
    guards, which is close enough to look guarded and is not. An ImportError
    then left ``pytest_configure`` as an INTERNALERROR: exit status 3, not a
    test collected, and nothing on the machine any longer running the suite
    the plugin was installed to report on.

    The dependency being declared is not a defence. No wheel for the platform
    and a source build that fails, a C extension against the wrong libc, ``pip
    install --no-deps``, a mirror carrying the pure Python packages and not
    the compiled one - each of them installs a plugin that cannot import its
    probes, and none of them is the customer's fault or fixable from here.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    _psutil_that_will_not_import(runner, monkeypatch)

    result = runner.pytester.runpytest_subprocess(ENABLE_FLAG, "-p", "no:xdist", "test_suite.py")
    result.assert_outcomes(passed=2)
    assert "INTERNALERROR" not in result.stdout.str()
    result.stderr.fnmatch_lines(["*failure instrumentation is off for this run*"])


@needs_xdist
def test_a_worker_whose_psutil_will_not_import_does_not_end_the_run(runner, monkeypatch):
    """The same import, in the process where it costs the most.

    A worker builds a recorder rather than an engine, through a different
    import that reaches the same probes. It is also the multiplying case: the
    controller failing this way loses one run, while every worker failing it
    loses the run several times over, with the traceback repeated once per
    process and no test result anywhere.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    _psutil_that_will_not_import(runner, monkeypatch)

    result = runner.pytester.runpytest_subprocess(
        ENABLE_FLAG, "-n", "2", "test_suite.py", timeout=180
    )
    result.assert_outcomes(passed=2)
    assert "INTERNALERROR" not in result.stdout.str()
    result.stderr.fnmatch_lines(["*failure instrumentation is off for this worker*"])


@needs_xdist
def test_nothing_in_the_evidence_directory_that_is_not_ours_is_touched(runner):
    """failure_directory is a natural thing to point at an existing artifacts
    directory. Clearing stale evidence used to take every .txt and .json with
    it, on a run where nothing failed at all.

    A run keeps to its own directory now, so there is nothing at this level it
    has any reason to delete - including the flat files an older version of
    this plugin left here, which cannot be mistaken for this run's evidence
    because this run does not look here for any."""
    runner.pytester.makepyfile(test_suite=SUITE)
    evidence = runner.pytester.path / "artifacts"
    evidence.mkdir()
    (evidence / "coverage.json").write_text('{"covered": 91}', encoding="utf-8")
    (evidence / "report.txt").write_text("somebody's build log", encoding="utf-8")
    (evidence / "collection-deadbeef.txt").write_text("stale", encoding="utf-8")
    (evidence / "gw9.events").write_text("stale\n", encoding="utf-8")

    runner.run("-n", "2", "-o", "failure_directory=artifacts", "test_suite.py", timeout=180)

    for survivor in ("coverage.json", "report.txt", "collection-deadbeef.txt", "gw9.events"):
        assert (evidence / survivor).exists(), survivor

    # And this run's own evidence went somewhere it cannot be confused with
    # any of that.
    runs = [path for path in evidence.iterdir() if path.is_dir()]
    assert len(runs) == 1
    assert (runs[0] / "owner.json").exists()
    assert list(runs[0].glob("*.events"))


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


# -- one directory per run ------------------------------------------------


def test_a_finished_runs_directory_is_pruned_and_a_live_one_is_not(tmp_path):
    """Over, not old. Several runs happen at once - that is the whole reason
    each has a directory - so age says nothing about whether one is finished.
    """
    import json
    import os

    from pytest_failure_instrumentation.incidents.leftovers import prune_finished_runs

    live = tmp_path / "run-live"
    finished = tmp_path / "run-finished"
    for path, pid in ((live, os.getpid()), (finished, 999999)):
        path.mkdir()
        (path / "owner.json").write_text(json.dumps({"pid": pid}), encoding="utf-8")
        (path / "gw0.events").write_text("evidence\n", encoding="utf-8")

    prune_finished_runs(tmp_path)

    assert live.exists() and (live / "gw0.events").exists()
    assert not finished.exists()


def test_a_run_reusing_a_directory_does_not_inherit_the_last_attempt_s_workers(
    distributed, monkeypatch
):
    """Sequential reuse of PYTEST_RUN_ID is supported; inheriting is not.

    The sweep that removes finished runs used to run *after* this run wrote its
    own marker - so our own directory named a live pid and was skipped by the
    very sweep meant to clear it. A four-worker run followed by a one-worker
    run under one build id left the second reporting four workers, three of
    them the first attempt's corpses, out of a directory holding two distinct
    xdist run ids.
    """
    import time

    from pytest_failure_instrumentation import topology

    distributed.pytester.makepyfile(
        test_suite="def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n"
    )
    monkeypatch.setenv("PYTEST_RUN_ID", "build-123")

    distributed.run("-n", "4", "test_suite.py", timeout=180)
    evidence = distributed.pytester.path / ".pytest-failures" / "build-123"
    assert len(sorted(evidence.glob("*.state"))) == 4, "the first run wrote four"

    distributed.run("-n", "1", "test_suite.py", timeout=180)

    assert [path.stem for path in sorted(evidence.glob("*.state"))] == ["gw0"]
    described = topology.run(evidence, time.time())
    assert [entry["worker"] for entry in described["workers"]] == ["gw0"]


# -- the name an operator gives the run -----------------------------------

#: Values a build system really hands over, and every one of them has to keep
#: working: a bare build number, a git SHA, a matrix cell, a slugified branch,
#: a release tag, and the longest name accepted at all.
REAL_RUN_NAMES = (
    "4821",
    "0f7c2a1b3d4e5f60718293a4b5c6d7e8f9a0b1c2",
    "ubuntu-22.04-py3.11",
    "release_2024-06-01",
    "v1.2.3-rc.1",
    "a" * 128,
)

#: Values that are not a name for one directory, whatever they look like. The
#: first two are the whole reason this is checked at all - one replaces the
#: configured directory and the other walks out of it - and the rest are the
#: ways a name stops meaning itself: a separator on either platform, a
#: drive-relative spelling, a NUL that ``mkdir`` answers with a ``ValueError``
#: no ``except OSError`` catches, a Windows device, one character over the
#: limit, and the two names that resolve to a directory that already exists.
IMPOSSIBLE_RUN_NAMES = (
    "/tmp/somewhere-else",
    "../../escaped",
    "feature/branch",
    "run\\id",
    "C:foo",
    "run\x00id",
    "NUL",
    "CON.evidence",
    "a" * 129,
    ".",
    "..",
    "...",
    "run id",
)


def test_only_a_name_for_one_directory_is_accepted_as_a_run_name():
    """The shape, in one place, before any of it reaches a filesystem call.

    Both halves matter and only one of them is about safety. A rule strict
    enough to reject ``/tmp/x`` and loose enough to accept a SHA and a matrix
    cell is the only rule that can be applied at all: PYTEST_RUN_ID is
    documented as *the* way to name a run, so a check that broke that would be
    turned off by the next person who needed the correlation.
    """
    from pytest_failure_instrumentation.incidents.engine import usable_as_a_run_name

    for name in REAL_RUN_NAMES:
        assert usable_as_a_run_name(name), name
    for name in IMPOSSIBLE_RUN_NAMES:
        assert not usable_as_a_run_name(name), name


def test_a_run_name_that_cannot_be_used_is_said_out_loud_and_not_dropped(monkeypatch):
    """Falling back is right; falling back in silence is the actual failure.

    A CI job exports a build id so that this run's incidents carry it. Ignoring
    the value leaves that correlation broken with nothing anywhere saying so -
    the job is green, the evidence is filed under a name nobody searched for,
    and the person who set the variable finds out from an incident that will
    not join. So the advice names the value that was refused and the name used
    instead, through the same path every other unusable setting goes through.
    """
    from pytest_failure_instrumentation.config import FailureInstrumentationWarning
    from pytest_failure_instrumentation.incidents.engine import name_this_run

    monkeypatch.setenv("PYTEST_RUN_ID", "/tmp/somewhere-else")
    with pytest.warns(FailureInstrumentationWarning, match="somewhere-else"):
        assert name_this_run("run-fallback") == "run-fallback"

    # Surrounding space is a YAML accident rather than a decision, and is
    # folded the way every other setting a person types is folded.
    monkeypatch.setenv("PYTEST_RUN_ID", "  build-4821  ")
    assert name_this_run("run-fallback") == "build-4821"

    # Unset and exported-empty both say nothing, and there is nothing to warn
    # about: this is the ordinary run that names itself.
    monkeypatch.setenv("PYTEST_RUN_ID", "")
    assert name_this_run("run-fallback") == "run-fallback"
    monkeypatch.delenv("PYTEST_RUN_ID")
    assert name_this_run("run-fallback") == "run-fallback"


def _sandboxed(runner):
    """A directory holding nothing but the configured evidence directory.

    So that "the run wrote outside failure_directory" is one assertion about
    one directory's contents, rather than a guess at which of the files an
    inner pytest leaves behind were supposed to be there.
    """
    root = runner.pytester.path / "sandbox"
    root.mkdir()
    return root


@needs_xdist
def test_a_run_name_that_is_an_absolute_path_does_not_move_the_evidence(
    distributed, monkeypatch
):
    """An absolute right-hand side replaces the left, and pathlib says nothing.

    ``failure_directory / PYTEST_RUN_ID`` is the only place a person's value
    becomes a path in this package, and with ``PYTEST_RUN_ID=/tmp/x`` the
    configured directory stopped being where the run wrote: owner.json, every
    worker's state, events, crash and frozen file, and the live view's
    discovery file all went to /tmp/x. Nothing failed, so nothing said so.
    """
    distributed.pytester.makepyfile(test_suite=SUITE)
    root = _sandboxed(distributed)
    monkeypatch.setenv("PYTEST_RUN_ID", str(root / "escaped-absolute"))

    distributed.run(
        "-n", "2", "-o", f"failure_directory={root / 'artifacts'}", "test_suite.py",
        timeout=180,
    )

    assert [path.name for path in root.iterdir()] == ["artifacts"]
    # Directories only: the ignore file that keeps the evidence out of git
    # sits beside them, and this is a count of runs.
    runs = [path for path in (root / "artifacts").iterdir() if path.is_dir()]
    assert len(runs) == 1 and (runs[0] / "owner.json").exists()
    assert list(runs[0].glob("*.state")), "the workers wrote where the controller did"
    distributed.result.stderr.fnmatch_lines(["*PYTEST_RUN_ID*escaped-absolute*"])


@needs_xdist
def test_a_run_name_that_climbs_out_of_the_directory_does_not_climb(
    distributed, monkeypatch
):
    """``..`` is the other half of the same hole, and mkdir(parents=True)
    creates whatever it is handed rather than refusing to leave.

    Not a hypothetical spelling: a job that sets PYTEST_RUN_ID from a path
    fragment - a relative artifact path, a workspace-relative directory - hands
    over exactly this, and the run reports a green suite whose evidence is a
    level above the directory anything collects from.
    """
    distributed.pytester.makepyfile(test_suite=SUITE)
    root = _sandboxed(distributed)
    monkeypatch.setenv("PYTEST_RUN_ID", "../escaped-upward")

    distributed.run(
        "-n", "2", "-o", f"failure_directory={root / 'artifacts'}", "test_suite.py",
        timeout=180,
    )

    assert [path.name for path in root.iterdir()] == ["artifacts"]
    # Directories only: the ignore file that keeps the evidence out of git
    # sits beside them, and this is a count of runs.
    runs = [path for path in (root / "artifacts").iterdir() if path.is_dir()]
    assert len(runs) == 1 and (runs[0] / "owner.json").exists()
    distributed.result.stderr.fnmatch_lines(["*PYTEST_RUN_ID*escaped-upward*"])


@needs_xdist
def test_a_build_id_shaped_run_name_still_names_the_directory(distributed, monkeypatch):
    """The documented use case, which the check above must not have cost.

    Naming the directory after the build is why anybody sets this variable, and
    a validation that rejected the shapes CI actually produces would be worse
    than none: the evidence would be filed under a minted name and the
    correlation the operator asked for would be gone for every run.
    """
    distributed.pytester.makepyfile(test_suite=SUITE)
    root = _sandboxed(distributed)
    monkeypatch.setenv("PYTEST_RUN_ID", "build-4821.7_ubuntu-22.04-py3.11")

    distributed.run(
        "-n", "2", "-o", f"failure_directory={root / 'artifacts'}", "test_suite.py",
        timeout=180,
    )

    evidence = root / "artifacts" / "build-4821.7_ubuntu-22.04-py3.11"
    assert (evidence / "owner.json").exists()
    assert list(evidence.glob("*.state"))
    assert "PYTEST_RUN_ID" not in distributed.result.stderr.str()


def test_a_directory_that_is_not_ours_is_never_pruned(tmp_path):
    """The marker is what makes a directory ours to delete. Without it, this is
    somebody's build output that happens to live beside our own."""
    import json

    from pytest_failure_instrumentation.incidents.engine import prune_finished_runs

    stranger = tmp_path / "coverage-html"
    stranger.mkdir()
    (stranger / "index.html").write_text("<html>", encoding="utf-8")
    # Even one that looks like ours but says nothing about who owns it.
    unmarked = tmp_path / "run-abc123"
    unmarked.mkdir()
    (unmarked / "owner.json").write_text(json.dumps({"note": "not a pid"}), encoding="utf-8")

    prune_finished_runs(tmp_path)

    assert (stranger / "index.html").exists()
    assert unmarked.exists()


def test_pruning_a_directory_that_does_not_exist_is_not_an_error(tmp_path):
    from pytest_failure_instrumentation.incidents.engine import prune_finished_runs

    prune_finished_runs(tmp_path / "never-created")


# -- and out of the repository it is standing in --------------------------


def test_the_evidence_directory_ignores_itself_in_git(runner):
    """A plugin the developer did not ask to think about must not turn up in
    the ``git status`` they read to see what they changed.

    Everything under the directory is one run's scratch that a later run
    deletes, so the directory carries the ignore file itself rather than the
    repository's own ``.gitignore`` carrying a line about us - which works the
    same in the next checkout and on the colleague's machine.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.run("test_suite.py")

    ignore = runner.pytester.path / ".pytest-failures" / ".gitignore"
    assert "*" in ignore.read_text(encoding="utf-8").splitlines()


def test_a_directory_that_already_holds_our_runs_still_gets_one(runner):
    """The upgrade path. A directory made by an older version is as much ours
    as one made a moment ago, and it is the one already in somebody's
    checkout."""
    runner.pytester.makepyfile(test_suite=SUITE)
    import json
    import os

    earlier = runner.pytester.path / ".pytest-failures" / "run-earlier"
    earlier.mkdir(parents=True)
    (earlier / "owner.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    runner.run("test_suite.py")

    assert (runner.pytester.path / ".pytest-failures" / ".gitignore").exists()


def test_a_gitignore_that_is_already_there_is_never_rewritten(runner):
    """It may be the developer's, and then it says what they meant."""
    runner.pytester.makepyfile(test_suite=SUITE)
    evidence = runner.pytester.path / ".pytest-failures"
    evidence.mkdir()
    (evidence / ".gitignore").write_text("*\n!keep-me.json\n", encoding="utf-8")

    runner.run("test_suite.py")

    assert (evidence / ".gitignore").read_text(encoding="utf-8") == "*\n!keep-me.json\n"


def test_a_directory_shared_with_somebody_else_gets_no_ignore_file(runner):
    """``failure_directory`` is a natural thing to point at an existing
    artifacts directory, and ``*`` dropped into one would quietly stop git
    from seeing files that were never ours to hide."""
    runner.pytester.makepyfile(test_suite=SUITE)
    artifacts = runner.pytester.path / "artifacts"
    artifacts.mkdir()
    (artifacts / "coverage.xml").write_text("<coverage/>", encoding="utf-8")

    runner.run("-o", "failure_directory=artifacts", "test_suite.py")

    assert not (artifacts / ".gitignore").exists()
    assert (artifacts / "coverage.xml").exists()
    assert [path for path in artifacts.iterdir() if path.is_dir()], (
        "the run still wrote its evidence there"
    )


@needs_xdist
def test_two_runs_sharing_a_directory_keep_their_evidence_apart(runner):
    """The bug this layout exists to remove.

    Every worker is ``gw0``, so two runs sharing a flat directory shared their
    state files too: one run read the other's evidence and believed it. Here
    the second run is given the first's directory with the first's evidence
    already in it, and has to leave it alone - because its owner is a process
    that is still running.
    """
    import json
    import os

    runner.pytester.makepyfile(test_suite=SUITE)
    evidence = runner.pytester.path / "artifacts"
    other = evidence / "run-somebody-else"
    other.mkdir(parents=True)
    (other / "owner.json").write_text(
        json.dumps({"pid": os.getpid(), "run_id": "run-somebody-else"}), encoding="utf-8"
    )
    (other / "gw0.state").write_text('{"nodeid": "someone_elses_test"}', encoding="utf-8")

    runner.run("-n", "2", "-o", "failure_directory=artifacts", "test_suite.py", timeout=180)

    # Untouched, because its owner is alive.
    assert json.loads((other / "gw0.state").read_text())["nodeid"] == "someone_elses_test"
    # And this run wrote its own gw0 somewhere else entirely.
    ours = [path for path in evidence.iterdir() if path.is_dir() and path != other]
    assert len(ours) == 1
    assert (ours[0] / "gw0.state").exists()


def test_the_worker_import_path_never_loads_pydantic():
    """A promise in the package docstring, and one nothing else checks.

    The incident models are pydantic and are built on the controller, once
    something has already gone wrong. The modules a *worker* imports on its
    per-test path must not drag pydantic in behind them, or every worker in
    every run pays import cost for models it will never build.

    It is one line that breaks this - hoisting a lazy import to module scope -
    and the breakage is invisible, because everything still works. The live
    view added a second pydantic module reachable from a worker-imported one,
    which is what makes this worth pinning rather than trusting.
    """
    source = (
        "import sys\n"
        "import pytest_failure_instrumentation\n"
        "import pytest_failure_instrumentation.plugin\n"
        "import pytest_failure_instrumentation.config\n"
        "import pytest_failure_instrumentation.registration\n"
        "import pytest_failure_instrumentation.hookspec\n"
        "import pytest_failure_instrumentation.stack_server\n"
        "import pytest_failure_instrumentation.capture.crash_stack\n"
        "import pytest_failure_instrumentation.capture.heartbeat\n"
        "import pytest_failure_instrumentation.capture.recorder\n"
        "import pytest_failure_instrumentation.probes\n"
        "assert 'pydantic' not in sys.modules, 'pydantic reached the worker path'\n"
    )
    # A subprocess because this test session has pydantic loaded already.
    finished = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, timeout=120
    )
    assert finished.returncode == 0, finished.stderr


#: Two frameworks in one run, disagreeing about the token. The values are
#: distinctive strings rather than anything realistic, so that finding either
#: of them anywhere in the inner run's output is unambiguous.
INSTALLS_TWICE_WITH_TWO_TOKENS = """
from pytest_failure_instrumentation import install


def pytest_configure(config):
    install(config, stack_server_token="tok-in-force-9d41")
    install(config, stack_server_token="tok-offered-5b27")
"""


def test_the_token_is_never_printed_by_the_advice_about_two_installs(runner):
    """The one place this package could have written the secret down.

    ``install`` called twice warns that the second call lost, and lists what
    the two disagreed about by formatting every differing field with its
    value. ``stack_server_token`` is a field like any other, so two frameworks
    that disagreed about the token put both of them into a warning - which
    pytest reproduces in the warnings summary and CI keeps in the job log for
    the length of its retention.

    Everything else about the token is careful about this: it is supplied
    rather than minted so that nothing has to publish it, and the address file
    was demoted to ordinary data on the strength of that. A warning about a
    misconfiguration is not the place to give it back.

    The name still has to appear. "Something differs" that will not say what
    leaves the reader with two installs, one warning and nothing to look at.
    """
    runner.pytester.makepyfile(test_suite=SUITE, fw_plugin=INSTALLS_TWICE_WITH_TWO_TOKENS)

    result = runner.pytester.runpytest_subprocess(
        "-p", "no:xdist", "-p", "fw_plugin", "test_suite.py"
    )

    result.assert_outcomes(passed=2)
    said = result.stdout.str() + result.stderr.str()
    assert "already installed" in said
    assert "stack_server_token" in said, said
    for secret in ("tok-in-force-9d41", "tok-offered-5b27"):
        assert secret not in said, f"{secret} reached the run's output"
