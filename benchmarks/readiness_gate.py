"""On-demand qualification: real 80-worker I/O runs and native CPU attribution.

Run on a normal OS with working psutil process access. Results are synthetic
qualification evidence, not a guarantee for an unseen application workload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time

import psutil

MIB = 1024 * 1024
WORKLOAD = '''
import os
import time
import pytest

@pytest.mark.parametrize("case", range(int(os.environ["READINESS_CASES"])))
def test_mixed_io(case):
    # Fixed CPU work and fixed waits: profiling cannot reduce the work by
    # consuming part of a wall-clock deadline. About two seconds per test.
    for step in range(10):
        time.sleep(0.2)
        sum(value * value for value in range(2000))
'''
CONFTEST = '''
import json
import os
from pathlib import Path
import time
import pytest

finished = 0
last_test = None

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global finished, last_test
    yield
    finished += 1
    last_test = time.time()

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    started = time.perf_counter()
    yield
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    Path(os.environ["READINESS_STATS"], worker + ".json").write_text(json.dumps({
        "worker": worker, "tests": finished, "last_test": last_test,
        "sessionfinish_seconds": time.perf_counter() - started,
    }), encoding="utf-8")
'''


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


def environment():
    return {"python": sys.version, "platform": platform.platform(),
            "logical_cpus": psutil.cpu_count(), "ram_mb": psutil.virtual_memory().total / MIB,
            "commit": os.environ.get("GITHUB_SHA")}


def measured_run(root, mode, workers, cases, repetition):
    run = root / f"{mode}-{repetition}"
    run.mkdir()
    stats = run / "stats"
    stats.mkdir()
    evidence = run / "evidence"
    command = [sys.executable, "-m", "pytest", "-q", "-n", str(workers),
               "--max-worker-restart=0", "-p", "no:cacheprovider",
               "-o", f"failure_directory={evidence}",
               "-o", "failure_packages=test_work", "test_work.py"]
    if mode == "profile":
        command.append("--failure-profile")
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_CURRENT_TEST")}
    env.update(READINESS_CASES=str(cases), READINESS_STATS=str(stats), PYTHONHASHSEED="0")
    started = time.perf_counter()
    peak_total = peak_controller = peak_child = 0
    max_children = 0
    samples = []
    with (run / "pytest.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        process = psutil.Process(child.pid)
        try:
            while child.poll() is None:
                elapsed = time.perf_counter() - started
                if elapsed > 600:
                    raise TimeoutError("qualification run exceeded 600 seconds")
                try:
                    controller = process.memory_info().rss
                    descendants = process.children(recursive=True)
                    resident = []
                    for descendant in descendants:
                        try:
                            resident.append(descendant.memory_info().rss)
                        except psutil.Error:
                            continue
                    total = controller + sum(resident)
                    peak_total = max(peak_total, total)
                    peak_controller = max(peak_controller, controller)
                    peak_child = max(peak_child, max(resident, default=0))
                    max_children = max(max_children, len(descendants))
                    samples.append([round(elapsed, 2), round(total / MIB, 2), round(controller / MIB, 2)])
                except psutil.NoSuchProcess:
                    break
                time.sleep(0.5)
        finally:
            if child.poll() is None:
                for descendant in process.children(recursive=True):
                    try:
                        descendant.kill()
                    except psutil.Error:
                        pass
                child.kill()
            child.wait()
    elapsed = time.perf_counter() - started
    summaries = [json.loads(path.read_text()) for path in stats.glob("*.json")]
    worker_stats = [entry for entry in summaries if entry["worker"] != "controller"]
    controller_stats = next((entry for entry in summaries if entry["worker"] == "controller"), {})
    last_test = max((entry["last_test"] or 0 for entry in worker_stats), default=0)
    result = {"mode": mode, "repetition": repetition, "seconds": elapsed,
              "returncode": child.returncode, "workers_completed": len(worker_stats),
              "tests_completed": sum(entry["tests"] for entry in worker_stats),
              "max_descendants": max_children, "peak_total_rss_mb": peak_total / MIB,
              "peak_controller_rss_mb": peak_controller / MIB, "peak_child_rss_mb": peak_child / MIB,
              "controller_sessionfinish_seconds": controller_stats.get("sessionfinish_seconds"),
              "seconds_after_last_test": time.time() - last_test if last_test else None,
              "evidence_mb": sum(path.stat().st_size for path in evidence.rglob("*") if path.is_file()) / MIB,
              "rss_samples": samples}
    (run / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "rss_samples"}), flush=True)
    return result


def stress(args):
    root = args.output.parent / "stress-runs"
    root.mkdir(parents=True, exist_ok=False)
    (root / "test_work.py").write_text(WORKLOAD, encoding="utf-8")
    (root / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    results = []
    for repeat in range(args.repeats):
        # Alternate order to reduce cache and runner drift bias.
        for mode in (("baseline", "profile") if repeat % 2 == 0 else ("profile", "baseline")):
            results.append(measured_run(root, mode, args.workers, args.cases, repeat))
    medians = {mode: statistics.median(row["seconds"] for row in results if row["mode"] == mode)
               for mode in ("baseline", "profile")}
    ratio = medians["profile"] / medians["baseline"]
    failures = []
    for row in results:
        if row["returncode"] or row["workers_completed"] != args.workers or row["tests_completed"] != args.cases:
            failures.append(f'{row["mode"]}-{row["repetition"]}: incomplete run')
        if row["peak_total_rss_mb"] > 10_240:
            failures.append(f'{row["mode"]}-{row["repetition"]}: process RSS sum exceeds 10 GiB')
        if row["mode"] == "profile" and (row["controller_sessionfinish_seconds"] is None or row["controller_sessionfinish_seconds"] > 30):
            failures.append("profile controller reporting exceeds 30 seconds or is missing")
    if ratio > 1.20:
        failures.append("80-worker synthetic runtime overhead exceeds 20% qualification budget")
    report = {"environment": environment(), "workers": args.workers, "cases_per_run": args.cases,
              "repeats": args.repeats, "profile_ratio": ratio, "median_seconds": medians,
              "rss_note": "Sum of process RSS includes shared pages more than once; sampled every 0.5 s.",
              "runs": [{key: value for key, value in row.items() if key != "rss_samples"} for row in results],
              "failures": failures, "passed": not failures}
    write_report(args.output, report)
    return int(bool(failures))


def native(args):
    from pytest_failure_instrumentation.profile.sampler import Sampler
    # CPython's regex engine holds the GIL; OpenSSL PBKDF2 releases it.
    pattern = re.compile(r"(a+)+$")
    size = 18
    while size < 25:
        start = time.thread_time()
        pattern.fullmatch("a" * size + "!")
        if time.thread_time() - start >= 0.15:
            break
        size += 1
    subject = "a" * size + "!"

    def held_native():
        pattern.fullmatch(subject)

    def released_native():
        hashlib.pbkdf2_hmac("sha256", b"password", b"salt", 600_000)

    results = []
    for function in (held_native, released_native):
        for shape in ("sustained", "single_call"):
            for trial in range(3):
                records = []
                sampler = Sampler(records.append, lambda: psutil.Process().memory_info().rss // MIB, worker="main")
                sampler.start()
                sampler.begin_phase("native::check", "call")
                # No forced samples, private sampler calls, or yields inside
                # the work: exercise what an ordinary application gets.
                time.sleep(0.05)
                start = time.thread_time()
                if shape == "sustained":
                    while time.thread_time() - start < 2.0:
                        function()
                else:
                    function()
                actual = time.thread_time() - start
                time.sleep(0.1)
                sampler.end_phase("call")
                sampler.end_test("native::check")
                sampler.stop()
                record = next(entry for entry in records if entry["record"] == "test")
                attributed = sum(stack["cpu_ns"] for stack in record["stacks"]
                                 if any(record["frames"][index].split("|")[-1].endswith(function.__name__)
                                        for index in stack["frames"])) / 1e9
                total = sum(stack["cpu_ns"] for stack in record["stacks"]) / 1e9
                results.append({"function": function.__name__, "shape": shape, "trial": trial,
                                "actual_thread_cpu_s": actual, "attributed_cpu_s": attributed,
                                "total_sampled_cpu_s": total, "coverage": attributed / actual,
                                "thread_clock": record["thread_clock"], "samples": sampler.samples_taken,
                                "passed": 0.70 <= attributed / actual <= 1.30})
    failures = [f'{row["function"]}/{row["shape"]}/{row["trial"]}: coverage {row["coverage"]:.1%}'
                for row in results if not row["passed"]]
    diagnostic = args.native_diagnostic
    limitation = "Native-call attribution is not guaranteed, including Python wrappers around GIL-holding calls."
    write_report(args.output, {"environment": environment(), "results": results,
                               "failures": failures, "passed": not failures,
                               "release_blocking": not diagnostic, "limitation": limitation})
    if diagnostic:
        summary = ("Native attribution diagnostic (not a release gate): "
                   f"{len(results) - len(failures)}/{len(results)} probes met the attribution range. "
                   + limitation + "\n")
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                handle.write(summary)
                for failure in failures:
                    handle.write(f"- {failure}\n")
    return 0 if diagnostic else int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stress", "native"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=80)
    parser.add_argument("--cases", type=int, default=2400)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--native-diagnostic", action="store_true",
                        help="Report native attribution limitations without gating release; execution errors still fail")
    args = parser.parse_args()
    if args.native_diagnostic and args.mode != "native":
        parser.error("--native-diagnostic requires native mode")
    args.output = args.output.resolve()
    return stress(args) if args.mode == "stress" else native(args)


if __name__ == "__main__":
    raise SystemExit(main())
