"""What this machine can and cannot measure.

Recorded on every alert. On mixed, customer-controlled machines a reader needs
to tell "nothing went wrong" from "unmeasurable here", and only the capability
record answers that.
"""

from __future__ import annotations

import os
import platform
from typing import Any

from . import memory, pyspy, stacks, tracing
from .platform_flags import IS_WINDOWS, platform_description


def capabilities(*, resources: bool = False) -> dict[str, Any]:
    resident, resident_source = memory.resident_megabytes()
    available, available_source = memory.system_available_megabytes()
    result = {
        "platform": platform_description(),
        "system": "Windows" if IS_WINDOWS else platform.system(),
        "python": platform.python_version(),
        "resident_memory": resident_source if resident is not None else "unavailable",
        "system_memory": available_source if available is not None else "unavailable",
        "cgroup_oom_counter": memory.cgroup_oom_kills() is not None,
        "exit_status": "waitid"
        if hasattr(os, "waitid")
        else ("windows" if IS_WINDOWS else "popen-only"),
        "live_stack": stacks.can_request_stack(),
        # Whether another process can be read from outside it, which is the
        # only way to get a stack out of a worker whose GIL is held by
        # native code.
        "external_stack": "py-spy" if pyspy.available() else "unavailable",
        # A dependency rather than an upgrade, so this is a constant now.
        # Kept because a consumer's table has the column, and a field that
        # disappears is a migration where a field that stops varying is not.
        "psutil": True,
        # What this machine lets one process read of another. Decides whether
        # the live view can answer for a worker at all - see probes.tracing.
        "ptrace_scope": tracing.ptrace_scope(),
    }

    if resources:
        # Explicit preflight only: the ordinary incident/default path does not
        # import or initialize the resource collector's native adapters.
        from .resource_metrics import PlatformMetrics, reason
        probe = PlatformMetrics()
        try:
            _, missing = probe.host()
            result["resource_cgroup"] = {key: str(path) for key, path in probe.cgroups.items()}
            result["resource_native"] = {"system": probe.system, "unavailable": missing}
            from pathlib import Path

            import psutil
            pid = os.getpid()
            if probe.system == "Linux":
                pid = int(Path("/proc/self/stat").read_text().split(" ", 1)[0])
            values, unavailable = probe.process(psutil.Process(pid))
            result["resource_process_io"] = {"supported": "read_total_bytes" in values,
                                             "unavailable": unavailable}
        except Exception as error:
            result["resource_preflight_error"] = reason(error)
        finally:
            probe.close()
    return result
