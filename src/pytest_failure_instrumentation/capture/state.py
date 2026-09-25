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

What a fixed slot costs is a bound on the record, and the node ids are the only
fields that can approach it - a parametrized id runs to hundreds of characters.
So the *node ids* are trimmed to fit, never the encoded record: a truncated JSON
object does not parse, and the reader then loses the phase and the counters
too, and reports a worker that died mid-test as one that died before running
anything.

A trimmed id is still not the id, and two cases that differ only in the part
that was dropped trim to the same text. So each id is written twice: the text,
elided when it had to be, and the sha256 of the *whole* id beside it - taken
before anything is cut, 64 characters whatever the id was, and therefore of a
fixed cost to the slot. See :mod:`..nodeid`. The hash is what a reader joins
on; the text is what a person reads.

There are two of them, and the difference is the whole reason this file is
read at all. ``nodeid`` is the test *in flight* and is cleared when the test
ends; ``last_nodeid`` is the most recent test whether or not it finished. A
single field cannot be both, and being both is how a worker that died in the
gap between two tests came to be reported as having died in the one that had
already passed - with an owner, a severity and somebody's name on it.

The record also carries the run that wrote it. This directory outlives a run,
and cleaning it is best-effort: a file another process still has open cannot be
unlinked on Windows. A reader that skips the check attributes an earlier run's
pid to this one, and that pid is the one the stall probe signals.

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
import threading
import time
from pathlib import Path
from typing import Any

from ..nodeid import hash_of

#: One write of one buffer, so the size is nearly free - see the module
#: docstring. The record holds the id twice - in flight and last - and a hash
#: of each, so around 2350 characters of an id survive whole, which is past
#: any real one by an order of magnitude: a path, a class, a test name and a
#: couple of content hashes together use a fiftieth of it.
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


def _elide(nodeid: str | None, keep: int) -> str | None:
    """``keep`` characters of a node id, taken from both ends.

    A hash lives at the end of a parametrized id and the module lives at
    the start, so a cut that keeps only one end throws away either what
    the incident is attributed to or which case it was.
    """
    if nodeid is None:
        return None
    if keep <= 0 or not nodeid:
        return ""
    if keep >= len(nodeid):
        return nodeid
    head = round(keep * HEAD_SHARE)
    tail = keep - head
    return nodeid[:head] + ELIDED + (nodeid[-tail:] if tail else "")


