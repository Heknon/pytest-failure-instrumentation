"""Explain instrumentation costs separately from the unprofiled timing gate."""

from __future__ import annotations

import json
import os
import pstats
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from profile_gate import WORKLOAD

PARALLEL_TIMING = '''
import json
import os
import time
from pathlib import Path
import pytest

started = time.perf_counter()
cases = []
phases = {}

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
    path.write_text(json.dumps({"worker": worker, "phases": phases, "cases": cases}), encoding="utf-8")
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
        for trial, mode in enumerate(("baseline", "profile", "profile", "baseline")):
            destination = output / f"parallel-{trial}-{mode}"
            destination.mkdir(exist_ok=True)
            command = [sys.executable, "-m", "pytest", "-q", "-n", "4",
                       "-p", "no:cacheprovider", "-o", f"failure_directory={destination / 'evidence'}",
                       "-o", "failure_packages=test_work",
                       "-o", "failure_profile_cpu_floor_seconds=0.25",
                       "test_work.py"]
            if mode == "profile":
                command.append("--failure-profile")
            started = time.perf_counter()
            completed = subprocess.run(command, cwd=root, check=False, capture_output=True, text=True,
                                       env={**os.environ, "PYTHONHASHSEED": "0", "PFI_BENCH_CASE_SECONDS": "0.40",
                                            "PFI_COST_OUTPUT": str(destination)})
            elapsed = time.perf_counter() - started
            (destination / "pytest.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
            if completed.returncode:
                return completed.returncode
            workers = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(destination.glob("gw*.json"))]
            print(json.dumps({"mode": mode, "elapsed": elapsed, "workers": workers}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
