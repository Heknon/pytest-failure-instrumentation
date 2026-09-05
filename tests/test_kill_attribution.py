"""Who killed it - each witness asked for real where this machine allows it.

The wait status is the same ``-9`` for the OOM killer, a cancelled job and a
stray ``kill``. What separates them is a record kept by whoever did the
killing: the kernel's signal tracepoint, the kernel log, or the SIGTERM the
controller was sent first. Each is exercised against the real thing where the
machine permits - the tracepoint needs root or ``sudo -n`` - and against
recorded output everywhere, since a CI cell that cannot be OOM-killed on
demand still has to read the log of one that was.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation.analysis import classify, severity
from pytest_failure_instrumentation.incidents import killer
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident
from pytest_failure_instrumentation.incidents.killer import (
    KillSources,
    OomKillRecord,
    SignalRecord,
)
from pytest_failure_instrumentation.probes import etw_trace, kernel_log, signal_trace

from .conftest import ENABLE_FLAG, INNER_CONFTEST, needs_xdist

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="these witnesses are Linux sources")


def _tracepoint_here() -> bool:
    return sys.platform == "linux" and signal_trace.availability(elevate=True)[0]


needs_tracepoint = pytest.mark.skipif(
    not _tracepoint_here(),
    reason="the signal tracepoint needs tracefs and root, or a sudo -n that answers",
)

# -- what the kernel prints -----------------------------------------------

#: A 5.x kernel, global OOM, with the task table. Three workers of one run and
#: a bystander; the victim is not the largest.
GLOBAL_OOM = """\
[ 1201.101000] python3 invoked oom-killer: gfp_mask=0x140dca(GFP_HIGHUSER_MOVABLE|__GFP_COMP|__GFP_ZERO), order=0, oom_score_adj=0
[ 1201.102000] Tasks state (memory values in pages):
[ 1201.102000] [  pid  ]   uid  tgid total_vm      rss pgtables_bytes swapents oom_score_adj name
[ 1201.102000] [    611]     0   611    26843      512   204800        0             0 systemd-journal
[ 1201.102000] [   4240]  1000  4240   512000   440320  3276800        0             0 python3
[ 1201.102000] [   4241]  1000  4241   520000   419840  3276800        0             0 python3
[ 1201.102000] [   4242]  1000  4242   530000   430080  3276800        0             0 python3
[ 1201.103000] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice/user-1000.slice/session-3.scope,task=python3,pid=4242,uid=1000
[ 1201.103000] Out of memory: Killed process 4242 (python3) total-vm:2120000kB, anon-rss:1720320kB, file-rss:0kB, shmem-rss:0kB, UID:1000 pgtables:3200kB oom_score_adj:0
[ 1201.150000] oom_reaper: reaped process 4242 (python3), now anon-rss:0kB, file-rss:0kB, shmem-rss:0kB
"""

#: A 4.x kernel, a cgroup's limit, no summary line and no table.
MEMCG_OOM_OLD = """\
[ 5000.100000] Memory cgroup out of memory: Kill process 777 (python) score 950 or sacrifice child
[ 5000.100000] Killed process 777 (python) total-vm:400000kB, anon-rss:390000kB, file-rss:0kB, shmem-rss:0kB
"""

#: A 5.x kernel inside a container: the cgroup is named, and the pid is the
#: host's.
MEMCG_OOM_CONTAINER = """\
[ 9000.000000] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,oom_memcg=/docker/abc123,task_memcg=/docker/abc123,task=python,pid=5100,uid=0
[ 9000.000000] Memory cgroup out of memory: Killed process 5100 (python) total-vm:3000000kB, anon-rss:2048000kB, file-rss:0kB, shmem-rss:0kB, UID:0 pgtables:6000kB oom_score_adj:0
"""


def lines(text: str) -> list[tuple[float | None, str]]:
    """dmesg-shaped text into what the parser is handed."""
    found = []
    for raw in text.splitlines():
        match = kernel_log._DMESG_LINE.match(raw)
        assert match is not None, raw
        found.append((float(match.group("seconds")), match.group("message")))
    return found


def test_a_global_oom_kill_is_parsed_with_its_table():
    (kill,) = kernel_log.parse(lines(GLOBAL_OOM))
    assert (kill.victim_pid, kill.victim_comm) == (4242, "python3")
    assert kill.constraint == "CONSTRAINT_NONE"
    assert kill.task_memcg == "/user.slice/user-1000.slice/session-3.scope"
    assert kill.memcg is None
    assert (kill.anon_rss_kb, kill.total_vm_kb, kill.uid, kill.oom_score_adj) == (
        1720320, 2120000, 1000, 0,
    )
    assert kill.at == pytest.approx(1201.103)
    assert kill.tasks_considered == 4
    assert kill.tasks_rss_pages == 512 + 440320 + 419840 + 430080
    # Largest first, so a reader's "top three" is the first three.
    assert [task.pid for task in kill.tasks] == [4240, 4242, 4241, 611]
    assert kill.tasks[0].name == "python3" and kill.tasks[0].oom_score_adj == 0


def test_an_old_kernels_memcg_kill_is_still_a_memcg_kill():
    (kill,) = kernel_log.parse(lines(MEMCG_OOM_OLD))
    assert kill.victim_pid == 777
    assert kill.constraint == "CONSTRAINT_MEMCG"  # from the prefix, not a summary line
    assert kill.anon_rss_kb == 390000
    assert kill.tasks == [] and kill.tasks_considered == 0


def test_a_container_kill_names_its_cgroup():
    (kill,) = kernel_log.parse(lines(MEMCG_OOM_CONTAINER))
    assert kill.constraint == "CONSTRAINT_MEMCG"
    assert kill.memcg == "/docker/abc123" and kill.task_memcg == "/docker/abc123"
    assert kill.uid == 0


def test_two_kills_each_keep_their_own_table():
    kills = kernel_log.parse(lines(GLOBAL_OOM + MEMCG_OOM_OLD))
    assert [kill.victim_pid for kill in kills] == [4242, 777]
    assert kills[0].tasks_considered == 4
    assert kills[1].tasks_considered == 0, "the table belongs to the kill that followed it"


def test_the_ladder_says_which_rung_answered_or_why_none_did():
    reading = kernel_log.read(elevate=False)
    if sys.platform != "linux":
        assert reading.source == "unavailable"
        return
    assert reading.source in ("kmsg", "journal", "dmesg", "unavailable")
    if reading.source == "unavailable":
        # Every rung is named with its refusal, sudo included.
        assert "kmsg:" in reading.detail and "sudo dmesg:" in reading.detail


# -- the fleet arithmetic ---------------------------------------------------


def roles():
    return {999: killer.CONTROLLER, 4240: "gw0", 4241: "gw1", 4242: "gw2"}


def test_the_fleet_table_says_it_was_pressure_across_workers(monkeypatch):
    # This Linux log fixture was recorded with 4 KiB pages, independent of
    # the machine reading it (macOS ARM runners use 16 KiB pages).
    monkeypatch.setattr(kernel_log, "page_kb", lambda: 4)
    (kill,) = kernel_log.parse(lines(GLOBAL_OOM))
    record = killer._oom_record(kill, "pid", roles(), 4242, "kmsg", None)
    assert record.run_tasks == 3 and record.tasks_considered == 4
    assert record.victim_rank == 2
    assert record.largest[0] == {"pid": 4240, "name": "python3", "rss_mb": 1720, "role": "gw0"}
    assert record.largest[2]["role"] == "gw1"
    assert record.run_median_rss_mb == 1680
    # The victim sat beside its peers: the run exceeded the limit together.
    assert record.pressure == "fleet"


def test_a_victim_far_above_its_peers_is_its_own_weight():
    (kill,) = kernel_log.parse(lines(GLOBAL_OOM))
    for task in kill.tasks:
        if task.pid == 4242:
            task.rss_pages *= 5
    kill.anon_rss_kb *= 5
    record = killer._oom_record(kill, "pid", roles(), 4242, "kmsg", None)
    assert record.pressure == "own weight"
    assert record.victim_rank == 1


def test_the_kernel_origin_witness_becomes_who_triggered_the_kill():
    (kill,) = kernel_log.parse(lines(GLOBAL_OOM))
    trigger = SignalRecord(signal=9, origin="kernel", sender_pid=4240, sender_comm="python3")
    record = killer._oom_record(kill, "pid", roles(), 4242, "kmsg", trigger)
    assert (record.triggered_by_pid, record.triggered_by_role) == (
        4240, "gw0, another process of this run",
    )


# -- what the tracepoint prints ---------------------------------------------

TRACE_LINE = (
    "          python-1771    [000] d..1.   401.375501: signal_generate: "
    "sig=9 errno=0 code=0 comm=sleep pid=1772 grp=1 res=0"
)
KERNEL_LINE = (
    "         python3-4240    [003] d..1.  1201.103000: signal_generate: "
    "sig=9 errno=0 code=128 comm=python3 pid=4242 grp=1 res=0"
)


def test_a_trace_line_names_sender_and_target():
    seen = signal_trace.parse_line(TRACE_LINE, at=100.0)
    assert seen is not None
    assert (seen.sender_comm, seen.sender_pid) == ("python", 1771)
    assert (seen.target_comm, seen.target_pid, seen.signal) == ("sleep", 1772, 9)
    assert seen.si_code == 0 and not seen.from_kernel and seen.delivered and seen.to_group
    assert seen.at == 100.0 and seen.trace_seconds == pytest.approx(401.375501)


def test_a_kernel_kill_is_told_from_a_process_kill_by_its_code():
    seen = signal_trace.parse_line(KERNEL_LINE)
    assert seen is not None and seen.from_kernel and seen.sender_pid == 4240


def test_a_sender_with_a_space_in_its_name_still_parses():
    seen = signal_trace.parse_line(
        "     my helper-42    [001] d..1.    5.000000: signal_generate: "
        "sig=15 errno=0 code=0 comm=python pid=7 grp=1 res=0"
    )
    assert seen is not None and (seen.sender_comm, seen.sender_pid) == ("my helper", 42)


def test_anything_else_is_not_a_witness():
    assert signal_trace.parse_line("# tracer: nop") is None
    assert signal_trace.parse_line("") is None


def _trace_file(directory: Path, *records: dict) -> Path:
    path = directory / signal_trace.TRACE_FILE
    header = {"header": True, "pid": 1, "wall": 0.0, "monotonic": 0.0}
    path.write_text(
        "\n".join(json.dumps(record) for record in (header, *records)) + "\n", encoding="utf-8"
    )
    return path


def _line(sender_comm: str, sender_pid: int, sig: int, target_pid: int, code: int = 0) -> str:
    return (
        f"{sender_comm:>16}-{sender_pid}    [000] d..1.   500.000000: signal_generate: "
        f"sig={sig} errno=0 code={code} comm=python pid={target_pid} grp=1 res=0"
    )


def test_attribution_reads_the_trace_and_places_the_sender(tmp_path):
    _trace_file(
        tmp_path,
        {"line": _line("bash", 31337, 9, 4242), "wall": 1000.0, "sender_cmdline": "kill -9 4242"},
    )
    found = killer.attribute(
        killer.Sources(tmp_path, trace_status="tracefs", run_pids=roles),
        pid=4242, exit_status=-9, started_at=900.0, died_at=1000.4,
    )
    assert found.killer is not None
    assert found.killer.origin == "process"
    assert (found.killer.sender_pid, found.killer.sender_comm) == (31337, "bash")
    assert found.killer.sender_cmdline == "kill -9 4242"
    assert found.killer.sender_role == "outside this run"
    assert found.killer.seconds_before_death == pytest.approx(0.4)
    assert found.sources.signal_trace == "tracefs"
    assert "bash" in found.killer.who() and "outside this run" in found.killer.who()


def test_the_controller_and_a_sibling_are_named_as_such(tmp_path):
    _trace_file(
        tmp_path,
        {"line": _line("python", 999, 15, 4242), "wall": 990.0},
        {"line": _line("python", 4241, 9, 4242), "wall": 1000.0},
    )
    found = killer.attribute(
        killer.Sources(tmp_path, run_pids=roles),
        pid=4242, exit_status=-9, started_at=900.0, died_at=1000.4,
    )
    assert found.killer is not None and found.killer.sender_role == "gw1, another process of this run"
    # The SIGTERM from the controller ten seconds earlier is context, kept
    # apart from the signal that ended it.
    assert [record.sender_role for record in found.before] == ["this run's controller"]


def test_a_kill_from_before_this_worker_existed_is_not_its_death(tmp_path):
    _trace_file(tmp_path, {"line": _line("bash", 1, 9, 4242), "wall": 100.0})
    found = killer.attribute(
        killer.Sources(tmp_path, run_pids=roles),
        pid=4242, exit_status=-9, started_at=900.0, died_at=1000.0,
    )
    assert found.killer is None, "a recycled pid's earlier kill must not be borrowed"


def test_the_controllers_own_sigterm_is_attached_with_its_sender(tmp_path):
    (tmp_path / killer.CONTROLLER_EVENTS).write_text(
        json.dumps({
            "event": "signal_received", "time": 991.0, "signal": 15, "name": "SIGTERM",
            "si_code": 0, "origin": "process", "sender_pid": 812, "sender_uid": 998,
            "sender_comm": "gitlab-runner", "sender_cmdline": "gitlab-runner run",
        }) + "\n",
        encoding="utf-8",
    )
    found = killer.attribute(
        killer.Sources(tmp_path, witness_status="on", run_pids=roles),
        pid=4242, exit_status=-9, started_at=900.0, died_at=1000.0,
    )
    (term,) = found.before
    assert term.target == killer.CONTROLLER and term.source == "controller-witness"
    assert term.sender_comm == "gitlab-runner" and term.sender_role == "outside this run"
    assert term.seconds_before_death == pytest.approx(9.0)


# -- the verdicts -----------------------------------------------------------


def death(**fields) -> WorkerDeathIncident:
    fields.setdefault("worker", "gw1")
    fields.setdefault("test_in_flight", "test_api.py::test_thing")
    fields.setdefault("phase", "call")
    fields.setdefault("exit_status", -9)
    return WorkerDeathIncident(**fields)


def outside(**fields) -> SignalRecord:
    fields.setdefault("signal", 9)
    fields.setdefault("name", "SIGKILL")
    fields.setdefault("origin", "process")
    fields.setdefault("sender_pid", 812)
    fields.setdefault("sender_comm", "gitlab-runner")
    fields.setdefault("sender_role", "outside this run")
    fields.setdefault("target", "this worker")
    return SignalRecord(**fields)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_a_kill_from_outside_is_a_stop_not_a_defect():
    incident = death(killer=outside())
    verdict, confidence, evidence = classify.of(incident)
    assert (verdict, confidence) == ("KILLED_BY_PROCESS", "high")
    assert any("gitlab-runner (pid 812), outside this run" in line for line in evidence)
    incident.verdict = verdict
    assert incident.suspect_nodeid() is None, "the test in flight did not do this"
    assert severity.of("worker_death", "unknown", verdict, confidence, False)[0] == "informational"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_a_worker_that_signalled_itself_is_self_killed():
    verdict, _, evidence = classify.of(death(killer=outside(origin="self", sender_role="itself")))
    assert verdict == "SELF_KILLED"
    assert any("to itself" in line for line in evidence)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_a_kill_from_the_controller_is_execnets():
    verdict, _, evidence = classify.of(
        death(killer=outside(sender_comm="python", sender_role="this run's controller"))
    )
    assert verdict == "KILLED_BY_RUN"
    assert any("execnet" in line for line in evidence)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_a_kernel_kill_without_a_log_says_so():
    verdict, confidence, evidence = classify.of(
        death(
            killer=outside(origin="kernel", si_code=128, sender_comm="python3", sender_role="gw0, another process of this run"),
            kill_sources=KillSources(kernel_log="unavailable (kmsg: permission denied)"),
        )
    )
    assert (verdict, confidence) == ("KILLED_BY_KERNEL", "medium")
    assert any("SI_KERNEL" in line and "gw0" in line for line in evidence)
    assert any("Kill witnesses:" in line for line in evidence)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_the_kernel_log_outranks_every_other_witness():
    incident = death(
        killer=outside(origin="kernel", si_code=128),
        oom=OomKillRecord(
            victim_pid=4242, victim_comm="python3", constraint="CONSTRAINT_MEMCG",
            memcg="/docker/abc", anon_rss_mb=1680, source="journal",
            tasks_considered=100, tasks_rss_mb=29800, run_tasks=100, run_rss_mb=29800,
            run_median_rss_mb=290, victim_rank=3, pressure="fleet",
            largest=[{"pid": 1, "name": "python3", "rss_mb": 1720, "role": "gw17"}],
            triggered_by_pid=4240, triggered_by_comm="python3", triggered_by_role="gw0, another process of this run",
        ),
    )
    verdict, confidence, evidence = classify.of(incident)
    assert (verdict, confidence) == ("OOM_KILLED", "high")
    joined = "\n".join(evidence)
    assert "The kernel log (journal) records the OOM killer choosing pid 4242" in joined
    assert "the limit of cgroup /docker/abc" in joined
    assert "100 of them were this run's" in joined and "3rd largest" in joined
    assert "Fleet pressure" in joined
    assert "[gw17]" in joined
    assert "context of python3 (pid 4240, gw0" in joined


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_a_sigterm_to_the_controller_explains_the_kill_that_followed():
    term = outside(signal=15, name="SIGTERM", target=killer.CONTROLLER, seconds_before_death=9.8)
    verdict, confidence, evidence = classify.of(death(signals_before_death=[term]))
    assert (verdict, confidence) == ("KILLED_AFTER_SIGTERM", "medium")
    assert any("the controller received SIGTERM from gitlab-runner" in line and "10s before" in line for line in evidence)
    assert severity.of("worker_death", "unknown", verdict, confidence, False)[0] == "informational"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_no_witness_at_all_says_which_were_withheld():
    verdict, _, evidence = classify.of(
        death(kill_sources=KillSources(
            kernel_log="unavailable (kmsg: permission denied (kernel.dmesg_restrict=1))",
            signal_trace="off: tracefs needs root; set failure_elevate to use sudo",
            controller_witness="on",
        ))
    )
    assert verdict == "SIGKILLED"
    (line,) = [line for line in evidence if line.startswith("Kill witnesses:")]
    assert "dmesg_restrict=1" in line and "failure_elevate" in line


def test_a_recovered_run_told_to_stop_is_not_unknown():
    term = outside(signal=15, name="SIGTERM", target=killer.CONTROLLER, seconds_before_death=20.0)
    verdict, _, evidence = classify.of(
        death(exit_status=None, recovered_from_run="run-dead", signals_before_death=[term])
    )
    assert verdict == "RUN_STOPPED"
    assert any("told to stop" in line for line in evidence)


def test_a_recovered_run_the_kernel_log_names_is_an_oom_kill():
    verdict, confidence, _ = classify.of(
        death(exit_status=None, recovered_from_run="run-dead",
              oom=OomKillRecord(victim_pid=4242, source="kmsg"))
    )
    assert (verdict, confidence) == ("OOM_KILLED", "high")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL classification")
def test_a_witnessed_signal_stands_in_for_a_status_nobody_could_read():
    verdict, _, evidence = classify.of(
        death(exit_status=None, recovered_from_run="run-dead", killer=outside())
    )
    assert verdict == "KILLED_BY_PROCESS"
    assert any("No exit status was readable, but the kernel's signal trace saw SIGKILL" in line for line in evidence)


def test_the_senders_name_is_on_a_sigterm_death_too():
    verdict, _, evidence = classify.of(
        death(exit_status=-15, killer=outside(signal=15, name="SIGTERM"))
    )
    assert verdict == "SIGNAL_15"
    assert any("SIGTERM was sent by gitlab-runner" in line for line in evidence)


def test_the_witness_fields_round_trip():
    incident = death(killer=outside(), kill_sources=KillSources())
    again = WorkerDeathIncident.model_validate_json(incident.model_dump_json())
    assert again.killer == incident.killer and again.kill_sources == incident.kill_sources


# -- the witnesses, for real -----------------------------------------------


@linux_only
def test_the_controller_witness_names_the_sender_and_still_dies():
    """The whole contract in one process: SIGTERM blocked, waited for with its
    siginfo, written down, and then let through - so the process dies of
    SIGTERM exactly as it would have, one line richer."""
    script = """
