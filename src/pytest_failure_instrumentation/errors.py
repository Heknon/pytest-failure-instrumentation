"""Exceptions shared across setup without importing optional runtime probes.

Setup - :mod:`.registration` - is written so that no import it makes at module
scope can reach psutil or py-spy: a declared dependency that is somehow absent
has to become a warning and a run that still runs, not an ImportError thrown
out of ``pytest_configure``. This module exists to be importable from there,
so it imports nothing but the standard library.
"""

from __future__ import annotations

import tracemalloc


class TracemallocConflict(RuntimeError):
    """Allocation profiling was asked to take ownership of an active tracer."""


def refuse_a_shared_tracer() -> None:
    """Raise if something else is already tracing allocations.

    ``tracemalloc`` has one set of tables per process and one depth for them,
    so a second owner reports the first one's frames at the first one's depth
    and the first one's totals include the second's: both profiles are then
    somebody else's. There is no way to take a private one.

    This is the single place that decides it, called from two: once at setup,
    before the controller has spawned a worker, and again by the session that
    actually starts the tracer. The early call is what makes the refusal a
    usage error at the top of the run rather than the same error on every
    worker at once, four hookspec tracebacks deep, after xdist has spent a
    minute starting them.
    """
    if not tracemalloc.is_tracing():
        return
    raise TracemallocConflict(
        "--failure-profile-allocations requires exclusive ownership of "
        f"tracemalloc, but it is already active with depth {tracemalloc.get_traceback_limit()}; "
        "disable the other allocation profiler and rerun"
    )
