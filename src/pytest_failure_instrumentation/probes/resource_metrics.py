"""Cheap, read-only resource probes. No shells, process pauses or heap walks.

Metric names include their units. Missing values are accompanied by a reason;
Windows private commit, RSS and macOS footprint deliberately stay distinct.
"""
from __future__ import annotations

import platform
from pathlib import Path
from typing import Any, Callable

import psutil


def reason(error: Exception) -> str:
    if isinstance(error, (psutil.AccessDenied, PermissionError)):
        return "permission_denied"
    if isinstance(error, (psutil.NoSuchProcess, ProcessLookupError)):
        return "process_gone"
    if isinstance(error, (AttributeError, NotImplementedError)):
        return "unsupported"
    return type(error).__name__


def attempt(values: dict[str, Any], missing: dict[str, str], group: str,
            read: Callable[..., dict[str, Any]]) -> None:
    try:
        values.update(read())
    except Exception as error:
        missing[group] = reason(error)


def fields(value: Any, prefix: str, names: dict[str, str]) -> dict[str, Any]:
    return {prefix + dest: getattr(value, source) for source, dest in names.items()
            if hasattr(value, source)}


def pairs(path: Path, kilobytes: bool = False) -> dict[str, int]:
    result = {}
    for line in path.read_text().splitlines():
        words = line.replace(":", "").split()
        if len(words) >= 2:
            result[words[0]] = int(words[1]) * (1024 if kilobytes else 1)
    return result


def pressure(path: Path) -> dict[str, float]:
    result = {}
    for line in path.read_text().splitlines():
        words = line.split()
        for word in words[1:]:
            name, value = word.split("=")
            suffix = "total_us" if name == "total" else name + "_percent"
            result[words[0] + "_" + suffix] = float(value)
    return result


def _unescape(value: str) -> str:
    for escaped, char in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(escaped, char)
    return value


