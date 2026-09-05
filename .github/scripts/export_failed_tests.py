"""Export pytest's last-failed cache as portable, line-delimited node IDs."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    source = Path(sys.argv[1] if len(sys.argv) > 1 else ".pytest_cache/v/cache/lastfailed")
    destination = Path(sys.argv[2] if len(sys.argv) > 2 else ".test-results/failed-tests.txt")
    if not source.is_file():
        print(f"No pytest last-failed cache found at {source}")
        return 0

    payload = json.loads(source.read_text(encoding="utf-8"))
    failed = sorted(nodeid for nodeid, is_failed in payload.items() if is_failed)
    if not failed:
        print("The pytest last-failed cache contains no failed tests")
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(f"{nodeid}\n" for nodeid in failed), encoding="utf-8")
    print(f"Exported {len(failed)} failed test(s) to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
