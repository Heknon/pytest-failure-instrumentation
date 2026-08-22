"""Proof that the interpreter is still running, on a thread of its own.

This is the only signal that separates a worker doing slow work from one that
is wedged. Silence from the controller means nothing on its own - it only ever
hears from a worker when a *phase completes*, so a twenty-minute test looks
exactly like a deadlock.

Each beat carries CPU time, which is what makes that distinction passive: a
worker burning CPU is working. And the absence of beats is itself the strongest
signal there is, because it means the worker's own background thread cannot
run - native code holding the GIL, or a stopped process.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_INTERVAL = 5.0


class Heartbeat:
    def __init__(
        self,
        record: Callable[..., None],
        *,
        interval: float = DEFAULT_INTERVAL,
        observers: list[Any] | None = None,
    ) -> None:
        self.record = record
        self.interval = max(1.0, interval)
        self.observers = observers or []
        self.nodeid: str | None = None
        self.phase: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="failure-heartbeat", daemon=True
        )

    def start(self) -> None:
        # A baseline beat before any test can seize the interpreter. Without it
        # code that takes the GIL immediately leaves no heartbeat at all, and
        # "frozen" cannot be told from "never started".
        self._beat()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _beat(self) -> int | None:
        from .. import probes

        resident, _source = probes.resident_megabytes()
        self.record(
            "heartbeat",
            cpu_seconds=round(time.process_time(), 3),
            rss_mb=resident,
            nodeid=self.nodeid,
            phase=self.phase,
        )
        return resident

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            resident = self._beat()
            for observer in self.observers:
                observer.observe(resident, self.nodeid)
