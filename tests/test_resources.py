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


def test_settings_keeps_all_existing_positional_arguments():
    from dataclasses import fields

    defaults = Settings()
    legacy = [field.name for field in fields(Settings) if not field.name.startswith("resources_")]
    assert [field.name for field in fields(Settings)][:len(legacy)] == legacy
    values = [getattr(defaults, name) for name in legacy]
    values[legacy.index("stack_server")] = True
    values[legacy.index("stack_server_port")] = 4321
    restored = Settings(*values)
    assert restored.stack_server is True
    assert restored.stack_server_port == 4321
    assert restored.resources_seconds == 0


def test_manifest_sharing_violations_are_retried_and_bounded(tmp_path, monkeypatch):
    from pytest_failure_instrumentation.capture import resource_history as module

    store = history(tmp_path)
    original_replace = os.replace
    original_read = Path.read_text
    calls = {"replace": 0, "read": 0}

    def replace(source, target):
        calls["replace"] += 1
        if calls["replace"] == 1:
            raise PermissionError("sharing violation")
        return original_replace(source, target)

    def read(path, *args, **kwargs):
        if path.name == "manifest.json":
            calls["read"] += 1
            if calls["read"] == 1:
                raise PermissionError("sharing violation")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(Path, "read_text", read)
    store.append(batch())
    assert read_history(tmp_path)["batches"][0]["sequence"] == 1
    assert calls == {"replace": 2, "read": 2}
    attempts = []

    def denied():
        attempts.append(1)
        raise PermissionError("persistent denial")

    with pytest.raises(PermissionError, match="persistent denial"):
        module._sharing_retry(denied)
    assert len(attempts) == 4


def test_real_sampler_round_trips_through_typed_http_client(tmp_path):
    pytest.importorskip("httpx")
    from pytest_failure_instrumentation.client import FailureServerClient
    from pytest_failure_instrumentation.stack_server import StackService

    directory = tmp_path / "run"
    directory.mkdir()
    sampler = ResourceSampler(directory, "run", Settings(resources_seconds=1))
    server = StackService(0, directory=directory, token="secret")
    try:
        written = sampler.sample()
        sampler.history.append(written)
        server.start()
        deadline = time.monotonic() + 10
        while not server.serving and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.serving

        async def ask():
            async with FailureServerClient(url=server.url, token="secret") as client:
                response = await client.resources("run", latest=True)
                assert response.product_version is None
                assert len(response.batches) == 1
                for raw, typed in zip(written["processes"], response.batches[0].processes):
                    assert set(raw) <= set(type(typed).model_fields)
                    assert typed.first_observed_at == raw["first_observed_at"]
        asyncio.run(ask())
    finally:
        server.stop()
        sampler.close()


def test_events_survive_failed_append_and_concurrent_arrival(tmp_path, monkeypatch):
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    original = sampler.history.append
    attempted = []
    sampler.event({"kind": "incident", "worker": "gw0"})

    def append(batch):
        attempted.append(batch)
        if len(attempted) == 1:
            sampler.event({"kind": "arrived_during_write"})
            raise OSError("disk full")
        original(batch)
        sampler.event({"kind": "arrived_after_commit"})
        sampler.stop_event.set()

    monkeypatch.setattr(sampler.history, "append", append)
    try:
        sampler._run()
        assert len(attempted) == 2
        kinds = [e["kind"] for e in attempted[1]["events"]]
        assert "incident" in kinds and "arrived_during_write" in kinds
        assert [e["kind"] for e in sampler.events] == ["arrived_after_commit"]
        assert sampler.errors == 1
    finally:
        sampler.close()


def test_oversized_and_invalid_file_snapshot_are_explicit(tmp_path):
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    try:
        path = sampler.history.directory / "files.json"
        path.write_bytes(b"x" * (256 * 1024 + 1))
        assert sampler.sample()["files"]["status"] == "inventory_too_large"
        path.write_text("{")
        assert sampler.sample()["files"]["status"] == "unavailable"
    finally:
        sampler.close()


