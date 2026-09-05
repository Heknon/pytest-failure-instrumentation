"""Run explicitly requested pytest node IDs without turning input into shell code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    inline = os.environ.get("TARGET_TESTS", "").strip()
    target_file = os.environ.get("TARGET_TESTS_FILE", "").strip()
    if bool(inline) == bool(target_file):
        raise SystemExit("Supply exactly one of TARGET_TESTS or TARGET_TESTS_FILE")

    raw = Path(target_file).read_text(encoding="utf-8") if target_file else inline
    targets = [line.strip() for line in raw.splitlines() if line.strip()]
    if not targets:
        raise SystemExit("No test targets were supplied")

    for target in targets:
        path = target.split("::", 1)[0]
        if target.startswith("-") or not path.startswith("tests/") or not Path(path).is_file():
            raise SystemExit(f"Invalid test target: {target!r}; targets must name files under tests/")

    return subprocess.run([sys.executable, "-m", "pytest", "-q", *targets], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
