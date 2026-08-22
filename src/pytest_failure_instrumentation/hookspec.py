"""The hook your product implements.

One hook for every kind of incident. Named for what it delivers - most of what
arrives is not a crash - and namespaced to this distribution, the way
``pytest_xdist_*`` hooks are, so it cannot collide with anyone else's.

    def pytest_failure_incident(incident):
        database.save(incident.model_dump())
        alerts.send(str(incident))

``incident`` is a pydantic model, one class per kind, discriminated on
``incident.kind``:

======================= ==================================================
``worker_death``        ``incidents.death.WorkerDeathIncident``
``worker_stall``        ``incidents.stall.WorkerStallIncident``
``collection_mismatch`` ``incidents.collection.CollectionMismatchIncident``
``internal_error``      ``incidents.internal_error.InternalErrorIncident``
``run_summary``         ``incidents.summary.RunSummaryIncident``
======================= ==================================================

They share ``kind``, ``verdict``, ``confidence``, ``severity``, ``owner``,
``fingerprint``, ``run_id``, ``worker`` and ``evidence``; the rest belongs to
the kind, because a segfault's resident memory and a run summary's exit code
have nothing to say to each other. ``str(incident)`` is the alert text.
``incidents.registry.parse`` turns a stored row back into its own model, and
``registry.json_schema()`` is the contract for a table migration.

The first three need xdist; the last two are raised by any run. ``worker_stall``
and ``collection_mismatch`` reach no pytest hook at all - a wedged worker is the
absence of anything being said, and xdist writes a collection disagreement only
into its own log - so the engine sources them itself. ``run_summary`` is emitted
once at the end of every run, so its *absence* means the controller died too.

A stall is assessed on a watcher thread, so an implementation of this hook can
be called from a thread other than the one running the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # kept out of the runtime import path: a worker never
    from .incidents.base import Incident  # loads pydantic


@pytest.hookspec()
def pytest_failure_incident(incident: Incident) -> None:
    """Called once per incident, on the controller.

    Never raises into the run: the caller wraps this, because an exception in
    a reporting hook becomes an INTERNALERROR that ends the customer's run.
    """
