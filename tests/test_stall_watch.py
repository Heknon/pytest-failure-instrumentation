"""The stall watcher's relationship with the run it is watching.

It is the only part of this plugin that runs on a thread of its own and raises
from it, which makes two things its own to get right: it must stop when the run
does, and it must measure silence against a clock that cannot move under it.
"""

from __future__ import annotations

import time as clock

from pytest_failure_instrumentation.config import Settings
from pytest_failure_instrumentation.incidents.engine import IncidentEngine


def engine_for(pytester) -> IncidentEngine:
    return IncidentEngine(
        pytester.parseconfig(), Settings(directory=pytester.path / "evidence")
    )


def test_nothing_is_raised_after_the_summary_has_said_how_many_there_were(pytester):
    """The stall watcher runs on its own thread and can be inside an
    assessment when the session ends. It is asked to stop and joined first, so
    this is the backstop - but without it a late incident arrives after the
    number of incidents has already been reported, into a consumer that has
    finished writing, or into interpreter shutdown."""
    from pytest_failure_instrumentation.config import Settings
    from pytest_failure_instrumentation.incidents.engine import IncidentEngine
    from pytest_failure_instrumentation.incidents.stall import WorkerStallIncident

    class Collecting:
        def __init__(self):
            self.incidents = []

        def pytest_failure_incident(self, incident):
            self.incidents.append(incident)

    class Stub:
        def __init__(self, hook, pluginmanager):
            self.hook = hook
            self.pluginmanager = pluginmanager

    config = pytester.parseconfig()
    engine = IncidentEngine(config, Settings(directory=pytester.path / "evidence"))
    hook = Collecting()
    engine.config = Stub(hook, config.pluginmanager)

    engine.raise_incident(WorkerStallIncident(worker="gw0", verdict="STALLED_BLOCKED"))
    assert len(hook.incidents) == 1

    engine.closed = True
    engine.raise_incident(WorkerStallIncident(worker="gw1", verdict="STALLED_FROZEN"))
    assert len(hook.incidents) == 1, "an incident arrived after the run had ended"


def test_a_wall_clock_that_steps_does_not_manufacture_a_fleet_of_stalls(
    pytester, monkeypatch
):
    """Silence is one process measuring an interval against itself, so it is
    measured on the monotonic clock.

    A wall clock that steps forward - an NTP correction on a freshly booted CI
    machine is the ordinary way that happens - would otherwise make every
    worker look silent for the length of the step at the same moment, and
    report the whole fleet as stalled.
    """
    engine = engine_for(pytester)
    engine._touch("gw0")
    before = engine.activity["gw0"]

    # A day, forward, between one worker speaking and the next.
    wall_clock = clock.time
    monkeypatch.setattr(clock, "time", lambda: wall_clock() + 86_400)
    engine._touch("gw0")

    assert 0 <= engine.activity["gw0"] - before < 60


def test_a_worker_that_speaks_again_can_be_reported_again(pytester):
    """A stall that turns out to have been a slow test leaves the worker in
    the reported set. Left there, that worker could never be reported again
    however badly it went on to hang later in the same run."""
    engine = engine_for(pytester)
    engine.stalled.add("gw0")

    engine._touch("gw0")

    assert "gw0" not in engine.stalled
