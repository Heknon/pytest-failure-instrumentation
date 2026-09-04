"""Every byte a worker writes to stderr, kept for when it dies without a word.

The line that explains a native death is on stderr and nowhere a stack can
reach it: ``OpenBLAS blas_thread_init: pthread_create failed`` when a hundred
workers each start a thread pool at once, ``malloc(): corrupted top size``, a
library's own abort message. It is written by C code straight to file
descriptor 2, and the process is gone before anything Python-level runs.

pytest captures fd 2 too, but it hands that capture to a report only for a
phase that *completed* - so a message printed in the very phase that then
crashes, or at import, reaches no report and is lost. The only way not to miss
it is to read the descriptor, and to read it in a way that survives a write
followed immediately by ``abort()``: so fd 2 is pointed at a *real file*, where
a write is a synchronous ``write(2)`` the kernel has persisted before the abort
runs. A pipe drained by a thread of the same process cannot win that race - the
process dies with the bytes still in flight - which is why this is a file and
not a pipe.

**It coexists with pytest's own capture.** pytest owns fd 2 by pointing it at
its own file and re-points it there at the start of every phase; this takes it
over just after, at each phase, and hands pytest back the phase's bytes at the
phase's end - before pytest reads its own file to build the report. So pytest's
captured-output-on-failure is unchanged, and this keeps a durable copy besides,
including of the crashing phase pytest never got to.

**This is the one facility that takes over a process-wide descriptor**, opt-in
and guarded at every step: an fd operation that fails leaves fd 2 as it was and
records that output was not captured, rather than raising into a run it was
only meant to watch. POSIX only.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from ..probes.platform_flags import IS_WINDOWS

#: How much of the tail to keep on disk between phases. A crash message is a
#: line or three; this holds a few hundred, which covers a library that logs
#: across a whole setup. A single phase may exceed it while running - it is
#: trimmed back to this at the next phase, never during one, so a burst of
#: output right before a crash is never trimmed away underneath it.
RING_BYTES = 16 * 1024
STDERR_FD = 2


class StderrTee:
    """Points fd 2 at a real file for the length of each phase.

    ``active`` says whether the tee is installed; ``reason`` says why not when
    it is not, and that reason travels onto the incident so an absent tail is
    never read as an empty one. ``take`` at the start of collection and each
    phase, ``hand_back`` at the end of each, ``close`` at the end of the run.
    """

    def __init__(self, path: Path, limit: int = RING_BYTES) -> None:
        self.path = path
        self.limit = limit
        self.active = False
        self.reason = "off"
        self._file: Optional[int] = None
        self._passthrough: Optional[int] = None
        self._phase_offset = 0
        self._closed = False

    def start(self) -> bool:
        if IS_WINDOWS:
            self.reason = "off: capturing stderr is a POSIX facility for now"
            return False
        try:
            self._file = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        except OSError as failure:
            self.reason = f"off: the capture file could not be opened ({failure!r})"
            return False
        self.active = True
        self.reason = "on"
        return True

    def take(self) -> None:
        """Point fd 2 at the capture file, saving what was there to hand back.

        Called at the start of collection and each phase, because pytest points
        fd 2 at its own file each time. Idempotent: if fd 2 is already the
        capture file, nothing is done.
        """
        if not self.active or self._file is None:
            return
        try:
            if self._is_our_file(STDERR_FD):
                return
            self._trim()
            current = os.dup(STDERR_FD)
            self._passthrough = current
            self._phase_offset = os.lseek(self._file, 0, os.SEEK_END)
            os.dup2(self._file, STDERR_FD)
        except OSError:
            pass

    def hand_back(self) -> None:
        """Give fd 2 back to pytest, after copying this phase's bytes to it.

        Runs at the end of each phase, before pytest reads its own capture
        file to build the report, so pytest sees everything written to fd 2
        during the phase and its captured-output-on-failure is unchanged.
        """
        if not self.active or self._file is None or self._passthrough is None:
            return
        try:
            written = self._read_from(self._phase_offset)
            if written:
                os.write(self._passthrough, written)
            os.dup2(self._passthrough, STDERR_FD)
        except OSError:
            pass
        finally:
            passthrough = self._passthrough
            self._passthrough = None
            if passthrough is not None:
                try:
                    os.close(passthrough)
                except OSError:
                    pass

    def _read_from(self, offset: int) -> bytes:
        if self._file is None:
            return b""
        try:
            end = os.lseek(self._file, 0, os.SEEK_END)
            if end <= offset:
                return b""
            os.lseek(self._file, offset, os.SEEK_SET)
            return os.read(self._file, end - offset)
        except OSError:
            return b""

    def _trim(self) -> None:
        """Keep the file to its last ``limit`` bytes. Only between phases, so a
        phase's own output is never trimmed while it is still being written."""
        if self._file is None:
            return
        try:
            size = os.lseek(self._file, 0, os.SEEK_END)
            if size <= self.limit:
                return
            os.lseek(self._file, size - self.limit, os.SEEK_SET)
            tail = os.read(self._file, self.limit)
        except OSError:
            return
        cut = tail.find(b"\n")
        tail = tail[cut + 1 :] if cut != -1 else tail
        try:
            os.lseek(self._file, 0, os.SEEK_SET)
            os.write(self._file, tail)
            os.ftruncate(self._file, len(tail))
        except OSError:
            pass

    def _is_our_file(self, fd: int) -> bool:
        if self._file is None:
            return False
        try:
            here, ours = os.fstat(fd), os.fstat(self._file)
        except OSError:
            return False
        return (
            stat.S_ISREG(here.st_mode)
            and here.st_ino == ours.st_ino
            and here.st_dev == ours.st_dev
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.hand_back()  # if a phase was open, give fd 2 back and flush it
        self.active = False
        if self._file is not None:
            try:
                os.close(self._file)
            except OSError:
                pass
            self._file = None


def read_tail(path: Path, limit: int = RING_BYTES) -> list[str]:
    """A worker's captured stderr, as lines, or empty if none was kept.

    Seeks to the end rather than reading the file in: the ring is trimmed
    between phases and never during one - see :meth:`StderrTee._trim` - so a
    single phase that logs heavily leaves a file of any size at all, and this
    runs on the controller, once per dead worker. A partial first line is
    dropped when the seek landed inside one.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]
    return lines
