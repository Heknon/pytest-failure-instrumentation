"""Test attributes: what a product tags its tests with, and where it arrives.

The tags are read where the test runs and written into its state slot, so the
tests that matter are the ones no test report could have carried them
through: a worker killed in setup, a stall, and a run killed outright and
recovered by the next one. The collector's bounds are checked directly,
because every reader downstream trusts them.
"""

from __future__ import annotations

import json
import sys
import warnings
from types import SimpleNamespace

import pytest

from pytest_failure_instrumentation import attributes
from pytest_failure_instrumentation.attributes import MAX_BYTES, MAX_KEYS, Collector
from pytest_failure_instrumentation.capture.state import WorkerState, read_state
from pytest_failure_instrumentation.config import FailureInstrumentationWarning, Settings
from pytest_failure_instrumentation.sampling import WorkerSampler

from .conftest import needs_xdist
from .test_sampling import evidence
from .test_single_process import LEAVES_WITHOUT_FINISHING, SUITE
from .test_stall import STALL_ARGUMENTS


def item(markers=None, answers=None, raises=None):
    """A test item as the collector sees one: markers, and a hook proxy whose
    answers arrive nearest-conftest first, the way pluggy returns them."""
    markers = markers or {}
    calls = []

    def hook(item):
        calls.append(item)
        if raises is not None:
            raise raises
        return list(answers or [])

    def closest(name):
        if name not in markers:
            return None
        return SimpleNamespace(args=markers[name])

    implemented = answers is not None or raises is not None
    fake = SimpleNamespace(
        get_closest_marker=closest,
        ihook=SimpleNamespace(pytest_failure_test_attributes=hook),
        config=SimpleNamespace(hook=SimpleNamespace(
            pytest_failure_test_attributes=SimpleNamespace(
                get_hookimpls=lambda: ["impl"] if implemented else []
            )
        )),
    )
    return fake, calls


# -- what one test is tagged with -------------------------------------------


def test_nothing_asked_for_costs_a_test_nothing():
    """No marker named and no hook implemented: the hook proxy is never
    reached, which is the per-test cost of a run that tags nothing."""
    fake, calls = item()
    collect = Collector()
    assert collect(fake) == {}
    assert collect(fake) == {}
    assert calls == []


def test_a_named_marker_gives_its_first_argument_or_true():
    fake, _ = item(markers={"team": ("payments",), "slow": (), "tier": (2,)})
    assert Collector(("team", "slow", "tier", "absent"))(fake) == {
        "team": "payments", "slow": "true", "tier": "2",
    }


def test_the_nearest_answer_wins_and_the_test_s_own_marker_over_all():
    """A conftest's answer is a default for its directory, and one nearer the
    test is more specific - and a marker is on the test itself."""
    nearest = {"team": "search", "suite": "ranking"}
    outer = {"team": "platform", "tier": "1", "suite": "all"}
    fake, calls = item(markers={"team": ("payments",)}, answers=[nearest, outer, None])
    assert Collector(("team",))(fake) == {"team": "payments", "suite": "ranking", "tier": "1"}
    assert len(calls) == 1


def test_a_hook_that_raises_costs_its_answer_and_is_said_once():
    fake, _ = item(markers={"team": ("payments",)}, raises=RuntimeError("catalogue down"))
    collect = Collector(("team",))
    with pytest.warns(FailureInstrumentationWarning, match="catalogue down"):
        assert collect(fake) == {"team": "payments"}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert collect(fake) == {"team": "payments"}


def test_what_cannot_be_a_dimension_is_dropped_rather_than_cut():
    """A cut value is a different value: two tests would share a tag neither
    of them has. So anything over a bound is dropped whole, and said."""
    fake, _ = item(answers=[{
        "team": "payments",
        "1st": "not a name",
        "has space": "not a name either",
        "x" * 65: "too long a name",
        "ticket": "t" * 129,
        "empty": "",
        "owner": None,
        "data": {"nested": "no"},
        "flag": False,
        "ratio": 0.5,
    }])
    with pytest.warns(FailureInstrumentationWarning):
        found = Collector()(fake)
    assert found == {"team": "payments", "flag": "false", "ratio": "0.5"}


def test_a_test_carries_a_bounded_number_of_attributes_in_a_bounded_size():
    many = {f"key{index:02d}": "v" for index in range(MAX_KEYS + 4)}
    fake, _ = item(answers=[many])
    with pytest.warns(FailureInstrumentationWarning, match=f"at most {MAX_KEYS}"):
        assert len(Collector()(fake)) == MAX_KEYS

    wide = {f"k{index}": "v" * 120 for index in range(12)}
    fake, _ = item(answers=[wide])
    with pytest.warns(FailureInstrumentationWarning, match=f"{MAX_BYTES} bytes"):
        kept = Collector()(fake)
    assert kept and len(json.dumps(kept).encode()) <= MAX_BYTES


def test_only_a_record_with_a_test_in_flight_has_tags():
    """A worker between tests is running nobody's test, and a record read
    back from disk is trusted for nothing it does not have the shape of."""
    assert attributes.in_flight({"nodeid": "t.py::a", "test_attributes": {"team": "x"}}) == {"team": "x"}
    assert attributes.in_flight({"nodeid": None, "test_attributes": {"team": "x"}}) == {}
    assert attributes.in_flight({"nodeid": "t.py::a", "test_attributes": ["team"]}) == {}
    assert attributes.in_flight({"nodeid": "t.py::a", "test_attributes": {"team": 1, "ok": "y"}}) == {"ok": "y"}
    assert attributes.in_flight(None) == {}


# -- where they are written and read ----------------------------------------


