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
from pathlib import Path
from typing import Any, Optional

NAME = "resources-live"
MAX_LINE = 2 * 1024 * 1024
MAX_REPLY = 2 * 1024 * 1024


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)
    os.replace(temporary, path)


class ResourceHistory:
    def __init__(self, directory: Path, metadata: dict[str, Any], max_bytes: int) -> None:
        self.directory = directory / NAME
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_bytes = max(4 * MAX_LINE, max_bytes)
        self.segment_bytes = max(MAX_LINE, self.max_bytes // 16)
        self.segments: list[dict[str, Any]] = []
        self.sequence = 0
        self.metadata = {"schema_version": 1, **metadata, "max_bytes": self.max_bytes}
        self._publish()

    def _publish(self) -> None:
        atomic_json(self.directory / "manifest.json", {
            **self.metadata, "segments": self.segments,
            "latest_sequence": self.sequence,
            "earliest_sequence": self.segments[0]["first"] if self.segments else None,
            "history_bytes": sum(s["bytes"] for s in self.segments),
        })

    def append(self, batch: dict[str, Any]) -> None:
        sequence = self.sequence + 1
        encoded = (json.dumps({**batch, "sequence": sequence}, separators=(",", ":"),
                              allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_LINE:
            raise ValueError("resource sample exceeds record budget")
        if not self.segments or self.segments[-1]["bytes"] + len(encoded) > self.segment_bytes:
            self.segments.append({"file": f"{sequence:016d}.jsonl", "first": sequence,
                                  "last": sequence, "bytes": 0,
                                  "from": batch["observed_at"], "to": batch["observed_at"]})
        while len(self.segments) > 1 and sum(s["bytes"] for s in self.segments) + len(encoded) > self.max_bytes:
            old = self.segments[0]
            (self.directory / old["file"]).unlink(missing_ok=True)
            self.segments.pop(0)
        segment = self.segments[-1]
        with (self.directory / segment["file"]).open("ab") as stream:
            stream.write(encoded)
            stream.flush()
        segment.update(last=sequence, to=max(segment["to"], batch["observed_at"]),
                       bytes=segment["bytes"] + len(encoded))
        segment["from"] = min(segment["from"], batch["observed_at"])
        self.sequence = sequence
        self._publish()

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


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
    try:
        manifest = json.loads((root / "manifest.json").read_text())
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
                while True:
                    line = stream.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE or not line.endswith(b"\n"):
                        break
                    try:
                        batch = json.loads(line)
                    except ValueError:
                        break
                    seq = batch["sequence"]
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
    return {**manifest, "batches": batches, "next_after": cursor, "has_more": more,
            "history_truncated": bool(earliest and earliest > 1),
            "cursor_expired": bool(after and earliest and after < earliest - 1)}
