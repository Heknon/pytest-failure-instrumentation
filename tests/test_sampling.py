"""Periodic worker samples, and the two things that keep them affordable.

Sampling every worker's stack on a cadence is the most expensive thing this
package can be asked to do. These are the tests for the two decisions that
make it cheap - read a stack only for a worker the truth table already calls
stuck, and send a stack only when it is not the one already sent.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation.probes import pyspy
from pytest_failure_instrumentation.sampling import (
    MAX_STACKS_PER_SAMPLE,
    WorkerSampler,
    digest_of,
)

from .conftest import needs_xdist

needs_pyspy = pytest.mark.skipif(
    not pyspy.available(), reason="py-spy is not installed in this environment"
)

FRAMES_A = [{"name": "t", "frames": [{"function": "wait", "file": "a.py", "line": 3}]}]
FRAMES_B = [{"name": "t", "frames": [{"function": "poll", "file": "b.py", "line": 9}]}]


def evidence(root: Path, workers, run_id="run-1", beats_apart=5.0, now=None, pid=None):
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
                 "time": moment, "tests_started": 1, "tests_finished": 0}
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


def reader(answer):
    """A stand-in for py-spy, so the dedupe can be driven without it."""
    def read(pid):
        value = answer() if callable(answer) else answer
        return (value, None) if value else (None, "nothing to read")
    return read


# -- what gets a stack at all ---------------------------------------------


def test_a_working_worker_is_reported_but_never_read(tmp_path):
    """The saving that makes this affordable at all. A worker burning CPU is
    working, the heartbeat already said so, and reading its stack costs a
    subprocess and a pause to learn nothing."""
    asked: list[int] = []

    def counting(pid):
        asked.append(pid)
        return FRAMES_A, None

    # A rising CPU series is a busy worker.
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0, 5.0, 7.0]})
    sample = WorkerSampler(root, reader=counting).sample()

    assert [w.status for w in sample.workers] == ["working"]
    assert asked == [], "a working worker was read"
    assert sample.workers[0].stack is None
    # Still reported: the row is the live view, and it is nearly free.
    assert sample.workers[0].nodeid == "test_x.py::gw0"
    assert sample.workers[0].cpu_rate is not None


def test_a_blocked_worker_is_read(tmp_path):
    """Flat CPU with a live heartbeat is the case worth a stack."""
    root = evidence(tmp_path / "run", {"gw0": [5.0, 5.0, 5.0, 5.0]})
    sample = WorkerSampler(root, reader=reader(FRAMES_A)).sample()

    assert [w.status for w in sample.workers] == ["blocked"]
    assert sample.workers[0].stack == FRAMES_A
    assert sample.workers[0].stack_digest == digest_of(FRAMES_A)
    assert sample.workers[0].stack_repeats == 0


def test_stacks_can_be_declined_while_the_statuses_are_kept(tmp_path):
    """The two halves have very different prices, so they are separable: the
    rows come from files the run wrote anyway, the frames do not."""
    root = evidence(tmp_path / "run", {"gw0": [5.0, 5.0, 5.0]})
    sample = WorkerSampler(root, want_stacks=False, reader=reader(FRAMES_A)).sample()

    assert sample.workers[0].status == "blocked"
    assert sample.workers[0].stack is None
    assert sample.workers[0].stack_digest is None


# -- not sending the same stack twice --------------------------------------


def test_the_digest_does_not_change_when_only_the_thread_order_does():
    """The saving rests on two reads of one place agreeing, and neither reader
    promises an order: py-spy walks the interpreter's thread list, and the
    in-process reader iterates ``sys._current_frames()``, a dict. Order-
    sensitive, the digest changed when nothing had moved and the full stack
    went out again every pass - the eight thousand copies this module exists to
    avoid."""
    threads = [
        {"thread_id": 1, "thread_name": "MainThread", "frames": [{"function": "wait", "file": "t.py", "line": 3}]},
        {"thread_id": 2, "thread_name": "heartbeat", "frames": [{"function": "_run", "file": "h.py", "line": 9}]},
    ]

    assert digest_of(threads) == digest_of(list(reversed(threads)))
    # Still a digest of the frames, so a process that moved still reads as
    # moved - and two threads in the same place still count twice.
    moved = [dict(threads[0], frames=[{"function": "wait", "file": "t.py", "line": 4}]), threads[1]]
    assert digest_of(threads) != digest_of(moved)
    assert digest_of([threads[0], dict(threads[0])]) != digest_of([threads[0]])


def test_an_unchanged_stack_is_sent_once_and_then_counted(tmp_path):
    """The whole reason this is affordable for the workers that matter most.
    A worker wedged for a day has one stack, and storing it 8,640 times is
    8,640 copies of one fact."""
    root = evidence(tmp_path / "run", {"gw0": [5.0, 5.0, 5.0]})
    sampler = WorkerSampler(root, reader=reader(FRAMES_A))

    first = sampler.sample().workers[0]
    assert first.stack == FRAMES_A and first.stack_repeats == 0

    for expected in (1, 2, 3):
        later = sampler.sample().workers[0]
        assert later.stack is None, "the same stack was sent twice"
        # Still identified, so a suppressed row joins to the one with frames.
        assert later.stack_digest == digest_of(FRAMES_A)
        assert later.stack_repeats == expected


def test_a_stack_that_moves_is_sent_again(tmp_path):
    """Suppression must be of repeats, not of stacks. A worker that moved to a
    different frame has news, and reporting it as a repeat would describe it
    with the stack it left."""
    root = evidence(tmp_path / "run", {"gw0": [5.0, 5.0, 5.0]})
    frames = [FRAMES_A]
    sampler = WorkerSampler(root, reader=reader(lambda: frames[0]))

    assert sampler.sample().workers[0].stack == FRAMES_A
    assert sampler.sample().workers[0].stack is None
    frames[0] = FRAMES_B
    moved = sampler.sample().workers[0]
    assert moved.stack == FRAMES_B
    assert moved.stack_digest == digest_of(FRAMES_B)
    assert moved.stack_repeats == 0


def test_the_same_stack_under_a_new_test_is_sent_again(tmp_path):
    """The subtle one. A worker that blocks in the same library call on the
    next test has an identical stack and a different subject - suppressing it
    files the new test's evidence under the old test's document."""
    root = tmp_path / "run"
    evidence(root, {"gw0": [5.0, 5.0, 5.0]})
    sampler = WorkerSampler(root, reader=reader(FRAMES_A))
    assert sampler.sample().workers[0].stack == FRAMES_A
    assert sampler.sample().workers[0].stack is None

    # Same worker, same frames, different test.
    state = json.loads((root / "gw0.state").read_bytes().rstrip(b"\x00"))
    state["nodeid"] = "test_x.py::a_different_test"
    raw = json.dumps(state).encode()
    (root / "gw0.state").write_bytes(raw + b"\x00" * (5120 - len(raw)))

    again = sampler.sample().workers[0]
    assert again.nodeid == "test_x.py::a_different_test"
    assert again.stack == FRAMES_A, "frames were suppressed across a change of test"


def test_a_worker_that_recovers_starts_over(tmp_path):
    """A worker that went back to work and later blocks again is reporting a
    new stall. Carrying the old digest forward would make its first stack read
    as a repeat of something hours earlier."""
    root = tmp_path / "run"
    evidence(root, {"gw0": [5.0, 5.0, 5.0]})
    sampler = WorkerSampler(root, reader=reader(FRAMES_A))
    assert sampler.sample().workers[0].stack == FRAMES_A

    evidence(root, {"gw0": [1.0, 3.0, 5.0, 7.0]})   # busy again
    assert sampler.sample().workers[0].status == "working"

    evidence(root, {"gw0": [9.0, 9.0, 9.0]})        # blocked again
    resumed = sampler.sample().workers[0]
    assert resumed.stack == FRAMES_A
    assert resumed.stack_repeats == 0


# -- not falling over -------------------------------------------------------


def test_a_reader_that_fails_says_so_rather_than_reporting_no_frames(tmp_path):
    """Absence of a stack is a fact about the host - no py-spy, a refused
    ptrace - and an empty stack field would read as "the worker had none"."""
    def refuses(pid):
        return None, "Operation not permitted (os error 1)"

    root = evidence(tmp_path / "run", {"gw0": [5.0, 5.0, 5.0]})
    entry = WorkerSampler(root, reader=refuses).sample().workers[0]
    assert entry.stack is None
    assert entry.stack_digest is None
    assert "not permitted" in (entry.stack_error or "")


def test_a_reader_that_raises_does_not_end_the_sample(tmp_path):
    def explodes(pid):
        raise RuntimeError("py-spy went away")

    root = evidence(tmp_path / "run", {"gw0": [5.0, 5.0, 5.0],
                                       "gw1": [5.0, 5.0, 5.0]})
    sample = WorkerSampler(root, reader=explodes).sample()
    assert len(sample.workers) == 2
    assert all("py-spy went away" in (w.stack_error or "") for w in sample.workers)


def test_a_run_where_everything_wedged_at_once_is_bounded_and_says_so(tmp_path):
    """Each stack is a subprocess that pauses its target. A run that wedged
    entirely should not turn its own diagnosis into the slowest thing on the
    host - and what was dropped is named, because a bound nobody is told about
    reads as full coverage."""
    stuck = {f"gw{i}": [5.0, 5.0, 5.0] for i in range(MAX_STACKS_PER_SAMPLE + 4)}
    root = evidence(tmp_path / "run", stuck)
    sample = WorkerSampler(root, reader=reader(FRAMES_A)).sample()

    with_frames = [w for w in sample.workers if w.stack is not None]
    assert len(with_frames) == MAX_STACKS_PER_SAMPLE
    assert len(sample.stacks_not_taken) == 4
    # Everyone is still reported, whether or not they were read.
    assert len(sample.workers) == MAX_STACKS_PER_SAMPLE + 4
    assert all(w.status == "blocked" for w in sample.workers)


def test_a_fleet_larger_than_the_cap_is_covered_over_passes_not_never(tmp_path):
    """The cap bounds cost; it must not decide who is visible.

    Read from the top of the list every pass and the seventeenth stuck worker
    onwards was never read at all - not late, never - while the first sixteen
    were re-read and then suppressed as repeats. Measured: twenty-two stuck
    workers over four passes left six of them unread on every one. On a
    sixty-four-way run that wedged entirely, which is the case this exists
    for, that is forty-eight workers a UI never sees a frame of.
    """
    names = {f"gw{index:02d}": [5.0, 5.0, 5.0] for index in range(MAX_STACKS_PER_SAMPLE + 6)}
    root = evidence(tmp_path / "run", names)
    sampler = WorkerSampler(root, reader=reader(FRAMES_A))

    read_at_least_once: set[str] = set()
    for _ in range(2):
        sample = sampler.sample()
        assert len(sample.stacks_not_taken) == 6, "the cap still bounds each pass"
        read_at_least_once |= {
            entry.worker
            for entry in sample.workers
            if entry.stack is not None or entry.stack_digest
        }

    assert read_at_least_once == set(names), "some worker is never read on any pass"


def test_an_empty_directory_samples_nothing_rather_than_raising(tmp_path):
    sample = WorkerSampler(tmp_path / "nothing-here").sample()
    assert sample.workers == []


# -- the whole chain, in a real run -----------------------------------------


@needs_xdist
def test_a_real_run_pushes_samples_to_a_product_that_implements_the_hook(pytester):
    """Everything above drives the sampler directly, so none of it would
    notice the thread never starting or the hook never being registered."""
    pytester.makeconftest(
        """
        import json


        def pytest_failure_worker_sample(sample):
            with open("samples.jsonl", "a") as handle:
                handle.write(sample.model_dump_json() + "\\n")
        """
    )
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
        "-p", "failure_instrumentation", "-n", "2",
        "-o", "failure_sample_seconds=1",
        "-o", "failure_heartbeat_interval=1",
        "-o", "failure_sample_stacks=false",
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
        "working", "blocked", "frozen", "gone", "unmeasured"
    }


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
    result = pytester.runpytest_subprocess("-p", "failure_instrumentation")
    result.assert_outcomes(passed=1)
    assert not (pytester.path / "samples.jsonl").exists()


@needs_xdist
@needs_pyspy
def test_a_real_run_samples_real_frames_from_a_blocked_worker(pytester):
    """The seam every test above stubs: the sampler calling the real reader.

    The dedupe is well covered with a stand-in, and a stand-in cannot fail the
    way the real thing does - no py-spy, a refused ptrace, a worker that moved
    on between the status read and the dump. Without this, the expensive half
    of the feature is verified only in the abstract.
    """
    pytester.makeconftest(
        """
        import json


        def pytest_failure_worker_sample(sample):
            with open("samples.jsonl", "a") as handle:
                handle.write(sample.model_dump_json() + "\\n")
        """
    )
    pytester.makepyfile(
        """
        import threading

        never_set = threading.Event()

        def test_blocks():
            never_set.wait(12)
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "failure_instrumentation", "-n", "1",
        "-o", "failure_sample_seconds=1",
        "-o", "failure_heartbeat_interval=1",
        "-o", "failure_sample_stacks=true",
    )
    result.assert_outcomes(passed=1)

    samples = [
        json.loads(line)
        for line in (pytester.path / "samples.jsonl").read_text().strip().splitlines()
    ]
    workers = [w for s in samples for w in s["workers"]]
    assert workers, "the sampler pushed nothing"

    # A worker parked in Event.wait burns no CPU, so the truth table calls it
    # blocked - which is the status that earns a stack.
    with_frames = [w for w in workers if w.get("stack")]
    assert with_frames, (
        "no sample carried frames; statuses seen: "
        f"{sorted({w['status'] for w in workers})}, "
        f"errors: {sorted({w['stack_error'] for w in workers if w.get('stack_error')})}"
    )

    frames = [f for w in with_frames for t in w["stack"] for f in t["frames"]]
    assert any(f.get("function") == "test_blocks" for f in frames), (
        "the frames came back but not the test's own"
    )
    # And the dedupe held against the real reader: a worker parked in one place
    # sends its stack once and counts the rest.
    repeats = [w for w in workers if w.get("stack_digest") and not w.get("stack")]
    assert repeats, "every sample re-sent the stack of a worker that never moved"


def test_sampling_a_run_with_no_workers_says_so_rather_than_pushing_nothing(pytester):
    """The recorder is installed on workers only, so a single-process run
    writes no state at all and the sampler would poll an empty directory for
    the life of the run. From the outside that is indistinguishable from a
    product whose hook is simply never called - which is the misreading this
    package exists to prevent, arriving in its own newest feature."""
    pytester.makepyfile("def test_one():\n    assert True\n")
    result = pytester.runpytest_subprocess(
        "-p", "failure_instrumentation", "-o", "failure_sample_seconds=1"
    )
    result.assert_outcomes(passed=1)
    # Raised at session start, which is outside the window pytest folds into
    # its warnings summary, so it lands on stderr - where a person running the
    # suite still sees it.
    combined = result.stdout.str() + result.stderr.str()
    assert "not distributed" in combined and "no workers to sample" in combined
