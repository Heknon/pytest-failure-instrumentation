"""On-demand resource qualification; preserves hardware, memory and timing evidence."""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

WORK = '''
import time
from pathlib import Path
import pytest
@pytest.mark.parametrize("case", range(CASES))
def test_io(case, tmp_path):
    for step in range(5):
        path = tmp_path / str(step)
        path.write_bytes(b"x" * 4096)
        assert path.stat().st_size == 4096
        time.sleep(0.1)
    if SCAN_FILES:
        Path("tracked", str(case)).write_bytes(b"x" * 4096)
'''
CONF = '''
import json, os, sys, time
from pathlib import Path
import pytest
values = []
def memory():
    import psutil
    pid = os.getpid()
    if sys.platform == "linux":
        pid = int(Path("/proc/self/stat").read_text().split()[0])
    proc = psutil.Process(pid)
    data = {"rss_bytes": proc.memory_info().rss}
    try:
        data["uss_bytes"] = proc.memory_full_info().uss
    except (psutil.Error, AttributeError, NotImplementedError):
        pass
    return data
@pytest.fixture(scope="session", autouse=True)
def resident_baseline(request):
    blob = bytearray(WORKER_MB * 1024 * 1024)
    for offset in range(0, len(blob), 4096):
        blob[offset] = 1  # touch pages; virtual reservations are not resident RAM
    first = memory()
    yield
    who = getattr(request.config, "workerinput", {}).get("workerid", "main")
    Path("memory-" + who + ".json").write_text(json.dumps({"start": first, "finish": memory()}))
def pytest_runtest_logreport(report):
    if report.when == "call":
        values.append(report.duration)
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):
        yield
        return
    before = time.monotonic()
    samples = []
    # Only the controller reads history, once, outside the measured test calls.
    for path in sorted(Path("evidence").glob("*/resources-live/*.jsonl")):
        with path.open() as handle:
            for line in handle:
                batch = json.loads(line)
                samples.append({"collector": batch["collector"], "processes": len(batch["processes"]),
                    "workers": len([p for p in batch["processes"] if p["role"] == "worker"]),
                    "rss_sum_bytes": sum(p["metrics"].get("rss_bytes", 0) for p in batch["processes"]),
                    "host": batch["host"], "cgroup": batch["cgroup"], "files": batch["files"]})
    collector_errors = 0
    for plugin in session.config.pluginmanager.get_plugins():
        collector = getattr(plugin, "resources", None)
        if collector is not None:
            collector_errors += collector.errors
    controller_memory = memory()
    yield
    Path("result.json").write_text(json.dumps({"test_seconds": values,
        "sessionfinish_seconds": time.monotonic()-before, "resources": samples,
        "collector_errors": collector_errors, "controller_memory": controller_memory,
        "worker_memory": [json.loads(p.read_text()) for p in sorted(Path('.').glob('memory-gw*.json'))],
        "history_removed": not list(Path("evidence").glob("*/resources-live"))}))
'''


def percentile(values, p):
    return sorted(values)[min(len(values)-1, int((len(values)-1)*p))]


