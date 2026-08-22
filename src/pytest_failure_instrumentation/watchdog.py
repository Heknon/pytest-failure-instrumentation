"""One background thread per worker, on a timer rather than per test.

It exists for two facts that cannot be recovered after the process dies:

* who was holding memory, for a kill that leaves no stack;
* whether the interpreter is running at all, which is what separates a slow
  test from a wedged one.

Both are sampled on an interval, so their cost is bounded by wall-clock time
rather than by how many tests run. A suite of ten thousand fast tests pays the
same as a suite of ten slow ones.

The genuinely expensive probes are opt-in. Walking the heap to count objects on
a worker that is already near its memory ceiling is exactly the kind of
instrumentation that turns a problem into a worse one.
"""

from __future__ import annotations

import gc
import threading
import time
import tracemalloc
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import probes

DEFAULT_INTERVAL = 5.0


def resident_megabytes() -> int | None:
    value, _source = probes.resident_megabytes()
    return value


class Watchdog:
    def __init__(
        self,
        record: Callable[..., None],
        *,
        interval: float = DEFAULT_INTERVAL,
        threshold_mb: int | None = None,
        share_count: int = 1,
        high_water_fraction: float = 0.75,
        object_census: bool = False,
        snapshot_limit: int = 8,
    ) -> None:
        self.record = record
        self.interval = max(1.0, interval)
        self.object_census = object_census
        self.snapshot_limit = snapshot_limit
        self.nodeid: str | None = None
        self.phase: str | None = None

        discovered = probes.memory_limit()
        self.limit_mb = discovered["limit_mb"]
        self.limit_source = discovered["limit_source"]
        self.share_count = max(1, share_count)
        # A worker's share of the limit, not the whole limit: with no cgroup
        # the discovered ceiling is the machine's RAM, and N workers exhaust it
        # together long before any one of them approaches it alone.
        self.share_mb = round(self.limit_mb / self.share_count) if self.limit_mb else None
        if threshold_mb:
            self.threshold_mb: int | None = threshold_mb
        elif self.share_mb:
            self.threshold_mb = round(self.share_mb * high_water_fraction)
        else:
            self.threshold_mb = None

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="failure-watchdog", daemon=True)

    def start(self) -> None:
        self.record(
            "watchdog_started",
            interval=self.interval,
            limit_mb=self.limit_mb,
            limit_source=self.limit_source,
            share_mb=self.share_mb,
            threshold_mb=self.threshold_mb,
            tracemalloc=tracemalloc.is_tracing(),
            object_census=self.object_census,
            capabilities=probes.capabilities(),
        )
        # A baseline beat before any test can seize the interpreter: without
        # it, code that takes the GIL immediately leaves no heartbeat at all,
        # and "frozen" cannot be told from "never started".
        self._beat(resident_megabytes())
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _beat(self, resident: int | None) -> None:
        self.record(
            "heartbeat",
            cpu_seconds=round(time.process_time(), 3),
            rss_mb=resident,
            nodeid=self.nodeid,
            phase=self.phase,
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            resident = resident_megabytes()
            if resident is None:
                return
            # The heartbeat is the stall signal and the memory sample at once:
            # one record, one interval, no separate timers.
            self._beat(resident)
            if self.threshold_mb and resident >= self.threshold_mb:
                self._capture(resident)

    def _capture(self, resident: int) -> None:
        self.record(
            "memory_high_water",
            rss_mb=resident,
            limit_mb=self.limit_mb,
            limit_source=self.limit_source,
            threshold_mb=self.threshold_mb,
            system_available_mb=probes.system_available_megabytes()[0],
            nodeid=self.nodeid,
            top_allocations=self._top_allocations(),
            objects_by_type=self._objects_by_type(),
        )
        # Re-arm above the current level so a worker sitting at the mark
        # snapshots once, while real further growth is still captured.
        self.threshold_mb = resident + max(128, resident // 4)

    def _top_allocations(self) -> list[dict[str, Any]]:
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

    def _objects_by_type(self) -> list[list[Any]]:
        """Off by default: walking the heap on a worker near its ceiling is
        slow and allocates, which is the last thing that worker needs."""
        if not self.object_census:
            return []
        try:
            counter: Counter[str] = Counter()
            for item in gc.get_objects():
                counter[type(item).__name__] += 1
            return [[name, count] for name, count in counter.most_common(self.snapshot_limit)]
        except Exception:
            return []


def enable_tracemalloc(depth: int) -> bool:
    if depth <= 0 or tracemalloc.is_tracing():
        return tracemalloc.is_tracing()
    try:
        tracemalloc.start(depth)
        return True
    except Exception:
        return False
