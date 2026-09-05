"""Controller-owned resource history for the active pytest session only."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import psutil

from .capture.resource_history import ResourceHistory
from .capture.state import read_state
from .config import Settings
from .probes.resource_metrics import PlatformMetrics, cgroup_metrics, reason

MAX_PROCESSES = 512
MAX_INVENTORY = 8192


class ResourceSampler:
    def __init__(self, directory: Path, session: str, settings: Settings) -> None:
        self.directory = directory
        self.settings = settings
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.events: deque[dict[str, Any]] = deque(maxlen=256)
        self.events_dropped = 0
        self.thread: threading.Thread | None = None
        self.helper: subprocess.Popen[str] | None = None
        self.probe = PlatformMetrics()
        # Some container launchers expose an ancestor's procfs. Resolve the
        # collector in that counter namespace instead of probing an unrelated
        # process with the same namespace-local PID.
        self.pid_map: dict[int, int] = {}
        self.foreign_procfs = False
        self.namespace = ""
        visible_pid = os.getpid()
        if sys.platform == "linux":
            visible_pid = int(Path("/proc/self/stat").read_text().split(" ", 1)[0])
            self.foreign_procfs = visible_pid != os.getpid()
            self.namespace = os.readlink("/proc/self/ns/pid")
        self.owner = psutil.Process(visible_pid)
        self.created = self.owner.create_time()
        self.started = time.monotonic()
        self.history = ResourceHistory(directory, {
            "session": session, "controller_pid": self.owner.pid, "controller_created_at": self.created,
            "pytest_pid": os.getpid(), "pid_scope": "procfs" if self.foreign_procfs else "local",
            "started_at": time.time(), "system": platform.system(), "python": platform.python_version(),
            "product_version": settings.product_version, "worker_count": settings.worker_count,
            "sample_seconds": settings.resources_seconds,
            "scope": "visible_os_and_process_namespace",
            "cgroups": {key: str(path) for key, path in self.probe.cgroups.items()},
            "limits": {"tracked_processes": MAX_PROCESSES, "inventory_processes": MAX_INVENTORY,
                       "surrounding_consumers": 20},
        }, int(settings.resources_max_mb) * 1024 * 1024)
        self.tracked: dict[tuple[int, float], dict[str, Any]] = {}
        self.inventory_at = 0.0
        self.inventory: list[dict[str, Any]] = []
        self.inventory_status: dict[str, str] = {}
        self.errors = 0
        self.last_error: str | None = None
        self.missed_intervals = 0
        self._track((self.owner.pid, self.created),
                    {"name": self.owner.name(), "ppid": self.owner.ppid()}, None, "controller")

    def start(self) -> None:
        try:
            # One helper serves both slow free-space queries and optional
            # directory walks. It has no pytest hooks and outlives no session.
            self.helper = subprocess.Popen(
                [sys.executable, "-m", "pytest_failure_instrumentation.capture.file_resources"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, env=os.environ.copy(),
            )
            assert self.helper.stdin is not None
            self.helper.stdin.write(json.dumps({
                "directory": str(self.history.directory), "pid": self.owner.pid, "created_at": self.created,
                "roots": list(self.settings.resources_roots),
                "volumes": list(dict.fromkeys([str(Path.cwd()), str(self.directory),
                                               *self.settings.resources_roots])),
                "excluded": str(self.directory.parent.resolve()),
                "max_entries": int(self.settings.resources_max_files),
                "scan_seconds": self.settings.resources_scan_seconds,
            }) + "\n")
            self.helper.stdin.close()
        except (OSError, BrokenPipeError) as error:
            self.last_error = "file_helper: " + reason(error)
        self.thread = threading.Thread(target=self._run, name="failure-resources", daemon=True)
        self.thread.start()

    def event(self, value: dict[str, Any]) -> None:
        with self.lock:
            if len(self.events) == self.events.maxlen:
                self.events_dropped += 1
            self.events.append({"observed_at": time.time(), **value})

    def _workers(self) -> dict[int, dict[str, Any]]:
        workers: dict[int, dict[str, Any]] = {}
        for path in self.directory.glob("*.state"):
            state = read_state(path)
            pid = state.get("pid")
            if isinstance(pid, int) and len(workers) < MAX_PROCESSES:
                visible = self.pid_map.get(pid) if self.foreign_procfs else pid
                if visible is None:
                    self.inventory_at = 0.0
                    continue
                workers[visible] = {"worker": path.stem, "nodeid": (state.get("nodeid") or "")[:1024],
                                "phase": state.get("phase"), "state_time": state.get("time", 0)}
        return workers

    def _discover(self, workers: dict[int, dict[str, Any]], now: float) -> None:
        observed: dict[int, dict[str, Any]] = {}
        errors = 0
        truncated = False
        self.pid_map = {}
        for index, proc in enumerate(psutil.process_iter()):
            if index >= MAX_INVENTORY:
                truncated = True
                break
            try:
                if self.foreign_procfs:
                    try:
                        root = Path("/proc") / str(proc.pid)
                        if os.readlink(root / "ns/pid") == self.namespace:
                            for line in (root / "status").read_text().splitlines():
                                if line.startswith("NSpid:"):
                                    self.pid_map[int(line.split()[-1])] = proc.pid
                    except (OSError, ValueError):
                        pass
                with proc.oneshot():
                    created = proc.create_time()
                    observed[proc.pid] = {"pid": proc.pid, "created_at": created, "ppid": proc.ppid(),
                                          "name": proc.name()[:128], "rss_bytes": proc.memory_info().rss,
                                          "cpu_total_seconds": sum(proc.cpu_times()[:2])}
            except psutil.Error:
                errors += 1
        self.inventory_status = {}
        if truncated:
            self.inventory_status["processes"] = "inventory_truncated"
        if errors:
            self.inventory_status["inaccessible_process_count"] = str(errors)
        # Only cheap fields for unrelated consumers; never inspect their
        # command line, environment or file handles.
        for row in observed.values():
            self.probe.rates("inventory:" + str((row["pid"], row["created_at"])), row, now)
        memory = sorted(observed.values(), key=lambda p: p["rss_bytes"], reverse=True)[:10]
        cpu = sorted(observed.values(), key=lambda p: p.get("cpu_cores") or 0, reverse=True)[:10]
        self.inventory = list({(r["pid"], r["created_at"]): r for r in memory + cpu}.values())
        self.inventory_at = now
        # Establish ownership while parent relationships are observable, then
        # retain it after reparenting. Never infer a test owner for a shared child.
        known = {pid: key for key in self.tracked for pid in [key[0]]}
        controller_key = (self.owner.pid, self.created)
        for row in observed.values():
            key = row["pid"], row["created_at"]
            worker = workers.get(row["pid"])
            if worker and row["created_at"] <= worker["state_time"]:
                self._track(key, row, worker["worker"], "worker")
                known[row["pid"]] = key
            elif key == controller_key:
                self._track(key, row, None, "controller")
                known[row["pid"]] = key
        # At most O(n * depth), with a fixed maximum depth and process count.
        for _ in range(16):
            added = False
            for row in observed.values():
                key = row["pid"], row["created_at"]
                parent_key = known.get(row["ppid"])
                if key in self.tracked or parent_key is None:
                    continue
                parent = self.tracked.get(parent_key)
                parent_row = observed.get(row["ppid"])
                if not parent or not parent_row or parent_row["created_at"] != parent_key[1]:
                    continue
                if row["created_at"] < parent_key[1]:
                    continue
                if self.helper is not None and row["pid"] == (self.pid_map.get(self.helper.pid) if self.foreign_procfs else self.helper.pid):
                    continue
                if self._track(key, row, parent["worker"], "descendant"):
                    known[row["pid"]] = key
                    added = True
            if not added:
                break
        keep = {"inventory:" + str((r["pid"], r["created_at"])) for r in observed.values()}
        for rate_key in list(self.probe.previous):
            if rate_key.startswith("inventory:") and rate_key not in keep:
                del self.probe.previous[rate_key]

    def _track(self, key: tuple[int, float], row: dict[str, Any], worker: str | None, role: str) -> bool:
        if key in self.tracked:
            self.tracked[key].update(worker=worker, role=role)
            return False
        if len(self.tracked) >= MAX_PROCESSES:
            self.inventory_status["tracked_processes"] = "truncated"
            return False
        self.tracked[key] = {"pid": key[0], "created_at": key[1], "name": row["name"],
                             "parent_pid": row["ppid"], "worker": worker, "role": role,
                             "first_observed_at": time.time()}
        self.event({"kind": "process_observed", **self.tracked[key]})
        return True

    def sample(self) -> dict[str, Any]:
        began = time.monotonic()
        workers = self._workers()
        if began - self.inventory_at >= 15 or not self.tracked:
            self._discover(workers, began)
            workers = self._workers()
        # Register newly started workers promptly without waiting for the
        # slower full-host inventory. Validate their state timestamp.
        for pid, worker in workers.items():
            try:
                proc = psutil.Process(pid)
                key = pid, proc.create_time()
                if key[1] <= worker["state_time"]:
                    self._track(key, {"name": proc.name(), "ppid": proc.ppid()}, worker["worker"], "worker")
            except psutil.Error:
                pass
        processes = []
        for key, row in list(self.tracked.items()):
            try:
                proc = psutil.Process(key[0])
                if proc.create_time() != key[1] or not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    raise psutil.NoSuchProcess(key[0])
                values, missing = self.probe.process(proc)
                self.probe.rates("process:" + str(key), values, time.monotonic())
                worker = workers.get(key[0], {}) if row["role"] == "worker" else {}
                processes.append({**row, "metrics": values, "unavailable": missing,
                                  "observed_at": time.time(), "nodeid": worker.get("nodeid"),
                                  "phase": worker.get("phase")})
            except psutil.NoSuchProcess:
                self.event({"kind": "process_no_longer_observed", **row})
                del self.tracked[key]
                self.probe.previous.pop("process:" + str(key), None)
            except psutil.Error as error:
                processes.append({**row, "metrics": {}, "unavailable": {"process": reason(error)}})
        live_workers = {p["worker"] for p in processes if p["role"] == "worker"}
        for row in processes:
            row["worker_exited"] = bool(row["role"] == "descendant" and row["worker"] and row["worker"] not in live_workers)
        host, missing = self.probe.host()
        self.probe.rates("host", host, time.monotonic())
        host["cpu_percent"] = None
        if host.get("cpu_cores") is not None and host.get("cpu_logical_count"):
            host["cpu_percent"] = host["cpu_cores"] / host["cpu_logical_count"] * 100
        disks, disk_missing = self.probe.disks()
        for disk in disks:
            self.probe.rates("disk:" + disk["entity"], disk["metrics"], time.monotonic())
        cgroup, cgroup_missing = cgroup_metrics(self.probe.cgroups) if self.probe.cgroups else ({}, {"cgroup": "unavailable"})
        files: dict[str, Any] = {"roots": [], "volumes": [], "status": "starting"}
        try:
            with (self.history.directory / "files.json").open("rb") as handle:
                raw = handle.read(256 * 1024 + 1)
                if len(raw) <= 256 * 1024:
                    files = json.loads(raw)
        except (OSError, ValueError):
            pass
        if self.helper is not None and self.helper.poll() is not None:
            files["status"] = "helper_stopped"
        with self.lock:
            events = list(self.events)
            self.events.clear()
        return {"observed_at": time.time(), "elapsed_s": began - self.started,
                "host": {"metrics": host, "unavailable": missing},
                "cgroup": {"metrics": cgroup, "unavailable": cgroup_missing},
                "disks": disks, "disk_unavailable": disk_missing,
                "processes": processes, "consumers": self.inventory,
                "inventory_age_s": began - self.inventory_at, "inventory_unavailable": self.inventory_status,
                "files": files, "events": events,
                "collector": {"sample_duration_s": time.monotonic() - began,
                              "errors": self.errors, "last_error": self.last_error,
                              "missed_intervals": self.missed_intervals, "events_dropped": self.events_dropped,
                              "filesystem_helper_pid": self.helper.pid if self.helper is not None else None}}

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                began = time.monotonic()
                try:
                    batch = self.sample()
                    if not self.stop_event.is_set():
                        self.history.append(batch)
                except Exception as error:
                    self.errors += 1
                    self.last_error = reason(error)
                elapsed = time.monotonic() - began
                self.missed_intervals += int(elapsed // self.settings.resources_seconds)
                if self.stop_event.wait(max(0.1, self.settings.resources_seconds - elapsed)):
                    break
        finally:
            self.probe.close()
            if self.stop_event.is_set():
                self.history.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.helper is not None and self.helper.poll() is None:
            self.helper.terminate()
            try:
                self.helper.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.helper.kill()
                try:
                    self.helper.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        if self.thread is not None:
            self.thread.join(timeout=2)
        else:
            self.probe.close()
        self.history.close()
