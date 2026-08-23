"""Settings, and what happens to the ones that cannot be read.

A setting nobody can see failing is worse than one that is rejected: the
reader believes they configured a stall detector and never hears from it.
"""

from __future__ import annotations

import warnings

import pytest

from pytest_failure_instrumentation import config as settings_module
from pytest_failure_instrumentation.config import (
    MIN_HEARTBEAT_INTERVAL,
    FailureInstrumentationWarning,
    resolve,
)


class FakeConfig:
    """Just enough of pytest.Config to resolve settings against."""

    def __init__(self, **values: object) -> None:
        self.values = values

    def getini(self, name: str) -> object:
        return self.values.get(name, DEFAULTS[name])


DEFAULTS = {
    "failure_directory": ".pytest-failures",
    "failure_packages": [],
    "failure_product_version": "",
    "failure_watchdog": "true",
    "failure_heartbeat_interval": "5.0",
    "failure_tracemalloc_depth": "0",
    "failure_object_census": "false",
    "failure_high_water_mb": "0",
    "failure_memory_limit_mb": "0",
    "failure_slow_test_seconds": "120",
    "failure_stall_seconds": "300",
    "failure_stack_probe": "true",
}


def test_a_setting_that_is_not_a_number_says_so_rather_than_vanishing():
    with pytest.warns(FailureInstrumentationWarning, match="failure_stall_seconds"):
        resolved = resolve(FakeConfig(failure_stall_seconds="5m"))
    assert resolved.stall_seconds == 300.0


def test_a_readable_setting_warns_about_nothing():
    with warnings.catch_warnings():
        warnings.simplefilter("error", FailureInstrumentationWarning)
        assert resolve(FakeConfig(failure_stall_seconds="45")).stall_seconds == 45.0


def test_the_heartbeat_interval_is_clamped_where_both_sides_can_see_it():
    """The worker used to clamp this and the controller did not.

    Two answers to "how often does a worker beat" put the controller's
    staleness window inside the worker's own cadence, and a blocked worker
    confirmed as frozen - reported as native code holding the GIL.
    """
    resolved = resolve(FakeConfig(failure_heartbeat_interval="0.05"))
    assert resolved.heartbeat_interval == MIN_HEARTBEAT_INTERVAL

    from pytest_failure_instrumentation.capture.heartbeat import Heartbeat

    beating = Heartbeat(lambda *a, **k: None, interval=resolved.heartbeat_interval)
    assert beating.interval == resolved.heartbeat_interval


def test_a_worker_takes_the_run_id_the_controller_pushed_down():
    class Worker(FakeConfig):
        workerinput = {"workerid": "gw3", "workercount": 4, "failure_run_id": "run-abc"}

    assert resolve(Worker()).run_id == "run-abc"
    # A plain run has no controller to be told by, and says so with None
    # rather than inventing one that would match nothing.
    assert resolve(FakeConfig()).run_id is None


def test_every_setting_in_the_readme_table_is_registered():
    """The table is how anybody finds these, so a setting that drifts out of it
    is a setting nobody can turn on."""
    registered: list[str] = []

    class Parser:
        def addini(self, name, help, type=None, default=None):  # noqa: A002
            registered.append(name)

    settings_module.add_options(Parser())
    assert sorted(registered) == sorted(DEFAULTS)