def test_worker_displaces_descendant_at_tracking_budget(tmp_path, monkeypatch):
    import pytest_failure_instrumentation.resource_sampling as module
    monkeypatch.setattr(module, "MAX_PROCESSES", 2)
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    try:
        assert sampler._track((123, 1), {"name": "child", "ppid": 1}, "gw0", "descendant")
        assert sampler._track((124, 2), {"name": "worker", "ppid": 1}, "gw1", "worker")
        assert (123, 1) not in sampler.tracked
        assert len(sampler.tracked) == 2
    finally:
        sampler.close()


def test_unmapped_worker_does_not_force_repeated_inventory(tmp_path):
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    try:
        sampler.foreign_procfs = True
        # Use the existing wire format without a process in the visible namespace.
        import json
        (tmp_path / "gw0.state").write_text(json.dumps({"pid": 987654321, "time": time.time()}))
        sampler.inventory_at = 42
        sampler._workers()
        assert sampler.inventory_at == 0
        sampler.inventory_at = 43
        sampler._workers()
        assert sampler.inventory_at == 43
    finally:
        sampler.close()


def test_documented_inventory_budget_holds_long_paths(tmp_path):
    scanner = Scanner(tmp_path, tmp_path / "index.sqlite", 50000, tmp_path / "excluded", lambda _: None)
    try:
        scanner.db.executemany("INSERT INTO current VALUES (?, ?)",
                              (("nested/" + "x" * 200 + str(i), i) for i in range(50000)))
        scanner.db.execute("INSERT INTO baseline SELECT * FROM current")
        scanner.db.commit()
        assert scanner.db.execute("SELECT COUNT(*) FROM baseline").fetchone()[0] == 50000
        assert (tmp_path / "index.sqlite").stat().st_size <= scanner.max_bytes
    finally:
        scanner.close()


def test_repeated_write_failure_stops_and_reports_reason(tmp_path, monkeypatch, capsys):
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    calls = []
    def fail(batch):
        calls.append(batch)
        raise OSError("disk full")
    monkeypatch.setattr(sampler.history, "append", fail)
    monkeypatch.setattr(sampler.stop_event, "wait", lambda _: False)
    try:
        sampler._run()
        assert len(calls) == 5
        assert read_history(tmp_path)["collection_status"] == "stopped_after_errors"
        assert "resource collection stopped" in capsys.readouterr().err
        assert sampler.events  # unacknowledged evidence remains bounded
    finally:
        sampler.close()


def test_file_snapshots_deduplicate_without_changing_paged_wire_contract(tmp_path):
    import json
    store = history(tmp_path)
    files = {"roots": [{"path": "x" * 2000}], "volumes": []}
    try:
        for i in range(30):
            value = batch(i, "p" * 400_000)
            value["files"] = files
            store.append(value)
        records = [json.loads(line) for p in store.directory.glob("*.jsonl") for line in p.read_text().splitlines()]
        assert any("_files_sequence" in r for r in records)
        page = read_history(tmp_path, after=20, limit=1)
        assert page["batches"][0]["files"] == files
        assert "_files_sequence" not in page["batches"][0]
        assert read_history(tmp_path, latest=True)["batches"][0]["files"] == files
        assert read_history(tmp_path, start=28)["batches"][0]["files"] == files
        assert read_history(tmp_path)["history_truncated"]
    finally:
        store.close()


def test_resource_preflight_is_explicit_and_reports_process_io():
    from pytest_failure_instrumentation import probes
    assert "resource_process_io" not in probes.capabilities()
    result = probes.capabilities(resources=True)
    assert "resource_preflight_error" not in result
    assert isinstance(result["resource_process_io"]["supported"], bool)
    assert result["resource_native"]["system"]


def test_bounded_discovery_prioritizes_run_identities(tmp_path, monkeypatch):
    import pytest_failure_instrumentation.resource_sampling as module
    sampler = ResourceSampler(tmp_path, "run", Settings(resources_seconds=1))
    try:
        monkeypatch.setattr(module, "MAX_INVENTORY", 1)
        monkeypatch.setattr(module.psutil, "pids", lambda: [1, 2, sampler.owner.pid])
        sampler._discover({}, time.monotonic())
        assert [row["pid"] for row in sampler.inventory] == [sampler.owner.pid]
        assert sampler.inventory_status["processes"] == "inventory_truncated"
    finally:
        sampler.close()