def cgroup_paths(proc: Path = Path("/proc")) -> dict[str, Path]:
    """Resolve membership against mount roots, including cgroup namespaces."""
    memberships = {}
    for line in (proc / "self/cgroup").read_text().splitlines():
        _, controller_list, member_path = line.split(":", 2)
        for controller in controller_list.split(","):
            memberships[controller] = member_path
    found: dict[str, Path] = {}
    for line in (proc / "self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount = left.split()
        info = right.split()
        if info[0] not in ("cgroup", "cgroup2"):
            continue
        root, point = _unescape(mount[3]), Path(_unescape(mount[4]))
        controllers = [""] if info[0] == "cgroup2" else info[2].split(",")
        for controller in controllers:
            member = memberships.get(controller)
            if member is None:
                continue
            if member == "/":  # namespace root is the visible mount
                relative = ""
            elif root == "/":
                relative = member.lstrip("/")
            elif member == root or member.startswith(root.rstrip("/") + "/"):
                relative = member[len(root):].lstrip("/")
            else:
                continue
            if ".." not in Path(relative).parts:
                found[controller or "v2"] = point / relative
    return found


def cgroup_metrics(paths: dict[str, Path]) -> tuple[dict[str, Any], dict[str, str]]:
    values: dict[str, Any] = {}
    missing: dict[str, str] = {}

    def scalar(path: Path, name: str) -> dict[str, Any]:
        raw = path.read_text().strip()
        value = None if raw == "max" else int(raw)
        if name == "memory_limit_bytes" and value is not None and value >= 1 << 60:
            value = None
        return {name: value}

    if "v2" in paths:
        root = paths["v2"]
        for file, name in (("memory.current", "memory_current_bytes"),
                           ("memory.max", "memory_limit_bytes"),
                           ("memory.peak", "memory_peak_bytes"),
                           ("memory.swap.current", "swap_current_bytes"),
                           ("memory.swap.max", "swap_limit_bytes")):
            attempt(values, missing, name, lambda f=file, n=name: scalar(root / f, n))
        for file in ("memory.events", "cpu.stat"):
            attempt(values, missing, file, lambda f=file: {
                f.replace(".", "_") + "_" + k: v for k, v in pairs(root / f).items()})
        def quota() -> dict[str, Any]:
            quota, period = (root / "cpu.max").read_text().split()
            return {"cpu_quota_cores": None if quota == "max" else int(quota) / int(period)}
        attempt(values, missing, "cpu_quota", quota)
        for resource in ("cpu", "memory", "io"):
            attempt(values, missing, resource + "_pressure", lambda r=resource: {
                r + "_pressure_" + k: v for k, v in pressure(root / (r + ".pressure")).items()})
    else:
        for controller, file, name in (
            ("memory", "memory.usage_in_bytes", "memory_current_bytes"),
            ("memory", "memory.limit_in_bytes", "memory_limit_bytes"),
            ("memory", "memory.failcnt", "memory_limit_failures"),
            ("cpu", "cpu.cfs_quota_us", "cpu_quota_us"),
            ("cpu", "cpu.cfs_period_us", "cpu_period_us"),
        ):
            if controller in paths:
                attempt(values, missing, name, lambda c=controller, f=file, n=name:
                        scalar(paths[c] / f, n))
        if "cpu" in paths:
            attempt(values, missing, "cpu_stat", lambda: {
                "cpu_stat_" + k: v for k, v in pairs(paths["cpu"] / "cpu.stat").items()})
        quota_value = values.get("cpu_quota_us")
        period = values.get("cpu_period_us")
        if quota_value is not None and period:
            values["cpu_quota_cores"] = None if quota_value < 0 else quota_value / period
    return values, missing


class PlatformMetrics:
    def __init__(self) -> None:
        self.system = platform.system()
        self.cgroups: dict[str, Path] = {}
        self.native: Any = None
        self.native_error = "unsupported"
        try:
            if self.system == "Linux":
                self.cgroups = cgroup_paths()
            elif self.system == "Windows":
                from .resource_windows import WindowsMetrics
                self.native = WindowsMetrics()
            elif self.system == "Darwin":
                from .resource_macos import MacMetrics
                self.native = MacMetrics()
        except Exception as error:
            self.native_error = reason(error)
        self.previous: dict[str, tuple[float, dict[str, float]]] = {}

    def rates(self, key: str, values: dict[str, Any], now: float) -> None:
        counters = {k: float(v) for k, v in values.items()
                    if isinstance(v, (int, float)) and (
                        k.endswith("_total_bytes") or k.endswith("_total_count")
                        or k == "cpu_total_seconds" or k.endswith("_time_ms"))}
        previous = self.previous.get(key)
        if key.startswith("disk:"):
            for direction in ("read", "write"):
                target = direction + "_latency_ms"
                values[target] = None
                duration, count = direction + "_time_ms", direction + "_total_count"
                if previous and all(name in counters and name in previous[1] for name in (duration, count)):
                    operations = counters[count] - previous[1][count]
                    elapsed = counters[duration] - previous[1][duration]
                    if operations > 0 and elapsed >= 0:
                        values[target] = elapsed / operations
        self.previous[key] = now, counters
        for name, value in counters.items():
            target = "cpu_cores" if name == "cpu_total_seconds" else name.replace("_total_", "_per_second_")
            values[target] = None
            if previous and now > previous[0] and name in previous[1] and value >= previous[1][name]:
                values[target] = (value - previous[1][name]) / (now - previous[0])

    def host(self) -> tuple[dict[str, Any], dict[str, str]]:
        values: dict[str, Any] = {}
        missing: dict[str, str] = {}
        attempt(values, missing, "memory", lambda: fields(psutil.virtual_memory(), "", {
            "total": "ram_total_bytes", "available": "ram_available_bytes"}))
        attempt(values, missing, "swap", lambda: fields(psutil.swap_memory(), "", {
            "total": "swap_total_bytes", "used": "swap_used_bytes",
            "sin": "swap_in_total_bytes", "sout": "swap_out_total_bytes"}))
        if self.system == "Windows":
            # psutil reports zero for unsupported Windows swap traffic.
            for name in ("swap_in_total_bytes", "swap_out_total_bytes"):
                values.pop(name, None)
                missing[name] = "unsupported"
        def cpu() -> dict[str, Any]:
            times = psutil.cpu_times()
            excluded = {"guest", "guest_nice"}
            if self.system == "Windows":
                excluded.update(("interrupt", "dpc"))  # already included in system time
            total = sum(v for k, v in times._asdict().items() if k not in excluded)
            idle = times.idle + getattr(times, "iowait", 0)
            return {"cpu_total_seconds": total - idle, "cpu_capacity_seconds": total,
                    "cpu_logical_count": psutil.cpu_count(),
                    **fields(times, "cpu_", {"iowait": "iowait_seconds", "steal": "steal_seconds",
                                               "interrupt": "interrupt_seconds", "dpc": "dpc_seconds"})}
        attempt(values, missing, "cpu", cpu)
        if self.system == "Linux":
            for resource in ("cpu", "memory", "io"):
                attempt(values, missing, resource + "_pressure", lambda r=resource: {
                    r + "_pressure_" + k: v for k, v in pressure(Path("/proc/pressure") / r).items()})
            def vm() -> dict[str, Any]:
                data = pairs(Path("/proc/vmstat"))
                return {"vm_" + k + "_total_count": data[k] for k in (
                    "pgfault", "pgmajfault", "pswpin", "pswpout", "pgscan_kswapd", "pgscan_direct",
                    "pgsteal_kswapd", "pgsteal_direct", "oom_kill") if k in data}
            attempt(values, missing, "vm", vm)
        elif self.native is not None:
            attempt(values, missing, "native", lambda: self.native.host())
            missing.update(getattr(self.native, "unavailable", {}))
        else:
            missing["native"] = self.native_error
        return values, missing

    def disks(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        try:
            disks = psutil.disk_io_counters(perdisk=True) or {}
            result = []
            for name, counters in sorted(disks.items())[:128]:
                values = fields(counters, "", {
                    "read_bytes": "read_total_bytes", "write_bytes": "write_total_bytes",
                    "read_count": "read_total_count", "write_count": "write_total_count",
                    "read_time": "read_time_ms", "write_time": "write_time_ms",
                    "busy_time": "busy_time_ms"})
                result.append({"entity": name, "metrics": values})
            return result, {"inventory": "truncated"} if len(disks) > 128 else {}
        except Exception as error:
            return [], {"disk_io": reason(error)}

    def process(self, process: psutil.Process) -> tuple[dict[str, Any], dict[str, str]]:
        values: dict[str, Any] = {}
        missing: dict[str, str] = {}
        with process.oneshot():
            attempt(values, missing, "cpu", lambda: {
                "cpu_total_seconds": sum(process.cpu_times()[:2])})
            attempt(values, missing, "memory", lambda: fields(process.memory_info(), "", {
                "rss": "rss_bytes", "vms": "virtual_bytes", "private": "private_commit_bytes",
                "peak_wset": "peak_rss_bytes", "num_page_faults": "page_faults_total_count"}))
            attempt(values, missing, "io", lambda: fields(process.io_counters(), "", {
                "read_bytes": "read_total_bytes", "write_bytes": "write_total_bytes",
                "read_count": "read_total_count", "write_count": "write_total_count"}))
            attempt(values, missing, "threads", lambda: {"thread_count": process.num_threads()})
            if self.system == "Windows":
                attempt(values, missing, "handles", lambda: {"handle_count": process.num_handles()})
            else:
                attempt(values, missing, "fds", lambda: {"fd_count": process.num_fds()})
        if self.system == "Linux":
            def resident_breakdown() -> dict[str, Any]:
                root = Path(psutil.PROCFS_PATH) / str(process.pid)
                names = {"RssAnon:": "rss_anonymous_bytes", "RssFile:": "rss_file_bytes",
                         "RssShmem:": "rss_shared_bytes", "VmSwap:": "swap_bytes", "VmHWM:": "peak_rss_bytes"}
                found = {}
                for line in (root / "status").read_text().splitlines():
                    words = line.split()
                    if words and words[0] in names:
                        found[names[words[0]]] = int(words[1]) * 1024
                raw = (root / "stat").read_text().rsplit(")", 1)[1].split()
                found["minor_faults_total_count"] = int(raw[7])
                found["major_faults_total_count"] = int(raw[9])
                return found
            attempt(values, missing, "resident_breakdown", resident_breakdown)
        if self.system == "Darwin" and self.native is not None:
            attempt(values, missing, "process_native", lambda: self.native.process(process.pid))
            if "read_total_bytes" in values:
                missing.pop("io", None)
        return values, missing

    def close(self) -> None:
        if self.native is not None:
            self.native.close()
