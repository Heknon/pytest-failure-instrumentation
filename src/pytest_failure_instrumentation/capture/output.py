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
and guarded at every step. Failed fd operations record degraded capture rather
than raising into the run. If restoration fails, the saved descriptor stays
open so closing the tee can retry restoration. POSIX only.
"""

from __future__ import annotations

import os
import stat
import sys
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

#: How far a session-long capture file - see :meth:`StderrTee.drain` - may
#: grow past what it last gave back, in rings, before it gives back more.
RELEASE_FACTOR = 2

#: Holes are punched in whole filesystem blocks; 4 KiB is every common one.
HOLE_ALIGNMENT = 4096

#: Where the tail of a rotated capture file is kept.
PREVIOUS_SUFFIX = ".prev"


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
        #: The file :meth:`_rotate` replaced, and how far it was passed on:
        #: followed by every drain until the next rotation. Never set without lanes.
        self._retired: Optional[tuple[int, int]] = None
        #: How much of the file's start :meth:`_release` has given back.
        self._released = 0

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
            if self._passthrough is not None:
                self.hand_back()
                if self._passthrough is not None:
                    return
            self._trim()
            current = os.dup(STDERR_FD)
            self._passthrough = current
            self._phase_offset = os.lseek(self._file, 0, os.SEEK_END)
            os.dup2(self._file, STDERR_FD)
        except OSError:
            pass

    def hand_back(self) -> None:
        """Restore fd 2 even if copying fails; never allocate a phase-sized buffer."""
        if not self.active or self._file is None or self._passthrough is None:
            return
        try:
            self._drain_retired()
        except OSError:
            self.reason = "degraded: stderr copy failed"
        passthrough = self._passthrough
        # Restore first. If restoration itself fails, retain the handle so
        # close() can retry instead of throwing away the only recovery path.
        try:
            os.dup2(passthrough, STDERR_FD)
        except OSError:
            self.reason = "degraded: stderr restoration failed"
            return
        self._passthrough = None
        try:
            end = os.lseek(self._file, 0, os.SEEK_END)
            with self.path.open("rb") as source:
                source.seek(self._phase_offset)
                remaining = max(0, end - self._phase_offset)
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(passthrough, view)
                        if written <= 0:
                            raise OSError("stderr copy made no progress")
                        view = view[written:]
        except OSError:
            self.reason = "degraded: stderr copy failed"
        finally:
            os.close(passthrough)

    def drain(self) -> None:
        """Copy what reached the capture file since the last copy on to the
        stderr it was taken from, and keep fd 2.

        For a process running pytest-threadlanes, which takes fd 2 once for the
        whole session rather than per phase: its lanes run their phases at the
        same time, so handing fd 2 back at the end of one lane's phase would
        take it away from every sibling still inside theirs - see
        :meth:`..recorder.WorkerRecorder._tee_session`. Without this the
        terminal would see nothing written to fd 2 until the session ended.

        And without :meth:`_release` the file would keep the whole session's
        stderr: the ring is trimmed between phases, and a session of lanes
        never is between phases.

        Not thread-safe on its own; the one caller serializes it.
        """
        if not self.active or self._file is None or self._passthrough is None:
            return
        try:
            self._follow_retired()
            self._phase_offset = self._copy(self._file, self._phase_offset)
            if self._phase_offset - self._released >= RELEASE_FACTOR * self.limit:
                self._release()
        except OSError:
            self.reason = "degraded: stderr copy failed"

    def _release(self) -> None:
        """Give back the disk under what has been passed on, bar the ring.

        Where the filesystem can, by punching a hole in the file rather than
        by replacing it. That keeps the one open file description every writer
        of fd 2 shares - and a child process started by a test holds its own
        copy of it, inherited, which no ``dup2`` in this process can reach. A
        file replaced under such a child keeps being written by it, after
        this process has stopped reading it: measured, a run whose tests each
        started a child writing a mebibyte lost three of twenty on the way to
        the terminal. A hole changes no offset and moves no byte, so nobody's
        writes go anywhere else; the file's length still counts every byte
        ever written, and its blocks hold the tail.

        Where holes cannot be punched - not Linux, or a filesystem without
        them - the file is rotated instead (:meth:`_rotate`), which is exact
        for this process's own writes and loses what a child that outlives
        the rotation writes afterwards.
        """
        upto = max(0, self._phase_offset - self.limit) // HOLE_ALIGNMENT * HOLE_ALIGNMENT
        if upto > self._released and self._file is not None and _punch_hole(self._file, upto):
            self._released = upto
            return
        self._rotate()

    def _copy(self, source: int, offset: int) -> int:
        """Pass ``source`` on from ``offset`` to its end; the new offset."""
        assert self._passthrough is not None
        while True:
            chunk = os.pread(source, 64 * 1024, offset)
            if not chunk:
                return offset
            offset += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(self._passthrough, view)
                if written <= 0:
                    raise OSError("stderr copy made no progress")
                view = view[written:]

    def _rotate(self) -> None:
        """Start a fresh capture file, keeping the old one's tail beside it.

        Nothing written to fd 2 may be lost or passed on twice, and other
        threads are writing it throughout. So fd 2 is pointed at the fresh
        file first - ``dup2`` swaps it in one step, and every write after it
        lands there - and only then is the old file read to its end and
        passed on. Its last ``limit`` bytes are kept as ``<name>.prev``, which
        :func:`read_tail` reads ahead of the current file, so the ring still
        holds the lines before the switch. The old descriptor is kept and
        followed by every drain until the next rotation, and drained once more
        when fd 2 is handed back: a write another thread had already begun
        when the switch happened lands in the old file whenever the scheduler
        lets it finish, which on a loaded machine is after the next drain -
        closing the old file after one more drain lost such a line in CI.
        """
        assert self._file is not None
        fresh_path = self.path.with_name(self.path.name + ".next")
        fresh = os.open(str(fresh_path), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.dup2(fresh, STDERR_FD)
        except OSError:
            os.close(fresh)
            raise
        old, offset = self._file, self._phase_offset
        self._file, self._phase_offset, self._released = fresh, 0, 0
        offset = self._copy(old, offset)
        self._drain_retired()
        self._retired = (old, offset)
        tail = os.pread(old, self.limit, max(0, offset - self.limit))
        previous = self.path.with_name(self.path.name + PREVIOUS_SUFFIX)
        staging = self.path.with_name(self.path.name + PREVIOUS_SUFFIX + ".part")
        staging.write_bytes(tail)
        os.replace(staging, previous)
        os.replace(fresh_path, self.path)

    def _follow_retired(self) -> None:
        """Pass on whatever has reached the file :meth:`_rotate` replaced
        since it was last read, and keep it open."""
        if self._retired is None:
            return
        old, offset = self._retired
        self._retired = (old, self._copy(old, offset))

    def _drain_retired(self) -> None:
        """The last of the file :meth:`_rotate` replaced, then close it."""
        if self._retired is None:
            return
        old, offset = self._retired
        self._retired = None
        try:
            self._copy(old, offset)
        finally:
            os.close(old)

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
        self.hand_back()  # if a phase was open, give fd 2 back and flush it
        if self._passthrough is not None:
            return  # preserve the recovery handle and allow another close()
        self._closed = True
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

    A process running pytest-threadlanes rotates the file instead - see
    :meth:`StderrTee._rotate` - and keeps the last one's tail beside it, which
    is read first when the current file is too short to fill the ring alone.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            raw = handle.read()
    except OSError:
        return []
    if size < limit:
        earlier = _tail_bytes(path.with_name(path.name + PREVIOUS_SUFFIX), limit - size)
        if earlier is not None:
            raw = earlier[1] + raw
            size += earlier[0]
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]
    return lines


def _tail_bytes(path: Path, limit: int) -> Optional[tuple[int, bytes]]:
    """A file's size and its last ``limit`` bytes, or None if it is not there."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return size, handle.read()
    except OSError:
        return None


def _punch_hole(descriptor: int, length: int) -> bool:
    """Free the blocks under the first ``length`` bytes of a file, keeping its
    size and every offset in it. False where it cannot be done here."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        fallocate = ctypes.CDLL(None, use_errno=True).fallocate
        fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
        fallocate.restype = ctypes.c_int
        # FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE
        return fallocate(descriptor, 0x01 | 0x02, 0, length) == 0
    except (OSError, AttributeError, ValueError):
        return False
