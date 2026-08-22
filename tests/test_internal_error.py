"""pytest raised inside its own machinery, and nothing else will report it.

pytest excludes ExitCode.INTERNAL_ERROR from the exit codes that get a terminal
summary, so pytest_terminal_summary never fires for one of these.
"""

from __future__ import annotations

from .conftest import needs_xdist

BOOM_PLUGIN = """
import os

import victim


def pytest_collection_modifyitems(session, config, items):
    target = os.environ.get("BOOM_ON", "")
    if target and os.environ.get("PYTEST_XDIST_WORKER", "main") == target:
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
