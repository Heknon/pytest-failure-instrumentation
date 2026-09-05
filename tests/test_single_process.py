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

from pytest_failure_instrumentation.capture.state import read_state

from .conftest import RERUN_CONFTEST, needs_pyspy, needs_xdist

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


#: A test that fails once and, on its rerun, reads the record of itself. The
#: counters can only be checked *during* a rerun from inside one.
RERUN_INTROSPECTIVE = """
import json
from pathlib import Path

from pytest_failure_instrumentation.capture.state import read_state


def test_first():
    assert True


def test_reruns():
    slot, = Path(".pytest-failures").glob("*/main.state")
    if not Path("first.json").exists():
        Path("first.json").write_text(json.dumps(read_state(slot)), encoding="utf-8")
        assert False, "the first attempt fails"
    Path("again.json").write_text(json.dumps(read_state(slot)), encoding="utf-8")


def test_last():
    assert True
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



def test_a_rerun_is_one_test_however_many_times_it_runs(runner):
    """A rerun plugin runs a failed test's phases again inside the same
    protocol. Counted at setup, every attempt was a test started, and a
    suite of 368 with six rerun once read as 374 - on the live view, where
    the controller's total is floored at this count. So the second setup of
    the same protocol is not a start, and the finish counted at the end of
    the first attempt is taken back: during the rerun the row reads one
    running, which is what the slot beside it says."""
    runner.pytester.makeconftest(RERUN_CONFTEST)
    runner.pytester.makepyfile(test_rerun=RERUN_INTROSPECTIVE)
    runner.run("test_rerun.py")
    runner.result.assert_outcomes(passed=3)

    first = json.loads((runner.pytester.path / "first.json").read_text(encoding="utf-8"))
    again = json.loads((runner.pytester.path / "again.json").read_text(encoding="utf-8"))
    assert (first["tests_started"], first["tests_finished"]) == (2, 1)
    assert (again["tests_started"], again["tests_finished"]) == (2, 1), again
    assert again["nodeid"] == "test_rerun.py::test_reruns"

    final = read_state(evidence(runner.pytester) / "main.state")
    assert (final["tests_started"], final["tests_finished"]) == (3, 3), final


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
#: asked about, and is still read from outside it - one reader, whoever the
#: target turns out to be.
PULLS_ITS_OWN_STACK = """
import json
import os
import time
import urllib.error
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
    try:
        stack = pull("/stack?pid=%d" % os.getpid())
    except urllib.error.HTTPError as refused:
        # With no py-spy installed the endpoint answers with the reason
        # instead of a stack, which is an answer and not a failure. What the
        # view says about this run is the other file.
        stack = json.loads(refused.read())
    Path("stack.json").write_text(json.dumps(stack), encoding="utf-8")
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


@needs_pyspy
def test_a_lone_run_reads_its_own_stack_the_way_it_reads_any_other(runner):
    runner.pytester.makeconftest(SERVER_CONFTEST)
    runner.pytester.makeini(ini(failure_stack_server="true"))
    runner.pytester.makepyfile(test_lone=PULLS_ITS_OWN_STACK)
    runner.run("test_lone.py", timeout=180)
    assert runner.result.ret == 0, runner.result.stdout.str()

    stack = read(runner.pytester, "stack.json")
    # In a run with no workers the process being asked about is the one
    # answering, and it is still read from outside itself: one reader, one
    # source, whoever the target turns out to be.
    assert stack["source"] == "py-spy"
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

    Nothing here needs py-spy. Where it is absent the verdict is the same
    verdict - it is reached from beats, not from frames - and the stack is
    whatever the watchdog last wrote, which names the same blocked test.
    """
    runner.pytester.makepyfile(test_hang=HANGS)
    incidents = runner.run(*STALL_ARGUMENTS, "test_hang.py", timeout=180)

    stall = runner.only(incidents, "worker_stall")
    assert stall.worker == "main"
    assert stall.verdict == "STALLED_BLOCKED"
    assert stall.test_in_flight == "test_hang.py::test_deadlocks"
    assert stall.run_ending is True
    assert stall.blamed_frame is not None
    assert stall.blamed_frame.function == "test_deadlocks"


@needs_pyspy
def test_a_lone_run_reads_its_stall_stack_the_way_every_process_is_read(runner):
    """py-spy, not a second mechanism for the one process that could avoid it.

    The frames are directly available in this process - it is the one that
    stalled - and reading them that way would be a reader with its own failure
    modes and its own source to explain. This is the reader everything else
    uses, so the stack is current, it says which thread holds the GIL, and no
    signal is sent that could return a blocked syscall early.
    """
    runner.pytester.makepyfile(test_hang=HANGS)
    incidents = runner.run(*STALL_ARGUMENTS, "test_hang.py", timeout=180)

    stall = runner.only(incidents, "worker_stall")
    assert stall.stack_source == "py-spy"
    assert stall.stack_probed is True
    # Taken at the moment of the report, rather than left by the watchdog some
    # part of failure_slow_test_seconds ago.
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


LEAVES_WITHOUT_FINISHING = """
import victim


