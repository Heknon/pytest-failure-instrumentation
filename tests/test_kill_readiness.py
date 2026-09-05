"""Regressions for attribution correctness and non-interference under failure."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pytest_failure_instrumentation.analysis import classify
from pytest_failure_instrumentation.capture.output import StderrTee
from pytest_failure_instrumentation.capture.timeouts import effective
from pytest_failure_instrumentation.incidents import killer, leftovers, reporter
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident, _time_the_test
from pytest_failure_instrumentation.probes import kernel_log, signal_trace

from .conftest import INNER_CONFTEST, needs_xdist
from .test_run_death_reporter import dead_run, payload_for

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptors/signals")


def test_dump_only_faulthandler_is_not_a_terminating_deadline():
    config = SimpleNamespace(
        pluginmanager=SimpleNamespace(hasplugin=lambda _: False),
        getini=lambda key: {"faulthandler_timeout": 2, "faulthandler_exit_on_timeout": False}[key],
    )
    assert effective(SimpleNamespace(config=config)) == []


@pytest.mark.parametrize("phase,start,died,matched", [
    ("setup", 1., 20., False), ("call", 19., 20., False), ("call", 17., 20., True),
    ("teardown", 1., 20., False), ("call", 19.9, 20., False),
])
def test_call_only_deadline_uses_only_the_call_clock(phase, start, died, matched):
    incident = WorkerDeathIncident(worker="gw0", phase=phase, test_in_flight="test.py::test_one")
    _time_the_test(incident, {
        "test_started": 1., "phase_started": start,
        "timeout_settings": [{"source": "pytest-timeout", "seconds": 2., "scope": "call"}],
    }, died)
    assert (incident.matched_timeout is not None) is matched


@needs_xdist
@pytest.mark.parametrize("case", ["dump_only", "disabled_marker", "raised_marker", "actual_timeout"])
def test_real_deadline_overrides_do_not_falsely_explain_a_manual_exit(distributed, case):
    pytest.importorskip("pytest_timeout")
    marker = {"disabled_marker": "@pytest.mark.timeout(0)",
              "raised_marker": "@pytest.mark.timeout(20)"}.get(case, "")
    distributed.pytester.makepyfile(test_case=f'''
        import os, time, pytest
        def test_ok(): pass
        {marker}
        def test_exits():
            time.sleep({10 if case == "actual_timeout" else 2})
            os._exit(1)
    ''')
    args = (["-o", "faulthandler_timeout=1", "-o", "faulthandler_exit_on_timeout=false"]
            if case == "dump_only" else ["--timeout=1", "--timeout-method=thread"])
    found = distributed.run("-n", "2", "test_case.py", *args, timeout=45)
    death = distributed.only(found, "worker_death")
    if case == "actual_timeout":
        assert (death.verdict, death.confidence) == ("POSSIBLE_TIMEOUT", "medium")
        assert death.matched_timeout == 1
    else:
        assert death.verdict == "SELF_EXIT"
        assert death.matched_timeout is None


@posix
def test_direct_kill_evidence_outranks_an_earlier_cgroup_oom():
    incident = WorkerDeathIncident(worker="gw0", exit_status=-9,
        cgroup_oom_kills_since_start=1,
        killer=killer.SignalRecord(signal=9, name="SIGKILL", origin="process",
                                  sender_pid=123, sender_role="outside this run"))
    assert classify.of(incident)[:2] == ("KILLED_BY_PROCESS", "high")


def test_rejected_trace_event_does_not_replace_the_actual_sender(tmp_path):
    trace = tmp_path / signal_trace.TRACE_FILE
    trace.write_text("\n".join(json.dumps({"wall": 99., "line":
        f"killer-{sender} [000] d..1. 1.000000: signal_generate: sig=9 errno=0 code=0 "
        f"comm=python pid=456 grp=1 res={result}"}) for sender, result in [(123, 0), (789, 1)]))
    found, _ = killer._from_trace(killer.Sources(tmp_path, live=False), {}, 456, -9, 90., 105., 100.)
    assert found is not None and found.sender_pid == 123


def test_old_sigterm_is_not_a_new_deaths_cause(tmp_path):
    trace = tmp_path / signal_trace.TRACE_FILE
    trace.write_text(json.dumps({"wall": 10., "line":
        "killer-123 [000] d..1. 1.000000: signal_generate: sig=15 errno=0 code=0 "
        "comm=python pid=456 grp=1 res=0"}))
    found, before = killer._from_trace(killer.Sources(tmp_path, live=False), {}, 456, None,
                                     0., 1005., 1000.)
    assert found is None and before == []


def test_cgroup_neighbour_is_not_this_victim(tmp_path, monkeypatch):
    other = kernel_log.OomKill(victim_pid=999, victim_comm="other", at=99., task_memcg="/shared",
        constraint=None, memcg=None, total_vm_kb=None, anon_rss_kb=None, file_rss_kb=None,
        shmem_rss_kb=None, uid=None, oom_score_adj=None)
    monkeypatch.setattr(kernel_log, "own_cgroup", lambda: "/shared")
    sources = killer.Sources(tmp_path, live=False)
    monkeypatch.setattr(sources, "kernel_log_reading", lambda *_: kernel_log.KernelLogReading([other], "dmesg", "1 line"))
    found, _ = killer._from_kernel_log(sources, {}, 456, 90., 105., 100., None)
    assert found is None


@posix
@needs_xdist
@pytest.mark.parametrize("preblocked", [False, True])
def test_controller_and_helpers_keep_the_callers_signal_mask(runner, preblocked):
    runner.pytester.makeconftest(INNER_CONFTEST + f'''
import signal, subprocess, sys
import pytest
if {preblocked!r}:
    signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGTERM}})
@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput"):
        return
    assert (signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())) == {preblocked!r}
    code = "import signal; print(signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set()))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == {str(preblocked)!r}
''')
    runner.pytester.makepyfile(test_case="def test_ok(): pass")
    runner.run("-n", "2", "test_case.py", timeout=45)
    runner.result.assert_outcomes(passed=1)


def test_cascade_shares_kernel_and_trace_reads(tmp_path, monkeypatch):
    calls = []
    def read(**_):
        calls.append(1)
        return kernel_log.KernelLogReading([], "unavailable", "refused")
    monkeypatch.setattr(kernel_log, "read", read)
    sources = killer.Sources(tmp_path)
    for _ in range(80):
        sources.kernel_log_reading(10., time.time())
    assert len(calls) == 1
    (tmp_path / signal_trace.TRACE_FILE).write_text("")
    parses = []
    monkeypatch.setattr(signal_trace, "witnessed", lambda _: parses.append(1) or [])
    for _ in range(80):
        sources.trace_reading()
    assert len(parses) == 1


def test_blocked_kernel_reader_does_not_block_a_death_cascade(tmp_path, monkeypatch):
    release = threading.Event()
    def read(**_):
        release.wait(10)
        return kernel_log.KernelLogReading([], "unavailable", "refused")
    monkeypatch.setattr(kernel_log, "read", read)
    sources = killer.Sources(tmp_path)
    start = time.monotonic()
    try:
        for _ in range(80):
            result = sources.kernel_log_reading(10., time.time())
        assert time.monotonic() - start < 1.0
        assert "pending" in result.detail
    finally:
        release.set()
        sources._refresh.join(timeout=2)


def test_reporter_excludes_competitors_and_pruning(tmp_path, monkeypatch):
    directory = dead_run(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def target(_):
        calls.append(1)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(reporter, "resolve", lambda _: target)
    payload = payload_for(directory, "unused:callback")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(reporter.report, payload)
        try:
            assert entered.wait(5)
            assert reporter.report(payload) == []
            leftovers.prune_finished_runs(tmp_path)
            assert directory.exists()
        finally:
            release.set()
        assert len(first.result(timeout=5)) == 1
    assert calls == [1]


def test_reporter_retry_skips_checkpointed_successes(tmp_path):
    directory = dead_run(tmp_path)
    items = [WorkerDeathIncident(worker=f"gw{i}", worker_pid=i) for i in range(2)]
    calls = []
    def first(incident):
        if incident.worker == "gw1":
            raise RuntimeError("delivery unavailable")
        calls.append(incident.worker)
    with leftovers.claim(directory) as acquired:
        assert acquired
        with pytest.raises(RuntimeError):
            leftovers.deliver(directory, items, first)
    assert not leftovers.marker(directory).get(leftovers.REPORTED_KEY)
    with leftovers.claim(directory) as acquired:
        assert acquired
        leftovers.deliver(directory, items, lambda incident: calls.append(incident.worker))
    assert calls == ["gw0", "gw1"]


@posix
@pytest.mark.parametrize("failure", ["error", "partial"])
def test_stderr_restore_survives_failed_or_partial_copy(tmp_path, monkeypatch, failure):
    tee = StderrTee(tmp_path / "output")
    saved = os.dup(2)
    real_write = os.write
    payload = b"native output\n" * 10000
    with tempfile.TemporaryFile() as sink:
        try:
            os.dup2(sink.fileno(), 2)
            assert tee.start()
            tee.take()
            os.write(2, payload)
            sizes = []
            def write(fd, data):
                sizes.append(len(data))
                if failure == "error":
                    raise OSError("disk full")
                return real_write(fd, data[:100])
            with monkeypatch.context() as scoped:
                scoped.setattr(os, "write", write)
                tee.hand_back()
            assert os.fstat(2).st_ino == os.fstat(sink.fileno()).st_ino
            assert max(sizes) <= 65536
            if failure == "partial":
                sink.seek(0)
                assert sink.read() == payload
            else:
                assert "failed" in tee.reason
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            tee.close()


def test_recovery_retains_evidence_while_a_worker_is_alive(tmp_path, monkeypatch):
    from pytest_failure_instrumentation.capture.state import WorkerState

    from .test_run_death_reporter import remember

    directory = dead_run(tmp_path)
    state = WorkerState(directory / "gw0.state", os.getpid())
    state.update()
    state.close()
    monkeypatch.setattr(reporter, "WORKERS_GONE_SECONDS", 0)
    assert reporter.report(payload_for(directory, remember)) == []
    assert not leftovers.marker(directory).get(leftovers.REPORTED_KEY)
    leftovers.prune_finished_runs(directory.parent)
    assert directory.exists()


@posix
def test_stderr_restoration_failure_keeps_a_retryable_handle(tmp_path, monkeypatch):
    tee = StderrTee(tmp_path / "output")
    saved = os.dup(2)
    try:
        assert tee.start()
        tee.take()
        with monkeypatch.context() as scoped:
            def fail(*_):
                raise OSError("interrupted restoration")
            scoped.setattr(os, "dup2", fail)
            tee.close()
        assert tee._passthrough is not None
        tee.close()
        assert tee._passthrough is None
        assert os.fstat(2) == os.fstat(saved)
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        tee.close()


@pytest.mark.parametrize("backend", ["tracefs", "etw"])
def test_reporter_deadline_covers_blocked_input_and_reaps_child(tmp_path, monkeypatch, backend):
    import ast
    import subprocess

    from pytest_failure_instrumentation.probes import etw_trace

    children = []
    real_popen = subprocess.Popen
    def start(*_, **kwargs):
        kwargs.pop("creationflags", None)
        child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", start)
    payload = {"large": "x" * 1000000}
    if backend == "etw":
        monkeypatch.setattr(etw_trace, "REPORTER_TIMEOUT", 0.1)
        etw_trace._report(payload, str(tmp_path / "trace"))
    else:
        tree = ast.parse(signal_trace.SIDECAR)
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "report")
        namespace = dict(os=os, sys=sys, json=json, subprocess=subprocess,
                         directory=str(tmp_path), output=str(tmp_path / "trace"),
                         uid=None, gid=None, own=lambda _: None,
                         REPORTER="", REPORTER_TIMEOUT=0.1)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "sidecar", "exec"), namespace)
        namespace["report"](payload)
    assert len(children) == 1
    assert children[0].poll() is not None
    assert "TimeoutExpired" in (tmp_path / "reporter.log").read_text()



def test_etw_sidecar_runs_without_the_package_or_site_dependencies(tmp_path):
    import subprocess

    from pytest_failure_instrumentation.probes import etw_trace

    output = tmp_path / "trace"
    completed = subprocess.run(
        [sys.executable, "-I", "-S", etw_trace.__file__, "test-session", str(output), "watch"],
        input=b'{"stop": true}\n', capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert json.loads(output.read_text())["mode"] == "watch"
    assert not (tmp_path / "reporter.log").exists()