import json, sys, time
from pytest_failure_instrumentation.capture import signals
blocked = signals.block()
print(json.dumps(sorted(blocked)), flush=True)
def record(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)
signals.SignalWitness(record, blocked).start()
time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    assert json.loads(child.stdout.readline()) == [int(signal.SIGTERM)]
    time.sleep(0.3)  # the waiting thread has to be inside sigtimedwait
    os.kill(child.pid, signal.SIGTERM)
    output = child.stdout.read()
    assert child.wait(timeout=10) == -signal.SIGTERM, output
    record = json.loads(output.strip().splitlines()[-1])
    assert record["event"] == "signal_received"
    assert record["sender_pid"] == os.getpid() and record["origin"] == "process"
    assert record["sender_uid"] == os.getuid()


@linux_only
def test_a_handler_somebody_installed_is_not_stolen():
    script = """
import json, signal
signal.signal(signal.SIGTERM, lambda *_: None)
from pytest_failure_instrumentation.capture import signals
print(json.dumps(sorted(signals.block())))
"""
    output = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert json.loads(output.stdout) == []


@needs_tracepoint
def test_the_tracepoint_names_the_sender_of_a_kill(tmp_path):
    output = tmp_path / signal_trace.TRACE_FILE
    tracer = signal_trace.SignalTracer(output, elevate=True)
    how = tracer.start()
    assert how in ("tracefs", "sudo tracefs"), how
    try:
        # Its ring buffer is the one cost that scales with the machine rather
        # than the run: the kernel's default is ~1.4 MB per CPU.
        own = Path(signal_trace.tracefs_root() or "/nonexistent") / "instances" / (
            f"{signal_trace.INSTANCE_PREFIX}{os.getpid()}"
        )
        if os.access(own / "buffer_size_kb", os.R_OK):
            # Rounded up to pages by the kernel; the default it replaced is 1410.
            assert int((own / "buffer_size_kb").read_text()) < 256
        victim = subprocess.Popen(["sleep", "30"])
        time.sleep(0.3)
        victim.kill()
        victim.wait()
        deadline = time.monotonic() + 5
        seen: list[signal_trace.Witness] = []
        while time.monotonic() < deadline and not seen:
            seen = signal_trace.sent_to(output, victim.pid, signal=9)
            time.sleep(0.05)
    finally:
        tracer.stop()
    (kill,) = seen
    assert kill.sender_pid == os.getpid()
    assert kill.si_code == signal_trace.SI_USER and not kill.from_kernel
    assert kill.sender_cmdline and "python" in kill.sender_cmdline.lower()
    assert not tracer.active
    # Its own instance, and only its own: another run on the machine - the
    # suite's own inner pytests included - may be tracing at the same time.
    instances = Path(signal_trace.tracefs_root() or "/nonexistent") / "instances"
    if instances.is_dir() and os.access(instances, os.R_OK):
        assert f"{signal_trace.INSTANCE_PREFIX}{os.getpid()}" not in os.listdir(instances)