def environment():
    result = {"platform": platform.platform(), "python": sys.version,
              "logical_cpus": psutil.cpu_count(), "visible_ram_bytes": psutil.virtual_memory().total,
              "visible_available_bytes": psutil.virtual_memory().available,
              "versions": {}}
    from importlib.metadata import version
    result["versions"] = {name: version(name) for name in ("pytest", "pytest-xdist", "psutil")}
    if sys.platform == "linux":
        from pytest_failure_instrumentation.probes.resource_metrics import (
            cgroup_metrics,
            cgroup_paths,
        )
        values, missing = cgroup_metrics(cgroup_paths())
        result["cgroup"] = {"metrics": values, "unavailable": missing}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=80)
    parser.add_argument("--cases", type=int, default=2400)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--worker-mb", type=int, default=0, help="Resident allocation per worker in MiB")
    parser.add_argument("--scan-files", type=int, default=0, help="Prepopulate and inventory a local test tree")
    parser.add_argument("--sample-seconds", type=float, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.workers, args.cases, args.pairs) < 1 or min(args.worker_mb, args.scan_files) < 0 or args.sample_seconds < 1:
        parser.error("counts must be positive, allocation/scan size nonnegative, interval >= 1")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    hardware = environment()
    results = []
    for pair in range(args.pairs):
        for enabled in ((False, True) if pair % 2 == 0 else (True, False)):
            with tempfile.TemporaryDirectory(prefix="pfi-resource-cost-") as name:
                root = Path(name)
                (root / "test_work.py").write_text(WORK.replace("CASES", str(args.cases)).replace("SCAN_FILES", str(args.scan_files)))
                (root / "conftest.py").write_text(CONF.replace("WORKER_MB", str(args.worker_mb)))
                if args.scan_files:
                    (root / "tracked").mkdir()
                    for index in range(args.scan_files):
                        (root / "tracked" / f"baseline-{index}").write_bytes(b"x" * 128)
                command = [sys.executable, "-m", "pytest", "-q", "-n", str(args.workers),
                           "--failure-instrumentation", "--callstack-port", "0",
                           "-o", "failure_kill_trace=false", "-o", "failure_directory=evidence",
                           "-o", f"failure_resources_seconds={args.sample_seconds if enabled else 0}",
                           "-o", "failure_resources_scan_seconds=10",
                           "-o", "failure_resources_max_mb=8"]
                if enabled and args.scan_files:
                    command += ["-o", "failure_resources_roots=tracked"]
                began = time.monotonic()
                run = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                     env=os.environ.copy(), timeout=600)
                elapsed = time.monotonic() - began
                log = args.output.with_name(f"{args.output.stem}-{pair}-{int(enabled)}.log")
                log.write_text(run.stdout + run.stderr)
                if run.returncode:
                    print(f"Failed run: {log}")
                    return 1
                result = json.loads((root / "result.json").read_text())
                durations = result.pop("test_seconds")
                samples = result.pop("resources")
                result.update(enabled=enabled, elapsed_s=elapsed, tests=len(durations),
                              test_median_s=statistics.median(durations), test_p99_s=percentile(durations, .99),
                              samples=len(samples), max_workers=max((s["workers"] for s in samples), default=0),
                              max_processes=max((s["processes"] for s in samples), default=0),
                              sample_max_s=max((s["collector"]["sample_duration_s"] for s in samples), default=0),
                              sample_errors=max((s["collector"]["errors"] for s in samples), default=0),
                              sampled_rss_sum_max_bytes=max((s["rss_sum_bytes"] for s in samples), default=0),
                              last_sample=samples[-1] if samples else None)
                results.append(result)
                print(json.dumps({k: v for k, v in result.items() if k not in ("last_sample", "worker_memory")}), flush=True)
                args.output.write_text(json.dumps({"environment": hardware, "workers": args.workers,
                    "worker_mb": args.worker_mb, "scan_files": args.scan_files, "cases": args.cases,
                    "runs": results}, indent=2))
                if len(result["worker_memory"]) != args.workers:
                    return 1
                if enabled and (not samples or result["max_workers"] != args.workers
                                or result["sample_errors"] or result["collector_errors"]):
                    return 1
                if enabled and args.scan_files:
                    scans = [r for s in samples for r in s["files"].get("roots", [])]
                    if not any(r["status"] == "complete" and "new_remaining_count" in r for r in scans):
                        print("Qualification failed: no completed inventory comparison")
                        return 1
    disabled = [r["test_p99_s"] for r in results if not r["enabled"]]
    enabled = [r["test_p99_s"] for r in results if r["enabled"]]
    return int(statistics.median(enabled) - statistics.median(disabled) >= 120
               or not all(r["history_removed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
