"""A background poller that burns CPU whatever test is running.

A fixture starts it once per session to watch for something. It sleeps
between polls, but not for long, and each poll does real work - so the worker
sits at a fraction of a core for the whole run, with no test to blame. This is
the shape of the "my worker is at 30% and I do not know why" report.
"""

from __future__ import annotations

import hashlib
import threading


class Poller:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="status-poller", daemon=True)
        self.polls = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        payload = b"x" * 400_000
        while not self._stop.wait(0.001):
            # "Check whether the status changed": a digest of a large payload,
            # five hundred times a second.
            hashlib.sha256(payload).hexdigest()
            self.polls += 1