@needs_tracepoint
def test_a_sidecar_outlives_its_owner_only_long_enough_to_record_the_kill(tmp_path):
    """The run that started the sidecar is SIGKILLed. The sidecar sees EOF on
    the pipe only its owner could write to, keeps reading the trace for a
    moment so the owner's own kill line lands, removes its instance and
    exits - with nothing for anybody to clean up after."""
    output = tmp_path / signal_trace.TRACE_FILE
    owner_source = f"""
import sys, time
from pathlib import Path
from pytest_failure_instrumentation.probes import signal_trace
tracer = signal_trace.SignalTracer(Path({str(output)!r}), elevate=True)
print(tracer.start(), tracer.process.pid, flush=True)
time.sleep(60)
"""
    owner = subprocess.Popen([sys.executable, "-c", owner_source], stdout=subprocess.PIPE, text=True)
    assert owner.stdout is not None
    how, sidecar = owner.stdout.readline().rsplit(maxsplit=1)
    assert how.endswith("tracefs"), how
    time.sleep(0.5)
    os.kill(owner.pid, signal.SIGKILL)
    assert owner.wait(timeout=10) == -signal.SIGKILL

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _running(int(sidecar)):
        time.sleep(0.1)
    assert not _running(int(sidecar)), "the sidecar must not outlive the run that started it"
    instances = Path(signal_trace.tracefs_root() or "/nonexistent") / "instances"
    if os.access(instances, os.R_OK):
        assert f"{signal_trace.INSTANCE_PREFIX}{owner.pid}" not in os.listdir(instances)
    (kill,) = signal_trace.sent_to(output, owner.pid, signal=9)
    assert kill.sender_pid == os.getpid(), "the owner's own kill is the line that matters most"


