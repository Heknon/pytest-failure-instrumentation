"""On-demand baseline/resources comparison with real xdist workers.

Example: python benchmarks/resource_cost.py --workers 80 --cases 2400 --pairs 2
This does not run on every commit. Results are synthetic, not customer p99 guarantees.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORK = '''
import time
import pytest
@pytest.mark.parametrize("case", range(CASES))
def test_io(case, tmp_path):
    for step in range(5):
        path = tmp_path / str(step)
        path.write_bytes(b"x" * 4096)
        assert path.stat().st_size == 4096
        time.sleep(0.1)
'''
CONF = '''
import json
import os
import time
from pathlib import Path
import pytest
values = []
started = time.monotonic()
def pytest_runtest_logreport(report):
    if report.when == "call":
        values.append(report.duration)
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    before = time.monotonic()
    resources = []
    for path in Path("evidence").glob("*/resources-live/*.jsonl"):
        for line in path.read_text().splitlines():
            try:
                batch = json.loads(line)
                resources.append({"collector": batch["collector"],
                                  "processes": len(batch["processes"]),
                                  "controller": [p["metrics"] for p in batch["processes"] if p["role"]=="controller"]})
            except ValueError:
                pass
    yield
    if not hasattr(session.config, "workerinput"):
        Path("result.json").write_text(json.dumps({"test_seconds": values,
            "sessionfinish_seconds": time.monotonic()-before, "resources": resources,
            "history_removed": not list(Path("evidence").glob("*/resources-live"))}))
'''


def percentile(values, p):
    return sorted(values)[min(len(values)-1, int((len(values)-1)*p))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=80)
    parser.add_argument("--cases", type=int, default=2400)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for pair in range(args.pairs):
        for enabled in ((False, True) if pair % 2 == 0 else (True, False)):
            with tempfile.TemporaryDirectory(prefix="pfi-resource-cost-") as name:
                root = Path(name)
                (root / "test_work.py").write_text(WORK.replace("CASES", str(args.cases)))
                (root / "conftest.py").write_text(CONF)
                command = [sys.executable, "-m", "pytest", "-q", "-n", str(args.workers),
                           "--failure-instrumentation", "--callstack-port", "0",
                           "-o", "failure_kill_trace=false", "-o", "failure_directory=evidence",
                           "-o", f"failure_resources_seconds={5 if enabled else 0}"]
                began = time.monotonic()
                run = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                     env=os.environ.copy(), timeout=600)
                elapsed = time.monotonic() - began
                if run.returncode:
                    print(run.stdout[-4000:], run.stderr[-4000:])
                    return 1
                result = json.loads((root / "result.json").read_text())
                durations = result.pop("test_seconds")
                samples = result.pop("resources")
                result.update(enabled=enabled, elapsed_s=elapsed, tests=len(durations),
                              test_median_s=statistics.median(durations), test_p99_s=percentile(durations, .99),
                              samples=len(samples), max_processes=max((s["processes"] for s in samples), default=0),
                              sample_max_s=max((s["collector"]["sample_duration_s"] for s in samples), default=0),
                              sample_errors=max((s["collector"]["errors"] for s in samples), default=0))
                results.append(result)
                print(json.dumps(result), flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({"workers": args.workers, "cases": args.cases,
                                                   "runs": results}, indent=2))
                if enabled and (not samples or result["max_processes"] < args.workers or result["sample_errors"]):
                    print("Qualification failed: insufficient worker sampling or collector errors", flush=True)
                    return 1
    disabled = [r["test_p99_s"] for r in results if not r["enabled"]]
    enabled = [r["test_p99_s"] for r in results if r["enabled"]]
    return int(statistics.median(enabled) - statistics.median(disabled) >= 120
               or not all(r["history_removed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
