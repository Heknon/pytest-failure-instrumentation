"""The hook your product implements.

One hook for every kind of incident. Named for what it delivers - most of what
arrives is not a crash - and namespaced to this distribution, the way
``pytest_xdist_*`` hooks are, so it cannot collide with anyone else's.

    def pytest_failure_incident(incident):
        database.save(incident.model_dump())
        alerts.send(str(incident))

``incident`` is a pydantic model, one class per kind, discriminated on
``incident.kind``:

===================== ==========================================
``worker_death``      ``incidents.death.WorkerDeathIncident``
``internal_error``    ``incidents.internal_error.InternalErrorIncident``
``run_summary``       ``incidents.summary.RunSummaryIncident``
===================== ==========================================

They share ``kind``, ``verdict``, ``confidence``, ``severity``, ``owner``,
``fingerprint``, ``run_id``, ``worker`` and ``evidence``; the rest belongs to
the kind, because a segfault's resident memory and a run summary's exit code
have nothing to say to each other. ``str(incident)`` is the alert text.
``incidents.registry.parse`` turns a stored row back into its own model, and
``registry.json_schema()`` is the contract for a table migration.

Two further kinds - ``collection_mismatch`` and ``worker_stall`` - are
implemented but not yet raised; neither reaches ``pytest_testnodedown`` or
``pytest_internalerror``, which is the reason they need a source of their own.
``run_summary`` is emitted once at the end of every run, so its *absence* means
the controller died too.
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