@needs_tracepoint
def test_an_instance_left_by_a_killed_sidecar_is_swept_by_the_next(tmp_path):
    root = Path(signal_trace.tracefs_root() or "/nonexistent")
    stale = root / "instances" / f"{signal_trace.INSTANCE_PREFIX}999999"
    if os.geteuid() != 0:
        pytest.skip("making a stale instance to sweep takes root")
    stale.mkdir(exist_ok=True)
    assert stale.is_dir()
    tracer = signal_trace.SignalTracer(tmp_path / signal_trace.TRACE_FILE, elevate=True)
    try:
        assert tracer.start().endswith("tracefs")
    finally:
        tracer.stop()
    assert not stale.exists(), "an instance whose owner pid is gone is nobody's"


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# -- Windows: TerminateProcess, witnessed through ETW -----------------------


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="the SDK sizes asserted are x64's")
def test_the_etw_structures_measure_what_the_windows_sdk_says():
    """The one check a Windows-only code path can get everywhere.

    ETW writes into these structures by the sizes in its own headers. One
    that measures differently here is one it writes past the end of, and the
    consumer that finds out is the sidecar, on a machine this suite may not
    have run on. ctypes lays them out the same on every x64 platform, so the
    measurement is made on all of them.
    """
    measured = {name: ctypes.sizeof(kind) for name, kind in etw_trace.STRUCTURES.items()}
    assert measured == etw_trace.EXPECTED_SIZES_X64


