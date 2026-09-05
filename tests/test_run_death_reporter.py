"""A killed run, reported by the process that outlived it.

Every other incident is raised by a survivor. A run whose controller is
killed has none, and on a runner with a fresh workspace per job there is no
next run to recover it either. The sidecar is the survivor: it sees the run's
pipe close without a "stop", starts the reporter as the user, and the
callable the user configured is called with the same incident the next run
would have raised - before any next run.
"""

from __future__ import annotations

import functools
import json
import os
import pickle
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation import Settings
from pytest_failure_instrumentation.incidents import leftovers, reporter
from pytest_failure_instrumentation.incidents.registry import parse

from .conftest import INNER_CONFTEST, needs_xdist

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM")

#: What a reporter callable in this module records. Module-level, so a
#: partial of it pickles by name the way a user's would.
CALLS: list[tuple[str, str, str, str]] = []


def remember(tag: str, incident) -> None:
    CALLS.append((tag, incident.kind, incident.worker, incident.verdict))


def refuse(_incident) -> None:
    raise RuntimeError("the alerting service is down")


# -- the callable in transit ------------------------------------------------


def test_a_partial_of_a_module_function_travels_by_pickle():
    spec = reporter.describe_callable(functools.partial(remember, "tagged"))
    assert set(spec) == {"pickle"}
    back = reporter.resolve(spec)
    assert isinstance(back, functools.partial)
    assert back.func is remember and back.args == ("tagged",)


def test_a_dotted_path_travels_by_name():
    assert reporter.resolve({"path": "tests.test_run_death_reporter:remember"}) is remember
    assert reporter.resolve({"path": "tests.test_run_death_reporter.remember"}) is remember


def test_a_lambda_cannot_travel_and_the_failure_is_immediate():
    with pytest.raises((pickle.PicklingError, AttributeError)):
        reporter.describe_callable(lambda incident: None)


def test_something_that_is_not_callable_is_refused_on_arrival():
    with pytest.raises(TypeError):
        reporter.resolve({"path": "tests.test_run_death_reporter:CALLS"})


def test_install_takes_the_callable_and_never_sends_it_to_a_worker():
    target = functools.partial(remember, "x")
    settings = Settings(on_run_death=target)
    assert settings.on_run_death is target
    assert "on_run_death" not in settings.as_payload()
    assert Settings(on_run_death="pkg.mod:attr").with_overrides(on_run_death=target).on_run_death is target


# -- the report -------------------------------------------------------------


def dead_run(tmp_path: Path, *, finished: bool = False) -> Path:
    """A distributed run whose controller died of SIGTERM while its one
    worker finished cleanly on execnet's SIGINT - a cancelled job."""
    directory = tmp_path / "run-dead"
    directory.mkdir()
    marker = {"pid": 999999, "session_id": "run-dead", "started_at": 900.0}
    if finished:
        marker[leftovers.FINISHED_KEY] = 1000.0
    (directory / "owner.json").write_text(json.dumps(marker), encoding="utf-8")
    (directory / "controller.events").write_text(
        json.dumps({
            "event": "signal_received", "time": 990.0, "run_id": "xdist-abc", "signal": 15,
            "name": "SIGTERM", "si_code": 0, "origin": "process", "sender_pid": 812,
            "sender_uid": 998, "sender_comm": "gitlab-runner", "sender_cmdline": "gitlab-runner run",
        }) + "\n",
        encoding="utf-8",
    )
    (directory / "gw0.events").write_text(
        "\n".join(json.dumps(event) for event in (
            {"event": "worker_start", "time": 901.0, "pid": 999998, "run_id": "xdist-abc"},
            {"event": "heartbeat", "time": 989.0, "rss_mb": 120, "run_id": "xdist-abc"},
            {"event": "worker_finish", "time": 996.0, "exitstatus": 2, "run_id": "xdist-abc"},
        )) + "\n",
        encoding="utf-8",
    )
    return directory


