"""Installing the plugin by hand, the way a framework has to.

A framework that wraps pytest computes its settings in Python - from a service
catalogue, a deploy manifest, an environment it already parsed - and cannot
express them as an ini block every consuming team has to copy and keep in step.
So it calls ``install`` instead.

Three things have to hold for that to be usable, and each of them is a way it
could quietly not work: the framework's settings have to win wherever the
framework is loaded from, they have to reach the workers, and turning the entry
point off has to leave a plugin that still functions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pytest_failure_instrumentation import Settings, installed_settings

from .conftest import INCIDENT_FILE, needs_xdist

REPORTING_CONFTEST = f"""
import json


def pytest_failure_incident(incident):
    with open({INCIDENT_FILE!r}, "a") as handle:
        handle.write(incident.model_dump_json() + "\\n")
"""

#: An ini nobody should end up obeying once a framework has spoken.
IGNORED_INI = """
[pytest]
failure_packages = ini_package
failure_product_version = 0.0.0-ini
failure_stall_seconds = 999
"""

SUITE = """
def test_one():
    assert True
"""


def framework(body: str) -> str:
    """A framework plugin that installs and then records what took effect."""
    return f"""
import json
from pathlib import Path

from pytest_failure_instrumentation import Settings, install, installed_settings


def pytest_configure(config):
{body}


def pytest_sessionstart(session):
    settings = installed_settings(session.config)
    where = "worker" if hasattr(session.config, "workerinput") else "controller"
    with open("installed.jsonl", "a") as handle:
        handle.write(json.dumps({{
            "where": where,
            "packages": list(settings.packages),
            "directory": str(settings.directory),
            "product_version": settings.product_version,
            "stall_seconds": settings.stall_seconds,
            "worker_count": settings.worker_count,
            "run_id": settings.run_id,
        }}) + "\\n")
"""


def installs(pytester) -> list[dict]:
    path = pytester.path / "installed.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def framework_run(runner):
    """A run whose ini says one thing and whose framework says another."""
    runner.pytester.makeini(IGNORED_INI)
    runner.pytester.makeconftest(REPORTING_CONFTEST)
    runner.pytester.makepyfile(test_suite=SUITE)
    return runner


# -- the settings a framework asked for are the settings in force ---------


def test_a_framework_loaded_as_a_plugin_wins_over_ini(framework_run):
    """The ordering case that decides whether this works at all.

    A conftest's pytest_configure runs *before* this plugin's and a plugin
    loaded by name runs *after* it, so registering eagerly would honour a
    framework shipped one way and silently ignore one shipped the other. The
    entry point registers trylast for exactly this reason.
    """
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            '    install(config, packages=("fwcore",), product_version="9.9.9")'
        )
    )
    framework_run.run("-p", "no:xdist", "-p", "fw_plugin", "test_suite.py")

    (took_effect,) = installs(framework_run.pytester)
    assert took_effect["packages"] == ["fwcore"]
    assert took_effect["product_version"] == "9.9.9"


def test_a_framework_in_a_conftest_wins_too(framework_run):
    framework_run.pytester.makeconftest(
        REPORTING_CONFTEST
        + framework('    install(config, packages=("fwcore",), product_version="9.9.9")')
    )
    framework_run.run("-p", "no:xdist", "test_suite.py")

    (took_effect,) = installs(framework_run.pytester)
    assert took_effect["packages"] == ["fwcore"]


def test_keyword_overrides_layer_on_top_of_ini(framework_run):
    """Setting one field must not silently discard the rest of somebody's ini."""
    framework_run.pytester.makepyfile(
        fw_plugin=framework('    install(config, packages=("fwcore",))')
    )
    framework_run.run("-p", "no:xdist", "-p", "fw_plugin", "test_suite.py")

    (took_effect,) = installs(framework_run.pytester)
    assert took_effect["packages"] == ["fwcore"]
    # Untouched, and still the ini's.
    assert took_effect["product_version"] == "0.0.0-ini"
    assert took_effect["stall_seconds"] == 999


def test_a_whole_settings_replaces_ini_rather_than_layering(framework_run):
    """The other thing a framework wants, and not the same thing: when it owns
    the configuration, a stray ini key must not survive into the run."""
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            "    install(config, Settings(packages=('fwcore',), product_version='9.9.9'))"
        )
    )
    framework_run.run("-p", "no:xdist", "-p", "fw_plugin", "test_suite.py")

    (took_effect,) = installs(framework_run.pytester)
    assert took_effect["packages"] == ["fwcore"]
    assert took_effect["product_version"] == "9.9.9"
    assert took_effect["stall_seconds"] == Settings().stall_seconds  # not 999


