"""A bounded copy of what a worker printed, kept for when it dies mid-run.

The one line that explains a native death is usually on stderr and nowhere a
stack can reach it: ``OpenBLAS blas_thread_init: pthread_create failed``,
``malloc(): corrupted top size``, a library's own abort message. pytest
already captures that - its fd-level capture catches even the writes C code
makes straight to file descriptor 2 - but it keeps it in a temporary file that
is thrown away when the worker is killed, and hands it to the report only for a
phase that *completed*.

So each completed phase's captured output is copied here, into ``<worker>.output``,
a fixed ring that never grows. When the worker is then killed, the last few
kilobytes of what it said are on disk: the import that spun up the thread pool,
the setup that logged before it aborted, the previous test's last words. What
is lost is only the output of the exact phase that was still running when the
kill landed, because pytest has not made its report yet - and that is a bound
worth stating rather than a tee worth fighting pytest's own capture for.

This reads pytest's capture rather than taking over a file descriptor, so it
touches nothing the run depends on: it cannot disturb pytest's own captured
output, and there is no descriptor to restore. It works under pytest's default
capture, where the native output would otherwise be lost, and does nothing
under ``-s`` because there is nothing for pytest to hand it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: How much of the tail to keep. A crash message is a line or three; this holds
#: a few hundred, which covers a library that logs across a whole setup.
RING_BYTES = 16 * 1024


class OutputLog:
    """A bounded, on-disk copy of a worker's captured output.

    Fed one completed phase's capture at a time, and rewritten whole on each
    so that what is on disk is always current: a SIGKILL gives no chance to
    flush, so the ring cannot live only in memory. Rewriting a file of at
    most ``RING_BYTES`` costs nothing beside a test, and only happens when a
    phase actually printed something, which a healthy run rarely does.
    """

    def __init__(self, path: Path, limit: int = RING_BYTES) -> None:
        self.path = path
        self.limit = limit
        self._buffer = bytearray()
        self._descriptor: Optional[int] = None

    def add(self, stderr: str = "", stdout: str = "") -> None:
        """Append one phase's captured streams. Empty strings add nothing."""
        chunk = self._render(stderr, stdout)
        if not chunk:
            return
        self._buffer.extend(chunk)
        if len(self._buffer) > self.limit:
            # Drop whole lines off the front: a ring that begins mid-line
            # reads as corruption rather than as a tail.
            overflow = len(self._buffer) - self.limit
            cut = self._buffer.find(b"\n", overflow)
            del self._buffer[: cut + 1 if cut != -1 else overflow]
        self._persist()

    @staticmethod
    def _render(stderr: str, stdout: str) -> bytes:
        parts = []
        if stderr:
            parts.append(stderr if stderr.endswith("\n") else stderr + "\n")
        if stdout:
            # stdout after stderr and tagged: a crash reason is on stderr, and
            # a wall of prints must not be mistaken for it. Kept because a
            # program that logs to stdout is common.
            lines = stdout.rstrip("\n").split("\n")
            parts.append("".join(f"[stdout] {line}\n" for line in lines))
        return "".join(parts).encode("utf-8", "replace")

    def _persist(self) -> None:
        data = bytes(self._buffer)
        try:
            if self._descriptor is None:
                self._descriptor = os.open(
                    str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644
                )
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            os.write(self._descriptor, data)
            os.ftruncate(self._descriptor, len(data))
        except OSError:
            pass  # bookkeeping must never break a run

    def close(self) -> None:
        if self._descriptor is not None:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = None


def read_tail(path: Path, limit: int = RING_BYTES) -> list[str]:
    """A worker's captured output, as lines, or empty if none was kept."""
    try:
        raw = path.read_bytes()[-limit:]
    except OSError:
        return []
    return raw.decode("utf-8", "replace").splitlines()
