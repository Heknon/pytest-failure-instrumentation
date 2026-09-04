"""Exceptions shared across setup without importing optional runtime probes."""


class TracemallocConflict(RuntimeError):
    """Allocation profiling was asked to take ownership of an active tracer."""