class WorkerState:
    """The current nodeid, phase and counters for one worker."""

    def __init__(
        self,
        path: Path,
        pid: int,
        run_id: str | None = None,
        *,
        lane: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        #: Who a lane's record is, written after everything else: the process
        #: it runs in, and the thread it runs on - by name, by native id, and
        #: by the id faulthandler prints - see :mod:`..lanes`. None for a
        #: worker that is a process, whose record is byte for byte what it
        #: was before lanes existed.
        self.lane = dict(lane) if lane else None
        #: Set on a process's own record once its first lane has started a
        #: test: from then on the process is a container, and its lanes are
        #: its workers. Absent from the record until then, and forever in a
        #: run without lanes.
        self.lanes: bool | None = None
        #: A lane's own CPU, as ``[time, seconds]`` pairs, newest last - see
        #: :meth:`record_cpu`. Only ever set on a lane's record.
        self.cpu: list[list[float]] | None = None
        #: A lane's slot has two writers - its own thread at each phase, and
        #: the heartbeat's with its CPU - and one pwrite of a record assembled
        #: from both must not interleave with the other. A worker's slot has
        #: one writer, and takes no lock.
        self._lock = threading.Lock() if self.lane else None
        from ..probes.process import creation_time

        self.pid = pid
        self.created_at = creation_time(pid)
        self.run_id = run_id
        self.sequence = 0
        self.tests_started = 0
        self.tests_finished = 0
        self.nodeid: str | None = None
        #: The most recent test, kept after ``nodeid`` is cleared. See the
        #: module docstring: "which test was it in" and "which test was it
        #: last in" are different questions and only one of them is a finding.
        self.last_nodeid: str | None = None
        #: The sha256 of each of the two, whole - see :mod:`..nodeid`. Written
        #: beside the text and never elided, because the text can be: an id
        #: that lost its middle is no longer an identity, and the pair is what
        #: lets a reader have both.
        self.nodeid_hash: str | None = None
        self.last_nodeid_hash: str | None = None
        self.phase: str | None = None
        #: Which attempt of the test in flight is running: 1 for an ordinary
        #: test, 2 upwards while a rerun plugin is repeating it, and None
        #: between tests, like ``nodeid``. The counters beside it deliberately
        #: collapse the attempts into one test - a rerun is the same test, and
        #: a total that counted attempts was the bug that made this package
        #: report 374 tests on a run of 368 - so this is the only thing in the
        #: record that says an attempt is happening at all. None also means
        #: "not said" for a record written by a version that did not have it.
        self.attempt: int | None = None
        #: When the current phase began, and when the current *test* began
        #: (its setup). These clocks let the controller correlate death with
        #: the test's effective timeout; timing alone does not prove which
        #: process or mechanism ended it. Wall clock, shared with the
        #: controller that reads the death.
        self.phase_started: float | None = None
        self.test_started: float | None = None
        self.timeout_settings: list[dict[str, Any]] = []
        # Opened once; the descriptor lives for the process lifetime so a
        # write costs one syscall and survives interpreter shutdown.
        # O_BINARY matters on Windows: without it os.write translates "\n"
        # into "\r\n" and the fixed-size slot silently overflows.
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
        self._descriptor = os.open(str(path), flags, 0o644)
        # pwrite is one syscall but Unix-only; seek+write is the portable
        # equivalent and still cheap enough for a per-phase write.
        self._pwrite = getattr(os, "pwrite", None)
        #: The last pair of ids that had to be trimmed, and how much of each
        #: was kept. update() runs six times per test with the same ids, and
        #: the search below is the only expensive thing in this file.
        self._trimmed: tuple[tuple[str | None, str | None], int] | None = None
        #: The pair the hashes above were taken of, so that six updates with
        #: the same ids hash them once. Hashing is cheap, but so is a write,
        #: and nothing in this file is allowed to cost a test anything.
        self._hashed: tuple[str | None, str | None] = (None, None)

    def update(self, **fields: Any) -> None:
        if self._lock is None:
            self._update(fields)
            return
        with self._lock:
            self._update(fields)

    def record_cpu(self, stamp: float, seconds: float, keep: int) -> None:
        """Add one reading of a lane's thread CPU, keeping the newest ``keep``.

        On the lane's own record rather than on the process's beat: a beat
        carrying every lane's figure grew with the lane count - fourteen
        kilobytes a beat at a thousand lanes, which pushed all but a handful
        of beats out of the tail a reader takes, and with them the window a
        rate is measured over. Here each lane's figures cost its own slot a
        few dozen bytes, and a reader of one lane reads one slot.
        """
        with self._lock or _NO_LOCK:
            readings = list(self.cpu or [])
            readings.append([round(stamp, 3), round(seconds, 3)])
            self.cpu = readings[-keep:]
            self._update({})

    def _update(self, fields: dict[str, Any]) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        if self.nodeid:
            self.last_nodeid = self.nodeid
        if self.nodeid != self._hashed[0]:
            self.nodeid_hash = hash_of(self.nodeid)
        if self.last_nodeid != self._hashed[1]:
            self.last_nodeid_hash = hash_of(self.last_nodeid)
        self._hashed = (self.nodeid, self.last_nodeid)
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
        """The record as bytes, with the node ids elided until it fits.

        Trimming the encoded JSON instead would save a byte count and lose the
        record: the reader gets an unparseable object and falls back to knowing
        nothing at all about the worker, which is the one thing this file
        exists to prevent.
        """
        stamp = round(time.time(), 3)
        ids = (self.nodeid, self.last_nodeid)
        encoded = self._record(self.nodeid, self.last_nodeid, stamp)
        if len(encoded) <= SLOT_SIZE - 1 or not any(ids):
            return encoded

        if self._trimmed is not None and self._trimmed[0] == ids:
            # The same ids arrive six times per test. Re-checked rather than
            # trusted, because the counters beside them gain a digit as the run
            # goes on and a fit is a fit of the whole record.
            cached = self._elided(self._trimmed[1], stamp)
            if len(cached) <= SLOT_SIZE - 1:
                return cached

        # The most of each id that still fits, found by search rather than by
        # subtracting an overflow: json escaping means a character is not a
        # byte, and a quote or a non-ASCII parameter costs several. Both ids
        # are held to the same budget, so there is one number to search for;
        # keeping more of either can only lengthen the record, so the fit is
        # monotone and the search is exact. It runs once per oversized pair,
        # not per write.
        low, high = 0, max(len(text or "") for text in ids)
        while low < high:
            middle = (low + high + 1) // 2
            if len(self._elided(middle, stamp)) <= SLOT_SIZE - 1:
                low = middle
            else:
                high = middle - 1
        self._trimmed = (ids, low)
        return self._elided(low, stamp)

    def _elided(self, keep: int, stamp: float) -> bytes:
        return self._record(
            _elide(self.nodeid, keep), _elide(self.last_nodeid, keep), stamp
        )

    def _record(
        self, nodeid: str | None, last_nodeid: str | None, stamp: float
    ) -> bytes:
        payload = json.dumps(
            {
                "sequence": self.sequence,
                "time": stamp,
                "run_id": self.run_id,
                "pid": self.pid,
                "created_at": self.created_at,
                "nodeid": nodeid,
                "last_nodeid": last_nodeid,
                # Of the whole ids, whether or not the two above were cut to
                # fit. Fixed width, so they are part of the budget the search
                # in _encode works within rather than something it can trade.
                "nodeid_hash": self.nodeid_hash,
                "last_nodeid_hash": self.last_nodeid_hash,
                "phase": self.phase,
                "attempt": self.attempt,
                "phase_started": self.phase_started,
                "test_started": self.test_started,
                "timeout_settings": self.timeout_settings,
                "tests_started": self.tests_started,
                "tests_finished": self.tests_finished,
                # Only ever present under lanes, and last, so a record written
                # without them is the record this file always wrote.
                **({"lanes": True} if self.lanes else {}),
                **(self.lane or {}),
                **({"cpu": self.cpu} if self.cpu else {}),
            }
        )
        return payload.encode("utf-8") + b"\n"

    def close(self) -> None:
        try:
            os.close(self._descriptor)
        except OSError:
            pass


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *failure: Any) -> None:
        return None


_NO_LOCK = _NoLock()


def read_state(path: Path, run_id: str | None = None) -> dict[str, Any]:
    """Read a worker's current state; empty dict if unavailable or not ours.

    ``run_id`` is the run doing the reading. A record stamped with a different
    one was left by an earlier run whose files this one could not delete, and
    every field in it is wrong for this one - including the pid, which is what
    the stall probe signals. Only a *disagreement* rejects it: a record with no
    id at all was written by a worker the controller never reached with one,
    and dropping that loses real evidence to protect against nothing.
    """
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
            record = json.loads(text)
        except ValueError:
            time.sleep(0.01)  # torn read; the writer is mid-update
            continue
        if not isinstance(record, dict):
            return {}
        written_by = record.get("run_id")
        if run_id and written_by and written_by != run_id:
            return {}
        return record
    return {}