@needs_xdist
def test_the_settings_actually_change_the_attribution(framework_run):
    """The end of the point: whose code the incident blames follows from the
    packages the framework named, not from anything in the ini."""
    framework_run.pytester.makepyfile(
        fwcore="""
        import ctypes
        import os
        import sys


        def boom():
            # ctypes wraps every foreign call in structured exception handling
            # on Windows, so an access violation comes back as an OSError and
            # the worker lives. abort() is a real death there - the same split
            # the shared victim module makes, for the same reason.
            if sys.platform == "win32":
                os.abort()
            ctypes.string_at(1)
        """
    )
    framework_run.pytester.makepyfile(
        test_crash="""
        import fwcore


        def test_filler():
            assert True


        def test_crashes():
            fwcore.boom()
        """
    )
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            '    install(config, packages=("fwcore",), product_version="9.9.9")'
        )
    )
    incidents = framework_run.run(
        "-n", "2", "-p", "fw_plugin", "test_crash.py", timeout=180
    )

    death = framework_run.only(incidents, "worker_death")
    assert death.owner == "product"
    assert death.product_version == "9.9.9"


# -- crossing to the workers ----------------------------------------------


@needs_xdist
def test_settings_computed_on_the_controller_reach_every_worker(framework_run):
    """A worker is a different process.

    Ini it can read for itself; a framework's settings do not exist there at
    all, and this framework deliberately does nothing on the worker - so if
    they arrive, they arrived from the controller.
    """
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            "    if hasattr(config, 'workerinput'):\n"
            "        return\n"
            '    install(config, packages=("fwcore",), product_version="9.9.9",\n'
            '            directory=Path("framework-evidence"))'
        )
    )
    framework_run.run("-n", "2", "-p", "fw_plugin", "test_suite.py", timeout=180)

    recorded = installs(framework_run.pytester)
    workers = [row for row in recorded if row["where"] == "worker"]
    assert len(workers) == 2
    for worker in workers:
        assert worker["packages"] == ["fwcore"]
        assert worker["product_version"] == "9.9.9"
        # The *resolved* directory: the framework named the parent, and the
        # controller picked this run's own subdirectory under it. A worker
        # cannot work that out for itself, so arriving at all proves it came
        # from the controller.
        assert Path(worker["directory"]).parent.name == "framework-evidence"
    # And every worker was told the same one, or they would be writing their
    # evidence into different runs.
    assert len({worker["directory"] for worker in workers}) == 1

    # The evidence went where the framework said, not to the default.
    assert (framework_run.pytester.path / "framework-evidence").is_dir()
    assert not (framework_run.pytester.path / ".pytest-failures").exists()


@needs_xdist
def test_the_per_worker_fields_are_not_copied_from_the_controller(framework_run):
    """worker_count is xdist's answer about *this* process. Sending the
    controller's 1 to every worker would make each of them size its memory
    share as though it had the machine to itself."""
    framework_run.pytester.makepyfile(
        fw_plugin=framework('    install(config, packages=("fwcore",))')
    )
    framework_run.run("-n", "2", "-p", "fw_plugin", "test_suite.py", timeout=180)

    recorded = installs(framework_run.pytester)
    assert [row["worker_count"] for row in recorded if row["where"] == "worker"] == [2, 2]
    assert [row["worker_count"] for row in recorded if row["where"] == "controller"] == [1]


@needs_xdist
def test_a_framework_can_supply_the_run_id_that_groups_a_run(framework_run):
    """Correlating incidents with a build id is a reason to install by hand,
    and the id has to be the same on both sides to be worth anything."""
    framework_run.pytester.makepyfile(
        fw_plugin=framework('    install(config, run_id="ci-build-4821")')
    )
    incidents = framework_run.run(
        "-n", "2", "-p", "fw_plugin", "test_suite.py", timeout=180
    )

    assert {row["run_id"] for row in installs(framework_run.pytester)} == {"ci-build-4821"}
    assert {incident.run_id for incident in incidents} == {"ci-build-4821"}


# -- turning the entry point off ------------------------------------------


