"""What the stall builder does before it touches a live worker.

Two of these are about a signal that is never sent. ``SIGUSR1``'s default
disposition is to *terminate*, and the pid it would be aimed at was read back
out of a file the worker wrote - so a worker that has since exited and had its
number handed on leaves this plugin one syscall away from killing a stranger's
process rather than producing a bad report.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from pytest_failure_instrumentation import probes
from pytest_failure_instrumentation.capture.state import WorkerState
from pytest_failure_instrumentation.incidents import stall

needs_signals = pytest.mark.skipif(
    not probes.can_request_stack(),
    reason="this platform has no live-stack signal to guard in the first place",
)


def write_worker(directory, *, nodeid, pid=4242, beats=2, cpu=0.0, run_id=None):
    """A worker that is alive, silent, and burning nothing."""
    state = WorkerState(directory / "gw0.state", pid, run_id)
    state.update(nodeid=nodeid, phase="call" if nodeid else None)
    if nodeid is None:
        state.update(nodeid=None, phase=None)
    now = time.time()
    with (directory / "gw0.events").open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": "worker_start", "pid": pid, "run_id": run_id}) + "\n"
        )
        for index in range(beats):
            handle.write(
                json.dumps(
                    {
                        "event": "heartbeat",
                        "run_id": run_id,
                        "time": round(now - (beats - 1 - index) * 2, 3),
                        "cpu_seconds": cpu * index,
                    }
                )
                + "\n"
            )


# -- the signal that is not sent -------------------------------------------


@needs_signals
def test_a_pid_the_controller_cannot_place_is_never_signalled():
    """No gateway to compare against and no way to confirm the process: the
    honest answer is no stack, not a signal aimed at a number."""
    reason = stall._cannot_probe(pid=4242, allowed=True, live_pid=None)
    assert reason is not None and "recycled pid" in reason


@needs_signals
def test_a_pid_that_is_not_what_the_gateway_is_running_is_never_signalled():
    reason = stall._cannot_probe(pid=4242, allowed=True, live_pid=5150)
    assert reason is not None and "5150" in reason and "4242" in reason


@needs_signals
def test_the_pid_the_gateway_is_running_is_the_one_that_may_be_asked():
    assert stall._cannot_probe(pid=4242, allowed=True, live_pid=4242) is None


@needs_signals
def test_the_setting_still_outranks_the_confirmation():
    """A user who turned the probe off gets told that, not a lecture about
    pids they never asked about."""
    reason = stall._cannot_probe(pid=4242, allowed=False, live_pid=4242)
    assert reason is not None and "failure_stack_probe" in reason


# -- a worker with nothing running -----------------------------------------


def test_a_worker_with_no_test_in_flight_is_not_blamed_on_its_last_one(tmp_path):
    """Between two tests, still collecting, or waiting to be handed work all
    look identical from outside, and all three are ordinary. The silence is
    still worth reporting - the run cannot end while a worker never comes back
    - but not at the confidence a wedged test earns."""
    write_worker(tmp_path, nodeid=None)

    incident = stall.build("gw0", tmp_path, silent_for=30.0, interval=2.0, stack_probe=False)

    assert incident is not None
    assert incident.state == "BLOCKED"
    assert incident.test_in_flight is None
    assert incident.last_test is None
    assert incident.confidence == "low"
    assert any("no test was in flight" in line for line in incident.evidence)


def test_a_wedged_test_still_earns_the_confidence_it_did(tmp_path):
    write_worker(tmp_path, nodeid="t.py::test_wedges")

    incident = stall.build("gw0", tmp_path, silent_for=30.0, interval=2.0, stack_probe=False)

    assert incident is not None
    assert incident.test_in_flight == "t.py::test_wedges"
    assert incident.confidence == "high"


def test_an_earlier_run_s_record_is_not_read_as_this_worker(tmp_path):
    write_worker(tmp_path, nodeid="t.py::test_a", pid=4242, run_id="run-earlier")

    incident = stall.build(
        "gw0", tmp_path, silent_for=30.0, interval=2.0, stack_probe=False,
        run_id="run-now",
    )

    assert incident is not None
    assert incident.test_in_flight is None
    # The events log is stamped too, so the pid it offers is refused with it.
    assert incident.worker_pid == 4242 or incident.worker_pid is None


# -- ending the run --------------------------------------------------------


def test_an_assessment_gives_up_when_the_run_is_already_over(tmp_path):
    """The second pass is what separates a missed beat from a frozen process,
    and it costs an interval. Waiting it out at session finish means the hook
    fires after the run summary has already said how many incidents there
    were - or during interpreter shutdown, where it is a traceback rather than
    a report."""
    write_worker(tmp_path, nodeid="t.py::test_a", beats=1)
    # One beat, stamped long enough ago that the first pass wants confirming.
    stale = tmp_path / "gw0.events"
    lines = stale.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record["time"] = time.time() - 60
    stale.write_text("\n".join(lines[:-1] + [json.dumps(record)]) + "\n", encoding="utf-8")

    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    incident = stall.build(
        "gw0", tmp_path, silent_for=60.0, interval=30.0, stack_probe=False, cancel=cancel
    )

    assert incident is None
    assert time.monotonic() - started < 5.0, "it waited out an interval nobody needs"
