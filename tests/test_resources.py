"""Live resources: retention, real filesystem changes, lifecycle and wire contracts."""
from __future__ import annotations

import asyncio
import os
import platform
import sys
import time
from pathlib import Path

import psutil
import pytest

from pytest_failure_instrumentation.capture.file_resources import Scanner
from pytest_failure_instrumentation.capture.resource_history import ResourceHistory, read_history
from pytest_failure_instrumentation.config import Settings
from pytest_failure_instrumentation.probes.resource_metrics import (
    PlatformMetrics,
    cgroup_metrics,
    cgroup_paths,
)
from pytest_failure_instrumentation.resource_sampling import ResourceSampler


def current_process():
    pid = os.getpid()
    if sys.platform == "linux":
        pid = int(Path("/proc/self/stat").read_text().split(" ", 1)[0])
    return psutil.Process(pid)


def history(path: Path, budget=8 * 1024 * 1024):
    owner = current_process()
    return ResourceHistory(path, {"session": path.name, "controller_pid": owner.pid,
                                  "controller_created_at": owner.create_time(), "started_at": time.time(),
                                  "sample_seconds": 5}, budget)


def batch(stamp=1, padding=""):
    return {"observed_at": stamp, "elapsed_s": stamp, "host": {"metrics": {}, "unavailable": {}},
            "cgroup": {"metrics": {}, "unavailable": {}},
            "processes": [{"pid": 1, "created_at": 1, "worker": "gw0", "metrics": {"rss_bytes": 10}},
                          {"pid": 2, "created_at": 2, "worker": "gw1", "metrics": {"rss_bytes": 20}}],
            "events": [], "padding": padding}


def test_history_rotates_and_pages_without_merging_workers(tmp_path):
    store = history(tmp_path)
    for index in range(30):
        store.append(batch(index, "x" * 400_000))
    data = read_history(tmp_path, after=1, limit=2, worker="gw0")
    assert data["history_bytes"] <= store.max_bytes
    assert data["history_truncated"] and data["cursor_expired"]
    assert data["has_more"]
    assert len(data["batches"]) == 2
    assert all([p["worker"] for p in b["processes"]] == ["gw0"] for b in data["batches"])
    next_page = read_history(tmp_path, after=data["next_after"], limit=2)
    assert next_page["batches"][0]["sequence"] == data["next_after"] + 1
    assert read_history(tmp_path, latest=True)["batches"][0]["sequence"] == 30
    store.close()
    assert not (tmp_path / "resources-live").exists()


def test_history_truncated_tail_and_pid_reuse(tmp_path):
    store = history(tmp_path)
    store.append(batch())
    path = next(store.directory.glob("*.jsonl"))
    with path.open("ab") as stream:
        stream.write(b'{"observed_at":')
    assert len(read_history(tmp_path)["batches"]) == 1
    store.metadata["controller_created_at"] -= 1
    store._publish()
    with pytest.raises(FileNotFoundError):
        read_history(tmp_path)


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 501}, {"after": -1},
                                    {"start": float("nan")}, {"start": 5, "end": 4}])
def test_invalid_queries(tmp_path, kwargs):
    with pytest.raises(ValueError):
        read_history(tmp_path, **kwargs)


def test_partial_directory_scan_does_not_claim_deletions(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "existing").write_bytes(b"a" * 10)
    updates = []
    scanner = Scanner(root, tmp_path / "index.sqlite", 10, tmp_path / "excluded", updates.append)
    try:
        first = scanner.scan()
        assert first["status"] == "complete"
        assert first["baseline"]["logical_bytes"] == 10
        (root / "existing").write_bytes(b"a" * 15)
        (root / "new").write_bytes(b"a" * 7)
        second = scanner.scan()
        assert second["new_remaining_count"] == 1
        assert second["new_remaining_bytes"] == 7
        assert second["grown_existing_bytes"] == 5
        assert second["net_logical_bytes"] == 12
        (root / "existing").unlink()
        assert scanner.scan()["deleted_baseline_count"] == 1
        for index in range(20):
            (root / f"more-{index}").touch()
        partial = scanner.scan()
        assert partial["status"] == "partial"
        assert "deleted_baseline_count" not in partial
        assert partial["observed_file_count"] <= 10
    finally:
        scanner.close()


