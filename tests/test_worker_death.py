"""A worker process that ended when it should not have.

Cross-platform by construction: an access violation is reachable everywhere
through ctypes, and the POSIX-only signals are marked as such rather than
skipped silently.
"""

from __future__ import annotations

import signal
import sys

import pytest

from .conftest import INNER_CONFTEST, needs_xdist

pytestmark = needs_xdist

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def crashing_test(body: str) -> str:
    return f"""
        import ctypes
        import os
        import signal

        import victim


        def test_filler():
            assert True


        def test_crashes():
            {body}
        """


def test_a_native_crash_is_reported_with_the_status_the_platform_gives(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.native_call(1)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "NATIVE_CRASH"
    assert death.test_in_flight == "test_crash.py::test_crashes"
    assert death.phase == "call"
    assert death.run_ending is False

    if sys.platform != "win32":
        assert death.exit_status == -signal.SIGSEGV
    else:
        # abort() exits with 3, the same as a deliberate os._exit(3). What
        # separates them is the dump asserted below, and nothing else.
        assert death.exit_status == 3

    # The deepest frame is ctypes or the CRT; the useful one is whoever
    # called it.
    assert death.crash_stack
    assert death.blamed_frame is not None
    assert death.blamed_frame.module == "victim"
    assert death.owner == "product"
    assert death.severity == "critical"


def test_a_deliberate_exit_is_not_reported_as_a_crash(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.hard_exit(3)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "SELF_EXIT"
    assert death.exit_status == 3


@posix_only
def test_sigkill_does_not_claim_an_oom_it_cannot_prove(distributed):
    distributed.pytester.makepyfile(
        test_crash=crashing_test("os.kill(os.getpid(), signal.SIGKILL)")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.exit_status == -signal.SIGKILL
    if death.kill_sources is not None and death.kill_sources.signal_trace.endswith("tracefs"):
        # Root, or a sudo this run may spend: the kernel's signal tracepoint
        # saw the worker send the signal to itself, and says so.
        assert death.verdict == "SELF_KILLED"
        assert death.killer is not None and death.killer.origin == "self"
    else:
        # OOM_KILLED needs the cgroup counter to have moved. Without it, or a
        # witness to the sender, the honest answer is that something killed it
        # - and which witnesses this machine withheld is on the incident.
        assert death.verdict == "SIGKILLED"
        assert any("No cgroup OOM kill was counted" in line for line in death.evidence)
        assert any(line.startswith("Kill witnesses:") for line in death.evidence)


def _has_pytest_timeout() -> bool:
    try:
        import pytest_timeout  # noqa: F401
    except ImportError:
        return False
    return True


needs_pytest_timeout = pytest.mark.skipif(
    not _has_pytest_timeout(), reason="pytest-timeout is not installed"
)


@posix_only
@needs_pytest_timeout
def test_a_worker_killed_by_pytest_timeout_reads_as_a_timeout(distributed):
    """pytest-timeout's thread method os._exit(1)s a hung worker. That is a
    plain SELF_EXIT from the outside; the test having reached the configured
    timeout is what names it."""
    distributed.pytester.makepyfile(
        test_hang="""
        import time


        def test_filler():
            assert True


        def test_hangs():
            time.sleep(60)
        """
    )
    incidents = distributed.run(
        "-n", "2", "--timeout", "3", "--timeout-method", "thread",
        "test_hang.py", timeout=120,
    )
    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "POSSIBLE_TIMEOUT", death.evidence
    assert death.test_in_flight == "test_hang.py::test_hangs"
    assert death.matched_timeout == 3.0 and death.timeout_source == "pytest-timeout"
    assert death.test_seconds is not None and death.test_seconds >= 3.0
    # It points at the hung test, so it is scored like the test's owner.
    assert death.suspect_nodeid() == "test_hang.py::test_hangs"


@posix_only
def test_a_stop_signal_is_not_a_defect(distributed):
    distributed.pytester.makepyfile(
        test_crash=crashing_test("os.kill(os.getpid(), signal.SIGTERM)")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == f"SIGNAL_{int(signal.SIGTERM)}"
    # Nobody is paged for a run somebody asked to stop.
    assert death.severity == "informational"


@pytest.mark.skipif(sys.platform != "win32", reason="NTSTATUS is a Windows exit code")
@pytest.mark.parametrize(
    "status, verdict, meaning",
    [
        (0xC0000005, "NATIVE_CRASH", "access violation"),
        (0xC000013A, "INTERRUPTED", "control-C"),
    ],
)
def test_a_windows_ntstatus_is_decoded_as_what_it_stands_for(
    distributed, status, verdict, meaning
):
    """The decode table is unit-tested everywhere; this is the only place a
    real process actually exits with one of those codes."""
    distributed.pytester.makepyfile(
        test_crash=crashing_test(f"victim.exit_with_ntstatus({status})")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.exit_status == status
    assert death.verdict == verdict
    assert meaning in death.exit_status_meaning
    # TerminateProcess leaves no dump, so there is no frame to blame at all.
    # What is left is the test that was in flight, offered as a lead: it names
    # whoever owns the test module, which is not the same claim as knowing
    # whose code failed.
    assert death.owner == "unknown"
    assert death.suspect_owner == "customer-code"
    assert "test_crash.py" in (death.suspect_basis or "")


def test_a_wrapped_signal_is_not_mistaken_for_a_chosen_exit_code(distributed):
    """Shells and container runtimes report a signal death as 128 + signal
    rather than passing the signal through, so the code alone is a convention
    and the confidence has to say so."""
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.hard_exit(143)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.exit_status == 143
    assert death.verdict == "PROBABLY_SIGNALLED"
    assert death.confidence == "low"


def test_the_phase_is_recorded_because_pytest_cannot_tell_you(distributed):
    distributed.pytester.makepyfile(
        test_crash="""
        import victim


        def test_dies_in_teardown(request):
            request.addfinalizer(lambda: victim.native_call(1))
        """
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.phase == "teardown"
    assert death.test_in_flight == "test_crash.py::test_dies_in_teardown"


def test_a_parametrized_node_id_still_names_the_test_that_died(distributed):
    """A real node id is long: a path, a class, and a parameter set spelling
    out the case - often with content hashes in it, which are the part that
    says *which* case. It has to survive the fixed-size slot it is written to
    whole, or the one fact this plugin exists to recover is the one it loses,
    and the death is reported as one that happened before any test ran."""
    distributed.pytester.makepyfile(
        test_crash="""
        import pytest

        import victim

        CASE = (
            "input=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
            "-expected=2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
        )


        def test_filler():
            assert True


        @pytest.mark.parametrize("case", [CASE])
        def test_invoice_reconciliation_matrix_for_the_quarterly_close(case):
            victim.native_call(1)
        """
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "NATIVE_CRASH"
    assert death.phase == "call"
    assert death.tests_started == 1
    # Whole, hashes and all: a truncated id names the test but not the case,
    # which is the difference between a report you can re-run and one you
    # cannot.
    assert death.test_in_flight is not None
    assert death.test_in_flight.startswith(
        "test_crash.py::test_invoice_reconciliation_matrix_for_the_quarterly_close"
    )
    assert death.test_in_flight.endswith("2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae]")
    assert "before running any test" not in str(death)


def test_the_run_id_is_the_one_xdist_writes_in_its_own_logs(distributed):
    """Correlating an incident with the run it came from means using xdist's
    id for that run, not one invented here - and every incident in the run has
    to carry the same one."""
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.native_call(1)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    # xdist's testrunuid is a uuid4 hex; the fallback is prefixed "run-".
    assert len(death.run_id) == 32 and not death.run_id.startswith("run-")
    assert {incident.run_id for incident in incidents} == {death.run_id}


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="RLIMIT_AS is not reliably enforced outside Linux",
)
def test_a_memory_ceiling_turns_a_silent_kill_into_a_traceback(distributed):
    """The opt-in cap makes the allocation fail *inside* the process.

    An OOM kill leaves no exception, no traceback and no node id, because
    SIGKILL cannot be caught. A ceiling trades a hard limit per worker for a
    MemoryError that names the test - which is the whole point of offering it.
    """
    distributed.pytester.makepyfile(
        test_greedy="""
        def test_filler():
            assert True


        def test_allocates_too_much():
            held = bytearray(4 * 1024 ** 3)
            assert held
        """
    )
    incidents = distributed.run(
        "-n", "2", "-o", "failure_memory_limit_mb=2048", "test_greedy.py", timeout=180
    )

    # The worker survived, so there is nothing for this plugin to report.
    assert distributed.of_kind(incidents, "worker_death") == []
    distributed.result.assert_outcomes(passed=1, failed=1)
    distributed.result.stdout.fnmatch_lines(["*MemoryError*"])


def test_the_capabilities_of_the_machine_are_recorded(distributed):
    distributed.pytester.makepyfile(test_crash=crashing_test("victim.native_call(1)"))
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.capabilities is not None
    assert death.capabilities.system
    # A figure that is absent must be distinguishable from a healthy one.
    assert death.capabilities.resident_memory
    assert death.product_version == "1.2.3"


def test_the_opt_in_probes_name_the_line_holding_the_memory(distributed):
    """tracemalloc and the object census are off by default because walking
    the heap on a worker near its ceiling is what makes things worse. Switched
    on, a high-water snapshot is what attributes a memory problem to a source
    line - the OOM equivalent of a stack."""
    distributed.pytester.makepyfile(
        test_greedy="""
        import time

        import victim

        HELD = []


        def test_filler():
            assert True


        def test_grows_then_dies():
            HELD.append(bytearray(64 * 1024 * 1024))
            time.sleep(3)          # outlive one watchdog tick
            victim.native_call(1)
        """
    )
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_high_water_mb=1",
        "-o", "failure_heartbeat_interval=1",
        "-o", "failure_tracemalloc_depth=1",
        "-o", "failure_object_census=true",
        "test_greedy.py",
        timeout=180,
    )

    death = distributed.only(incidents, "worker_death")
    assert death.high_water, "the watchdog crossed no mark"
    mark = death.high_water[-1]
    assert mark["rss_mb"] >= 1
    assert mark["nodeid"] == "test_greedy.py::test_grows_then_dies"
    # The allocating line, which is the whole reason the depth setting exists.
    assert any(
        "test_greedy.py" in entry["file"] for entry in mark["top_allocations"]
    ), mark["top_allocations"]
    assert mark["objects_by_type"]


def test_the_watchdog_does_not_kill_the_workers_it_is_watching(distributed):
    """The stack watchdog used to be ``faulthandler.dump_traceback_later``.

    That dumps from a C thread which does not hold the GIL, walking every
    other thread's frames while those threads push and pop them. A dump that
    lands while the interpreter is *executing* - a phase boundary rather than
    a sleep - reads a frame being torn down and the worker segfaults. With
    ``repeat=True`` and a cadence a quarter of the test length, that was 10
    runs out of 10 here; the file was left ending mid-frame, with a nonsense
    line number, and the crash file empty because the fault was inside the
    dumper.

    So this runs a suite whose every test outlives the cadence several times
    over, and asks for the one thing instrumentation must never do.
    """
    body = "\n".join(
        f"def test_{index:02d}():\n    time.sleep(0.2)\n" for index in range(40)
    )
    distributed.pytester.makepyfile(test_churn=f"import time\n\n{body}")

    incidents = distributed.run(
        "-n", "1",
        # Twelve or so dumps per test, every one of them landing somewhere the
        # old timer could not safely look.
        "-o", "failure_slow_test_seconds=0.05",
        "-o", "failure_heartbeat_interval=0.5",
        "test_churn.py",
        timeout=180,
    )

    assert distributed.of_kind(incidents, "worker_death") == [], (
        "the watchdog killed the worker it was watching"
    )
    distributed.result.assert_outcomes(passed=40)


def test_a_slow_test_that_passed_is_not_the_crash_that_killed_the_worker(distributed):
    """The watchdog dump and the fatal dump used to share a file.

    A test that outlives failure_slow_test_seconds writes a stack and then goes
    on to pass. Read afterwards as crash evidence, it turned the *next* test's
    deliberate exit into a NATIVE_CRASH and blamed the frame it happened to
    hold - a function that was not running, in a test that succeeded. When that
    frame is in the product's own package, an unrelated clean exit arrives as
    severity=critical, owner=product.
    """
    distributed.pytester.makepyfile(
        victim_slow="""
        import time


        def slow_product_call():
            time.sleep(4)
        """
    )
    distributed.pytester.makepyfile(
        test_slow_then_exit="""
        import os

        import victim_slow


        def test_is_merely_slow():
            victim_slow.slow_product_call()


        def test_leaves_on_purpose():
            os._exit(3)
        """
    )
    incidents = distributed.run(
        "-n", "1",
        # Both tests have to land on one worker, so the dump the first leaves
        # is on the file the second's death is read from. And the worker must
        # not be replaced: xdist reschedules the crashed test onto a fresh
        # worker, which exits on purpose too, and a second death is then
        # correct rather than a bug - but it is not what this is measuring.
        "--max-worker-restart=0",
        "-o", "failure_slow_test_seconds=2",
        "-o", "failure_packages=victim_slow",
        "test_slow_then_exit.py",
        timeout=180,
    )

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "SELF_EXIT"
    assert death.test_in_flight == "test_slow_then_exit.py::test_leaves_on_purpose"
    # Nothing to blame, so nobody is blamed - and above all not the package
    # whose only involvement was being slow once.
    assert death.blamed_frame is None
    assert death.owner != "product"
    assert death.severity != "critical"


@posix_only
def test_a_probe_stack_left_behind_is_dated_and_not_called_a_crash(distributed):
    """A worker diagnosed as stalled is asked for a stack, and that answer is
    written to the crash file. If the worker is then killed, that stack is the
    only one on file - older than the death, and not written by it.

    Saying "the worker wrote a stack before dying" of it is true only in the
    most useless sense, so the report says when it was written instead.
    """
    distributed.pytester.makepyfile(
        test_hang_then_die="""
        import os
        import signal
        import threading

        never_set = threading.Event()


        def test_filler():
            assert True


        def test_wedges_then_is_killed():
            never_set.wait(14)
            os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_stall_seconds=6",
        "-o", "failure_heartbeat_interval=2",
        # High, so nothing the watchdog wrote can be mistaken for the probe.
        "-o", "failure_slow_test_seconds=600",
        "test_hang_then_die.py",
        timeout=180,
    )

    death = distributed.only(incidents, "worker_death")
    # SELF_KILLED where the kernel's signal tracepoint could be watched (root,
    # or a sudo this run may spend) and saw the worker signal itself.
    assert death.verdict in ("SIGKILLED", "SELF_KILLED")
    assert death.crash_stack, "the probe answered, so there is a stack on file"
    assert death.crash_stack_age_seconds is not None
    assert any(
        "written by a process that went on running" in line and "before this report" in line
        for line in death.evidence
    ), death.evidence


@posix_only
def test_a_real_crash_outranks_a_probe_stack_taken_earlier(distributed):
    """Both end up in the same file, the probe first. The dump that describes
    the death is the last one, and it is the one that has to be blamed."""
    distributed.pytester.makepyfile(
        victim_slow="""
        import ctypes
        import threading

        never_set = threading.Event()


        def wait_then_crash():
            never_set.wait(14)
            ctypes.string_at(1)
        """
    )
    distributed.pytester.makepyfile(
        test_c="""
        import victim_slow


        def test_filler():
            assert True


        def test_stalls_then_segfaults():
            victim_slow.wait_then_crash()
        """
    )
    incidents = distributed.run(
        "-n", "2",
        "-o", "failure_stall_seconds=6",
        "-o", "failure_heartbeat_interval=2",
        "-o", "failure_slow_test_seconds=600",
        "-o", "failure_packages=victim_slow",
        "test_c.py",
        timeout=180,
    )

    death = distributed.only(incidents, "worker_death")
    assert death.verdict == "NATIVE_CRASH"
    assert death.crash_stack[0].startswith("Fatal Python error")
    assert death.blamed_frame is not None
    assert death.blamed_frame.function == "wait_then_crash"
    assert any("wrote a stack as it died" in line for line in death.evidence)


def test_a_worker_that_dumped_nothing_is_not_given_a_stack_age(distributed):
    """The crash file is created empty when the worker starts, so its mtime
    answers whether or not anything was ever written to it. An age attached to
    no stack reads as a stack the reader then cannot find."""
    distributed.pytester.makepyfile(
        test_crash=crashing_test("victim.hard_exit(3)")
    )
    incidents = distributed.run("-n", "2", "test_crash.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.crash_stack == []
    assert death.crash_stack_age_seconds is None


BETWEEN_TESTS_CONFTEST = INNER_CONFTEST + '''
#: Set from pytest_configure because logfinish is not handed the config. It is
#: config, not PYTEST_XDIST_WORKER, that answers for *this* run: the variable
#: is inherited by any child process, so with this suite itself under -n the
#: controller below read the outer worker's id and exited on it.
in_a_worker = False


def pytest_configure(config):
    global in_a_worker
    in_a_worker = hasattr(config, "workerinput")


def pytest_runtest_logfinish(nodeid):
    """The gap between two tests: teardown has returned and the next test has
    not started, so nothing is in flight.

    Only in the worker. xdist relays logfinish to the controller as well, and
    a controller that exits here takes the whole run with it.
    """
    if nodeid.endswith("::test_finishes") and in_a_worker:
        import victim

        victim.hard_exit(9)
'''


def test_a_death_between_tests_is_not_blamed_on_the_test_that_passed(distributed):
    """The state slot used to keep the last node id after teardown, so a worker
    that died in the gap between two tests was reported as having died *in* the
    one that had already passed - and the incident was attributed to whoever
    owns it, with a severity and a name on it."""
    distributed.pytester.makeconftest(BETWEEN_TESTS_CONFTEST)
    distributed.pytester.makepyfile(
        test_gap="""
        def test_first():
            assert True


        def test_finishes():
            assert True


        def test_never_runs():
            assert True
        """
    )
    incidents = distributed.run("-n", "1", "test_gap.py", timeout=180)

    death = distributed.only(incidents, "worker_death")
    assert death.exit_status == 9
    assert death.test_in_flight is None, "no test was running when it died"
    assert death.last_test == "test_gap.py::test_finishes"
    assert death.phase is None
    assert death.tests_started == death.tests_finished == 2
    assert "between tests, after finishing 2 (the last was test_gap.py::test_finishes)" in str(death).splitlines()[0]
    # The test's clock is cleared with the test. Left running it measures the
    # gap since a test that already passed, and the controller matches that
    # against the run's timeouts - so an idle worker's exit is reported as
    # TIMED_OUT, against a test that was never killed.
    assert death.test_seconds is None and death.phase_seconds is None
    assert death.matched_timeout is None and death.verdict != "POSSIBLE_TIMEOUT"
    # The lead is still offered - it is the best one there is - but it says
    # which kind of guess it is rather than claiming the test was running.
    assert "nothing was running when it died" in str(death)
    if death.suspect_basis:
        assert "last test this worker finished" in death.suspect_basis
