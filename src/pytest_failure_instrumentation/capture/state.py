"""A fixed-size record of what a worker is doing right now.

The naive way to know which test a dead worker was running is to append a line
per test to a log. That costs a write, a flush and two ``/proc`` reads on every
test, for a fact that matters only when something dies - and on a large suite
it produces hundreds of thousands of lines nobody ever reads.

Instead each worker keeps one small file that is *overwritten* in place. It
never grows, costs a single ``pwrite`` per phase transition, and reading it is
one fixed-size read rather than parsing a log.

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

The slot is sized so that eliding is the exception rather than the rule. It is
one write of one buffer at whatever size it is, and the syscall costs the same
from 256 bytes to 8 KiB, so the only thing a small slot bought was node ids
losing their tails. What a larger one costs instead is a slightly wider window
for a torn read, which the reader already retries through and which degrades
to a stale-but-valid record rather than to nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

#: One write of one buffer, so the size is nearly free - see the module
#: docstring. This leaves around 4950 bytes for a node id, which is past any
#: real one by an order of magnitude: a path, a class, a test name and a
#: couple of content hashes together use a twentieth of it.
SLOT_SIZE = 5 * 1024

#: Marks the part of a node id that did not fit, so a reader can tell an
#: elided id from a short one.
ELIDED = "..."

#: How much of a too-long id to keep from the front. Both ends carry
#: something the other does not: the head is the module and the test, which
#: attribution and the fingerprint read, and the tail is where a parametrize
#: puts the value that says *which* case this was - a hash, a timestamp, an
#: account id. Cutting either end blind loses one of the two.
HEAD_SHARE = 0.6


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
        """The record as bytes, with the node id elided until it fits.

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

        # The most of the id that still fits, found by search rather than by
        # subtracting an overflow: json escaping means a character is not a
        # byte, and a quote or a non-ASCII parameter costs several. Keeping
        # more can only lengthen the record, so the fit is monotone and the
        # search is exact. It runs once per oversized id, not per write.
        low, high = 0, len(self.nodeid)
        while low < high:
            middle = (low + high + 1) // 2
            if len(self._record(self._elide(middle), stamp)) <= SLOT_SIZE - 1:
                low = middle
            else:
                high = middle - 1
        trimmed = self._elide(low)
        self._trimmed = (self.nodeid, trimmed)
        return self._record(trimmed, stamp)

    def _elide(self, keep: int) -> str:
        """``keep`` characters of the node id, taken from both ends.

        A hash lives at the end of a parametrized id and the module lives at
        the start, so a cut that keeps only one end throws away either what
        the incident is attributed to or which case it was.
        """
        # Bound locally: the caller only reaches here with a nodeid set, but
        # the attribute is Optional and nothing in the signature says so.
        nodeid = self.nodeid or ""
        if keep <= 0 or not nodeid:
            return ""
        if keep >= len(nodeid):
            return nodeid
        head = round(keep * HEAD_SHARE)
        tail = keep - head
        return nodeid[:head] + ELIDED + (nodeid[-tail:] if tail else "")

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