def test_the_provider_guid_is_laid_out_the_way_the_kernel_writes_it():
    # Mixed-endian: the first three fields little-endian, the rest as bytes.
    assert etw_trace.PROVIDER_BYTES.hex() == "1c842ae0a375a74fafc8ae09cf9b7f23"


def test_a_filetime_is_placed_on_the_wall_clock():
    # 2020-01-01T00:00:00Z as a FILETIME.
    assert etw_trace.filetime_to_epoch(132223104000000000) == pytest.approx(1577836800.0)


def test_a_windows_termination_is_read_as_a_witness(tmp_path):
    path = _trace_file(
        tmp_path,
        {
            "via": "TerminateProcess", "sender_pid": 5120, "target_pid": 4242,
            "api_status": 0, "wall": 1000.0,
            "sender_exe": "C:\\GitLab-Runner\\gitlab-runner.exe", "sender_comm": "gitlab-runner.exe",
        },
    )
    (seen,) = signal_trace.witnessed(path)
    assert seen.via == "TerminateProcess" and seen.api_status == 0 and seen.exit_code is None
    assert (seen.sender_pid, seen.sender_comm, seen.target_pid) == (5120, "gitlab-runner.exe", 4242)
    assert seen.signal == 0 and not seen.from_kernel

    found = killer.attribute(
        killer.Sources(tmp_path, trace_status="etw", run_pids=roles),
        pid=4242, exit_status=1, started_at=900.0, died_at=1000.5,
    )
    assert found.killer is not None
    assert found.killer.name == "TerminateProcess" and found.killer.exit_code == 1
    assert found.killer.sender_role == "outside this run"

    verdict, confidence, evidence = classify.of(
        death(exit_status=1, killer=found.killer, kill_sources=found.sources)
    )
    assert (verdict, confidence) == ("KILLED_BY_PROCESS", "high")
    assert any(
        "TerminateProcess was called on it by gitlab-runner.exe (pid 5120), outside this run with exit code 1"
        in line
        for line in evidence
    )


def test_a_failed_termination_is_not_attributed(tmp_path):
    _trace_file(
        tmp_path,
        {"via": "TerminateProcess", "sender_pid": 5120, "target_pid": 4242, "api_status": 0xC0000022, "wall": 1000.0},
    )
    found = killer.attribute(
        killer.Sources(tmp_path, run_pids=roles),
        pid=4242, exit_status=3, started_at=900.0, died_at=1000.5,
    )
    assert found.killer is None, "a denied API call did not terminate the victim"


def test_an_external_termination_can_choose_a_crash_shaped_code():
    verdict, _, _ = classify.of(
        death(exit_status=3221225477, killer=outside(signal=0, name="TerminateProcess", exit_code=3221225477))
    )
    assert verdict == "KILLED_BY_PROCESS"


def test_a_recovered_windows_controller_is_explained_by_its_terminator():
    verdict, _, evidence = classify.of(
        death(exit_status=None, recovered_from_run="run-dead", worker="controller",
              killer=outside(signal=0, name="TerminateProcess", exit_code=1))
    )
    assert verdict == "KILLED_BY_PROCESS"
    assert any("TerminateProcess was called on it by gitlab-runner" in line for line in evidence)