def payload_for(directory: Path, target) -> dict:
    return {
        "callable": reporter.describe_callable(target),
        "directory": str(directory),
        "session": directory.name,
        "controller_pid": 999999,
        "packages": ["victim"],
        "product_version": "4.2.0",
        "elevate": False,
        "sys_path": [],
        "rootdir": str(directory.parent),
        "env": {},
        "python": sys.executable,
    }


def test_the_reporter_raises_the_controllers_death_once(tmp_path):
    directory = dead_run(tmp_path)
    CALLS.clear()

    reported = reporter.report(payload_for(directory, functools.partial(remember, "tag")))

    # The worker finished; the controller did not. One incident, and it is
    # everything the hook would have carried.
    assert CALLS == [("tag", "worker_death", "controller", f"SIGNAL_{int(signal.SIGTERM)}")]
    (controller,) = reported
    assert controller.recovered_from_run == "run-dead"
    assert controller.run_id == "xdist-abc", "the dead run's own id, not the directory's"
    assert controller.severity == "informational"
    assert controller.product_version == "4.2.0"
    assert controller.capabilities is not None and controller.fingerprint
    assert controller.killer is not None and controller.killer.sender_comm == "gitlab-runner"
    assert controller.raised_at > 0
    # Stamped, so the next run over this directory does not raise it again.
    assert leftovers.marker(directory).get(leftovers.REPORTED_KEY)
    assert leftovers.deaths_left_behind(tmp_path) == []
    # And a second reporter - a retried job over the same directory - says nothing.
    CALLS.clear()
    assert reporter.report(payload_for(directory, functools.partial(remember, "again"))) == []
    assert CALLS == []


def test_a_run_that_finished_is_not_reported_as_killed(tmp_path):
    directory = dead_run(tmp_path, finished=True)
    CALLS.clear()
    assert reporter.report(payload_for(directory, functools.partial(remember, "tag"))) == []
    assert CALLS == []
    assert leftovers.REPORTED_KEY not in leftovers.marker(directory)


def test_a_callable_that_raises_costs_the_report_nothing(tmp_path, capsys):
    directory = dead_run(tmp_path)
    reported = reporter.report(payload_for(directory, refuse))
    assert len(reported) == 1
    assert "the alerting service is down" in capsys.readouterr().err
    assert leftovers.marker(directory).get(leftovers.REPORTED_KEY)


def test_the_entry_point_reads_the_payload_and_never_raises(tmp_path):
    directory = dead_run(tmp_path)
    CALLS.clear()
    payload = payload_for(directory, functools.partial(remember, "main"))
    stream = tmp_path / "payload.json"
    stream.write_text(json.dumps(payload), encoding="utf-8")
    with stream.open(encoding="utf-8") as handle:
        assert reporter.main(handle) == 0
    assert [call[0] for call in CALLS] == ["main"]
    with (tmp_path / "garbage").open("w+", encoding="utf-8") as handle:
        handle.write("not json")
        handle.seek(0)
        assert reporter.main(handle) == 1


# -- for real ---------------------------------------------------------------

SLEEPER = """
import time


def test_filler():
    assert True


def test_sleeps():
    time.sleep(60)
"""

ALERTS_MODULE = '''
def write_alert(path, incident):
    """A user's reporter: a module-level function with a bound argument."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(incident.model_dump_json() + "\\n")
'''

CONFTEST_WITH_REPORTER = INNER_CONFTEST + '''

import functools

from alerts import write_alert
from pytest_failure_instrumentation import install


def pytest_configure(config):
    install(config, on_run_death=functools.partial(write_alert, "alert.jsonl"))
'''


def _worker_in(runner, nodeid: str, timeout: float = 60.0) -> int:
    from pytest_failure_instrumentation.capture.state import read_state

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for state in (runner.pytester.path / ".pytest-failures").glob("*/gw*.state"):
            record = read_state(state, None)
            if (record.get("nodeid") or "").endswith(nodeid) and record.get("pid"):
                return int(record["pid"])
        time.sleep(0.1)
    pytest.fail(f"no worker reached {nodeid} within {timeout}s")


