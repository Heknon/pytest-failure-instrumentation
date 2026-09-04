"""The RAM monitor: when to look, and what to write down when it looks.

Answers one question - *who is holding the memory* - for the kill that leaves
no stack. It owns no thread; the heartbeat ticks it, because a memory sample
and a liveness beat want the same cadence and a second thread would buy
nothing.

The expensive probes are opt-in. Walking the heap to count objects on a worker
already near its ceiling is the instrumentation that makes the problem worse.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections import Counter
from typing import Any, Callable

from .. import probes

DEFAULT_HIGH_WATER_FRACTION = 0.75


class MemoryMonitor:
    def __init__(
        self,
        record: Callable[..., None],
        *,
        threshold_mb: int | None = None,
        share_count: int = 1,
        high_water_fraction: float = DEFAULT_HIGH_WATER_FRACTION,
        object_census: bool = False,
        snapshot_limit: int = 8,
    ) -> None:
        self.record = record
        self.object_census = object_census
        self.snapshot_limit = snapshot_limit

        discovered = probes.memory_limit()
        self.limit_mb = discovered["limit_mb"]
        self.limit_source = discovered["limit_source"]
        self.share_count = max(1, share_count)
        # A worker's share of the limit, not the whole limit: with no cgroup the
        # discovered ceiling is the machine's RAM, and N workers exhaust it
        # together long before any one approaches it alone.
        self.share_mb = (
            round(self.limit_mb / self.share_count) if self.limit_mb else None
        )
        if threshold_mb:
            self.threshold_mb: int | None = threshold_mb
            self.threshold_source = "configured"
        elif self.share_mb:
            self.threshold_mb = round(self.share_mb * high_water_fraction)
            self.threshold_source = (
                f"{high_water_fraction:.0%} of {self.share_mb} MB "
                f"({self.limit_mb} MB / {self.share_count} workers)"
            )
        else:
            self.threshold_mb = None
            self.threshold_source = "no limit discoverable"

    def describe(self) -> dict[str, Any]:
        return {
            "limit_mb": self.limit_mb,
            "limit_source": self.limit_source,
            "share_mb": self.share_mb,
            "threshold_mb": self.threshold_mb,
            "threshold_source": self.threshold_source,
            "tracemalloc": tracemalloc.is_tracing(),
            "object_census": self.object_census,
        }

    def observe(self, resident_mb: int | None, nodeid: str | None) -> None:
        """Called on each tick. Snapshots only when the mark is crossed."""
        if resident_mb is None or not self.threshold_mb:
            return
        if resident_mb < self.threshold_mb:
            return
        self.record(
            "memory_high_water",
            rss_mb=resident_mb,
            limit_mb=self.limit_mb,
            limit_source=self.limit_source,
            threshold_mb=self.threshold_mb,
            system_available_mb=probes.system_available_megabytes()[0],
            nodeid=nodeid,
            top_allocations=self.top_allocations(),
            objects_by_type=self.objects_by_type(),
        )
        # Re-arm above the current level: a worker sitting at the mark
        # snapshots once, while real further growth is still captured.
        self.threshold_mb = resident_mb + max(128, resident_mb // 4)

    def top_allocations(self) -> list[dict[str, Any]]:
        """Source lines holding the most memory - the OOM equivalent of a stack."""
        if not tracemalloc.is_tracing():
            return []
        try:
            statistics = tracemalloc.take_snapshot().statistics("lineno")
        except Exception:
            return []
        return [
            {
                "file": statistic.traceback[0].filename,
                "line": statistic.traceback[0].lineno,
                "size_mb": round(statistic.size / 1048576, 1),
                "count": statistic.count,
            }
            for statistic in statistics[: self.snapshot_limit]
        ]

    def objects_by_type(self) -> list[list[Any]]:
        if not self.object_census:
            return []
        try:
            counter: Counter[str] = Counter()
            for item in gc.get_objects():
                counter[type(item).__name__] += 1
            return [
                [name, count] for name, count in counter.most_common(self.snapshot_limit)
            ]
        except Exception:
            return []


def enable_tracemalloc(depth: int) -> bool:
    """Trace allocations with at least ``depth`` frames each.

    A tracer already running with that many is left alone. One running with
    fewer - the watchdog's, started at ``failure_tracemalloc_depth`` before
    the profiler asked for its own - is restarted deeper: what it had traced
    so far is forgotten, and every traceback from here on has the frames
    that were asked for, rather than the holders' lines coming back one
    frame deep with nothing to say why.
    """
    if depth <= 0:
        return tracemalloc.is_tracing()
    if tracemalloc.is_tracing():
        if tracemalloc.get_traceback_limit() >= depth:
            return True
        try:
            tracemalloc.stop()
        except Exception:
            return True
    try:
        tracemalloc.start(depth)
        return True
    except Exception:
        return False
