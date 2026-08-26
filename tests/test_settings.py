"""Settings, and what happens to the ones that cannot be read.

A setting nobody can see failing is worse than one that is rejected: the
reader believes they configured a stall detector and never hears from it.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

from pytest_failure_instrumentation import config as settings_module
from pytest_failure_instrumentation.config import (
    MIN_HEARTBEAT_INTERVAL,
    FailureInstrumentationWarning,
    resolve,
)


class FakeConfig:
    """Just enough of pytest.Config to resolve settings against."""

    def __init__(self, options: dict | None = None, **values: object) -> None:
        self.values = values
        self.options = options or {}

    def getini(self, name: str) -> object:
        return self.values.get(name, DEFAULTS[name])

    def getoption(self, name: str) -> object:
        return self.options.get(name)


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
    "failure_slow_test_seconds": "20",
    "failure_stall_seconds": "300",
    "failure_stack_probe": "true",
    "failure_stack_server": "false",
    "failure_stack_server_port": "0",
    "failure_stack_server_host": "127.0.0.1",
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
        """Both halves of what add_options registers. The command-line options
        go to a group, and a fake that cannot hand one out would make this test
        pass by never reaching them."""

        def addini(self, name, help, type=None, default=None):  # noqa: A002
            registered.append(name)

        def getgroup(self, name):
            return self

        def addoption(self, name, **kwargs):
            command_line.append(name)

    command_line: list[str] = []
    settings_module.add_options(Parser())
    assert sorted(command_line) == ["--callstack-host", "--callstack-port"]
    assert sorted(registered) == sorted(DEFAULTS)


def test_the_shipped_defaults_leave_a_stall_something_to_read():
    """These two settings look independent and are not.

    The stack a stalled worker is reported with is whatever the watchdog last
    wrote, so the cadence has to have fired before the stall is assessed. At
    the old 120s cadence a worker wedged for a minute left nothing at all -
    and on Windows, where nothing can ask a live process for a stack, that was
    every stall shorter than two minutes.
    """
    defaults = resolve(FakeConfig())
    assert 0 < defaults.slow_test_seconds < defaults.stall_seconds


def test_an_inverted_pair_says_what_it_will_cost():
    with pytest.warns(FailureInstrumentationWarning, match="before its watchdog"):
        resolve(FakeConfig(failure_slow_test_seconds="60", failure_stall_seconds="10"))


def test_switching_either_one_off_is_not_an_inversion():
    with warnings.catch_warnings():
        warnings.simplefilter("error", FailureInstrumentationWarning)
        resolve(FakeConfig(failure_stall_seconds="0"))       # no stall detection
        resolve(FakeConfig(failure_slow_test_seconds="0"))   # no watchdog
        resolve(FakeConfig(failure_watchdog="false"))


def test_the_two_places_the_version_is_written_agree():
    """The release workflow reads pyproject and refuses a tag that disagrees
    with it. It never reads ``__version__``.

    So that one can drift unnoticed: a release publishing 0.2.0 while the
    installed package reports 0.1.0 to anyone who asks it. Nothing else
    catches that, because the check that exists is between the tag and
    pyproject, not between pyproject and here.

    Read with a regex rather than tomllib, which is 3.11+, and against the
    source rather than the installed metadata, which is written at install
    time and goes stale the moment pyproject is edited.

    pyproject is found from this file rather than from the package, because
    the package is not always in the tree that declares it: the release
    workflow runs this same suite against the built wheel, where it sits in
    site-packages with no pyproject.toml anywhere above it. The tests only
    ever ship with the source, so their own location is the one that finds
    it - and against an installed wheel the comparison gets stronger, since
    the version being read out is the one that was packaged.
    """
    import pytest_failure_instrumentation

    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert declared, "no version in pyproject.toml"
    assert pytest_failure_instrumentation.__version__ == declared.group(1)


# -- where this run happens, as against what this project wants -----------


def test_no_port_asked_for_means_one_is_drawn():
    """The default is the lottery, because a session nobody has told which port
    to use has no reason to fight the other sessions for one."""
    assert resolve(FakeConfig()).stack_server_port == 0
    assert resolve(FakeConfig()).stack_server_host == "127.0.0.1"


def test_the_command_line_outranks_ini_for_both_of_them():
    """These two answer "where does this run happen", which the person starting
    it knows and the repository does not."""
    with pytest.warns(FailureInstrumentationWarning):  # 0.0.0.0 is an exposure
        resolved = resolve(
            FakeConfig(
                {"callstack_port": 9111, "callstack_host": "0.0.0.0"},
                failure_stack_server_port="8080",
                failure_stack_server_host="127.0.0.1",
                failure_stack_server="true",
            )
        )
    assert resolved.stack_server_port == 9111
    assert resolved.stack_server_host == "0.0.0.0"


def test_naming_either_one_on_the_command_line_switches_the_server_on():
    """An option that is accepted, parsed and then ignored because a separate
    ini flag was left at its default is the worst available behaviour."""
    assert resolve(FakeConfig({"callstack_port": 9111})).stack_server
    with pytest.warns(FailureInstrumentationWarning):  # 0.0.0.0 is an exposure
        assert resolve(FakeConfig({"callstack_host": "0.0.0.0"})).stack_server
    assert not resolve(FakeConfig()).stack_server


def test_binding_off_loopback_says_what_it_exposes():
    """Right for a container whose UI is outside it, wrong on a shared machine,
    and only the person who typed it can tell which this is."""
    with pytest.warns(FailureInstrumentationWarning, match="no authentication"):
        resolve(FakeConfig({"callstack_host": "0.0.0.0"}))


def test_loopback_by_any_of_its_names_is_not_an_exposure():
    with warnings.catch_warnings():
        warnings.simplefilter("error", FailureInstrumentationWarning)
        for name in ("127.0.0.1", "::1", "localhost"):
            resolve(FakeConfig({"callstack_host": name}))
