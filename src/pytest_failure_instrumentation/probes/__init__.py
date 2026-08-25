"""Platform truth. Nothing here raises, and nothing here reports a value
without saying which mechanism produced it."""

from __future__ import annotations

from .capabilities import capabilities
from .memory import (
    cgroup_memory,
    cgroup_oom_kills,
    memory_limit,
    resident_megabytes,
    system_available_megabytes,
)
from .process import exit_status
from .pyspy import dump as external_stack
from .stacks import can_request_stack, own_threads, request_stack

__all__ = [
    "capabilities",
    "cgroup_memory",
    "cgroup_oom_kills",
    "memory_limit",
    "resident_megabytes",
    "system_available_megabytes",
    "exit_status",
    "can_request_stack",
    "external_stack",
    "own_threads",
    "request_stack",
]
