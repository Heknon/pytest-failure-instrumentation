"""The hooks your product implements.

Three, and they answer different questions. ``pytest_failure_incident`` says
something went wrong, once per incident, and is the reason this package exists.
``pytest_failure_worker_sample`` says what every worker is doing right now,
on a cadence, and is off unless asked for. ``pytest_failure_server_ready`` says
the live-stack server is up and where - nothing went wrong, and a product that
never switches the server on never sees it.

All three are named for what they deliver - most of what arrives at the first
is not a crash - and namespaced to this distribution, the way ``pytest_xdist_*``
hooks are, so none of them can collide with anyone else's.

    def pytest_failure_incident(incident):
        database.save(incident.model_dump())
        alerts.send(str(incident))

``incident`` is a pydantic model, one class per kind, discriminated on
``incident.kind``. All of them are raised:

========================= ==================================================== =============
``worker_death``          ``incidents.death.WorkerDeathIncident``               needs xdist
``worker_stall``          ``incidents.stall.WorkerStallIncident``               needs xdist
``collection_mismatch``   ``incidents.collection.CollectionMismatchIncident``   needs xdist
``internal_error``        ``incidents.internal_error.InternalErrorIncident``    any run
``run_summary``           ``incidents.summary.RunSummaryIncident``              any run
``stack_server_unavailable`` ``incidents.stack_server.StackServerIncident``     live view on
``cpu_hotspot``           ``incidents.profile.CpuHotspotIncident``              profiling on
``cpu_burst``             ``incidents.profile.CpuBurstIncident``                profiling on
``memory_profile``        ``incidents.profile.MemoryProfileIncident``           profiling on
========================= ==================================================== =============

The last three are findings rather than failures. With ``--failure-profile`` or
``failure_profile`` on, the process running the tests samples every thread's
stack and CPU for the whole run, and at the end the functions that burnt the
CPU, the stretches where a core was held, and the tests that kept the memory
arrive here - informational whoever owns them, because nothing went wrong.
See :mod:`.profile`.

Three of them reach no pytest hook of their own, which is why they need a
source of their own: a stall is polled for, because the absence of anything
being said fires nothing; a collection mismatch is assembled from
``pytest_xdist_node_collection_finished``; and an internal error under xdist
arrives on the controller as a re-raised string.

``run_summary`` is emitted once at the end of every run, so its *absence*
means the controller died too.

``stack_server_unavailable`` is raised only when the live-stack server was
switched on and could not serve - a port held by something that is not one of
ours, or an address that could not be bound at all. It is *not* raised when
another of our own sessions holds the port, which is the shared mode working
as designed. The run is unaffected either way; what is lost is the live view,
and without this nothing would ever say so.

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

It is one thread out of the most recent dump, not the whole file: the other
threads in a worker are this plugin's heartbeat and execnet's receiver, and
reporting those blames the instrumentation for the failure it came to explain.
It is capped as well - 40 frames for a death, 14 for a stall, 4000 characters
for an internal error - and a cut stack ends with ``... and N more frames``
rather than passing for a whole one.
``incidents.registry.parse`` turns a stored row back into its own model, and
``registry.json_schema()`` is the contract for a table migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # kept out of the runtime import path: a worker never
    from .incidents.base import Incident  # loads pydantic
    from .live_view import LiveStackServer
    from .sampling import WorkerSample


@pytest.hookspec()
def pytest_failure_incident(incident: Incident) -> None:
    """Called once per incident, on the controller.

    Never raises into the run: the caller wraps this, because an exception in
    a reporting hook becomes an INTERNALERROR that ends the customer's run.
    """


@pytest.hookspec()
def pytest_failure_server_ready(server: LiveStackServer) -> None:
    """Called once, on the controller, when the live-stack server is serving.

    ``server`` is a :class:`.live_view.LiveStackServer` carrying the address
    and the evidence directory - everything a UI needs to start polling
    ``/workers`` and ``/stack``, and the only way to learn a drawn port
    without reading the discovery file yourself.

    Not called at all when the server was never switched on, and not called
    when this session stood down because another of ours already holds a named
    port - that session announced itself, and two announcements for one server
    would have a product storing the same address twice.

    Called from a thread of its own, once the server is already accepting, so
    an implementation may call straight back into the server it has just been
    handed - which is the first thing most of them do. It may also take as long
    as it likes: nothing waits for it, and the run does not.

    Never raises into the run, on the same grounds as the hook above.
    """


@pytest.hookspec()
def pytest_failure_worker_sample(sample: WorkerSample) -> None:
    """Called on the controller every ``failure_sample_seconds``, while the run
    is still going, with what each of this run's workers is doing.

    Off unless ``failure_sample_seconds`` is set. This is the only hook here
    that fires when nothing is wrong, so it is the only one with a running
    cost - see :mod:`.sampling` for what that cost is.

    ``sample.workers`` carries every worker's status, node id, phase, resident
    memory and CPU rate, all read from files the run was writing anyway. No
    frames: a sample asks the workers nothing, which is what lets it run where
    the live server cannot - a CI job that may not open a port, a run too
    short-lived for anything to discover and poll. Where something *can* reach
    the run, ``/workers`` reports the same rows and more of them, and
    ``/stack?pid=`` answers "what is gw3 in" at the moment a human asks it.

    Called from the sampler's own thread, and wrapped like the others - an
    exception here is never allowed to become an INTERNALERROR in the run.
    """