@needs_xdist
def test_the_plugin_works_with_auto_registration_turned_off(framework_run):
    """A framework that owns the configuration outright disables the entry
    point, which also skips the hookspec and the ini options. Installing by
    hand has to put back everything that skipped - including the spec, without
    which a consumer conftest implementing the hook fails the session at
    check_pending with "unknown hook" before a test has run.
    """
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            "    install(config, Settings(packages=('fwcore',),\n"
            "                             product_version='9.9.9',\n"
            "                             directory=Path('framework-evidence')))"
        )
    )
    incidents = framework_run.run(
        "-p", "no:failure_instrumentation",
        "-p", "fw_plugin",
        "-n", "2",
        "test_suite.py",
        timeout=180,
    )

    # The run has to have worked, not merely produced a payload. Without the
    # spec, pluggy fails the session at check_pending and the plugin still
    # reports a summary on the way down - so asserting only on the incident
    # would pass against a run that never collected a test.
    framework_run.result.assert_outcomes(passed=1)
    assert framework_run.of_kind(incidents, "internal_error") == []

    # The hook still reaches the consumer's conftest.
    summary = framework_run.only(incidents, "run_summary")
    assert summary.product_version == "9.9.9"
    assert {row["packages"][0] for row in installs(framework_run.pytester)} == {"fwcore"}


def test_turning_the_entry_point_off_needs_no_ini_at_all(runner):
    """With the options unregistered, ini keys are unknown to pytest. A
    framework that owns the settings should not need any, and the defaults
    have to stand on their own."""
    runner.pytester.makeini("[pytest]\n")
    runner.pytester.makeconftest(REPORTING_CONFTEST)
    runner.pytester.makepyfile(test_suite=SUITE)
    runner.pytester.makepyfile(
        fw_plugin=framework("    install(config, Settings(packages=('fwcore',)))")
    )
    incidents = runner.run(
        "-p", "no:failure_instrumentation", "-p", "fw_plugin", "-p", "no:xdist",
        "test_suite.py",
    )

    runner.result.assert_outcomes(passed=1)
    assert runner.of_kind(incidents, "internal_error") == []
    assert runner.only(incidents, "run_summary").verdict == "RUN_FINISHED"
    (took_effect,) = installs(runner.pytester)
    assert took_effect["stall_seconds"] == Settings().stall_seconds


# -- being called more than once ------------------------------------------


def test_a_second_install_keeps_the_first_and_says_so(framework_run):
    """Two frameworks in one run is a mess, but a silent one is worse: the
    second one's settings simply would not be there and nothing would say why.
    """
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            "    install(config, packages=('first',))\n"
            "    install(config, packages=('second',))"
        )
    )
    result = framework_run.pytester.runpytest_subprocess(
        "-p", "no:xdist", "-p", "fw_plugin", "test_suite.py"
    )

    (took_effect,) = installs(framework_run.pytester)
    assert took_effect["packages"] == ["first"]
    result.stderr.fnmatch_lines(["*already installed*second*"])


def test_installing_twice_with_the_same_settings_says_nothing(framework_run):
    framework_run.pytester.makepyfile(
        fw_plugin=framework(
            "    install(config, packages=('same',))\n"
            "    install(config, packages=('same',))"
        )
    )
    result = framework_run.pytester.runpytest_subprocess(
        "-p", "no:xdist", "-p", "fw_plugin", "test_suite.py"
    )
    assert "already installed" not in result.stderr.str()


# -- the API on its own ----------------------------------------------------


def test_a_misspelled_setting_is_an_error_rather_than_a_shrug():
    """Accepting `pacakges=` and ignoring it is a framework shipping
    unattributed incidents to every one of its customers, with nothing
    anywhere saying why."""
    with pytest.raises(TypeError, match="pacakges"):
        Settings().with_overrides(pacakges=("yourcore",))


def test_settings_is_constructible_with_nothing_and_coerces_what_it_is_given():
    assert Settings().directory.name == ".pytest-failures"
    handed_a_list = Settings(packages=["a", "b"], directory="/tmp/evidence")
    assert handed_a_list.packages == ("a", "b")
    assert handed_a_list.directory.as_posix() == "/tmp/evidence"


def test_a_hand_built_settings_cannot_skip_the_heartbeat_floor():
    """The floor exists because the controller judges staleness from the same
    number. Enforcing it only where ini is read would leave the framework path
    able to reintroduce the bug it was added for."""
    assert Settings(heartbeat_interval=0.05).heartbeat_interval == 1.0


def test_the_payload_carries_the_shared_settings_and_not_the_per_process_ones():
    settings = Settings(packages=("a",), stall_seconds=42, worker_count=9, run_id="r")
    payload = settings.as_payload()
    assert "worker_count" not in payload and "run_id" not in payload
    arrived = Settings.from_payload(payload, worker_count=4, run_id="pushed")
    assert arrived.packages == ("a",) and arrived.stall_seconds == 42
    assert arrived.worker_count == 4 and arrived.run_id == "pushed"


def test_nothing_is_installed_until_something_installs_it():
    class Bare:
        hook = None

    assert installed_settings(Bare()) is None
