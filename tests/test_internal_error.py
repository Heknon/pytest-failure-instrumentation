"""pytest raised inside its own machinery, and nothing else will report it.

pytest excludes ExitCode.INTERNAL_ERROR from the exit codes that get a terminal
summary, so pytest_terminal_summary never fires for one of these.
"""

from __future__ import annotations

import json

from .conftest import ENABLE_FLAG, INCIDENT_FILE, _has_xdist, needs_xdist

BOOM_PLUGIN = """
import os

import victim


def pytest_collection_modifyitems(session, config, items):
    target = os.environ.get("BOOM_ON", "")
    # Which process this is, asked the way the plugin itself asks it
    # (registration.py): workerinput exists on a worker and nowhere else. The
    # environment answers for whoever exported PYTEST_XDIST_WORKER last, which
    # when this suite is itself run under -n is the outer worker, not this run.
    worker = getattr(config, "workerinput", None)
    if target and (worker["workerid"] if worker else "main") == target:
        victim.break_pytest()
"""

SUITE = """
def test_one():
    assert True
"""


def prepare(runner, target, monkeypatch):
    monkeypatch.setenv("BOOM_ON", target)
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makepyfile(boom_plugin=BOOM_PLUGIN)


def test_a_single_process_run_reports_it_first_hand(runner, monkeypatch):
    prepare(runner, "main", monkeypatch)
    incidents = runner.run("-p", "no:xdist", "-p", "boom_plugin", "test_suite.py")

    failure = runner.only(incidents, "internal_error")
    assert failure.verdict == "INTERNAL_ERROR"
    assert failure.owner == "product"
    assert failure.severity == "critical"
    assert failure.run_ending is True
    assert failure.first_hand is True
    assert "RuntimeError" in failure.exception
    assert failure.blamed_frame is not None
    assert failure.blamed_frame.function == "break_pytest"


@needs_xdist
def test_a_workers_error_is_captured_where_it_was_raised(runner, monkeypatch):
    """xdist relays a worker's internal error as a flat string and re-raises it
    on the controller, so the INTERNALERROR block names xdist's frame."""
    prepare(runner, "gw1", monkeypatch)
    incidents = runner.run("-n", "2", "-p", "boom_plugin", "test_suite.py", timeout=180)

    failure = runner.only(incidents, "internal_error")
    assert failure.confidence == "high"
    assert failure.worker == "gw1"
    assert any("on the worker itself" in line for line in failure.evidence)
    # Attributed to whoever wrote the failing hook, not to xdist's re-raise.
    assert failure.owner == "product"


def test_a_previous_runs_evidence_is_not_reported_as_this_ones(runner, monkeypatch):
    """The evidence directory outlives a run.

    A distributed run leaves its worker events behind; a single-process run
    does not clean them, because it writes nothing there itself. Preferring a
    captured worker record without checking which run wrote it reported the
    *old* failure - under a worker id that does not exist in this run, at high
    confidence, with the controller's real error discarded entirely.
    """
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makepyfile(boom_plugin=BOOM_PLUGIN)

    # Both runs are given the same session id, which is what puts them in one
    # directory: runs have their own directory now, so sharing one is no longer
    # something that just happens - it is what naming them the same does. The
    # run id stamped on each line is then the only thing separating the two.
    monkeypatch.setenv("PYTEST_RUN_ID", "a-shared-directory")
    evidence = runner.pytester.path / ".pytest-failures" / "a-shared-directory"

    if _has_xdist():
        # Run one: a worker fails, and its record stays on disk.
        monkeypatch.setenv("BOOM_ON", "gw1")
        runner.pytester.runpytest_subprocess(
            ENABLE_FLAG, "-n", "2", "-p", "boom_plugin", "test_suite.py", timeout=180
        )
        assert list(evidence.glob("*.events"))
    else:
        # No xdist: forge the leftovers a distributed run would have left.
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "gw1.events").write_text(
            json.dumps(
                {
                    "event": "internal_error",
                    "run_id": "some-earlier-run",
                    "detail": "RuntimeError: an older failure entirely",
                    "nodeid": "test_gone.py::test_gone",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    # Run two: a different failure, in a process that has no workers at all.
    (runner.pytester.path / INCIDENT_FILE).unlink(missing_ok=True)
    monkeypatch.setenv("BOOM_ON", "main")
    incidents = runner.run("-p", "no:xdist", "-p", "boom_plugin", "test_suite.py")

    failure = runner.only(incidents, "internal_error")
    assert failure.worker == "controller"
    assert any("Raised on the controller" in line for line in failure.evidence)
    assert "victim broke pytest" in failure.exception
