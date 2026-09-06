"""Incident delivery is bounded and preserves independent failure evidence."""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from pytest_failure_instrumentation import probes
from pytest_failure_instrumentation.incidents import leftovers, reporter
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident
from pytest_failure_instrumentation.probes import process, signal_trace

from .test_run_death_reporter import CALLS, dead_run, payload_for, remember


def test_surviving_worker_does_not_suppress_controller_and_is_preserved(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    state = directory / "gw0.state"
    state.write_text(json.dumps({"pid": 999998}))
    monkeypatch.setattr(probes, "is_running", lambda pid: pid == 999998)
    monkeypatch.setattr(reporter, "WORKERS_GONE_SECONDS", 0)
    CALLS.clear()
    assert len(reporter.report(payload_for(directory, functools.partial(remember, "first")))) == 1
    assert CALLS[0][2] == "controller"
    assert not leftovers.marker(directory).get(leftovers.REPORTED_KEY)
    leftovers.prune_finished_runs(tmp_path)
    assert directory.exists()
    # Once the surviving worker finishes, recovery closes the run without
    # reporting the already delivered controller again.
    monkeypatch.setattr(probes, "is_running", lambda pid: False)
    assert reporter.report(payload_for(directory, functools.partial(remember, "again"))) == []
    assert len(CALLS) == 1
    assert leftovers.marker(directory).get(leftovers.REPORTED_KEY)


def test_reused_owner_pid_does_not_hide_a_dead_run(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    record = leftovers.marker(directory)
    record["created_at"] = 100.0
    leftovers._write_marker(directory, record)
    monkeypatch.setattr(probes, "is_running", lambda pid: pid == record["pid"])
    monkeypatch.setattr(process, "creation_time", lambda pid: 200.0)
    assert [item.worker for item in leftovers.deaths_of(directory)] == ["controller"]
    # Missing identity information remains conservative, including old files.
    monkeypatch.setattr(process, "creation_time", lambda pid: None)
    assert leftovers.deaths_of(directory) == []


def test_reused_worker_pid_does_not_hold_recovery_open(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    (directory / "gw0.state").write_text(json.dumps({"pid": 999998, "created_at": 100.0}))
    monkeypatch.setattr(probes, "is_running", lambda pid: pid == 999998)
    monkeypatch.setattr(process, "creation_time", lambda pid: 200.0)
    assert not leftovers.workers_alive(directory)
    assert [item.worker for item in leftovers.deaths_of(directory)] == ["controller"]


def test_failed_callback_retries_without_replaying_successes(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    first = WorkerDeathIncident(worker="gw0", worker_pid=1, verdict="NATIVE_CRASH")
    second = WorkerDeathIncident(worker="controller", worker_pid=2, verdict="UNKNOWN")
    monkeypatch.setattr(leftovers, "deaths_of", lambda *a, **kw: [first, second])
    calls = []
    def target(incident):
        calls.append(incident.worker)
        if calls == ["gw0", "controller"]:
            raise RuntimeError("transient transport failure")
    monkeypatch.setattr(reporter, "resolve", lambda spec: target)
    monkeypatch.setattr(reporter, "RETRY_SECONDS", 0)
    result = reporter.report(payload_for(directory, "unused:target"))
    assert calls == ["gw0", "controller", "controller"]
    assert [item.worker for item in result] == ["gw0", "controller"]
    assert leftovers.marker(directory).get(leftovers.REPORTED_KEY)


def test_permanent_callback_failure_has_a_fixed_attempt_budget(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    target = Mock(side_effect=RuntimeError("down"))
    monkeypatch.setattr(reporter, "resolve", lambda spec: target)
    monkeypatch.setattr(reporter, "RETRY_SECONDS", 0)
    assert reporter.report(payload_for(directory, "unused:target")) == []
    assert target.call_count == reporter.DELIVERY_ATTEMPTS
    assert not leftovers.marker(directory).get(leftovers.REPORTED_KEY)


def test_twenty_unknown_worker_losses_are_context_for_one_interrupted_run(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    # Different test modules intentionally give different fingerprints. Their
    # unresolved loss is still one interrupted-run report, with all facts kept.
    for i in range(20):
        (directory / f"gw{i}.events").write_text(json.dumps({
            "event": "worker_start", "time": 901, "pid": 990000 + i,
        }) + "\n")
        (directory / f"gw{i}.state").write_text(json.dumps({
            "pid": 990000 + i, "nodeid": f"test_{i}.py::test_case", "phase": "call",
        }))
    received = []
    monkeypatch.setattr(reporter, "resolve", lambda spec: received.append)
    result = reporter.report(payload_for(directory, "unused:target"))
    assert len(result) == len(received) == 1
    assert result[0].worker == "controller"
    assert len(result[0].related_deaths) == 20
    assert {item["worker"] for item in result[0].related_deaths} == {f"gw{i}" for i in range(20)}
    assert len(leftovers.marker(directory)["delivered_incidents"]) == 21
    assert reporter.report(payload_for(directory, "unused:target")) == []
    assert len(received) == 1


def test_known_independent_death_is_not_hidden_in_controller_report(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    found = [WorkerDeathIncident(worker="gw0", worker_pid=1, verdict="NATIVE_CRASH"),
             WorkerDeathIncident(worker="controller", worker_pid=2, verdict="SIGNAL_15")]
    monkeypatch.setattr(leftovers, "deaths_of", lambda *a, **kw: found)
    received = []
    monkeypatch.setattr(reporter, "resolve", lambda spec: received.append)
    reporter.report(payload_for(directory, "unused:target"))
    assert [item.worker for item in received] == ["gw0", "controller"]
    assert not any(item.related_deaths for item in received)


def test_live_delivery_checkpoint_prevents_recovery_replay(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    prior = WorkerDeathIncident(worker="gw0", worker_pid=999998)
    leftovers.checkpoint_live(directory, prior)
    controller = WorkerDeathIncident(worker="controller", worker_pid=999999)
    monkeypatch.setattr(leftovers, "deaths_of", lambda *a, **kw: [prior, controller])
    received = []
    monkeypatch.setattr(reporter, "resolve", lambda spec: received.append)
    reporter.report(payload_for(directory, "unused:target"))
    assert [item.worker for item in received] == ["controller"]
    assert not controller.related_deaths


def test_watcher_requires_acknowledgement_and_stops_cleanly(tmp_path):
    tracer = signal_trace.SignalTracer(tmp_path / "trace", trace=False,
                                      reporter={"controller_pid": os.getpid()})
    try:
        tracer.start()
        assert tracer.active
        assert tracer.reporter_armed
        assert (tmp_path / "trace.armed").read_text()
    finally:
        tracer.stop()
    assert not tracer.reporter_armed
    assert not (tmp_path / "reporter.log").exists()


def test_failed_reporter_send_cannot_claim_armed(tmp_path, monkeypatch):
    tracer = signal_trace.SignalTracer(tmp_path / "trace", trace=False, reporter={"x": 1})
    monkeypatch.setattr(tracer, "_send", lambda message: None)
    # Simulate a dead sidecar after a failed send; stale acknowledgements do
    # not match the per-attempt token.
    (tmp_path / "trace.armed").write_text("old-token")
    tracer._arm_reporter()
    assert not tracer.reporter_armed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_windows_watcher_notices_dead_controller_while_pipe_writer_survives(tmp_path):
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    output = tmp_path / "trace"
    result = tmp_path / "reported"
    code = f'''
from pytest_failure_instrumentation.probes import etw_trace
from pathlib import Path
etw_trace._report = lambda *args: Path({str(result)!r}).write_text("reported")
etw_trace.serve("test-watch", {str(output)!r}, "watch")
'''
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE)
    try:
        child.stdin.write((json.dumps({"reporter": {"controller_pid": victim.pid}, "ack": "ready"}) + "\n").encode())
        child.stdin.flush()
        deadline = time.monotonic() + 10
        while not (tmp_path / "trace.armed").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / "trace.armed").exists()
        victim.terminate()
        victim.wait(timeout=5)
        assert child.wait(timeout=5) == 0  # stdin remains open throughout
        assert result.read_text() == "reported"
    finally:
        for proc in (victim, child):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        child.stdin.close()


def test_repeated_live_failure_is_one_hook_and_late_stall_is_suppressed(pytester):
    from types import SimpleNamespace

    from pytest_failure_instrumentation import Settings
    from pytest_failure_instrumentation.incidents.engine import IncidentEngine
    from pytest_failure_instrumentation.incidents.stall import WorkerStallIncident

    config = pytester.parseconfig()
    engine = IncidentEngine(config, Settings(directory=pytester.path / "evidence"))
    received = []
    engine.config = SimpleNamespace(hook=SimpleNamespace(pytest_failure_incident=received.append),
                                    pluginmanager=config.pluginmanager)
    # The hook calls by keyword, matching pytest's contract.
    engine.config.hook.pytest_failure_incident = lambda incident: received.append(incident)
    for i in range(20):
        engine.raise_incident(WorkerDeathIncident(worker=f"gw{i}", worker_pid=990000+i,
                                                  verdict="NATIVE_CRASH"))
    assert len(received) == 1
    assert engine.raised == 1
    assert engine.suppressed == 19
    engine.workers_down.add("gw0")
    engine.workers_failed.add("gw0")
    assert not engine.raise_incident(WorkerStallIncident(worker="gw0", verdict="STALLED_FROZEN"))
    assert len(received) == 1
    assert engine.raise_incident(WorkerDeathIncident(worker="gw21", worker_pid=999990,
                                                    verdict="SIGNAL_15"))
    assert len(received) == 2  # Distinct failure evidence stays visible.


def test_twenty_assertion_failures_do_not_generate_twenty_plugin_incidents(runner):
    runner.pytester.makepyfile(test_cases="""
import pytest
@pytest.mark.parametrize('i', range(20))
def test_failure(i):
    assert False
""")
    incidents = runner.run("-p", "no:xdist", "test_cases.py")
    assert [item.kind for item in incidents] == ["run_summary"]
    assert incidents[0].raised == 0
    assert incidents[0].exitstatus == 1


def test_clean_worker_completion_does_not_hide_a_confirmed_stall(pytester):
    from types import SimpleNamespace

    from pytest_failure_instrumentation import Settings
    from pytest_failure_instrumentation.incidents.engine import IncidentEngine
    from pytest_failure_instrumentation.incidents.stall import WorkerStallIncident

    engine = IncidentEngine(pytester.parseconfig(), Settings(directory=pytester.path / "evidence"))
    received = []
    engine.config = SimpleNamespace(hook=SimpleNamespace(
        pytest_failure_incident=lambda incident: received.append(incident)))
    engine.workers_down.add("gw0")  # Clean completion, no death incident.
    # A stack request can interrupt native sleep after confirming a frozen
    # worker. Finishing cleanly does not invalidate that prior observation.
    assert engine.raise_incident(WorkerStallIncident(worker="gw0", verdict="STALLED_FROZEN"))
    assert len(received) == 1


@pytest.mark.parametrize("state_contents", [None, "{torn"])
def test_missing_worker_state_uses_event_identity_without_false_death(tmp_path, monkeypatch, state_contents):
    directory = dead_run(tmp_path)
    (directory / "gw0.events").write_text(json.dumps({
        "event": "worker_start", "time": 901, "pid": os.getpid(),
    }) + "\n")
    if state_contents is not None:
        (directory / "gw0.state").write_text(state_contents)
    monkeypatch.setattr(reporter, "WORKERS_GONE_SECONDS", 0)
    received = []
    monkeypatch.setattr(reporter, "resolve", lambda spec: received.append)
    assert leftovers.workers_alive(directory)
    reported = reporter.report(payload_for(directory, "unused:target"))
    assert [item.worker for item in reported] == ["controller"]
    assert not reported[0].related_deaths
    assert not leftovers.marker(directory).get(leftovers.REPORTED_KEY)
    leftovers.prune_finished_runs(tmp_path)
    assert directory.exists()
