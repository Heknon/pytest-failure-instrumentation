"""Explain instrumentation costs separately from the unprofiled timing gate."""

from __future__ import annotations

import os
import pstats
import subprocess
import sys
import tempfile
from pathlib import Path

from profile_gate import WORKLOAD


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