@posix_only
@needs_xdist
def test_a_killed_run_is_reported_by_its_sidecar_before_any_next_run(runner):
    """The controller is sent SIGTERM by this process and dies of it. Nothing
    runs pytest again; the alert file appears anyway, written by the
    reporter the sidecar started, naming this process as the sender. The run
    that *does* come next finds the directory already reported."""
    runner.pytester.makepyfile(alerts=ALERTS_MODULE, test_sleep=SLEEPER, test_quick="def test_quick():\n    assert True\n")
    runner.pytester.makeconftest(CONFTEST_WITH_REPORTER)
    inner = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-n", "2", "test_sleep.py", "-p", "no:cacheprovider"],
        cwd=runner.pytester.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _worker_in(runner, "test_sleeps")
        time.sleep(1.0)
        os.kill(inner.pid, signal.SIGTERM)
        output, _ = inner.communicate(timeout=60)
    finally:
        if inner.poll() is None:
            inner.kill()
    assert inner.returncode == -signal.SIGTERM, output.decode("utf-8", "replace")

    alert = runner.pytester.path / "alert.jsonl"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not alert.exists():
        time.sleep(0.5)
    (log,) = list((runner.pytester.path / ".pytest-failures").glob("*/reporter.log"))
    assert alert.exists(), log.read_text(encoding="utf-8")
    reported = [parse(json.loads(line)) for line in alert.read_text(encoding="utf-8").splitlines()]
    assert [incident.worker for incident in reported] == ["controller"], [
        (incident.worker, incident.verdict) for incident in reported
    ]
    (controller,) = reported
    assert controller.verdict == f"SIGNAL_{int(signal.SIGTERM)}"
    assert controller.killer is not None and controller.killer.sender_pid == os.getpid()
    assert controller.recovered_from_run
    assert "reported 1 incident" in log.read_text(encoding="utf-8")

    # The next run has nothing to say about it.
    incidents = runner.run("-p", "no:xdist", "test_quick.py", timeout=120)
    assert not [incident for incident in incidents if getattr(incident, "recovered_from_run", None)]


