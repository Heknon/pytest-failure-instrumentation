"""The hook your product implements.

One hook for every kind of incident. Named for what it delivers - most of what
arrives is not a crash - and namespaced to this distribution, the way
``pytest_xdist_*`` hooks are, so it cannot collide with anyone else's.

    def pytest_failure_incident(incident):
        database.save(incident.model_dump())
        alerts.send(str(incident))

``incident`` is a pydantic model, one class per kind, discriminated on
``incident.kind``. All five are raised:

========================= ==================================================== =============
``worker_death``          ``incidents.death.WorkerDeathIncident``               needs xdist
``worker_stall``          ``incidents.stall.WorkerStallIncident``               needs xdist
``collection_mismatch``   ``incidents.collection.CollectionMismatchIncident``   needs xdist
``internal_error``        ``incidents.internal_error.InternalErrorIncident``    any run
``run_summary``           ``incidents.summary.RunSummaryIncident``              any run
========================= ==================================================== =============

Three of them reach no pytest hook of their own, which is why they need a
source of their own: a stall is polled for, because the absence of anything
being said fires nothing; a collection mismatch is assembled from
``pytest_xdist_node_collection_finished``; and an internal error under xdist
arrives on the controller as a re-raised string.

``run_summary`` is emitted once at the end of every run, so its *absence*
means the controller died too.

A stall is assessed on a watcher thread, so an implementation of this hook can
be called from a thread other than the one running the session.

A framework that installs this itself, rather than letting the entry point do
it, gets the same hook: ``install`` registers this spec if the entry point was
disabled - see :mod:`.registration`.

They share ``kind``, ``verdict``, ``confidence``, ``severity``, ``owner``,
``fingerprint``, ``run_id``, ``worker`` and ``evidence``; the rest belongs to
the kind, because a segfault's resident memory and a run summary's exit code
have nothing to say to each other. ``str(incident)`` is the alert text.

**The stack is in the payload but not in that text.** Forty frames turn a
readable incident into a wall, and whether they belong in an alert is a
decision only you can make - so ``str(incident)`` gives you the one blamed
frame and ``incident.raw_stack()`` gives you all of them, as lines, whichever
kind it is::

    def pytest_failure_incident(incident):
        body = str(incident)
        frames = incident.raw_stack()
        if frames:
            body += "\n\n" + "\n".join(frames)
        alerts.send(body)

``top_frame`` and ``blamed_frame`` are the two frames already parsed for you,
each with ``file``, ``line``, ``function``, ``module`` and ``owner``. The kinds
keep their own raw fields too - ``crash_stack``, ``stack``, ``detail`` - and
``raw_stack`` is only so that nobody has to switch on ``kind`` to reach them.
``incidents.registry.parse`` turns a stored row back into its own model, and
``registry.json_schema()`` is the contract for a table migration.
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
