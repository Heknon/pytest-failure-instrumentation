"""Periodic worker samples: what is in one, and that a real run pushes them.

A sample is the run's own ``.state`` and ``.events`` files turned into a row
per worker, so what these check is the classification that makes those rows
worth reading - working against blocked - and the chain from a cadence setting
to a product's hook, which none of the direct tests would notice breaking.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pytest_failure_instrumentation.sampling import WorkerSampler

from .conftest import ENABLE_FLAG, needs_xdist


def evidence(root: Path, workers, run_id="run-1", beats_apart=5.0, now=None, pid=None,
             finished=0):
    """A run directory shaped like one a real run leaves behind.

    ``workers`` is {name: cpu_seconds_series}, and the series is the knob these
    tests turn: it is what decides working-vs-blocked.

    The pid is this test process, because it has to be one that exists. A dead
    pid is reported ``gone``, which outranks every other status by design - so
    a fixture with invented pids would test nothing but that rule.
    """
    moment = time.time() if now is None else now
    root.mkdir(parents=True, exist_ok=True)
    (root / "owner.json").write_text(json.dumps({"pid": 1, "started_at": moment - 60}))
    alive = os.getpid() if pid is None else pid
    for name, cpus in workers.items():
        state = {"pid": alive, "nodeid": f"test_x.py::{name}", "phase": "call",
                 "time": moment, "tests_started": finished + 1,
                 "tests_finished": finished}
        raw = json.dumps(state).encode()
        (root / f"{name}.state").write_bytes(raw + b"\x00" * (5120 - len(raw)))
        lines = [json.dumps({"event": "watchdog_started", "interval": beats_apart,
                             "run_id": run_id, "time": moment - 60})]
        for i, cpu in enumerate(cpus):
            lines.append(json.dumps({
                "event": "heartbeat", "cpu_seconds": cpu, "rss_mb": 40,
                "nodeid": f"test_x.py::{name}", "phase": "call", "run_id": run_id,
                "time": moment - (len(cpus) - 1 - i) * beats_apart,
            }))
        (root / f"{name}.events").write_text("\n".join(lines) + "\n")
    return root


# -- what one sample says ---------------------------------------------------


def test_a_busy_worker_and_a_stuck_one_are_told_apart(tmp_path):
    """The distinction the rows exist for. Both workers are beating and both
    are inside a test, so the node id and the phase say the same thing about
    each; only the CPU between the beats separates the one making progress
    from the one waiting on something."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0, 5.0, 7.0],
                                       "gw1": [5.0, 5.0, 5.0, 5.0]})
    sample = WorkerSampler(root, session_id="s-1").sample()

    by_worker = {entry.worker: entry for entry in sample.workers}
    assert by_worker["gw0"].status == "working"
    assert by_worker["gw1"].status == "blocked"
    # In words as well as in a status, because a product shows this to a human.
    assert by_worker["gw1"].why


def test_a_row_carries_what_a_dashboard_draws(tmp_path):
    """All of it out of files the run was writing anyway - the sample asks the
    worker nothing, which is what makes a cadence affordable."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0, 5.0, 7.0]})
    sample = WorkerSampler(root, session_id="s-1").sample(now=1234.5678)

    assert sample.session_id == "s-1"
    assert sample.run_id == "run-1"
    assert sample.observed_at == 1234.568
    entry = sample.workers[0]
    assert entry.worker == "gw0"
    assert entry.pid == os.getpid()
    assert entry.nodeid == "test_x.py::gw0"
    assert entry.phase == "call"
    assert entry.rss_mb == 40
    assert entry.cpu_rate is not None
    assert entry.heartbeat_age_s is not None


def test_each_pass_reports_the_evidence_as_it_stands(tmp_path):
    """One sampler serves the whole run, so a status must follow the files
    rather than the first pass that read them: a worker that blocks, goes back
    to work and blocks again is three different rows, not one remembered one."""
    root = tmp_path / "run"
    sampler = WorkerSampler(root)

    evidence(root, {"gw0": [5.0, 5.0, 5.0]})
    assert sampler.sample().workers[0].status == "blocked"

    evidence(root, {"gw0": [1.0, 3.0, 5.0, 7.0]})
    assert sampler.sample().workers[0].status == "working"

    evidence(root, {"gw0": [9.0, 9.0, 9.0]})
    assert sampler.sample().workers[0].status == "blocked"


def _schedule(root, **workers):
    (root / "schedule.json").write_text(
        json.dumps(
            {
                "dist": "load", "collected": 40, "unassigned": 12, "settled": False,
                "workers": {
                    name: {"assigned": a, "completed": c, "pending": a - c}
                    for name, (a, c) in workers.items()
                },
            }
        )
    )


def test_a_row_carries_the_denominator_the_worker_cannot_supply(tmp_path):
    """A status without a total is the row a dashboard cannot draw a bar
    from: the worker's files say what it has run and nothing says how much it
    was given, because no worker is ever told."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0]}, finished=9)
    _schedule(root, gw0=(14, 9))
    entry = WorkerSampler(root).sample().workers[0]

    assert entry.tests_assigned == 14
    assert (entry.tests_finished, entry.tests_running, entry.tests_queued) == (9, 1, 4)


