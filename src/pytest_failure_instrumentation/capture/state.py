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

What a fixed slot costs is a bound on the record, and the node id is the only
field that can approach it - a parametrized id runs to hundreds of characters.
So the *node id* is trimmed to fit, never the encoded record: a truncated JSON
object does not parse, and the reader then loses the phase and the counters
too, and reports a worker that died mid-test as one that died before running
anything.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SLOT_SIZE = 256

#: Marks a node id that did not fit the slot, so a reader can tell a trimmed
#: id from a short one. The tail goes because the head - the module and the
#: test - is what attribution, fingerprinting and the alert text all read.
TRIMMED = "..."


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
        #: The last id that had to be trimmed, and what it was trimmed to.
        #: update() runs six times per test with the same id, and the search
        #: below is the only expensive thing in this file.
        self._trimmed: tuple[str, str] | None = None

    def update(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        self.sequence += 1
        payload_bytes = self._encode().ljust(SLOT_SIZE)
        try:
            if self._pwrite is not None:
                self._pwrite(self._descriptor, payload_bytes, 0)
            else:
                os.lseek(self._descriptor, 0, os.SEEK_SET)
                os.write(self._descriptor, payload_bytes)
        except OSError:
            pass  # never let bookkeeping break a test run

    def _encode(self) -> bytes:
        """The record as bytes, with the node id trimmed until it fits.

        Trimming the encoded JSON instead would save a byte count and lose the
        record: the reader gets an unparseable object and falls back to knowing
        nothing at all about the worker, which is the one thing this file
        exists to prevent.
        """
        stamp = round(time.time(), 3)
        encoded = self._record(self.nodeid, stamp)
        if len(encoded) <= SLOT_SIZE - 1 or not self.nodeid:
            return encoded

        if self._trimmed is not None and self._trimmed[0] == self.nodeid:
            # The same id arrives six times per test. Re-checked rather than
            # trusted, because the counters beside it gain a digit as the run
            # goes on and a fit is a fit of the whole record.
            cached = self._record(self._trimmed[1], stamp)
            if len(cached) <= SLOT_SIZE - 1:
                return cached

        # The longest prefix that still fits, found by search rather than by
        # subtracting an overflow: json escaping means a character is not a
        # byte, and a quote or a non-ASCII parameter costs several. Adding a
        # character can only lengthen the record, so the fit is monotone and
        # the search is exact. It runs once per oversized id, not per write.
        low, high = 0, len(self.nodeid)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = self._record(self.nodeid[:middle] + TRIMMED, stamp)
            if len(candidate) <= SLOT_SIZE - 1:
                low = middle
            else:
                high = middle - 1
        trimmed = self.nodeid[:low] + TRIMMED if low else ""
        self._trimmed = (self.nodeid, trimmed)
        return self._record(trimmed, stamp)

    def _record(self, nodeid: str | None, stamp: float) -> bytes:
        payload = json.dumps(
            {
                "sequence": self.sequence,
                "time": stamp,
                "pid": self.pid,
                "nodeid": nodeid,
                "phase": self.phase,
                "tests_started": self.tests_started,
                "tests_finished": self.tests_finished,
            }
        )
        return payload.encode("utf-8") + b"\n"

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
