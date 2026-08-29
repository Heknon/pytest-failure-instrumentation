"""A run with no workers, recorded as deeply as one that has them.

Recording is done by whichever process runs the tests, because everything
worth knowing about a death or a stall is in that process and leaves it only
if it was written down first. Under xdist that is a worker. Without xdist it
is the session itself - which used to record nothing at all, so a plain
``pytest`` had no state slot, no heartbeat, no stack for a test that hung and
nothing for the live view to read.

These are the facts that follow from fixing that, and the one thing a lone run
deliberately does *not* take: the fatal dump pytest writes to a terminal
somebody is watching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import needs_xdist

SUITE = """
def test_one():
    assert True


def test_two():
    assert True
"""

#: A test that reads the plugin's own record of what it is doing, from inside
#: the test the record is about. Nothing else can prove the slot is written
#: *before* the phase rather than after it.
INTROSPECTIVE = """
import json
from pathlib import Path

from pytest_failure_instrumentation.capture.state import read_state


def test_reads_its_own_state():
    slot, = Path(".pytest-failures").glob("*/main.state")
    Path("seen.json").write_text(json.dumps(read_state(slot)), encoding="utf-8")
"""


def ini(**settings: str) -> str:
    """The runner fixture's ini with more keys, since pytester rewrites it
    whole rather than appending to it."""
    lines = ["[pytest]", "failure_packages = victim", "failure_product_version = 1.2.3"]
    lines += [f"{name} = {value}" for name, value in settings.items()]
    return "\n".join(lines) + "\n"


def evidence(pytester: pytest.Pytester) -> Path:
    """The one run directory a finished run leaves behind.

    One, because a run prunes the directories of runs that are over before it
    makes its own - so a second run here would not be evidence of two, it
    would be a prune that stopped working.
    """
    runs = sorted(path for path in (pytester.path / ".pytest-failures").iterdir() if path.is_dir())
    assert len(runs) == 1, [path.name for path in runs]
    return runs[0]


def events(directory: Path, worker: str = "main") -> list[dict]:
    lines = (directory / f"{worker}.events").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_a_run_with_no_workers_records_itself(runner):
    runner.pytester.makepyfile(test_lone=SUITE)
    runner.run("test_lone.py")

    directory = evidence(runner.pytester)
    assert (directory / "main.state").exists()
    assert (directory / "owner.json").exists()

    written = events(directory)
    kinds = [event["event"] for event in written]
    assert "worker_start" in kinds
    assert "heartbeat" in kinds, "the liveness thread never ran"
    assert "worker_finish" in kinds, "the run reached its end and did not say so"

    # Every line stamped with the id the incidents carry. Without it a later
    # run reading this directory cannot tell whose evidence it is holding.
    assert {event["run_id"] for event in written} == {directory.name}


def test_the_state_slot_is_written_before_the_phase_it_describes(runner):
    runner.pytester.makepyfile(test_lone=INTROSPECTIVE)
    runner.run("test_lone.py")

    seen = json.loads((runner.pytester.path / "seen.json").read_text(encoding="utf-8"))
    assert seen["nodeid"] == "test_lone.py::test_reads_its_own_state"
    assert seen["phase"] == "call"
    assert seen["tests_started"] == 1
    assert seen["tests_finished"] == 0


@needs_xdist
def test_a_controller_with_workers_records_the_workers_and_not_itself(distributed):
    distributed.pytester.makepyfile(test_lone=SUITE)
    distributed.run("-n", "2", "test_lone.py", timeout=180)

    directory = evidence(distributed.pytester)
    assert sorted(path.name for path in directory.glob("*.state")) == [
        "gw0.state",
        "gw1.state",
    ]
    # The controller runs no tests, so recording it would describe a process
    # that is only ever waiting.
    assert not (directory / "main.state").exists()


def test_the_fatal_dump_is_left_where_pytest_put_it(runner):
    runner.pytester.makepyfile(test_lone=SUITE)
    runner.run("test_lone.py")

    armed, = [
        event for event in events(evidence(runner.pytester))
        if event["event"] == "faulthandler_armed"
    ]
    # There is one destination for a fatal signal and it cannot be shared, so
    # a run whose stderr is somebody's terminal leaves the copy they can read
    # where it is, rather than moving it into a file for nobody.
    assert armed["fatal_stack"] == "stderr"


SERVER_CONFTEST = """
import json


def pytest_failure_incident(incident):
    with open("incidents.jsonl", "a") as handle:
        handle.write(incident.model_dump_json() + "\\n")


def pytest_failure_server_ready(server):
    with open("server.json", "w") as handle:
        json.dump({"url": server.url, "headers": server.headers()}, handle)
