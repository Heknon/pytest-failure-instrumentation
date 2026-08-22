"""faulthandler: arming it, and reading what it wrote.

Two jobs that look alike and are not. Arming happens once per worker and must
claim the handler back from pytest's own faulthandler plugin, which points it
at shared stderr where every worker's output interleaves. Reading happens on
the controller, after the fact.

The SIGUSR1 registration is what lets a *stalled* worker be asked for its stack
without being killed: the handler is async-signal-safe C and answers even while
native code holds the GIL.
"""

from __future__ import annotations

import faulthandler
import signal
from pathlib import Path
from typing import TextIO

from ..probes import stacks


def arm(stream: TextIO) -> bool:
    """Point fatal-signal dumps at this worker's own file.

    Returns whether a live stack can also be requested later.
    """
    faulthandler.enable(file=stream, all_threads=True)
    if not stacks.can_request_stack():
        return False
    faulthandler.register(
        signal.SIGUSR1, file=stream, all_threads=True, chain=False
    )
    return True


def read(path: Path, limit: int = 12, offset: int = 0) -> list[str]:
    """The head of a dump - most recent call first.

    ``offset`` reads only what was appended past a known point, so a stack
    requested now is never confused with one written earlier.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            lines = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return []
    if not lines:
        return []
    banner = lines[0] if lines[0].startswith("Fatal Python error") else None
    for index, line in enumerate(lines):
        if line.startswith("Current thread"):
            section = lines[index : index + limit]
            return ([banner] + section) if banner else section
    return lines[:limit]


def size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
