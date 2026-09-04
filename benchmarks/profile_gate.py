"""Repeatable production gate for profiler detection and wall-time overhead.

Run from an environment in which this checkout is installed::

    python benchmarks/profile_gate.py

The workload consumes a fixed amount of *test-thread CPU*, so a busy runner
may make every run slower without making the profiled run look artificially
better. Modes are interleaved and compared by median to reduce ordering noise.
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
from typing import Any

INSTRUMENTATION_BUDGET = 1.05
PROFILE_BUDGET = 1.10
XDIST_PROFILE_BUDGET = 1.12
DETECTION_TRIALS = 5
QUIET_TRIALS = 5

WORKLOAD = '''
import os
import time
import pytest


def burn_cpu(seconds):
    deadline = time.thread_time() + seconds
    value = 1
    while time.thread_time() < deadline:
        value = (value * 1664525 + 1013904223) & 0xffffffff
    return value


@pytest.mark.parametrize("case", range(20))
def test_cpu_work(case):
    assert burn_cpu(float(os.environ.get("PFI_BENCH_CASE_SECONDS", "0.20"))) >= 0
'''

QUIET_WORKLOAD = '''
import time
import pytest


@pytest.mark.parametrize("case", range(20))
def test_waiting_is_not_cpu(case):
    time.sleep(0.01)
'''


def _run(root: Path, mode: str, workers: int = 0, target: str = "test_work.py") -> tuple[float, str]:
    evidence = root / f"evidence-{mode}-{workers}-{time.monotonic_ns()}"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        f"failure_directory={evidence}",
        "-o",
        "failure_packages=test_work",
        "-o",
        "failure_profile_cpu_floor_seconds=0.25",
    ]
    if workers:
        command.extend(["-n", str(workers)])
    if mode == "instrumentation":
        command.append("--failure-instrumentation")
    elif mode == "profile":
        command.append("--failure-profile")
    command.append(target)
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONHASHSEED": "0",
            # Four workers otherwise turn the four-CPU-second serial workload
            # into a roughly one-second run whose fixed pytest startup and
            # report costs dominate the ratio. Two seconds per worker is long
            # enough to measure sustained sampling rather than startup.
            "PFI_BENCH_CASE_SECONDS": "0.40" if workers else "0.20",
        },
    )
    elapsed = time.perf_counter() - started
    output = completed.stdout + completed.stderr
    if completed.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({completed.returncode}):\n{output}")
    return elapsed, output


def _measure(root: Path, modes: tuple[str, ...], repeats: int, workers: int = 0) -> dict[str, Any]:
    timings = {mode: [] for mode in modes}
    outputs = {mode: [] for mode in modes}
    # Warm imports, bytecode caches and pytest's plugin discovery before the
    # measurements. The warmup is deliberately not one particular mode.
    _run(root, "baseline", workers)
    for repetition in range(repeats):
        ordered = modes if repetition % 2 == 0 else tuple(reversed(modes))
        for mode in ordered:
            elapsed, output = _run(root, mode, workers)
            timings[mode].append(elapsed)
            outputs[mode].append(output)
    medians = {mode: statistics.median(values) for mode, values in timings.items()}
    baseline = medians["baseline"]
    return {
        "workers": workers or 1,
        "seconds": timings,
        "median_seconds": medians,
        "ratios": {mode: value / baseline for mode, value in medians.items()},
        "outputs": outputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--skip-xdist", action="store_true")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="failure-profile-gate-") as temporary:
        root = Path(temporary)
        (root / "test_work.py").write_text(WORKLOAD, encoding="utf-8")
        (root / "test_quiet.py").write_text(QUIET_WORKLOAD, encoding="utf-8")
        serial = _measure(root, ("baseline", "instrumentation", "profile"), arguments.repeats)
        detection_outputs = list(serial["outputs"]["profile"][:DETECTION_TRIALS])
        # Detection gets two additional trials when the timing sample is only
        # three runs, so its contract is always based on five independent runs.
        while len(detection_outputs) < DETECTION_TRIALS:
            _, output = _run(root, "profile")
            detection_outputs.append(output)
        detection = sum("burn_cpu" in output for output in detection_outputs)
        quiet_outputs = [_run(root, "profile", target="test_quiet.py")[1] for _ in range(QUIET_TRIALS)]
        quiet_passes = sum(
            "CPU hotspot:" not in output and "CPU burst:" not in output
            for output in quiet_outputs
        )

        report: dict[str, Any] = {
            "budgets": {
                "instrumentation_ratio": INSTRUMENTATION_BUDGET,
                "profile_ratio": PROFILE_BUDGET,
                "xdist_profile_ratio": XDIST_PROFILE_BUDGET,
                "detection_trials": DETECTION_TRIALS,
                "quiet_trials": QUIET_TRIALS,
            },
            "serial": {key: value for key, value in serial.items() if key != "outputs"},
            "hotspot_detection": {"passed": detection, "trials": DETECTION_TRIALS},
            "quiet_false_positive_check": {"passed": quiet_passes, "trials": QUIET_TRIALS},
        }
        failures = []
        if detection != DETECTION_TRIALS:
            failures.append(f"hotspot detected in only {detection}/{DETECTION_TRIALS} runs")
        if quiet_passes != QUIET_TRIALS:
            failures.append(
                f"quiet run avoided CPU findings in only {quiet_passes}/{QUIET_TRIALS} runs"
            )
        if serial["ratios"]["instrumentation"] > INSTRUMENTATION_BUDGET:
            failures.append("instrumentation overhead exceeded its budget")
        if serial["ratios"]["profile"] > PROFILE_BUDGET:
            failures.append("profile overhead exceeded its budget")

        if not arguments.skip_xdist:
            parallel = _measure(root, ("baseline", "profile"), arguments.repeats, workers=4)
            report["xdist_4"] = {key: value for key, value in parallel.items() if key != "outputs"}
            if parallel["ratios"]["profile"] > XDIST_PROFILE_BUDGET:
                failures.append("four-worker profile overhead exceeded its budget")

        report["passed"] = not failures
        report["failures"] = failures
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
