"""A fixed-size record of what a worker is doing right now.

The naive way to know which test a dead worker was running is to append a line
per test to a log. That costs a write, a flush and two ``/proc`` reads on every
test, for a fact that matters only when something dies - and on a large suite
it produces hundreds of thousands of lines nobody ever reads.

Instead each worker keeps one small file that is *overwritten* in place. It
never grows, costs a single ``pwrite`` per phase transition, and reading it is
one 256-byte read rather than parsing a log.

The file is written by exactly one process and read by exactly one other. A
single ``pwrite`` of a small buffer is not formally atomic, so the reader
tolerates a torn read by retrying once; a stale-but-valid record is always
better than a crash in the reader.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SLOT_SIZE = 256


class WorkerState:
    """The current nodeid, phase and counters for one worker."""

    def __init__(self, path: Path, pid: int) -> None:
        self.path = path
        self.pid = pid
        self.sequence = 0
        self.tests_started = 0
        self.tests_finished = 0
        self.nodeid: str | None = None
        self.phase: str | None = None
        # Opened once; the descriptor lives for the process lifetime so a
        # write costs one syscall and survives interpreter shutdown.
        # O_BINARY matters on Windows: without it os.write translates "\n"
        # into "\r\n" and the fixed-size slot silently overflows.
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
        self._descriptor = os.open(str(path), flags, 0o644)
        # pwrite is one syscall but Unix-only; seek+write is the portable
        # equivalent and still cheap enough for a per-phase write.
        self._pwrite = getattr(os, "pwrite", None)

    def update(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        self.sequence += 1
        payload = json.dumps(
            {
                "sequence": self.sequence,
                "time": round(time.time(), 3),
                "pid": self.pid,
                "nodeid": self.nodeid,
                "phase": self.phase,
                "tests_started": self.tests_started,
                "tests_finished": self.tests_finished,
            }
        )
        encoded = payload.encode("utf-8")[: SLOT_SIZE - 1] + b"\n"
        payload_bytes = encoded.ljust(SLOT_SIZE)
        try:
            if self._pwrite is not None:
                self._pwrite(self._descriptor, payload_bytes, 0)
            else:
                os.lseek(self._descriptor, 0, os.SEEK_SET)
                os.write(self._descriptor, payload_bytes)
        except OSError:
            pass  # never let bookkeeping break a test run

    def close(self) -> None:
        try:
            os.close(self._descriptor)
        except OSError:
            pass


def read_state(path: Path) -> dict[str, Any]:
    """Read a worker's current state; empty dict if unavailable."""
    for _ in range(2):
        try:
            with path.open("rb") as handle:
                raw = handle.read(SLOT_SIZE)
        except OSError:
            return {}
        text = raw.rstrip(b"\x00").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            time.sleep(0.01)  # torn read; the writer is mid-update
    return {}