def test_the_split_is_measured_from_the_worker_s_own_counts(tmp_path):
    """The controller's count of what this worker has done and the worker's
    own are written by different processes into different files, and a row
    that took the total from one and the progress from the other could say a
    worker had finished more tests than it was given. So the total is the only
    thing taken from the controller; the split of it is measured here."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0]}, finished=11)
    _schedule(root, gw0=(14, 9))  # the controller is two behind the worker
    entry = WorkerSampler(root).sample().workers[0]

    assert entry.tests_assigned == 14
    assert (entry.tests_finished, entry.tests_running, entry.tests_queued) == (11, 1, 2)


def test_a_total_the_worker_has_already_passed_is_the_stale_one(tmp_path):
    """A worker cannot start a test it was never given, so its own count is a
    floor under the total rather than a contradiction of it."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0]}, finished=19)
    _schedule(root, gw0=(15, 15))
    entry = WorkerSampler(root).sample().workers[0]

    assert entry.tests_assigned == 20  # the slot says one is in flight past it
    assert (entry.tests_finished, entry.tests_running, entry.tests_queued) == (19, 1, 0)


def test_a_sample_says_how_big_the_run_is_and_whether_that_is_settled(tmp_path):
    """A total is what a worker has been given *so far*, so a consumer handed
    the totals and nothing else is drawing a bar whose end moves. This is the
    path for runs that cannot open a port, where there is no /workers to ask
    the run-level question instead."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0]}, finished=9)
    _schedule(root, gw0=(14, 9))
    sample = WorkerSampler(root).sample()

    assert sample.collected == 40
    assert sample.unassigned == 12
    assert sample.settled is False
    assert sample.dist == "load"


def test_a_sample_from_a_run_with_no_schedule_carries_none_of_it(tmp_path):
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0]})
    sample = WorkerSampler(root).sample()

    assert (sample.collected, sample.unassigned, sample.settled) == (None, None, None)


def test_a_row_from_a_run_with_no_schedule_says_nothing_rather_than_zero(tmp_path):
    """Zero pending is a worker about to finish; not knowing is not."""
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0]})
    entry = WorkerSampler(root).sample().workers[0]

    assert entry.tests_assigned is None
    assert entry.tests_running is None
    assert entry.tests_queued is None


def test_an_empty_directory_samples_nothing_rather_than_raising(tmp_path):
    sample = WorkerSampler(tmp_path / "nothing-here").sample()
    assert sample.workers == []


# -- the whole chain, in a real run -----------------------------------------


RECORDING_CONFTEST = """
import json


def pytest_failure_worker_sample(sample):
    with open("samples.jsonl", "a") as handle:
        handle.write(sample.model_dump_json() + "\\n")
"""


@needs_xdist
def test_a_real_run_pushes_samples_to_a_product_that_implements_the_hook(pytester):
    """Everything above drives the sampler directly, so none of it would
    notice the thread never starting or the hook never being registered."""
    pytester.makeconftest(RECORDING_CONFTEST)
    pytester.makepyfile(
        """
        import time

        def test_one():
            time.sleep(6)

        def test_two():
            time.sleep(6)
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "failure_instrumentation", ENABLE_FLAG, "-n", "2",
        "-o", "failure_sample_seconds=1",
        "-o", "failure_heartbeat_interval=1",
    )
    result.assert_outcomes(passed=2)

    lines = (pytester.path / "samples.jsonl").read_text().strip().splitlines()
    assert lines, "the sampler never pushed anything"
    samples = [json.loads(line) for line in lines]
    assert all(s["session_id"] for s in samples)
    # Both workers show up, doing the tests they were given.
    seen = {w["worker"] for s in samples for w in s["workers"]}
    assert {"gw0", "gw1"} <= seen
    nodeids = {w["nodeid"] for s in samples for w in s["workers"] if w["nodeid"]}
    assert any("test_one" in n or "test_two" in n for n in nodeids)
    # Statuses come from the truth table, not from a placeholder.
    assert {w["status"] for s in samples for w in s["workers"]} <= {
        "working", "blocked", "frozen", "gone", "unmeasured", "finished"
    }
    # And nothing was asked of a worker to produce any of it: a sample that
    # grew a frames field again would be a per-worker pause on a timer.
    assert not [key for s in samples for w in s["workers"] for key in w
                if "stack" in key]


