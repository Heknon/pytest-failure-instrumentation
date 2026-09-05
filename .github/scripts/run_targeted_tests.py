"""Run explicitly requested pytest node IDs without turning input into shell code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    targets = [line.strip() for line in os.environ.get("TARGET_TESTS", "").splitlines() if line.strip()]
    if not targets:
        raise SystemExit("No test targets were supplied")

    for target in targets:
        path = target.split("::", 1)[0]
        if target.startswith("-") or not path.startswith("tests/") or not Path(path).is_file():
            raise SystemExit(f"Invalid test target: {target!r}; targets must name files under tests/")

    return subprocess.run([sys.executable, "-m", "pytest", "-q", *targets], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