def test_the_slot_carries_them_only_while_a_tagged_test_is_in_flight(tmp_path):
    state = WorkerState(tmp_path / "gw0.state", pid=1, run_id="r")
    state.update(nodeid="t.py::a", phase="setup")
    assert "test_attributes" not in read_state(state.path)

    state.test_attributes = {"team": "payments"}
    state.update(nodeid="t.py::b", phase="setup")
    assert read_state(state.path)["test_attributes"] == {"team": "payments"}

    state.update(phase=None, nodeid=None, test_attributes=None)
    assert "test_attributes" not in read_state(state.path)
    state.close()


def test_a_sample_row_carries_them_and_an_untagged_one_dumps_as_it_did(tmp_path):
    root = evidence(tmp_path / "run", {"gw0": [1.0, 3.0], "gw1": [1.0, 3.0]})
    record = json.loads((root / "gw0.state").read_bytes().rstrip(b"\x00"))
    record["test_attributes"] = {"team": "payments"}
    raw = json.dumps(record).encode()
    (root / "gw0.state").write_bytes(raw + b"\x00" * (5120 - len(raw)))

    by_worker = {row.worker: row for row in WorkerSampler(root).sample().workers}
    assert by_worker["gw0"].test_attributes == {"team": "payments"}
    assert by_worker["gw0"].model_dump()["test_attributes"] == {"team": "payments"}
    assert "test_attributes" not in by_worker["gw1"].model_dump()


def test_the_marker_names_travel_to_the_workers_and_the_metrics_switch_does_not():
    settings = Settings(test_attributes="team  component team", otel_meter_provider=object())
    assert settings.test_attributes == ("team", "component")
    # A provider handed over is a request for the metrics it would carry.
    assert settings.otel is True
    payload = settings.as_payload()
    assert payload["test_attributes"] == ["team", "component"]
    assert "otel" not in payload and "otel_meter_provider" not in payload
    assert Settings.from_payload(payload).test_attributes == ("team", "component")


# -- the cases no test report survives --------------------------------------

TAGGING_CONFTEST = '''

def pytest_failure_test_attributes(item):
    return {"suite": item.module.__name__, "team": "platform"}
'''

DIES = """
import os
import signal

import pytest


@pytest.fixture
def dies():
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.mark.team("payments")
def test_dies_in_setup(dies):
    pass


def test_dies_in_call():
    os.kill(os.getpid(), signal.SIGKILL)
"""


@needs_xdist
@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_a_worker_killed_in_setup_still_says_whose_test_it_was(distributed):
    """A worker that dies in setup never sends a report for the test, so the
    controller has nothing of the test's own to go on - except the slot the
    worker wrote as setup began."""
    distributed.pytester.makeconftest(
        (distributed.pytester.path / "conftest.py").read_text() + TAGGING_CONFTEST
    )
    distributed.pytester.makeini(
        "[pytest]\nfailure_test_attributes = team\nmarkers =\n    team(name): owner\n"
    )
    distributed.pytester.makepyfile(test_dies=DIES)
    incidents = distributed.run("-n", "2")

    deaths = {death.test_in_flight: death for death in distributed.of_kind(incidents, "worker_death")}
    assert deaths["test_dies.py::test_dies_in_setup"].phase == "setup"
    assert deaths["test_dies.py::test_dies_in_setup"].test_attributes == {
        "suite": "test_dies", "team": "payments",
    }
    assert deaths["test_dies.py::test_dies_in_call"].test_attributes == {
        "suite": "test_dies", "team": "platform",
    }
    # And in the payload, for the table a product stores these in.
    assert deaths["test_dies.py::test_dies_in_call"].model_dump()["test_attributes"]["team"] == "platform"


def test_a_run_killed_outright_is_recovered_with_its_tags(runner):
    """The run that has no survivor at all. The next run reads the dead run's
    slot, and the tags are in it."""
    runner.pytester.makeconftest(
        (runner.pytester.path / "conftest.py").read_text() + TAGGING_CONFTEST
    )
    runner.pytester.makepyfile(test_gone=LEAVES_WITHOUT_FINISHING)
    assert runner.run("test_gone.py", "-o", "failure_kill_trace=false") == []

    runner.pytester.makepyfile(test_after=SUITE)
    death = runner.only(runner.run("test_after.py"), "worker_death")
    assert death.test_in_flight == "test_gone.py::test_leaves"
    assert death.test_attributes == {"suite": "test_gone", "team": "platform"}


def test_a_run_that_tags_nothing_reports_no_tags(runner):
    runner.pytester.makepyfile(test_gone=LEAVES_WITHOUT_FINISHING)
    runner.run("test_gone.py", "-o", "failure_kill_trace=false")
    runner.pytester.makepyfile(test_after=SUITE)
    death = runner.only(runner.run("test_after.py"), "worker_death")
    assert death.test_attributes == {}
    assert "test_attributes" not in death.model_dump()


@needs_xdist
def test_a_stalled_worker_says_whose_test_is_stuck(distributed):
    distributed.pytester.makeconftest(
        (distributed.pytester.path / "conftest.py").read_text() + TAGGING_CONFTEST
    )
    distributed.pytester.makepyfile(
        test_hang="""
        import threading

        never_set = threading.Event()


        def test_deadlocks():
            never_set.wait(25)
        """
    )
    incidents = distributed.run("-n", "1", *STALL_ARGUMENTS, "test_hang.py", timeout=180)

    stall = distributed.only(incidents, "worker_stall")
    assert stall.test_in_flight == "test_hang.py::test_deadlocks"
    assert stall.test_attributes == {"suite": "test_hang", "team": "platform"}