@needs_xdist
def test_a_worker_that_ran_out_of_work_is_finished_and_not_frozen(pytester):
    """Two workers, one test each, one of them slow. The worker with the quick
    test is done within the first second and its process then idles inside
    execnet until the slow one finishes and the controller tears both down -
    which is xdist's design, not a hang. It was sampled as ``frozen`` for the
    whole of that wait, with a py-spy stack showing nothing but
    ``integrate_as_primary_thread`` on an ``Event.wait``, and a dashboard
    drawing that row red for every run whose work is unevenly split."""
    pytester.makeconftest(RECORDING_CONFTEST)
    pytester.makepyfile(
        """
        import time

        def test_fast():
            pass

        def test_slow():
            time.sleep(6)
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "failure_instrumentation", ENABLE_FLAG, "-n", "2", "--dist", "load",
        "-o", "failure_sample_seconds=1",
        "-o", "failure_heartbeat_interval=1",
    )
    result.assert_outcomes(passed=2)

    lines = (pytester.path / "samples.jsonl").read_text(encoding="utf-8").splitlines()
    statuses: dict[str, list[str]] = {}
    for line in lines:
        for row in json.loads(line)["workers"]:
            statuses.setdefault(row["worker"], []).append(row["status"])
    done = [worker for worker, seen in statuses.items() if "finished" in seen]
    assert done, f"no worker was ever reported finished: {statuses}"
    for worker in done:
        seen = statuses[worker]
        assert "frozen" not in seen, f"{worker} read as frozen while merely done: {seen}"
        # And done is final: nothing a finished worker does afterwards is a
        # sign of life, so the row never goes back to being a finding.
        assert seen[seen.index("finished"):] == ["finished"] * (
            len(seen) - seen.index("finished")
        ), seen


def test_sampling_is_off_unless_it_is_asked_for(pytester):
    """It is the only hook here that fires when nothing is wrong, so it is the
    only one that must not arrive because somebody upgraded."""
    pytester.makeconftest(
        """
        def pytest_failure_worker_sample(sample):
            with open("samples.jsonl", "a") as handle:
                handle.write("called\\n")
        """
    )
    pytester.makepyfile("def test_one():\n    assert True\n")
    result = pytester.runpytest_subprocess("-p", "failure_instrumentation", ENABLE_FLAG)
    result.assert_outcomes(passed=1)
    assert not (pytester.path / "samples.jsonl").exists()


def test_a_run_with_no_workers_is_sampled_like_any_other(pytester):
    """It used to be refused, and the refusal was right at the time: the
    recorder was installed on workers only, so a run without them wrote no
    state and the sampler would have polled an empty directory for the length
    of the run - which from the outside is indistinguishable from a product
    whose hook is never called.

    What changed is the premise. The process running the tests records itself
    now, so there is one worker to sample and it is called ``main``.
    """
    pytester.makeconftest(RECORDING_CONFTEST)
    pytester.makepyfile("import time\n\n\ndef test_one():\n    time.sleep(3)\n")
    result = pytester.runpytest_subprocess(
        "-p", "failure_instrumentation", ENABLE_FLAG,
        "-o", "failure_sample_seconds=1",
        "-o", "failure_heartbeat_interval=1",
    )
    result.assert_outcomes(passed=1)

    lines = (pytester.path / "samples.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [row for line in lines for row in json.loads(line)["workers"]]
    assert rows, "the sampler pushed nothing for a run it should have sampled"
    assert {row["worker"] for row in rows} == {"main"}
    assert any(row["nodeid"] == "test_a_run_with_no_workers_is_sampled_like_any_other.py::test_one" for row in rows), rows
