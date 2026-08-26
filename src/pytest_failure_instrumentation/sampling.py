"""Periodic worker telemetry, pushed out while the run is still going.

The incident hook fires on a verdict - a worker died, a stall was confirmed.
That is the durable record, and it is deliberately rare. What it cannot give
anybody is the *approach*: a worker that sat in one frame for twenty minutes
before it was declared stalled looks, in the incident, exactly like one that
wedged a second before the threshold.

Polling every worker for a stack on a cadence would supply that and is what a
consumer reaches for first. It is also the single most expensive thing this
package could be made to do: a stack is ~6 KiB against a heartbeat's ~200 B,
and asking every worker every ten seconds costs a large fleet hundreds of
gigabytes a day - to answer, for almost every sample, "still working, as the
heartbeat already said".

So this samples on evidence rather than on cadence. Two things make that cheap:

**The status is free.** :mod:`.topology` already classifies every worker from
files the run was writing anyway, with nothing asked of the worker itself. A
worker burning CPU is *working*, and reading its stack tells you what you
already knew. Only ``blocked`` and ``frozen`` are worth a read.

**A stuck stack does not change.** The workers worth sampling are precisely the
ones whose frames are not moving, so the same stack is drawn over and over. It
is sent once; after that the sample carries its digest and a count of how many
times it has been seen. A worker wedged for a day costs one stack and a
counter, not eight thousand copies of one stack.

``unmeasured`` is deliberately not sampled. It means the worker never wrote a
heartbeat, which is what every worker looks like when the watchdog is off - so
sampling it would quietly mean sampling everything, which is the bill this
module exists to avoid.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from . import topology
from .probes import pyspy

#: The statuses whose stack is worth reading. See the module docstring: a
#: working worker's stack says what the heartbeat already said, and a gone
#: one has no process left to read.
WORTH_A_STACK = ("blocked", "frozen")

#: How many stacks one sample will take, however many workers are stuck. Each
#: is a subprocess that pauses its target, and a run where everything wedged at
#: once should not turn its own diagnosis into the slowest thing on the host.
#: Whatever this drops is counted in the sample rather than left implied.
MAX_STACKS_PER_SAMPLE = 16


class SampledWorker(BaseModel):
    """One worker at one instant, and its stack only when that is news."""

    model_config = ConfigDict(extra="forbid")

    worker: str = ""
    pid: Optional[int] = None
    nodeid: Optional[str] = None
    phase: Optional[str] = None

    #: ``working`` / ``blocked`` / ``frozen`` / ``gone`` / ``unmeasured`` -
    #: :mod:`.analysis.stall`'s truth table as a live status.
    status: str = ""
    #: The same finding in words, safe to show a human as-is.
    why: str = ""

    rss_mb: Optional[int] = None
    #: Cores burned between the last beats. ``None`` means "could not tell",
    #: which is not zero - that distinction is the whole of the truth table.
    cpu_rate: Optional[float] = None
    heartbeat_age_s: Optional[float] = None

    #: The frames, innermost first, and only on the sample that first sees
    #: them. ``None`` with a digest set means "unchanged since the sample that
    #: carried it".
    stack: Optional[list[dict[str, Any]]] = None
    #: Identifies the stack whether or not this sample carries it, so a
    #: consumer can join a suppressed sample to the one that has the frames.
    stack_digest: Optional[str] = None
    #: How many consecutive samples have now seen this same stack. 0 on the
    #: sample that carries it, so a worker wedged all day reads as one stack
    #: with a rising count.
    stack_repeats: int = 0
    #: Why there is no stack, when one was wanted. Absence of a stack is a
    #: fact about the host - no py-spy, a refused ptrace - and reporting it as
    #: nothing at all would read as "the worker had no frames".
    stack_error: Optional[str] = None


class WorkerSample(BaseModel):
    """One pass over this run's workers."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = ""
    run_id: Optional[str] = None
    observed_at: float = 0.0
    workers: list[SampledWorker] = Field(default_factory=list)

    #: Stuck workers this pass declined to read, having hit
    #: :data:`MAX_STACKS_PER_SAMPLE`. Named rather than counted, because the
    #: question a reader has is *which* worker they are missing.
    stacks_not_taken: list[str] = Field(default_factory=list)

    def stuck(self) -> list[SampledWorker]:
        return [w for w in self.workers if w.status in WORTH_A_STACK]