def _is_windows_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(sys.platform != "win32", reason="ETW is a Windows source")
def test_the_etw_sidecar_witnesses_a_termination(tmp_path):
    """The real thing, where the machine allows it: an ETW session on the
    audit provider, a process terminated by this one, and the line that
    names this process as the caller."""
    if not _is_windows_admin():
        pytest.skip("an ETW session needs administrator rights")
    output = tmp_path / signal_trace.TRACE_FILE
    tracer = signal_trace.SignalTracer(output)
    how = tracer.start()
    assert how == "etw", how
    try:
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        time.sleep(1.0)
        victim.terminate()  # TerminateProcess(handle, 1)
        victim.wait()
        deadline = time.monotonic() + 10
        seen: list[signal_trace.Witness] = []
        while time.monotonic() < deadline and not seen:
            seen = signal_trace.sent_to(output, victim.pid)
            time.sleep(0.1)
    finally:
        tracer.stop()
    assert seen, output.read_text(encoding="utf-8")
    kill = seen[-1]
    assert kill.via == "TerminateProcess"
    assert kill.sender_pid == os.getpid()
    assert kill.api_status == 0 and kill.exit_code is None
    assert kill.sender_exe and kill.sender_exe.lower().endswith(".exe")
    assert not tracer.active


@pytest.mark.skipif(sys.platform != "win32", reason="ETW is a Windows source")
@needs_xdist
def test_a_worker_terminated_from_outside_names_its_killer_on_windows(distributed):
    if not _is_windows_admin():
        pytest.skip("an ETW session needs administrator rights")
    distributed.pytester.makepyfile(test_sleep=SLEEPER)
    command = [
        sys.executable, "-m", "pytest", "--failure-instrumentation", "-n", "2",
        "test_sleep.py", "-p", "no:cacheprovider",
    ]
    inner = subprocess.Popen(
        command, cwd=distributed.pytester.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        victim = _worker_in(distributed, "test_sleeps")
        time.sleep(1.0)
        os.kill(victim, signal.SIGTERM)  # TerminateProcess(handle, 15) on Windows
        output, _ = inner.communicate(timeout=180)
    finally:
        if inner.poll() is None:
            inner.kill()
    death = distributed.only(distributed.incidents(), "worker_death")
    assert death.verdict == "KILLED_BY_PROCESS", output.decode("utf-8", "replace")
    assert death.killer is not None and death.killer.name == "TerminateProcess"
    assert death.killer.sender_pid == os.getpid()
    assert death.killer.exit_code == int(signal.SIGTERM)
    assert death.severity == "informational" and death.suspect_owner is None
    assert death.kill_sources is not None and death.kill_sources.signal_trace == "etw"


# -- the plugin must never be the thing that ends a run ----------------------


@needs_xdist
def test_a_witness_that_fails_to_start_costs_the_run_nothing(runner):
    """Every witness raising out of its start, in the controller of a real
    run: the run passes, the failure is a status string on the controller's
    log, and SIGTERM is not left blocked with nobody to deliver it."""
    runner.pytester.makeconftest(
        INNER_CONFTEST
        + """

import signal

from pytest_failure_instrumentation.capture import signals as witnesses
from pytest_failure_instrumentation.probes import signal_trace


def _refuse(*_args, **_kwargs):
    raise RuntimeError("no witnessing today")


signal_trace.SignalTracer.start = _refuse
witnesses.SignalWitness.start = _refuse


def pytest_sessionfinish(session):
    # The controller's own mask, after the engine has given up on the
    # witness: nothing may still be blocked.
    if hasattr(signal, "pthread_sigmask") and not hasattr(session.config, "workerinput"):
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        assert signal.SIGTERM not in blocked, blocked
"""
    )
    runner.pytester.makepyfile(test_suite="def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n")
    result = runner.pytester.runpytest_subprocess(ENABLE_FLAG, "-n", "2", "test_suite.py", timeout=180)
    result.assert_outcomes(passed=2)
    (log,) = list((runner.pytester.path / ".pytest-failures").glob("*/controller.events"))
    announced = [json.loads(line) for line in log.read_text().splitlines() if '"kill_witnesses"' in line]
    assert announced and "off: failed" in announced[0]["controller_witness"]


@linux_only
def test_a_waiting_thread_that_gives_up_says_so():
    """The engine releases the block when the witness reports failure; this
    is the report. A sigtimedwait that keeps refusing is simulated."""
    from pytest_failure_instrumentation.capture import signals as witnesses

    calls = []

    def refusing(*_args):
        calls.append(1)
        raise OSError("simulated")

    witness = witnesses.SignalWitness(lambda *a, **k: None, {int(signal.SIGTERM)}, poll_seconds=0.01)
    original = signal.sigtimedwait
    signal.sigtimedwait = refusing  # type: ignore[assignment]
    try:
        witness._wait()
    finally:
        signal.sigtimedwait = original  # type: ignore[assignment]
    assert witness.failed and len(calls) == 3


SLEEPER = """
import os, time


def test_filler():
    assert True


def test_sleeps():
    time.sleep(40)
"""


def _worker_in(runner, nodeid: str, timeout: float = 60.0) -> int:
    """The pid of the worker running ``nodeid``, read from its state slot."""
    from pytest_failure_instrumentation.capture.state import read_state

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for state in (runner.pytester.path / ".pytest-failures").glob("*/gw*.state"):
            record = read_state(state, None)
            if (record.get("nodeid") or "").endswith(nodeid) and record.get("pid"):
                return int(record["pid"])
        time.sleep(0.1)
    pytest.fail(f"no worker reached {nodeid} within {timeout}s")


@needs_tracepoint
@needs_xdist
def test_a_worker_killed_from_outside_names_its_killer(distributed):
    """The one this whole thing exists for: a SIGKILL from another process,
    which the wait status cannot explain and the tracepoint can."""
    distributed.pytester.makepyfile(test_sleep=SLEEPER)
    command = [
        sys.executable, "-m", "pytest", "--failure-instrumentation", "-n", "2",
        "-o", "failure_elevate=true", "test_sleep.py", "-p", "no:cacheprovider",
    ]
    inner = subprocess.Popen(
        command, cwd=distributed.pytester.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        victim = _worker_in(distributed, "test_sleeps")
        time.sleep(1.0)  # let its first heartbeat land, so the incident has a memory figure
        os.kill(victim, signal.SIGKILL)
        output, _ = inner.communicate(timeout=120)
    finally:
        if inner.poll() is None:
            inner.kill()
    incidents = distributed.incidents()
    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "KILLED_BY_PROCESS", output.decode("utf-8", "replace")
    assert death.severity == "informational"
    assert death.killer is not None
    assert death.killer.sender_pid == os.getpid()
    assert death.killer.sender_role == "outside this run"
    assert death.killer.sender_cmdline
    assert death.killer.sender_exe == os.readlink(f"/proc/{os.getpid()}/exe")
    assert death.suspect_owner is None, "no test did this"
    assert death.kill_sources is not None and death.kill_sources.signal_trace.endswith("tracefs")


@needs_tracepoint
@needs_xdist
def test_a_worker_that_kills_itself_is_told_apart(distributed):
    distributed.pytester.makepyfile(
        test_crash="""
        import os, signal

        def test_filler():
            assert True

        def test_kills_itself():
            os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    incidents = distributed.run("-n", "2", "-o", "failure_elevate=true", "test_crash.py", timeout=180)
    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "SELF_KILLED"
    assert death.killer is not None and death.killer.origin == "self"
    # A worker that signalled itself did so from a test, which is a lead.
    assert death.suspect_owner is not None


@linux_only
@needs_xdist
def test_a_run_stopped_without_a_witness_is_recovered_without_inventing_a_sender(distributed):
    """No privilege anywhere in this one. The controller is sent SIGTERM by
    this process, writes down who sent it, and dies of it. Its workers do not
    die: execnet sends each of them SIGINT once the controller is gone and
    they finish cleanly, which is why the next run over the directory has to
    recover the *controller* - from its marker and its own log - and say who
    stopped the run, rather than finding nothing at all."""
    distributed.pytester.makepyfile(test_sleep=SLEEPER, test_quick="def test_quick():\n    assert True\n")
    command = [
        sys.executable, "-m", "pytest", "--failure-instrumentation", "-n", "2",
        "-o", "failure_kill_trace=false", "test_sleep.py", "-p", "no:cacheprovider",
    ]
    inner = subprocess.Popen(
        command, cwd=distributed.pytester.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _worker_in(distributed, "test_sleeps")
        time.sleep(1.0)
        os.kill(inner.pid, signal.SIGTERM)
        output, _ = inner.communicate(timeout=60)
    finally:
        if inner.poll() is None:
            inner.kill()
    assert inner.returncode == -signal.SIGTERM, output.decode("utf-8", "replace")
    incidents = distributed.run("-p", "no:xdist", "test_quick.py", timeout=120)
    recovered = [
        incident for incident in distributed.of_kind(incidents, "worker_death")
        if incident.recovered_from_run
    ]
    assert [incident.worker for incident in recovered] == ["controller"], [
        (incident.worker, incident.verdict) for incident in incidents
    ]
    (controller,) = recovered
    assert controller.verdict == "UNKNOWN"
    assert controller.killer is None
    assert "without reaching session finish" in str(controller).splitlines()[0]


# -- the ladder, and what ends it -------------------------------------------


@linux_only
def test_a_kmsg_that_reads_as_empty_does_not_end_the_ladder(monkeypatch):
    """/dev/kmsg bound to /dev/null is what systemd-nspawn and a few container
    runtimes do, and it opens and reads cleanly.

    A fresh open of the real device starts at the oldest record the kernel
    still holds, so a running kernel never reads as empty: nothing is not an
    answer. Taken for one, the journal and dmesg are never tried and an OOM
    kill goes unfound while the incident reports that the log was read.
    """
    monkeypatch.setattr(kernel_log, "KMSG", "/dev/null")
    lines_read, why = kernel_log._read_kmsg()
    assert lines_read is None and why

    reading = kernel_log.read(elevate=False)
    assert reading.source != "kmsg"
    if reading.source == "unavailable":
        assert "kmsg:" in reading.detail and "journal:" in reading.detail


# -- the fleet arithmetic over a table the reader never sees ----------------


def wide_oom(rows: int, victim: int, ours: dict[int, int]) -> str:
    """A global OOM whose table is longer than KEPT_TASKS.

    ``ours`` maps a pid of the run to its RSS in pages. They are deliberately
    small: a run's workers are what falls off the end of a table trimmed to
    the heaviest rows, which is the case this exists to cover.
    """
    header = [
        "[ 1201.101000] python3 invoked oom-killer: gfp_mask=0x140dca, order=0, oom_score_adj=0",
        "[ 1201.102000] Tasks state (memory values in pages):",
        "[ 1201.102000] [  pid  ]   uid  tgid total_vm      rss pgtables_bytes swapents oom_score_adj name",
    ]
    table = []
    for index in range(rows):
        pid = 10000 + index
        table.append(
            f"[ 1201.102000] [  {pid}]  1000  {pid}   900000   900000  3276800        0             0 bystander"
        )
    for pid, pages in ours.items():
        table.append(
            f"[ 1201.102000] [   {pid}]  1000  {pid}   {pages * 2}   {pages}  3276800        0             0 python3"
        )
    return "\n".join(
        header + table + [
            "[ 1201.103000] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,"
            f"global_oom,task_memcg=/x,task=python3,pid={victim},uid=1000",
            f"[ 1201.103000] Out of memory: Killed process {victim} (python3) total-vm:8000kB, "
            "anon-rss:4000kB, file-rss:0kB, shmem-rss:0kB, UID:1000 pgtables:3200kB oom_score_adj:0",
        ]
    ) + "\n"


def test_the_fleet_figures_are_over_the_whole_table_not_the_kept_rows(monkeypatch):
    monkeypatch.setattr(kernel_log, "page_kb", lambda: 4)
    """``tasks_considered`` comes from the whole table, so the run's figures
    have to as well: two numbers drawn from different populations cannot be
    set beside each other and read as a proportion.

    The rows that fall off a table trimmed to the heaviest few are the small
    ones - which is exactly where a run's workers sit under a global OOM on a
    busy host. Dropped, the run reads as both smaller and heavier than it was.
    """
    ours = {4240: 1000, 4241: 1100, 4242: 1200}
    (kill,) = kernel_log.parse(lines(wide_oom(kernel_log.KEPT_TASKS + 50, 4242, ours)))
    assert kill.tasks_considered == kernel_log.KEPT_TASKS + 53
    assert len(kill.tasks) == kernel_log.KEPT_TASKS, "the reader's view is still trimmed"
    assert not any(task.pid in ours for task in kill.tasks), "ours are the small ones"

    record = killer._oom_record(kill, "pid", roles(), 4242, "kmsg", None)
    assert record.run_tasks == 3, "every worker the kernel weighed, kept or not"
    assert record.victim_rank == kernel_log.KEPT_TASKS + 51, "over the whole table"
    assert record.run_median_rss_mb is not None
    # An ordinary member of its run, which is the population the reader is
    # shown. Over the kept rows alone there is no run left to compare it to,
    # and the question "was this one process or the fleet" goes unanswered.
    assert record.pressure == "fleet"


def test_a_trimmed_table_still_gives_the_reader_the_heaviest_rows(monkeypatch):
    monkeypatch.setattr(kernel_log, "page_kb", lambda: 4)
    (kill,) = kernel_log.parse(lines(wide_oom(kernel_log.KEPT_TASKS + 50, 4242, {4242: 10})))
    record = killer._oom_record(kill, "pid", roles(), 4242, "kmsg", None)
    assert len(record.largest) == 3
    assert all(entry["rss_mb"] > 0 for entry in record.largest)


# -- one reading for a cascade ----------------------------------------------


def test_the_kernel_log_is_read_once_for_deaths_it_already_covers(tmp_path, monkeypatch):
    """An OOM kill takes a worker, the memory is still short, and the next
    goes. Each death forks journalctl or dmesg and parses everything it
    prints, in pytest_testnodedown, with the run waiting.

    A reading taken after a death, and opened no later than the window asked
    for, already contains everything that death could be explained by.
    """
    reads: list[float | None] = []

    def counted(since=None, elevate=False):
        reads.append(since)
        return kernel_log.KernelLogReading([], "dmesg", "0 lines")

    monkeypatch.setattr(kernel_log, "read", counted)
    sources = killer.Sources(directory=tmp_path, live=False)
    started, died = time.time() - 100, time.time()

    for _ in range(5):
        sources.kernel_log_reading(started, died)
    assert len(reads) == 1, reads

    # A death after the reading is not covered by it, and is read afresh.
    sources.kernel_log_reading(started, time.time() + 60)
    assert len(reads) == 2

    # Nor is one whose window opens earlier than the reading's did.
    sources.kernel_log_reading(started - 3600, died)
    assert len(reads) == 3


def test_a_narrowed_reading_keeps_only_the_kills_in_the_later_window():
    (kill,) = kernel_log.parse(lines(GLOBAL_OOM))
    reading = kernel_log.KernelLogReading([kill], "dmesg", "4 lines")
    assert kernel_log.narrowed(reading, kill.at - 10).kills == [kill]
    assert kernel_log.narrowed(reading, kill.at + 10).kills == []
    # The rung that answered travels with it: a narrowed reading is the same
    # reading, and the incident says which source it came from.
    assert kernel_log.narrowed(reading, None).source == "dmesg"


def test_the_trace_is_reparsed_only_when_it_has_grown(tmp_path, monkeypatch):
    """Waiting for a line to land polls twenty times a second, and the file is
    read to a 16 MB tail. Nothing new can have arrived while the size is
    unchanged, so parsing it again only costs."""
    trace = tmp_path / signal_trace.TRACE_FILE
    trace.write_text("")
    parses = []

    def counted(path):
        parses.append(path)
        return []

    monkeypatch.setattr(signal_trace, "witnessed", counted)
    monkeypatch.setattr(killer, "TRACE_SETTLE_SECONDS", 0.25)
    killer._settled(killer.Sources(directory=tmp_path, live=True), 4242)
    assert len(parses) == 1, "the file never grew, so once is all it is worth"


# -- a witness that has to outlast the signal it survived --------------------


@linux_only
def test_the_witness_keeps_waiting_after_a_signal_the_process_survived():
    """A handler installed after the block means the process does not die of
    the SIGTERM it was sent.

    The block is process-wide, so a witness that stops after one leaves the
    next stop request pending with nobody to receive it - undeliverable,
    unwitnessed, and a run that can no longer be stopped.
    """
    script = """
import json, signal, sys, time
from pytest_failure_instrumentation.capture import signals
blocked = signals.block()
# Installed *after* the block, which is the only way to get here: block()
# refuses to take a signal somebody already has a handler for.
signal.signal(signal.SIGTERM, lambda *_: None)
print(json.dumps(sorted(blocked)), flush=True)
def record(event, **fields):
    print(json.dumps({"event": event, "sender_pid": fields.get("sender_pid")}), flush=True)
signals.SignalWitness(record, blocked, poll_seconds=0.05).start()
time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    try:
        assert json.loads(child.stdout.readline()) == [int(signal.SIGTERM)]
        time.sleep(0.3)  # the waiting thread has to be inside sigtimedwait
        seen = []
        for _ in range(3):
            os.kill(child.pid, signal.SIGTERM)
            seen.append(json.loads(child.stdout.readline()))
            time.sleep(0.2)
        assert child.poll() is None, "the handler took it, so it is still running"
        assert [record["event"] for record in seen] == ["signal_received"] * 3
        assert {record["sender_pid"] for record in seen} == {os.getpid()}
    finally:
        child.kill()
        child.wait(timeout=10)
