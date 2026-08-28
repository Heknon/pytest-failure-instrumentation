"""Periodic worker telemetry, pushed out while the run is still going.

The incident hook fires on a verdict - a worker died, a stall was confirmed.
That is the durable record, and it is deliberately rare. What it cannot give
anybody is the *approach*: a worker that sat in one frame for twenty minutes
before it was declared stalled looks, in the incident, exactly like one that
wedged a second before the threshold.

So this pushes, every ``failure_sample_seconds``, one row per worker: the
status :mod:`.topology` reads out of the ``.state`` and ``.events`` files the
run was writing anyway, and the node id, phase, resident memory and CPU rate
that come with it. Nothing is asked of the workers themselves - no ptrace, no
subprocess, no pause - so the whole of a sample costs a directory walk and a
tail of each event log.

**Why this exists next to the live server.** :mod:`.stack_server`'s
``/workers`` answers the same question from the same files, in more detail and
at whatever cadence the thing watching chooses, and costs nothing at all when
nobody is watching - so where a dashboard can reach the run, that is the better
route and this one is redundant. What it needs is a listening socket, and there
are runs that cannot have one: CI that forbids opening a port, a container with
nothing routed to it, a run too short-lived for anything to discover and poll.
A push out of the process needs no port and no discovery, and that is the case
this sampler is for.

**Statuses, not frames.** Reading a stack per stuck worker per pass was tried
here and taken out again: it made a sample cost a subprocess and a pause per
worker, on runs where every healthy worker waiting on a database reads as
``blocked`` and so qualified. Frames are worth their price when a human is
asking about one worker, which is ``/stack?pid=`` on demand, rather than for
every stuck worker on a timer.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from . import topology


class SampledWorker(BaseModel):
    """One worker at one instant, as the run's own files describe it."""

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


class WorkerSample(BaseModel):
    """One pass over this run's workers."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = ""
    run_id: Optional[str] = None
    observed_at: float = 0.0
    workers: list[SampledWorker] = Field(default_factory=list)


class WorkerSampler:
    """Reads one sample per pass out of a run's evidence directory.

    An object rather than a function because the caller is a thread that
    samples the same directory under the same session for the life of the run,
    and because a sample carries the session id it was taken under: the run id
    does not exist until a worker has beaten, so the session is the only key a
    consumer can join early rows on.
    """

    def __init__(self, directory: Path, session_id: str = "") -> None:
        self.directory = directory
        self.session_id = session_id

    def sample(self, now: Optional[float] = None) -> WorkerSample:
        moment = time.time() if now is None else now
        described = topology.run(self.directory, moment)
        if described is None:
            return WorkerSample(session_id=self.session_id, observed_at=round(moment, 3))

        return WorkerSample(
            session_id=self.session_id,
            run_id=described.get("run_id"),
            observed_at=round(moment, 3),
            workers=[
                SampledWorker(
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
                for record in described.get("workers", [])
            ],
        )
