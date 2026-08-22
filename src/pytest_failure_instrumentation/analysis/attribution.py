"""Whose code is on the stack.

The deepest frame is usually the runtime - ``ctypes.string_at`` tells nobody
anything. Walking outward to the first frame that belongs to somebody is what
turns "a worker segfaulted" into "your package did" or "their test did", and
that single distinction is the difference between an alert worth paging on and
one worth filing.

A stack with *no* owned frame at all is not unknown: it is a positive finding
that the framework itself is what failed.
"""

from __future__ import annotations

import re
import sysconfig
from pathlib import Path
from typing import Any

FRAMEWORK_MARKERS = (
    "/_pytest/", "/pytest/", "/pluggy/", "/execnet/", "/xdist/",
    "/pytest_failure_instrumentation/", "<string>", "<frozen ",
)

FRAME_PATTERN = re.compile(r'File "([^"]+)", line (\d+),? in (\S+)')


class Attributor:
    def __init__(self, packages: tuple[str, ...]) -> None:
        self.packages = [name.replace("-", "_") for name in packages]
        stdlib = sysconfig.get_paths().get("stdlib", "")
        self.stdlib = stdlib.replace("\\", "/") if stdlib else ""

    def owner_of(self, path: str) -> str:
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if any(marker in normalized for marker in FRAMEWORK_MARKERS):
            return "runtime"
        installed = "site-packages" in parts or "dist-packages" in parts
        # site-packages often sits under the stdlib prefix, so check it first.
        if not installed and self.stdlib and normalized.startswith(self.stdlib):
            return "runtime"
        if any(
            package == part or normalized.endswith(f"/{package}.py")
            for package in self.packages
            for part in parts
        ):
            return "product"
        return "third-party" if installed else "customer-code"

    def frames(self, lines: list[str], reverse: bool = False) -> list[dict[str, Any]]:
        """Deepest first. faulthandler prints that way; tracebacks do not."""
        matches = [m for m in (FRAME_PATTERN.search(line) for line in lines) if m]
        if reverse:
            matches.reverse()
        return [
            {
                "file": path,
                "line": int(number),
                "function": function,
                "module": Path(path).stem,
                "owner": self.owner_of(path),
            }
            for path, number, function in (m.groups() for m in matches)
        ]

    def blame(self, lines: list[str], reverse: bool = False) -> dict[str, Any]:
        frames = self.frames(lines, reverse=reverse)
        if not frames:
            return {"top_frame": None, "blamed_frame": None, "owner": "unknown"}
        blamed = next((f for f in frames if f["owner"] != "runtime"), None)
        if blamed is None:
            # No test code anywhere on the stack: the framework is what waited
            # or failed. Point at the framework frame, not the stdlib call it
            # happened to make.
            framework = next(
                (f for f in frames
                 if any(marker in f["file"] for marker in FRAMEWORK_MARKERS)),
                None,
            )
            chosen = framework or frames[0]
            return {"top_frame": frames[0], "blamed_frame": chosen, "owner": "runtime"}
        return {"top_frame": frames[0], "blamed_frame": blamed, "owner": blamed["owner"]}