def digest_of(threads: list[dict[str, Any]]) -> str:
    """A name for a set of frames, stable across samples.

    Only the frames, never the thread names or the sample's own metadata: the
    question being asked is "is this the same place as last time", and a thread
    id that changes between reads would answer "no" to a process that has not
    moved at all.
    """
    frames = [thread.get("frames", []) for thread in threads]
    return hashlib.sha1(
        json.dumps(frames, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


class WorkerSampler:
    """Builds samples, remembering what it has already sent.

    The memo is keyed by worker *and* node id. A worker that finishes one test
    and blocks in the same library call on the next has an identical stack and
    a different subject, and suppressing the frames there would file the new
    test's evidence under the old test's document.
    """

    def __init__(
        self,
        directory: Path,
        session_id: str = "",
        want_stacks: bool = True,
        reader: Any = None,
    ) -> None:
        self.directory = directory
        self.session_id = session_id
        self.want_stacks = want_stacks
        #: Injectable so the tests do not need py-spy installed to drive the
        #: dedupe, which is the part with the logic in it.
        self.reader = reader or pyspy.dump
        self._seen: dict[str, tuple[Optional[str], str]] = {}
        self._repeats: dict[str, int] = {}

    def sample(self, now: Optional[float] = None) -> WorkerSample:
        moment = time.time() if now is None else now
        described = topology.run(self.directory, moment)
        if described is None:
            return WorkerSample(session_id=self.session_id, observed_at=round(moment, 3))

        workers: list[SampledWorker] = []
        skipped: list[str] = []
        taken = 0
        for record in described.get("workers", []):
            entry = SampledWorker(
                worker=record.get("worker") or "",
                pid=record.get("pid"),
                nodeid=record.get("nodeid"),
                phase=record.get("phase"),
                status=record.get("status") or "",
                why=record.get("why") or "",
                rss_mb=record.get("rss_mb"),
                cpu_rate=record.get("cpu_rate"),
                heartbeat_age_s=record.get("heartbeat_age_s"),
            )
            if self._wants_a_stack(entry):
                if taken >= MAX_STACKS_PER_SAMPLE:
                    skipped.append(entry.worker)
                else:
                    taken += 1
                    self._attach_stack(entry)
            else:
                # It moved on. Forgetting it means the next stall reports its
                # frames in full rather than as a repeat of something a reader
                # would have to go back hours to find.
                self._forget(entry.worker)
            workers.append(entry)

        return WorkerSample(
            session_id=self.session_id,
            run_id=described.get("run_id"),
            observed_at=round(moment, 3),
            workers=workers,
            stacks_not_taken=skipped,
        )

    def _wants_a_stack(self, entry: SampledWorker) -> bool:
        return bool(self.want_stacks and entry.pid and entry.status in WORTH_A_STACK)

    def _attach_stack(self, entry: SampledWorker) -> None:
        try:
            threads, error = self.reader(int(entry.pid or 0))
        except Exception as failure:  # noqa: BLE001 - a sample must never raise
            threads, error = None, repr(failure)
        if not threads:
            entry.stack_error = error or "the reader returned no threads"
            self._forget(entry.worker)
            return

        digest = digest_of(threads)
        previous = self._seen.get(entry.worker)
        entry.stack_digest = digest
        if previous == (entry.nodeid, digest):
            self._repeats[entry.worker] = self._repeats.get(entry.worker, 0) + 1
            entry.stack_repeats = self._repeats[entry.worker]
            return
        self._seen[entry.worker] = (entry.nodeid, digest)
        self._repeats[entry.worker] = 0
        entry.stack = threads
        entry.stack_repeats = 0

    def _forget(self, worker: str) -> None:
        self._seen.pop(worker, None)
        self._repeats.pop(worker, None)
