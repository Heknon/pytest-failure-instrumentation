"""Every verdict, including the ones no CI runner can be made to produce.

An OOM kill needs a kernel that just killed something, and an unobtainable exit
status needs a remote gateway. Those branches still have to be right, so they
are exercised here against a constructed incident rather than left to the
integration tests, which cover the ones a process can actually be driven into.
"""

from __future__ import annotations

import signal
import sys

import pytest

from pytest_failure_instrumentation.analysis import classify
from pytest_failure_instrumentation.incidents.base import CgroupMemory
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

STACK = ['  File "/srv/app/victim.py", line 6 in native_call']


def death(**fields) -> WorkerDeathIncident:
    fields.setdefault("worker", "gw1")
    fields.setdefault("test_in_flight", "test_api.py::test_thing")
    fields.setdefault("phase", "call")
    return WorkerDeathIncident(**fields)


def verdict_of(**fields):
    return classify.of(death(**fields))


@posix_only
def test_the_cgroup_counter_is_what_licenses_an_oom_verdict():
    verdict, confidence, evidence = verdict_of(
        exit_status=-signal.SIGKILL,
        cgroup_oom_kills_since_start=1,
        rss_mb_at_death=3900,
        cgroup=CgroupMemory(max_mb=4096),
    )
    assert verdict == "OOM_KILLED"
    assert confidence == "high"
    assert any("counter rose by 1" in line for line in evidence)
    assert any("of a 4096 MB cgroup limit" in line for line in evidence)


@posix_only
def test_the_same_kill_without_the_counter_is_only_a_kill():
    verdict, confidence, evidence = verdict_of(
        exit_status=-signal.SIGKILL, cgroup_oom_kills_since_start=0
    )
    # -9 is identical for the OOM killer, a cancelled CI job and a stray kill.
    assert verdict == "SIGKILLED"
    assert confidence == "medium"
    assert any("CI cancellation" in line for line in evidence)


@posix_only
def test_a_fault_is_high_confidence_from_the_signal_alone():
    verdict, confidence, _ = verdict_of(exit_status=-signal.SIGSEGV)
    assert (verdict, confidence) == ("NATIVE_CRASH", "high")


@posix_only
def test_a_faults_evidence_says_nothing_about_memory():
    """Resident size explains nothing about a segfault and sends the reader to
    look at memory on a crash that has nothing to do with it."""
    _, _, evidence = verdict_of(exit_status=-signal.SIGSEGV, rss_mb_at_death=3900)
    assert not any("resident memory" in line for line in evidence)


@posix_only
def test_a_stop_signal_is_named_and_excused():
    verdict, confidence, evidence = verdict_of(exit_status=-signal.SIGTERM)
    assert verdict == f"SIGNAL_{int(signal.SIGTERM)}"
    assert confidence == "high"
    assert any("nothing to triage" in line for line in evidence)


def test_a_windows_ntstatus_is_a_fault_wherever_it_is_decoded():
    verdict, confidence, evidence = verdict_of(exit_status=3221225477)
    assert (verdict, confidence) == ("NATIVE_CRASH", "high")
    assert any("access violation" in line for line in evidence)


def test_a_control_c_on_windows_is_an_interruption_not_a_defect():
    verdict, _, _ = verdict_of(exit_status=3221225786)
    assert verdict == "INTERRUPTED"


def test_the_128_plus_signal_convention_is_reported_as_a_convention():
    verdict, confidence, evidence = verdict_of(exit_status=143)
    assert verdict == "PROBABLY_SIGNALLED"
    # Low, because a wrapper ate the signal and 143 is a convention rather
    # than a fact about the process.
    assert confidence == "low"
    assert any("was not passed through" in line for line in evidence)


def test_a_clean_code_with_no_dump_is_the_worker_leaving_on_its_own():
    verdict, confidence, evidence = verdict_of(exit_status=3)
    assert (verdict, confidence) == ("SELF_EXIT", "medium")
    assert any("os._exit()" in line for line in evidence)


def test_a_dump_outranks_a_clean_exit_code():
    """On Windows abort() exits with 3, exactly as a deliberate os._exit(3)
    does. The dump is the only thing that tells them apart."""
    verdict, confidence, _ = verdict_of(exit_status=3, crash_stack=STACK)
    assert (verdict, confidence) == ("NATIVE_CRASH", "medium")


def test_exiting_zero_is_still_the_worker_leaving_on_its_own():
    """A worker that ends mid-run has gone wrong whatever number it exited
    with, and os._exit(0) inside a test is a real way to do it. Reported as
    UNKNOWN it reads as a status nobody could obtain, which sends the reader
    looking for a remote gateway that is not there."""
    verdict, confidence, evidence = verdict_of(exit_status=0)
    assert (verdict, confidence) == ("SELF_EXIT", "medium")
    assert any("os._exit()" in line for line in evidence)
    # And no memory figures: nothing about a clean exit points at RAM.
    assert not any("resident memory" in line for line in evidence)


def test_no_status_at_all_is_unknown_rather_than_assumed():
    verdict, confidence, evidence = verdict_of(exit_status=None)
    assert (verdict, confidence) == ("UNKNOWN", "low")
    assert any("remote gateways" in line for line in evidence)


def test_dying_before_any_test_says_so():
    _, _, evidence = verdict_of(exit_status=1, test_in_flight=None, phase=None)
    assert any("before running any test" in line for line in evidence)


def test_dying_between_tests_counts_what_finished():
    _, _, evidence = verdict_of(
        exit_status=1, test_in_flight=None, phase=None, tests_finished=12
    )
    assert any("after finishing 12" in line for line in evidence)