"""

#: A test that asks the live view what *it* is doing, over HTTP, while it is
#: doing it. In a run with no workers the server is inside the process being
#: asked about, so the answer comes out of its own frames and needs no py-spy.
PULLS_ITS_OWN_STACK = """
import json
import os
import time
import urllib.request
from pathlib import Path


def address():
    for _ in range(400):
        try:
            return json.loads(Path("server.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the live view never announced itself")


def pull(path):
    where = address()
    request = urllib.request.Request(
        where["url"].rstrip("/") + path, headers=where["headers"]
    )
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.load(answer)


def test_pulls_its_own_callstack():
    Path("workers.json").write_text(json.dumps(pull("/workers")), encoding="utf-8")
    Path("stack.json").write_text(
        json.dumps(pull("/stack?pid=%d" % os.getpid())), encoding="utf-8"
    )
"""

def read(pytester: pytest.Pytester, name: str):
    return json.loads((pytester.path / name).read_text(encoding="utf-8"))


def test_the_live_view_answers_for_a_run_with_no_workers(runner):
    runner.pytester.makeconftest(SERVER_CONFTEST)
    runner.pytester.makeini(ini(failure_stack_server="true"))
    runner.pytester.makepyfile(test_lone=PULLS_ITS_OWN_STACK)
    runner.run("test_lone.py", timeout=180)
    assert runner.result.ret == 0, runner.result.stdout.str()

    described = read(runner.pytester, "workers.json")
    session = evidence(runner.pytester).name
    run, = [entry for entry in described["runs"] if entry["session"] == session]
    worker, = run["workers"]
    assert worker["worker"] == "main"
    assert worker["nodeid"] == "test_lone.py::test_pulls_its_own_callstack"
    assert worker["phase"] == "call"
    assert worker["process_exists"] is True
    # The process serving is the process running the tests, so one pid answers
    # for both and the view says so rather than inventing a second.
    assert worker["pid"] == run["controller"]["pid"]


def test_a_lone_run_reads_its_own_stack_without_py_spy(runner):
    runner.pytester.makeconftest(SERVER_CONFTEST)
    runner.pytester.makeini(ini(failure_stack_server="true"))
    runner.pytester.makepyfile(test_lone=PULLS_ITS_OWN_STACK)
    runner.run("test_lone.py", timeout=180)
    assert runner.result.ret == 0, runner.result.stdout.str()

    stack = read(runner.pytester, "stack.json")
    # Reading another process needs ptrace and a subprocess. Reading this one
    # is a dict lookup - and in a run with no workers, this one is the only
    # process there is.
    assert stack["source"] == "in-process"
    frames = [frame for thread in stack["threads"] for frame in thread["frames"]]
    assert any(frame["function"] == "test_pulls_its_own_callstack" for frame in frames)


# Detected around 13s in; the tests release at 25s so the run ends on its own.
STALL_ARGUMENTS = (
    "-o", "failure_stall_seconds=10",
    "-o", "failure_heartbeat_interval=2",
    "-o", "failure_slow_test_seconds=4",
)

HANGS = """
import threading

never_set = threading.Event()


def test_filler():
    assert True


def test_deadlocks():
    never_set.wait(25)
"""

BUSY = """
import time


def test_filler():
    assert True


def test_is_merely_slow():
    deadline = time.time() + 20
    total = 0
    while time.time() < deadline:
        total += sum(range(10000))
    assert total
"""


def test_a_run_with_no_workers_reports_its_own_stall(runner):
    """The case that used to produce nothing at all.

    There is no controller watching from outside, and there does not need to
    be: a main thread blocked on a lock does not stop the other threads in its
    process, so the run says what is wrong with it while it is still wrong.
    """
    runner.pytester.makepyfile(test_hang=HANGS)
    incidents = runner.run(*STALL_ARGUMENTS, "test_hang.py", timeout=180)

    stall = runner.only(incidents, "worker_stall")
    assert stall.worker == "main"
    assert stall.verdict == "STALLED_BLOCKED"
    assert stall.test_in_flight == "test_hang.py::test_deadlocks"
    assert stall.run_ending is True

    # Read out of this process's own frames rather than asked for with a
    # signal: there is nothing to signal, nothing to wait for, and nothing
    # that could return a blocked syscall early and dissolve the stall.
    assert stall.stack_source == "in-process"
    assert stall.stack_probed is True
    assert stall.stack_age_seconds is not None and stall.stack_age_seconds < 5
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "test_deadlocks"


def test_a_lone_run_that_is_merely_slow_is_not_reported(runner):
    """Silence proves nothing on its own, here as much as under xdist: a
    twenty-minute test must not page anybody."""
    runner.pytester.makepyfile(test_slow=BUSY)
    incidents = runner.run(*STALL_ARGUMENTS, "test_slow.py", timeout=180)

    assert runner.of_kind(incidents, "worker_stall") == []
    assert runner.only(incidents, "run_summary").raised == 0
