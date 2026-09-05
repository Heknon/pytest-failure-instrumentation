"""Optional bounded directory inventories in a session-owned subprocess.

A filesystem syscall can block; putting the walk in a thread cannot impose a
shutdown deadline. This helper never runs tests and is terminated at session
finish. Only explicit roots are traversed, without following links/reparse
points. The baseline is an interval, not an atomic pre-test snapshot.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .resource_history import atomic_json


class Scanner:
    def __init__(self, root: Path, database: Path, max_entries: int,
                 excluded: Path, checkpoint: Callable[..., None]) -> None:
        self.root = root
        self.max_entries = max_entries
        self.excluded = excluded
        self.checkpoint = checkpoint
        self.db = sqlite3.connect(str(database))
        self.db.execute("PRAGMA cache_size=-1024")
        self.db.execute("PRAGMA max_page_count=8192")  # 32 MiB with default 4 KiB pages
        self.db.execute("PRAGMA journal_mode=OFF")  # disposable inventory, no recovery contract
        self.db.execute("CREATE TABLE baseline(path TEXT PRIMARY KEY, size INTEGER)")
        self.db.execute("CREATE TABLE current(path TEXT PRIMARY KEY, size INTEGER)")
        self.baseline: dict[str, Any] | None = None

    def scan(self) -> dict[str, Any]:
        started = time.time()
        root_info = self.root.lstat()
        if (stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & 0x400
                or self.root == self.excluded or self.excluded in self.root.parents):
            excluded_result = {"root": str(self.root), "status": "excluded", "observed_at": started}
            self.checkpoint(excluded_result)
            return excluded_result
        self.db.execute("DELETE FROM current")
        stack = [self.root]
        count = total = errors = 0
        capped = False
        visited = 0
        checkpoint_at = time.monotonic()
        result: dict[str, Any] = {"root": str(self.root), "scan_started_at": started,
                                  "status": "scanning", "attribution": "shared_directory",
                                  "baseline": self.baseline}
        self.checkpoint(result)
        while stack and not capped:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > self.max_entries:
                            capped = True
                            break
                        try:
                            info = entry.stat(follow_symlinks=False)
                            path = Path(entry.path)
                            reparse = getattr(info, "st_file_attributes", 0) & 0x400
                            if path == self.excluded or stat.S_ISLNK(info.st_mode) or reparse:
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                if len(stack) < self.max_entries:
                                    stack.append(path)
                                else:
                                    capped = True
                                    break
                            elif stat.S_ISREG(info.st_mode):
                                if count >= self.max_entries:
                                    capped = True
                                    break
                                self.db.execute("INSERT OR REPLACE INTO current VALUES (?, ?)",
                                                (str(path.relative_to(self.root)), info.st_size))
                                count += 1
                                total += info.st_size
                        except OSError:
                            errors += 1
                        if visited % 500 == 0 or time.monotonic() - checkpoint_at >= 0.05:
                            result.update(observed_file_count=count, observed_logical_bytes=total,
                                          errors=errors, observed_at=time.time())
                            self.checkpoint(result)
                            self.db.commit()
                            time.sleep(0.05)
                            checkpoint_at = time.monotonic()
            except OSError:
                errors += 1
        self.db.commit()
        result.update(status="partial" if capped or errors else "complete", errors=errors,
                      entry_limit_reached=capped, observed_file_count=count,
                      observed_logical_bytes=total, scan_finished_at=time.time(), observed_at=time.time())
        # Never establish a false exact baseline, nor infer deletion from a
        # partial enumeration of a concurrently changing directory.
        if self.baseline is None:
            if not capped and not errors:
                self.db.execute("INSERT INTO baseline SELECT * FROM current")
                self.db.commit()
                self.baseline = {"from": started, "to": result["scan_finished_at"],
                                 "file_count": count, "logical_bytes": total}
                result["baseline"] = self.baseline
        elif not capped and not errors:
            row = self.db.execute("SELECT COUNT(*), COALESCE(SUM(c.size),0) FROM current c "
                                  "LEFT JOIN baseline b ON c.path=b.path WHERE b.path IS NULL").fetchone()
            result.update(new_remaining_count=row[0], new_remaining_bytes=row[1],
                          net_logical_bytes=total - self.baseline["logical_bytes"])
            result["deleted_baseline_count"] = self.db.execute(
                "SELECT COUNT(*) FROM baseline b LEFT JOIN current c ON b.path=c.path WHERE c.path IS NULL"
            ).fetchone()[0]
            result["grown_existing_bytes"] = self.db.execute(
                "SELECT COALESCE(SUM(c.size-b.size),0) FROM current c JOIN baseline b ON c.path=b.path "
                "WHERE c.size>b.size").fetchone()[0]
            result["largest_growth"] = [{"path": row[0][:1024], "logical_bytes": row[1], "growth_bytes": row[2]}
                for row in self.db.execute(
                    "SELECT c.path, c.size, c.size-COALESCE(b.size,0) AS growth FROM current c "
                    "LEFT JOIN baseline b ON c.path=b.path WHERE growth>0 ORDER BY growth DESC LIMIT 20")]
        self.checkpoint(result)
        return result

    def close(self) -> None:
        self.db.close()


def serve(config: dict[str, Any]) -> None:
    import psutil
    directory = Path(config["directory"])
    owner = psutil.Process(config["pid"])
    created = config["created_at"]
    snapshots: dict[str, Any] = {}
    volumes: list[dict[str, Any]] = []

    def alive() -> bool:
        try:
            return owner.is_running() and owner.create_time() == created
        except psutil.Error:
            return False

    def publish(key: str, result: dict[str, Any]) -> None:
        if not alive():
            raise SystemExit(0)
        snapshots[key] = result
        atomic_json(directory / "files.json", {"roots": list(snapshots.values()), "volumes": volumes})

    scanners = []
    for index, root in enumerate(config["roots"]):
        scanners.append(Scanner(Path(root), directory / f"inventory-{index}.sqlite",
                                config["max_entries"], Path(config["excluded"]),
                                lambda value, key=root: publish(key, value)))
    next_scan = next_volumes = 0.0
    try:
        while alive():
            now = time.monotonic()
            if now >= next_volumes:
                volumes = []
                seen = set()
                for raw in config["volumes"]:
                    try:
                        path = Path(raw)
                        device = path.stat().st_dev
                        if device in seen:
                            continue
                        seen.add(device)
                        usage = psutil.disk_usage(str(path))
                        volumes.append({"path": raw, "device": str(device), "observed_at": time.time(),
                                        "metrics": {"total_bytes": usage.total, "free_bytes": usage.free,
                                                    "used_bytes": usage.used}, "unavailable": {}})
                    except OSError as error:
                        volumes.append({"path": raw, "observed_at": time.time(), "metrics": {},
                                        "unavailable": {"space": type(error).__name__}})
                atomic_json(directory / "files.json", {"roots": list(snapshots.values()), "volumes": volumes})
                next_volumes = now + 30
            if now >= next_scan:
                for scanner in scanners:
                    try:
                        scanner.scan()
                    except (OSError, sqlite3.Error) as error:
                        publish(str(scanner.root), {"root": str(scanner.root), "status": "failed",
                                                   "error": type(error).__name__, "observed_at": time.time()})
                next_scan = time.monotonic() + config["scan_seconds"]
            time.sleep(0.2)
    finally:
        for scanner in scanners:
            scanner.close()


if __name__ == "__main__":
    serve(json.loads(sys.stdin.readline()))