def test_filler():
    assert True


def test_leaves():
    victim.hard_exit(1)
"""

CRASHES = """
import victim


def test_crashes():
    victim.native_call(1)
"""


def test_a_run_that_never_came_back_is_reported_by_the_next_one(runner):
    """The case with no survivor.

    A run whose one process is killed has nobody left to report it - no
    controller watching, no hook to fire, and a summary that never happens. So
    the next run over the same directory reports it, which is the walk it was
    already making to clear the evidence away.
    """
    runner.pytester.makepyfile(test_gone=LEAVES_WITHOUT_FINISHING)
    assert runner.run("test_gone.py") == [], "a dead run reports nothing itself"
    killed = evidence(runner.pytester).name

    runner.pytester.makepyfile(test_after=SUITE)
    incidents = runner.run("test_after.py")

    death = runner.only(incidents, "worker_death")
    assert death.worker == "main"
    assert death.recovered_from_run == killed
    # The id of the run it happened in, not of the run that found it: that is
    # the key a consumer joins on, and the finder had no part in it.
    assert death.run_id == killed
    assert death.test_in_flight == "test_gone.py::test_leaves"
    assert death.phase == "call"
    assert death.tests_started == 2
    assert death.tests_finished == 1
    assert death.last_seen_at is not None

    # Nothing was entitled to read the status, and the incident says so rather
    # than blaming a gateway that was never in the picture.
    assert death.exit_status is None
    assert death.verdict == "UNKNOWN"
    assert any("only the parent process could read it" in line for line in death.evidence), death.evidence
    assert str(death).startswith("Worker main of run ")
    assert any(line.startswith("Found in the evidence of run ") for line in death.evidence)

    # And the sweep still happened: the recovered directory is gone, so the
    # run after this one has nothing left to report twice.
    assert evidence(runner.pytester).name != killed


def test_a_run_that_finished_is_never_reported_by_the_next_one(runner):
    """The guard the whole thing rests on. A run that reached session finish
    raised its own incidents, and re-raising them a day later against whoever
    noticed is worse than never raising them."""
    runner.pytester.makepyfile(test_first=SUITE)
    runner.run("test_first.py")

    runner.pytester.makepyfile(test_second=SUITE)
    incidents = runner.run("test_second.py")

    assert runner.of_kind(incidents, "worker_death") == []


def test_a_recovered_crash_is_attributed_when_the_run_kept_its_stack(runner):
    runner.pytester.makeini(ini(failure_crash_stack="true"))
    runner.pytester.makepyfile(test_boom=CRASHES)
    runner.run("test_boom.py")

    runner.pytester.makepyfile(test_after=SUITE)
    incidents = runner.run("test_after.py")

    death = runner.only(incidents, "worker_death")
    # No exit status was obtainable, so the dump is the whole of the verdict -
    # which is exactly the position Windows is in even for a watched worker.
    assert death.verdict == "NATIVE_CRASH"
    assert death.crash_stack
    assert death.blamed_frame is not None
    assert death.blamed_frame.module == "victim"
    assert death.owner == "product"


def test_a_recovered_run_says_where_its_stack_went(runner):
    """An absence with no reason beside it reads as "it left nothing", which
    is a finding. The truth is a setting away from being fixed."""
    runner.pytester.makepyfile(test_boom=CRASHES)
    runner.run("test_boom.py")

    runner.pytester.makepyfile(test_after=SUITE)
    incidents = runner.run("test_after.py")

    death = runner.only(incidents, "worker_death")
    assert death.crash_stack == []
    assert any("failure_crash_stack" in line for line in death.evidence), death.evidence
    # Nothing named the frame, so the test that was running is offered as the
    # lead it is - in the column a reader does not take for a finding.
    assert death.owner == "unknown"
    assert death.suspect_owner == "customer-code"
