"""Platform truth. Nothing here raises, and nothing here reports a value
without saying which mechanism produced it."""

from __future__ import annotations

from .capabilities import capabilities
from .memory import (
    allocator_figures,
    cgroup_memory,
    cgroup_oom_kills,
    heap_in_use_megabytes,
    memory_limit,
    resident_megabytes,
    system_available_megabytes,
)
from .process import exit_status, is_own_child, is_running
from .stacks import can_request_stack, live_reading, live_stack, request_stack

__all__ = [
    "allocator_figures",
    "capabilities",
    "cgroup_memory",
    "cgroup_oom_kills",
    "heap_in_use_megabytes",
    "memory_limit",
    "resident_megabytes",
    "system_available_megabytes",
    "exit_status",
    "is_own_child",
    "is_running",
    "can_request_stack",
    "live_reading",
    "live_stack",
    "request_stack",
]
