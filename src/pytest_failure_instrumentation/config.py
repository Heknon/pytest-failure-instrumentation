"""Every setting in one place, resolved once.

Defaults are chosen so that installing the plugin and doing nothing costs a
run almost nothing: the watchdog samples on a timer rather than per test, the
expensive probes are off, and no memory ceiling is imposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

DEFAULT_DIRECTORY = ".pytest-failures"


@dataclass(frozen=True)
class Settings:
    directory: Path
    packages: tuple[str, ...]
    product_version: str | None
    watchdog: bool
    heartbeat_interval: float
    tracemalloc_depth: int
    object_census: bool
    high_water_mb: int
    memory_limit_mb: int
    slow_test_seconds: float
    stall_seconds: float
    stack_probe: bool
    worker_count: int


def add_options(parser: pytest.Parser) -> None:
    parser.addini("failure_directory", help="Where evidence is written.", default=DEFAULT_DIRECTORY)
    parser.addini(
        "failure_packages",
        help="Your own top-level packages, so a failing frame can be told from "
        "a dependency's and from the customer's own tests.",
        type="args",
        default=[],
    )
    parser.addini("failure_product_version", help="Version recorded on every incident.", default="")
    parser.addini("failure_watchdog", help="Sample memory and liveness on a timer.", default="true")
    parser.addini("failure_heartbeat_interval", help="Seconds between liveness beats.", default="5.0")
    parser.addini(
        "failure_tracemalloc_depth",
        help="Allocation traceback frames to keep. 0 disables. 1 is cheap and "
        "names the allocating line, which is what attributes an OOM kill.",
        default="0",
    )
    parser.addini(
        "failure_object_census",
        help="Count live objects at a memory high-water mark. Off by default: "
        "walking the heap on a worker near its ceiling makes things worse.",
        default="false",
    )
    parser.addini("failure_high_water_mb", help="Absolute memory mark for a snapshot.", default="0")
    parser.addini(
        "failure_memory_limit_mb",
        help="Soft address-space cap per worker (POSIX). Turns a silent OOM "
        "kill into a MemoryError attributed to the offending test.",
        default="0",
    )
    parser.addini(
        "failure_slow_test_seconds",
        help="A test running longer than this dumps its own stack, so a hang "
        "leaves evidence without anything having to signal it.",
        default="120",
    )
    parser.addini("failure_stall_seconds", help="Silence before a stall is assessed. 0 disables.", default="300")
    parser.addini(
        "failure_stack_probe",
        help="Ask an already-diagnosed stalled worker for a fresh stack "
        "(POSIX only). Can nudge a C extension blocked in a syscall.",
        default="true",
    )


def _flag(config: pytest.Config, name: str) -> bool:
    return str(config.getini(name)).strip().lower() not in ("false", "0", "no", "")


def _number(config: pytest.Config, name: str, fallback: float) -> float:
    try:
        return float(config.getini(name) or fallback)
    except (ValueError, TypeError):
        return fallback


def resolve(config: pytest.Config) -> Settings:
    workerinput: dict[str, Any] = getattr(config, "workerinput", {}) or {}
    return Settings(
        directory=Path(config.getini("failure_directory") or DEFAULT_DIRECTORY),
        packages=tuple(config.getini("failure_packages") or ()),
        product_version=config.getini("failure_product_version") or None,
        watchdog=_flag(config, "failure_watchdog"),
        heartbeat_interval=_number(config, "failure_heartbeat_interval", 5.0),
        tracemalloc_depth=int(_number(config, "failure_tracemalloc_depth", 0)),
        object_census=_flag(config, "failure_object_census"),
        high_water_mb=int(_number(config, "failure_high_water_mb", 0)),
        memory_limit_mb=int(_number(config, "failure_memory_limit_mb", 0)),
        slow_test_seconds=_number(config, "failure_slow_test_seconds", 120.0),
        stall_seconds=_number(config, "failure_stall_seconds", 300.0),
        stack_probe=_flag(config, "failure_stack_probe"),
        worker_count=int(workerinput.get("workercount", 1) or 1),
    )