def test_scanner_excludes_own_evidence_and_links(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    excluded = root / "evidence"
    excluded.mkdir()
    (excluded / "big").write_bytes(b"x" * 100)
    (root / "normal").write_bytes(b"ok")
    try:
        (root / "loop").symlink_to(root, target_is_directory=True)
    except OSError:
        pass  # Windows without symlink privilege still tests evidence exclusion.
    scanner = Scanner(root, tmp_path / "index.sqlite", 100, excluded, lambda value: None)
    try:
        result = scanner.scan()
        assert result["observed_file_count"] == 1
        assert result["observed_logical_bytes"] == 2
    finally:
        scanner.close()


def test_cgroup_mount_root_resolution_and_metrics(tmp_path):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    mount = tmp_path / "cgroup"
    root = mount / "job"
    root.mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/tenant/job\n")
    (proc / "self/mountinfo").write_text(f"1 0 0:1 /tenant {mount} rw - cgroup2 cgroup rw\n")
    assert cgroup_paths(proc) == {"v2": root}
    for name, value in {"memory.current": "1024", "memory.max": "max",
                        "cpu.max": "200000 100000", "memory.events": "oom_kill 2\n",
                        "cpu.stat": "nr_throttled 3\n"}.items():
        (root / name).write_text(value)
    values, missing = cgroup_metrics({"v2": root})
    assert values["memory_current_bytes"] == 1024
    assert values["memory_limit_bytes"] is None
    assert values["cpu_quota_cores"] == 2
    assert values["memory_events_oom_kill"] == 2
    assert "memory_peak_bytes" in missing
    # A namespaced membership root resolves to the mounted subtree itself.
    (proc / "self/cgroup").write_text("0::/\n")
    assert cgroup_paths(proc) == {"v2": mount}


def test_rates_use_elapsed_time_and_reset_without_negative_values():
    probe = PlatformMetrics()
    try:
        first = {"cpu_total_seconds": 10, "read_total_bytes": 100}
        probe.rates("p", first, 1)
        assert first["cpu_cores"] is None
        second = {"cpu_total_seconds": 14, "read_total_bytes": 200}
        probe.rates("p", second, 3)
        assert second["cpu_cores"] == 2
        assert second["read_per_second_bytes"] == 50
        reset = {"cpu_total_seconds": 1, "read_total_bytes": 1}
        probe.rates("p", reset, 4)
        assert reset["cpu_cores"] is None
        assert reset["read_per_second_bytes"] is None
    finally:
        probe.close()


def test_real_platform_counter_smoke():
    probe = PlatformMetrics()
    try:
        values, missing = probe.host()
        assert values["ram_total_bytes"] > 0
        assert 0 <= values["ram_available_bytes"] <= values["ram_total_bytes"]
        values, missing = probe.process(current_process())
        assert values["rss_bytes"] > 0
        assert values["cpu_total_seconds"] >= 0
        if platform.system() == "Windows":
            values, missing = probe.host()
            assert "commit_bytes" in values, missing
            assert "kernel_nonpaged_bytes" in values
        if platform.system() == "Darwin":
            assert "physical_footprint_bytes" in values, missing
            assert "read_total_bytes" in values, missing
            host, missing = probe.host()
            assert "compressed_bytes" in host, missing
    finally:
        probe.close()


def test_sampler_records_while_running_and_deletes_only_resources(tmp_path):
    sentinel = tmp_path / "gw0.events"
    sentinel.write_text("incident evidence")
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    try:
        sampler.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = read_history(tmp_path)
            if len(result["batches"]) >= 2:
                break
            time.sleep(0.05)
        assert len(result["batches"]) >= 2, sampler.last_error
        assert any(p["role"] == "controller" for p in result["batches"][-1]["processes"])
        assert not result["batches"][-1]["collector"]["errors"]
    finally:
        sampler.close()
    assert sentinel.read_text() == "incident evidence"
    assert not (tmp_path / "resources-live").exists()
    assert sampler.helper is None or sampler.helper.poll() is not None
    sampler.close()  # pytest's cleanup fallback is deliberately idempotent.


def test_resource_configuration_is_controller_only():
    configured = Settings(resources_seconds=0.01, resources_roots="output", resources_max_mb=1)
    assert configured.resources_seconds == 1
    assert configured.resources_max_mb == 8
    assert configured.resources_roots == (str(Path("output").absolute()),)
    assert Settings().resources_seconds == 0
    assert Settings.from_payload(configured.as_payload()).resources_seconds == 0
    assert Settings(resources_seconds=float("nan")).resources_seconds == 0


def test_resource_endpoint_uses_existing_auth_and_typed_client(tmp_path):
    pytest.importorskip("httpx")
    from pytest_failure_instrumentation import stack_server
    from pytest_failure_instrumentation.client import (
        AuthenticationRequired,
        BadRequest,
        FailureServerClient,
        NotFound,
    )
    directory = tmp_path / "run"
    directory.mkdir()
    store = history(directory)
    store.append(batch(time.time()))
    server = stack_server.StackService(0, directory=directory, token="secret")
    server.start()
    try:
        deadline = time.monotonic() + 10
        while not server.serving and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.serving, server.status
        async def ask():
            async with FailureServerClient(url=server.url, token="secret") as client:
                page = await client.resources("run", worker="gw0")
                assert len(page.batches) == 1
                assert [p.worker for p in page.batches[0].processes] == ["gw0"]
                with pytest.raises(BadRequest):
                    await client.resources("run", limit=0)
                with pytest.raises(NotFound):
                    await client.resources("../run")
                store.close()
                with pytest.raises(NotFound):
                    await client.resources("run")
            async with FailureServerClient(url=server.url) as client:
                with pytest.raises(AuthenticationRequired):
                    await client.resources("run")
        asyncio.run(ask())
    finally:
        server.stop()


@pytest.mark.parametrize("workers", [0, 2])
def test_pytest_lifecycle_live_only_and_no_worker_collectors(pytester, workers):
    if workers:
        pytest.importorskip("xdist")
    pytester.makepyfile('''
import json
import time
from pathlib import Path

def test_live(request):
    root = Path(request.config.rootpath) / "evidence"
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        manifests = list(root.glob("*/resources-live/manifest.json"))
        if manifests:
            from pytest_failure_instrumentation.capture.resource_history import read_history
            page = read_history(manifests[0].parent.parent, latest=True)
            if page["batches"] and any(p["role"] == "worker" for p in page["batches"][0]["processes"]):
                assert len(manifests) == 1
                return
        time.sleep(0.05)
    assert False, "no worker resource sample reached live storage"
''')
    # Parent environment's explicit source path must reach spawned interpreters.
    command = ["--failure-instrumentation", "-o", "failure_resources_seconds=1",
               "-o", "failure_kill_trace=false", "-o", "failure_directory=evidence"]
    if workers:
        command += ["-n", str(workers)]
    result = pytester.runpytest_subprocess(*command, timeout=30)
    result.assert_outcomes(passed=1)
    assert not list((pytester.path / "evidence").glob("*/resources-live"))
    assert list((pytester.path / "evidence").glob("*/*.events"))


def test_default_path_never_imports_resource_sampler(pytester):
    pytester.makepyfile('''
import sys

def test_default():
    assert "pytest_failure_instrumentation.resource_sampling" not in sys.modules
    assert "pytest_failure_instrumentation.probes.resource_metrics" not in sys.modules
''')
    result = pytester.runpytest_subprocess("--failure-instrumentation", "-o", "failure_kill_trace=false")
    result.assert_outcomes(passed=1)


def test_disk_latency_is_an_interval_average_and_reset_is_missing():
    probe = PlatformMetrics()
    try:
        probe.rates("disk:x", {"read_time_ms": 100, "read_total_count": 20}, 1)
        values = {"read_time_ms": 160, "read_total_count": 30}
        probe.rates("disk:x", values, 2)
        assert values["read_latency_ms"] == 6
        values = {"read_time_ms": 1, "read_total_count": 1}
        probe.rates("disk:x", values, 3)
        assert values["read_latency_ms"] is None
    finally:
        probe.close()


def test_pid_reuse_does_not_inherit_worker_identity(tmp_path, monkeypatch):
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    try:
        worker = {"pid": 99999999, "created_at": 1.0, "name": "old", "parent_pid": 0,
                  "worker": "gw0", "role": "worker"}
        sampler.tracked = {(worker["pid"], 1.0): worker}
        sampler.inventory_at = time.monotonic()
        original = psutil.Process
        class Reused:
            def create_time(self):
                return 2.0
        monkeypatch.setattr(psutil, "Process", lambda pid=None: Reused() if pid == 99999999 else original(pid))
        result = sampler.sample()
        assert all(p["pid"] != 99999999 for p in result["processes"])
        assert any(e["kind"] == "process_no_longer_observed" and e["worker"] == "gw0" for e in result["events"])
    finally:
        sampler.close()


def test_rotation_does_not_forget_a_segment_when_windows_reader_holds_it(tmp_path, monkeypatch):
    store = history(tmp_path)
    for index in range(20):
        store.append(batch(index, "x" * 400_000))
    oldest = store.directory / store.segments[0]["file"]
    original = Path.unlink
    def held(path, *args, **kwargs):
        if path == oldest:
            raise PermissionError("open reader")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", held)
        with pytest.raises(PermissionError):
            for index in range(20, 30):
                store.append(batch(index, "x" * 400_000))
    assert oldest.name in [s["file"] for s in store.segments]
    store.append(batch(31, "x" * 400_000))
    assert not oldest.exists()
    assert sum(p.stat().st_size for p in store.directory.glob("*.jsonl")) <= store.max_bytes


def test_timestamps_can_move_backwards_without_losing_range_matches(tmp_path):
    store = history(tmp_path)
    store.append(batch(100))
    store.append(batch(80))
    store.append(batch(120))
    result = read_history(tmp_path, start=75, end=90)
    assert [b["observed_at"] for b in result["batches"]] == [80]