@posix_only
@needs_xdist
def test_the_reporter_needs_no_witness_at_all(runner):
    """failure_kill_trace off: no block, no tracepoint, nothing privileged -
    and a killed run is still reported, by a sidecar that only watches. The
    incident then says honestly that no witness saw who did it."""
    runner.pytester.makepyfile(alerts=ALERTS_MODULE, test_sleep=SLEEPER)
    runner.pytester.makeconftest(CONFTEST_WITH_REPORTER)
    inner = subprocess.Popen(
        [
            sys.executable, "-m", "pytest", "-n", "2", "-o", "failure_kill_trace=false",
            "test_sleep.py", "-p", "no:cacheprovider",
        ],
        cwd=runner.pytester.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _worker_in(runner, "test_sleeps")
        time.sleep(1.0)
        os.kill(inner.pid, signal.SIGTERM)
        output, _ = inner.communicate(timeout=60)
    finally:
        if inner.poll() is None:
            inner.kill()
    assert inner.returncode == -signal.SIGTERM, output.decode("utf-8", "replace")
    assert not list((runner.pytester.path / ".pytest-failures").glob("*/controller.events")), (
        "no witness was on, so the controller kept no log"
    )

    alert = runner.pytester.path / "alert.jsonl"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not alert.exists():
        time.sleep(0.5)
    (log,) = list((runner.pytester.path / ".pytest-failures").glob("*/reporter.log"))
    assert alert.exists(), log.read_text(encoding="utf-8")
    (controller,) = [parse(json.loads(line)) for line in alert.read_text(encoding="utf-8").splitlines()]
    assert controller.worker == "controller"
    assert controller.verdict == "UNKNOWN"
    assert controller.killer is None
    assert any(line.startswith("Kill witnesses:") for line in controller.evidence)


@posix_only
@needs_xdist
def test_a_run_that_finishes_is_never_reported_as_killed(runner):
    runner.pytester.makepyfile(alerts=ALERTS_MODULE, test_suite="def test_one():\n    assert True\n")
    runner.pytester.makeconftest(CONFTEST_WITH_REPORTER)
    result = runner.pytester.runpytest_subprocess("-n", "2", "test_suite.py", timeout=180)
    result.assert_outcomes(passed=1)
    time.sleep(2.0)  # the sidecar's own exit, after "stop"
    assert not (runner.pytester.path / "alert.jsonl").exists()
    assert not list((runner.pytester.path / ".pytest-failures").glob("*/reporter.log"))
    (log,) = list((runner.pytester.path / ".pytest-failures").glob("*/controller.events"))
    announced = [json.loads(line) for line in log.read_text().splitlines() if '"kill_witnesses"' in line]
    assert announced and announced[0]["reporter"] == "armed"


@needs_xdist
def test_a_reporter_that_cannot_travel_is_a_warning_not_a_broken_run(runner):
    runner.pytester.makeconftest(
        INNER_CONFTEST
        + """

from pytest_failure_instrumentation import install


def pytest_configure(config):
    install(config, on_run_death=lambda incident: None)
"""
    )
    runner.pytester.makepyfile(test_suite="def test_one():\n    assert True\n")
    result = runner.pytester.runpytest_subprocess("-n", "2", "test_suite.py", timeout=180)
    result.assert_outcomes(passed=1)
    result.stderr.fnmatch_lines(["*failure_on_run_death*cannot be handed to the sidecar*"])
    (log,) = list((runner.pytester.path / ".pytest-failures").glob("*/controller.events"))
    announced = [json.loads(line) for line in log.read_text().splitlines() if '"kill_witnesses"' in line]
    assert announced and announced[0]["reporter"].startswith("off: failure_on_run_death cannot travel")


#: Where the directory stood at the moment each incident was handed over.
CLAIMED: list[bool] = []


def note_the_claim(directory: Path, incident) -> None:
    CLAIMED.append(bool(leftovers.marker(directory).get(leftovers.REPORTED_KEY)))


def test_the_directory_is_claimed_before_a_single_incident_is_delivered(tmp_path):
    """The key is what stops a second reporter - or the next run to sweep this
    root - raising the same dead directory again, and delivery is the slow
    part of this: enriching each incident, then a call out to whatever the
    user configured, which may be a network hop.

    Stamped afterwards, everything from the marker check through the
    controller and worker grace periods is a window in which a second sweeper
    reads an unclaimed marker and starts its own copy of the work, and the
    dead run is reported twice.
    """
    directory = dead_run(tmp_path)
    CLAIMED.clear()

    reported = reporter.report(
        payload_for(directory, functools.partial(note_the_claim, directory))
    )

    assert len(reported) == 1
    assert CLAIMED == [True], "the claim is staked before the first delivery, not after"


def test_a_half_written_marker_is_never_what_another_run_reads(tmp_path):
    """Its readers are other runs, and ``marker`` on a truncated owner.json
    returns None - a directory that reads as "not ours" is one whose
    incidents are never raised at all."""
    directory = dead_run(tmp_path)
    before = leftovers.marker(directory)

    assert leftovers.stamp(directory, leftovers.REPORTED_KEY)

    after = leftovers.marker(directory)
    assert after is not None and after[leftovers.REPORTED_KEY] > 0
    assert {key: after[key] for key in before} == before, "nothing else was disturbed"
    # The temporary it renames over the marker is not left behind, and is
    # never itself mistaken for one.
    assert [path.name for path in directory.glob("owner.json*")] == ["owner.json"]
