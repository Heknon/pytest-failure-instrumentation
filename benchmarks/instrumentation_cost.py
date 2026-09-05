"""Explain instrumentation costs separately from the unprofiled timing gate."""

from __future__ import annotations

import json
import os
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from profile_gate import WORKLOAD, _run


def compare_master(root: Path, output: Path, baseline: Path, repeats: int = 5) -> dict:
    """Compare exact sources on one runner, including tracing's marginal cost."""
    current = Path(__file__).resolve().parents[1]
    modes = {
        "master_disabled": (baseline, "baseline", ""),
        "branch_disabled": (current, "baseline", ""),
        "master_profile": (baseline, "profile", ""),
        "branch_profile": (current, "profile", ""),
        "branch_profile_trace_off": (current, "profile", "-o failure_kill_trace=false"),
    }
    # Entry points are installed from the branch. Verify PYTHONPATH actually
    # selects the requested source in every controller and worker process.
    (root / "conftest.py").write_text('''
import os
from pathlib import Path
import pytest_failure_instrumentation as package
assert Path(package.__file__).resolve().is_relative_to(Path(os.environ["PFI_EXPECTED_SOURCE"]))
''', encoding="utf-8")
    destination = output / "master-comparison"
    destination.mkdir(exist_ok=True)
    timings: dict[str, list[float]] = {name: [] for name in modes}

    def run(name: str, trial: str) -> float:
        checkout, mode, options = modes[name]
        source = str((checkout / "src").resolve())
        with patch.dict(os.environ, {
            "PYTHONPATH": source, "PFI_EXPECTED_SOURCE": source,
            "PYTEST_ADDOPTS": options,
        }):
            elapsed, logs = _run(root, mode, workers=4)
        (destination / f"{trial}-{name}.log").write_text(logs, encoding="utf-8")
        return elapsed

    for name in modes:
        run(name, "warmup")
    names = tuple(modes)
    for repetition in range(repeats):
        # Rotate and reverse the order so one variant does not always get
        # the same position relative to runner load or filesystem caches.
        order = names[repetition % len(names):] + names[:repetition % len(names)]
        if repetition % 2:
            order = tuple(reversed(order))
        for name in order:
            timings[name].append(run(name, str(repetition)))
    medians = {name: statistics.median(values) for name, values in timings.items()}
    result = {
        "master_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=baseline, text=True).strip(),
        "branch_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=current, text=True).strip(),
        "seconds": timings,
        "median_seconds": medians,
        "branch_vs_master_profile_ratio": medians["branch_profile"] / medians["master_profile"],
        "master_profile_overhead_ratio": medians["master_profile"] / medians["master_disabled"],
        "branch_profile_overhead_ratio": medians["branch_profile"] / medians["branch_disabled"],
        "tracing_marginal_seconds": medians["branch_profile"] - medians["branch_profile_trace_off"],
    }
    (destination / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return result

PARALLEL_TIMING = '''
import json
import os
import time
from pathlib import Path
import pytest

started = time.perf_counter()
cases = []
phases = {}
costs = {}

def measure(cls, name):
    original = getattr(cls, name)
    key = cls.__name__ + "." + name
    def measured(*args, **kwargs):
        wall = time.perf_counter()
        cpu = time.thread_time()
        try:
            return original(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - wall
            burnt = time.thread_time() - cpu
            row = costs.setdefault(key, [0, 0.0, 0.0])
            row[0] += 1
            row[1] += elapsed
            row[2] += burnt
    setattr(cls, name, measured)

if os.environ.get("PFI_COST_METHODS"):
    from pytest_failure_instrumentation.profile import sampler
    from pytest_failure_instrumentation.capture.recorder import WorkerRecorder
    for cls, methods in ((sampler._WindowsThreads, ("read",)),
                         (sampler.ThreadClock, ("__init__", "discover")),
                         (sampler._MachineClock, ("__init__", "busy_permille")),
                         (sampler.Sampler, ("start", "_tick", "_record_of")),
                         (WorkerRecorder, ("_open", "_start_profiler", "_start_monitors"))):
        for name in methods:
            measure(cls, name)

def pytest_sessionstart(session):
    phases["session_start"] = time.perf_counter() - started

def pytest_runtest_logstart(nodeid, location):
    phases.setdefault("tests_start", time.perf_counter() - started)

def pytest_runtest_logreport(report):
    if report.when == "call":
        cases.append({"nodeid": report.nodeid, "seconds": report.duration})

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    phases["finish_start"] = time.perf_counter() - started
    yield
    phases["finish_end"] = time.perf_counter() - started
    worker = getattr(session.config, "workerinput", {}).get("workerid", "controller")
    path = Path(os.environ["PFI_COST_OUTPUT"]) / (worker + ".json")
    path.write_text(json.dumps({"worker": worker, "phases": phases, "cases": cases, "costs": costs}), encoding="utf-8")
'''


def main() -> int:
    output = Path("readiness").resolve()
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="failure-instrumentation-cost-") as temporary:
        root = Path(temporary)
        (root / "test_work.py").write_text(WORKLOAD, encoding="utf-8")
        for mode in ("baseline", "instrumentation"):
            stats_path = output / f"{mode}.pstats"
            command = [
                sys.executable, "-m", "cProfile", "-o", str(stats_path),
                "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "-o", f"failure_directory={root / 'evidence'}",
                "test_work.py",
            ]
            if mode == "instrumentation":
                command.append("--failure-instrumentation")
            completed = subprocess.run(
                command, cwd=root, check=False,
                env={**os.environ, "PYTHONHASHSEED": "0", "PFI_BENCH_CASE_SECONDS": "0.20"},
            )
            if completed.returncode:
                return completed.returncode
            with (output / f"{mode}-cost.txt").open("w", encoding="utf-8") as stream:
                stats = pstats.Stats(str(stats_path), stream=stream)
                stats.sort_stats("cumulative").print_stats(50)
                stats.print_stats("pytest_failure_instrumentation", 50)
                stats.print_callees("pytest_failure_instrumentation.*(pytest_sessionfinish|_open|capabilities)")
            print((output / f"{mode}-cost.txt").read_text(encoding="utf-8"), flush=True)
        (root / "conftest.py").write_text(PARALLEL_TIMING, encoding="utf-8")
        for trial, mode in enumerate(("baseline", "profile", "profile", "baseline", "profile-methods")):
            destination = output / f"parallel-{trial}-{mode}"
            destination.mkdir(exist_ok=True)
            command = [sys.executable, "-m", "pytest", "-q", "-n", "4",
                       "-p", "no:cacheprovider", "-o", f"failure_directory={destination / 'evidence'}",
                       "-o", "failure_packages=test_work",
                       "-o", "failure_profile_cpu_floor_seconds=0.25",
                       "test_work.py"]
            if mode.startswith("profile"):
                command.append("--failure-profile")
            started = time.perf_counter()
            completed = subprocess.run(command, cwd=root, check=False, capture_output=True, text=True,
                                       env={**os.environ, "PYTHONHASHSEED": "0", "PFI_BENCH_CASE_SECONDS": "0.40",
                                            "PFI_COST_OUTPUT": str(destination),
                                            "PFI_COST_METHODS": "1" if mode == "profile-methods" else ""})
            elapsed = time.perf_counter() - started
            (destination / "pytest.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
            if completed.returncode:
                return completed.returncode
            workers = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(destination.glob("gw*.json"))]
            print(json.dumps({"mode": mode, "elapsed": elapsed, "workers": workers}), flush=True)
        if baseline := os.environ.get("PFI_BASELINE_ROOT"):
            compare_master(root, output, Path(baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
