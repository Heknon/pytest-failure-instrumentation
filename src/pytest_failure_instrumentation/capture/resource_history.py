"""Bounded active-run history, readable by another session's live server.

Numbered JSONL segments make retention independent of run duration. Each line
is one sample batch. A tiny atomic manifest indexes segments; readers never
need to load an entire run, and tolerate rotation and incomplete final lines.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Optional, TypeVar

NAME = "resources-live"
MAX_LINE = 2 * 1024 * 1024
MAX_REPLY = 2 * 1024 * 1024
T = TypeVar("T")
LEASE = "active.lock"


def _lock(stream: BinaryIO, *, shared: bool = False) -> None:
    if sys.platform == "win32":
        import ctypes as c
        import msvcrt
        from ctypes import wintypes as w

        class Overlapped(c.Structure):
            _fields_ = [("Internal", c.c_size_t), ("InternalHigh", c.c_size_t),
                        ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

        kernel = c.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        lock = kernel.LockFileEx
        lock.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, c.POINTER(Overlapped)]
        lock.restype = w.BOOL
        offset = Overlapped()
        # Readers take shared locks so they never mistake another reader's
        # momentary probe for a live writer. Closing releases either lock.
        if not lock(msvcrt.get_osfhandle(stream.fileno()), 1 if shared else 3, 0, 1, 0, c.byref(offset)):
            error = c.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError("resource lease held")
            raise OSError(error, "resource lease query failed")
    else:
        import fcntl
        fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)


def is_active(directory: Path) -> bool:
    """An OS-held lease ends with the run, even if deleting files fails.

    Read-only users cannot test a Windows lock: propagate that error as
    unavailable, never assume it means a completed run is still active.
    """
    try:
        stream = (directory / LEASE).open("r+b")
    except FileNotFoundError:
        return False
    with stream:
        try:
            _lock(stream, shared=True)
        except BlockingIOError:
            return True
        # Closing releases a successfully acquired lock: nobody owns this run.
        return False


def _sharing_retry(operation: Callable[[], T]) -> T:
    # Windows can briefly deny either side of an atomic replacement while
    # another process has the manifest open. Bound the delay to 30 ms.
    for attempt in range(4):
        try:
            return operation()
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)
    _sharing_retry(lambda: os.replace(temporary, path))


class ResourceHistory:
    def __init__(self, directory: Path, metadata: dict[str, Any], max_bytes: int) -> None:
        self.directory = directory / NAME
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_bytes = max(4 * MAX_LINE, max_bytes)
        self.segment_bytes = max(MAX_LINE, self.max_bytes // 16)
        self.segments: list[dict[str, Any]] = []
        self.sequence = 0
        self.files_snapshot: Any = None
        self.files_sequence: Optional[int] = None
        self.closed = False
        self.cleanup_error: Optional[str] = None
        self.lease = (self.directory / LEASE).open("w+b")
        self.lease.write(b"1")
        self.lease.flush()
        _lock(self.lease)
        self.metadata = {"schema_version": 1, **metadata, "max_bytes": self.max_bytes}
        try:
            self._publish()
        except BaseException:
            self.close()
            raise

    def _publish(self) -> None:
        atomic_json(self.directory / "manifest.json", {
            **self.metadata, "segments": self.segments,
            "latest_sequence": self.sequence,
            "earliest_sequence": self.segments[0]["first"] if self.segments else None,
            "history_bytes": sum(s["bytes"] for s in self.segments),
        })

    def append(self, batch: dict[str, Any]) -> None:
        if self.closed:
            raise FileNotFoundError("resource run finished")
        sequence = self.sequence + 1
        encoded = (json.dumps({**batch, "sequence": sequence}, separators=(",", ":"),
                              allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_LINE:
            raise ValueError("resource sample exceeds record budget")
        if not self.segments or self.segments[-1]["bytes"] + len(encoded) > self.segment_bytes:
            self.segments.append({"file": f"{sequence:016d}.jsonl", "first": sequence,
                                  "last": sequence, "bytes": 0,
                                  "from": batch["observed_at"], "to": batch["observed_at"]})
        # References never cross segments: every retained segment is independently
        # readable after rotation, including latest/time-filtered queries. The wire
        # response always expands snapshots, so existing clients need no cursors
        # or back-reference resolver for filesystem data.
        files = batch.get("files")
        full_files = True
        if (self.segments[-1]["bytes"] and self.files_sequence is not None
                and files is not None and files == self.files_snapshot):
            compact = {**batch, "sequence": sequence, "_files_sequence": self.files_sequence}
            del compact["files"]
            encoded = (json.dumps(compact, separators=(",", ":"), allow_nan=False) + "\n").encode()
            full_files = False
        while len(self.segments) > 1 and sum(s["bytes"] for s in self.segments) + len(encoded) > self.max_bytes:
            old = self.segments[0]
            (self.directory / old["file"]).unlink(missing_ok=True)
            self.segments.pop(0)
        segment = self.segments[-1]
        path = self.directory / segment["file"]
        with path.open("r+b" if path.exists() else "w+b") as stream:
            # Recover any short write left by a prior disk-full/I/O failure.
            stream.truncate(segment["bytes"])
            stream.seek(segment["bytes"])
            stream.write(encoded)
            stream.flush()
        segment.update(last=sequence, to=max(segment["to"], batch["observed_at"]),
                       bytes=segment["bytes"] + len(encoded))
        segment["from"] = min(segment["from"], batch["observed_at"])
        self.sequence = sequence
        if full_files:
            # Detach from caller-owned dictionaries that may be mutated later.
            self.files_snapshot = json.loads(json.dumps(files))
            self.files_sequence = sequence if files is not None else None
        self._publish()

    def deactivate(self) -> None:
        self.closed = True
        self.lease.close()

    def close(self) -> bool:
        self.deactivate()
        try:
            _sharing_retry(lambda: shutil.rmtree(self.directory))
        except FileNotFoundError:
            pass
        except OSError as error:
            self.cleanup_error = f"{type(error).__name__}: {error}"
            return False
        # Older Python rmtree can report a vanished child during concurrent
        # cleanup before removing the root. Success means the root is gone.
        try:
            if self.directory.exists():
                self.cleanup_error = "resource directory still exists after cleanup"
                return False
        except OSError as error:
            self.cleanup_error = f"{type(error).__name__}: {error}"
            return False
        self.cleanup_error = None
        return True


def read_history(directory: Path, *, after: int = 0, limit: int = 120,
                 start: Optional[float] = None, end: Optional[float] = None,
                 worker: Optional[str] = None, latest: bool = False) -> dict[str, Any]:
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("after must be nonnegative; limit must be 1..500")
    if any(value is not None and not math.isfinite(value) for value in (start, end)):
        raise ValueError("time bounds must be finite")
    if start is not None and end is not None and start > end:
        raise ValueError("from must not exceed to")
    root = directory / NAME
    if not is_active(root):
        raise FileNotFoundError("resource run finished")
    try:
        manifest = json.loads(_sharing_retry(lambda: (root / "manifest.json").read_text()))
    except ValueError as error:
        raise OSError("invalid resource manifest") from error
    # Gate post-run reads even if cleanup was interrupted. PID creation time
    # prevents a reused PID making an abandoned history appear live.
    import psutil
    try:
        owner = psutil.Process(manifest["controller_pid"])
        if (owner.create_time() != manifest["controller_created_at"] or not owner.is_running()
                or owner.status() == psutil.STATUS_ZOMBIE):
            raise FileNotFoundError("resource owner exited")
    except psutil.Error as error:
        raise FileNotFoundError("resource owner unavailable") from error
    segments = manifest.pop("segments")
    if latest:
        after = max(after, manifest["latest_sequence"] - 1)
    batches: list[dict[str, Any]] = []
    size = 0
    cursor = after
    more = False
    for segment in segments:
        if segment["last"] <= after:
            continue
        if ((start is not None and segment["to"] < start)
                or (end is not None and segment["from"] > end)):
            cursor = segment["last"]
            continue
        try:
            with (root / segment["file"]).open("rb") as stream:
                remaining = segment["bytes"]
                files_snapshot: Any = None
                files_sequence = None
                while remaining > 0:
                    line = stream.readline(min(MAX_LINE + 1, remaining))
                    remaining -= len(line)
                    if not line:
                        raise OSError("published resource segment is truncated")
                    if len(line) > MAX_LINE or not line.endswith(b"\n"):
                        raise OSError("published resource record is truncated or oversized")
                    try:
                        batch = json.loads(line)
                    except ValueError as error:
                        raise OSError("invalid published resource record") from error
                    seq = batch["sequence"]
                    if not segment["first"] <= seq <= segment["last"]:
                        raise OSError("resource record outside published sequence range")
                    reference = batch.pop("_files_sequence", None)
                    if reference is not None:
                        if reference != files_sequence:
                            raise OSError("resource file snapshot reference is unavailable")
                        batch["files"] = files_snapshot
                    elif "files" in batch:
                        files_snapshot = batch["files"]
                        files_sequence = seq
                    if seq <= after:
                        continue
                    if ((start is not None and batch["observed_at"] < start)
                            or (end is not None and batch["observed_at"] > end)):
                        cursor = seq
                        continue
                    if worker:
                        batch["processes"] = [p for p in batch["processes"] if p.get("worker") == worker]
                        batch["events"] = [e for e in batch.get("events", []) if e.get("worker") == worker]
                    length = len(json.dumps(batch, separators=(",", ":")).encode())
                    if batches and (len(batches) >= limit or size + length > MAX_REPLY):
                        more = True
                        break
                    batches.append(batch)
                    size += length
                    cursor = seq
            if more:
                break
        except FileNotFoundError:
            # Concurrent rotation: caller sees the new retained range on
            # their next request. Never turn it into another run's data.
            continue
    earliest = manifest.get("earliest_sequence")
    if not is_active(root):
        raise FileNotFoundError("resource run finished during read")
    return {**manifest, "batches": batches, "next_after": cursor, "has_more": more,
            "history_truncated": bool(earliest and earliest > 1),
            "cursor_expired": bool(after and earliest and after < earliest - 1)}
