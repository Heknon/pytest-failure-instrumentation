"""The platform probes, called directly.

Most of these are shadowed in normal use: psutil answers before psapi does,
and execnet's Popen object answers before waitid does. Calling them directly is
the only way the fallback paths are executed at all - and the fallbacks are
precisely what a customer's machine will be running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pytest_failure_instrumentation import probes
from pytest_failure_instrumentation.probes import memory, process

# macOS does not expose os.waitid at all, so the plugin falls back to the
# Popen object there - which is why the capability record reports the
# mechanism rather than assuming one.
has_waitid = pytest.mark.skipif(
    not hasattr(os, "waitid"), reason="no os.waitid on this platform"
)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

#: A number no process in this run's tree has. Not pid 1, which is init on
#: POSIX and nothing at all on Windows - the ids there start at 0 for Idle -
#: so a test written around it would pass for the wrong reason on one of them.
DEFINITELY_NOT_A_PARENT = 999999


def child(*code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "; ".join(code)])


def wait_for(read, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if result is not None:
            return result
        time.sleep(0.05)
    return None


# -- what this machine can measure ---------------------------------------


def test_windows_description_reads_os_once_without_querying_processor(monkeypatch):
    from pytest_failure_instrumentation.probes import platform_flags

    calls = []

    def os_version():
        calls.append("os")
        return SimpleNamespace(platform_version=(10, 0, 26100), product_type=3, service_pack="")

    def unused_processor_query():
        pytest.fail("Windows OS description must not query WMI")

    monkeypatch.setattr(platform_flags, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_flags.sys, "getwindowsversion", os_version, raising=False)
    monkeypatch.setattr(platform_flags.platform, "win32_ver", unused_processor_query)
    monkeypatch.setattr(platform_flags.platform, "platform", unused_processor_query)
    platform_flags.platform_description.cache_clear()
    try:
        for _ in range(3):
            assert platform_flags.platform_description() == "Windows-Server-10.0.26100"
        assert calls == ["os"]
    finally:
        platform_flags.platform_description.cache_clear()


def test_resident_memory_is_measurable_on_every_supported_platform():
    """A missing figure is reported as unmeasurable rather than as fine, so an
    always-unavailable probe would be honest and useless at the same time."""
    value, source = memory.resident_megabytes()
    assert value is not None, source
    assert value > 0
    assert source != "unavailable"


def test_capabilities_names_the_mechanism_rather_than_claiming_a_capability():
    capabilities = probes.capabilities()
    assert capabilities["system"] and capabilities["python"]
    assert capabilities["resident_memory"] != "unavailable"
    assert capabilities["exit_status"] in {"waitid", "windows", "popen-only"}


@windows_only
def test_psapi_answers_without_psutil():
    """The psapi fallback is what every Windows machine without psutil uses,
    and psutil answering first is what kept it from ever being exercised."""
    assert (memory._windows_working_set() or 0) > 0


# -- exit status ---------------------------------------------------------


@has_waitid
def test_waitid_reads_a_status_without_consuming_it():
    """The whole reason for WNOWAIT: whoever owns the process still gets to
    reap it, so looking at a worker cannot break execnet's own cleanup."""
    popen = child("import sys", "sys.exit(7)")
    status = wait_for(lambda: process._waitid_status(popen.pid, 1.0))
    assert status == (7, "exited", "waitid")
    # Still reapable afterwards, and with the same answer.
    assert popen.wait(timeout=10) == 7


@has_waitid
def test_waitid_reports_a_kill_as_a_negative_status():
    popen = child("import time", "time.sleep(30)")
    popen.send_signal(signal.SIGKILL)
    status = wait_for(lambda: process._waitid_status(popen.pid, 1.0))
    assert status == (-int(signal.SIGKILL), "killed", "waitid")
    assert popen.wait(timeout=10) == -int(signal.SIGKILL)


@windows_only
def test_getexitcodeprocess_answers_from_a_handle(monkeypatch):
    """Windows lets any handle you can open answer, which is what makes the
    status readable without being the parent. psutil answers first in normal
    use, so it is made to decline here to reach the call underneath."""

    def declines(pid):
        raise OSError("psutil is not answering for this test")

    monkeypatch.setattr(process, "psutil", SimpleNamespace(Process=declines))
    popen = child("import sys", "sys.exit(7)")
    popen.wait(timeout=10)
    assert process._windows_exit_status(popen.pid) == (7, "exited", "GetExitCodeProcess")


def test_the_windows_path_survives_psutil_declining_to_answer(monkeypatch):
    """Runs everywhere, on purpose. The test above only proves the fallback
    on Windows, so a rename of the psutil import went unnoticed here until a
    Windows runner reached it. This one pins the same patch point on every
    platform: off Windows the ctypes call cannot load, and the contract is
    that it yields nothing rather than raising or claiming psutil answered."""

    def declines(pid):
        raise OSError("psutil is not answering for this test")

    monkeypatch.setattr(process, "psutil", SimpleNamespace(Process=declines))
    status = process._windows_exit_status(os.getpid())
    assert status is None or status[2] == "GetExitCodeProcess"


def test_a_process_that_was_never_ours_yields_no_status_rather_than_a_guess():
    assert probes.exit_status(None, None) == (None, None, "unavailable")


@windows_only
def test_an_ntstatus_is_normalised_to_the_unsigned_form_it_is_documented_as():
    """0xC000013A arrives signed or unsigned depending on who answered, and a
    negative status means "killed by signal N" everywhere downstream."""
    assert process.unsigned_on_windows(-1073741510) == 0xC000013A
    assert process.unsigned_on_windows(-1073741819) == 0xC0000005
    assert process.unsigned_on_windows(3) == 3


def test_a_posix_signal_status_is_left_alone():
    if sys.platform == "win32":
        pytest.skip("negative statuses are signals only on POSIX")
    assert process.unsigned_on_windows(-9) == -9


# -- the package as a dependency -----------------------------------------


def test_the_package_ships_its_types():
    """PEP 561. The typed payload is the product, and without this marker a
    consumer writing `incident: WorkerDeathIncident` against registry.parse()
    gets Any - which is the opposite of what a discriminated union is for."""
    import pytest_failure_instrumentation

    root = Path(pytest_failure_instrumentation.__file__).parent
    assert (root / "py.typed").is_file()


# -- liveness, which is a different mechanism per platform ----------------


def test_windows_liveness_never_goes_through_os_kill(monkeypatch):
    """``os.kill(pid, 0)`` is a POSIX question and a Windows *action*.

    There, ``os.kill`` sends a console event for CTRL_C_EVENT and
    CTRL_BREAK_EVENT and calls TerminateProcess for every other value -
    including zero. A liveness check written the POSIX way would kill each
    worker it inspected, and the live view inspects every worker on every
    request. This test runs on POSIX too, because that is where the mistake
    gets written.
    """
    from pytest_failure_instrumentation.probes import process as process_probe

    killed = []
    asked = []

    class FakePsutil:
        """Stands in for psutil so that *its* POSIX implementation - which
        legitimately uses os.kill on this machine - cannot be mistaken for
        ours. On Windows psutil takes an entirely different path."""

        @staticmethod
        def pid_exists(pid):
            asked.append(pid)
            return True

    monkeypatch.setattr(process_probe, "IS_WINDOWS", True)
    monkeypatch.setattr(process_probe, "psutil", FakePsutil)
    monkeypatch.setattr(process_probe.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    process_probe.is_running(os.getpid())

    assert killed == [], "the Windows path reached os.kill, which terminates there"
    assert asked == [os.getpid()], "the Windows path asked nothing at all"


def test_windows_liveness_asks_psutil(monkeypatch):
    from pytest_failure_instrumentation.probes import process as process_probe

    monkeypatch.setattr(process_probe, "IS_WINDOWS", True)
    asked = []

    class FakePsutil:
        @staticmethod
        def pid_exists(pid):
            asked.append(pid)
            return False

    monkeypatch.setattr(process_probe, "psutil", FakePsutil)
    assert process_probe.is_running(4321) is False
    assert asked == [4321]


def test_liveness_errs_towards_alive_when_it_cannot_tell(monkeypatch):
    """A wrong "it died" deletes evidence and reports a working worker as
    gone. A wrong "still there" costs a stale row, so that is the way to be
    wrong when psutil itself refuses to answer."""
    from pytest_failure_instrumentation.probes import process as process_probe

    class Broken:
        @staticmethod
        def pid_exists(pid):
            raise RuntimeError("psutil is unhappy")

    monkeypatch.setattr(process_probe, "IS_WINDOWS", True)
    monkeypatch.setattr(process_probe, "psutil", Broken)
    assert process_probe.is_running(4321) is True


def test_a_permission_error_means_the_process_exists(monkeypatch):
    """EPERM is the kernel saying there is something there that is not ours to
    signal, which is an answer rather than a failure."""
    from pytest_failure_instrumentation.probes import process as process_probe

    monkeypatch.setattr(process_probe, "IS_WINDOWS", False)

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(process_probe.os, "kill", denied)
    assert process_probe.is_running(4321) is True


# -- letting the reader read us -------------------------------------------


def test_asking_to_be_traceable_never_raises_and_answers_a_bool():
    """The call is a no-op wherever it is not needed - a kernel without Yama
    answers EINVAL, and every non-Linux platform never makes it. What must not
    happen is an exception on a worker's startup path, which would cost the run
    rather than a stack."""
    from pytest_failure_instrumentation.probes import tracing

    granted = tracing.permit_tracing("parent")
    assert isinstance(granted, bool)
    if sys.platform != "linux":
        assert granted is False, "nothing outside Linux has a tracer exception to grant"


def test_the_prctl_option_is_yamas_own_number():
    """0x59616d61 spells "Yama". Written down rather than imported because
    Python exposes no constant for it, and a wrong number here would fail
    silently - prctl returns EINVAL and the worker simply stays unreadable."""
    from pytest_failure_instrumentation.probes import tracing

    assert tracing.PR_SET_PTRACER == 0x59616D61
    assert bytes.fromhex(f"{tracing.PR_SET_PTRACER:x}") == b"Yama"
# -- whose process is that pid ---------------------------------------------
#
# is_own_child errs the opposite way from is_running above, and the tests hold
# that: a wrong "yes" signals a stranger's process, a wrong "no" only costs a
# stalled worker its stack. psutil is a hard dependency here, so there is no
# machine that cannot answer and no third value to handle.


def test_a_live_child_is_recognised_as_one():
    """What licenses the stack probe. SIGUSR1's default disposition is to
    terminate, and the pid it is aimed at was read back out of a file - so the
    question "is that still our worker" is the difference between a bad report
    and somebody else's process being killed."""
    process_handle = child("import time", "time.sleep(30)")
    try:
        assert probes.is_own_child(process_handle.pid) is True
    finally:
        process_handle.kill()
        process_handle.wait()


def test_a_process_that_is_not_ours_is_not_mistaken_for_a_worker():
    """This process is alive and is certainly not its own child.

    Not pid 1, which is init on POSIX and *nothing at all* on Windows - there
    the ids start at 0 for Idle and 4 for System, so a test written around pid
    1 asks about a process that does not exist and passes for the wrong
    reason.
    """
    assert probes.is_own_child(os.getpid()) is False


def test_a_pid_that_cannot_exist_is_not_ours():
    assert probes.is_own_child(0) is False
    assert probes.is_own_child(-1) is False


def test_a_child_that_has_gone_is_no_longer_ours():
    """The case the whole check exists for: the pid outlives the process, and
    the number is handed on to somebody else."""
    process_handle = child("import sys", "sys.exit(0)")
    process_handle.wait()  # reaped, so this asks about a pid with no process
    assert probes.is_own_child(process_handle.pid) is False


def test_the_two_liveness_questions_disagree_about_a_stranger():
    """is_running says this process is there; is_own_child says it is not ours.

    Both are right, and the live view depends on them being different: one
    decides whether to keep reporting a worker, the other whether it is safe to
    signal it. Asked about *this* process rather than pid 1, which does not
    exist on Windows - is_running answered False there and the test failed
    having proved nothing about either question.
    """
    assert probes.is_running(os.getpid()) is True
    assert probes.is_own_child(os.getpid()) is False


def test_a_libc_without_mallinfo2_is_looked_up_once_not_on_every_sample(monkeypatch):
    """find_library spawns a subprocess. The profiler reads the heap twenty-five
    times a second, so a libc that has no mallinfo2 - glibc before 2.33, musl -
    must be asked once and remembered, not paid for on every tick."""
    import ctypes.util

    calls = []
    monkeypatch.setattr(memory, "_libc", None)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: calls.append(name) or None)

    assert memory._mallinfo2() is None
    assert memory._mallinfo2() is None
    assert memory._mallinfo2() is None
    assert calls == ["c"]
    assert memory._libc is False


def test_malloc_info_is_read_per_arena_and_the_process_totals_are_not_double_counted():
    text = """<malloc version="1">
<heap nr="0">
<sizes>
  <size from="49" to="49" total="49" count="1"/>
</sizes>
<total type="fast" count="2" size="1048576"/>
<total type="rest" count="4" size="3145728"/>
<system type="current" size="8388608"/>
<system type="max" size="8388608"/>
<aspace type="total" size="8388608"/>
<aspace type="mprotect" size="8388608"/>
</heap>
<heap nr="1">
<sizes>
  <unsorted from="657" to="1541489" total="1542146" count="2"/>
</sizes>
<total type="fast" count="0" size="0"/>
<total type="rest" count="3" size="62914560"/>
<system type="current" size="67108864"/>
<system type="max" size="67108864"/>
<aspace type="total" size="67108864"/>
<aspace type="mprotect" size="67108864"/>
<aspace type="subheaps" size="1"/>
</heap>
<total type="fast" count="2" size="1048576"/>
<total type="rest" count="7" size="66060288"/>
<total type="mmap" count="3" size="610304"/>
<system type="current" size="75497472"/>
<system type="max" size="75497472"/>
<aspace type="total" size="75497472"/>
<aspace type="mprotect" size="75497472"/>
</malloc>
"""
    figures = memory.parse_malloc_info(text)
    assert figures == {"arenas": 2, "free_mb": 64, "main_free_mb": 4, "mapped_mb": 72}
    assert memory.parse_malloc_info("<malloc version=\"1\">\n</malloc>\n") is None


@pytest.mark.skipif(sys.platform != "linux", reason="glibc only")
def test_the_allocator_figures_are_read_from_this_process_where_glibc_answers():
    figures, source = probes.allocator_figures()
    if source == "unavailable":
        pytest.skip("no malloc_info in this libc")
    assert figures is not None
    assert figures["arenas"] >= 1
    assert set(figures) == {"arenas", "free_mb", "main_free_mb", "mapped_mb", "trim_mb"}
    assert figures["main_free_mb"] <= figures["free_mb"]


def test_windows_rss_falls_back_if_native_read_fails(monkeypatch):
    monkeypatch.setattr(memory, "IS_LINUX", False)
    monkeypatch.setattr(memory, "IS_WINDOWS", True)
    monkeypatch.setattr(memory, "_windows_working_set", lambda: None)
    monkeypatch.setattr(memory, "psutil", SimpleNamespace(
        Process=lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=10485760))
    ))
    assert memory.resident_megabytes() == (10, "psutil")


# -- the run's process tree -----------------------------------------------
#
# What a session is made of is wider than what it wrote down: the controller
# runs no tests, this package starts helpers beside the workers, and under
# each worker is whatever the tests started. The live view decides what it may
# read from these two walks, so a wrong answer here is either a stalled
# process nobody can look at or a stranger's process served as the run's.


SPAWNS_A_CHILD = """
import subprocess, sys, time

inner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(inner.pid, flush=True)
time.sleep(300)
"""


@pytest.fixture
def a_child_with_a_child():
    """``(pid, child_pid)`` for a process this one started, cleaned up after."""
    started = subprocess.Popen(
        [sys.executable, "-c", SPAWNS_A_CHILD], stdout=subprocess.PIPE, text=True
    )
    try:
        yield started.pid, int(started.stdout.readline().strip())
    finally:
        started.kill()
        started.wait(timeout=10)


def test_the_ancestry_of_a_process_starts_at_its_parent(a_child_with_a_child):
    spawned, deeper = a_child_with_a_child

    assert list(process.ancestry(deeper))[:2] == [spawned, os.getpid()]
    assert list(process.ancestry(spawned))[0] == os.getpid()


def test_an_ancestry_that_cannot_be_read_is_empty_rather_than_an_error():
    """These are the processes of a run in trouble: one that exits mid-walk is
    the ordinary case, and a shorter chain is an answer where a raised
    exception is a live view that stops working."""
    assert list(process.ancestry(0)) == []
    assert list(process.ancestry(-1)) == []


def test_an_ancestry_is_followed_no_further_than_it_is_asked_to_be(a_child_with_a_child):
    """The chain is read hop by hop out of a live table, so it has a ceiling."""
    _spawned, deeper = a_child_with_a_child

    assert len(list(process.ancestry(deeper, limit=1))) == 1
    assert list(process.ancestry(deeper, limit=0)) == []


def test_an_ancestry_holds_what_a_process_came_from_and_nothing_else(a_child_with_a_child):
    """What a caller asking "is this one of mine" matches against: one chain of
    parents rather than a walk of the machine."""
    spawned, deeper = a_child_with_a_child

    assert os.getpid() in list(process.ancestry(deeper))
    assert os.getpid() in list(process.ancestry(spawned))
    # A process is not above itself, and neither is anything it never came
    # from - which is what makes a stranger's pid a refusal rather than a
    # match somewhere up the chain.
    assert os.getpid() not in list(process.ancestry(os.getpid()))
    assert DEFINITELY_NOT_A_PARENT not in list(process.ancestry(deeper))


def test_every_process_under_a_root_is_found_and_labelled(a_child_with_a_child):
    spawned, deeper = a_child_with_a_child

    found, truncated = process.descendants({os.getpid(): "controller"})
    assert not truncated
    rows = {row["pid"]: row for row in found}
    assert spawned in rows and deeper in rows
    assert rows[deeper]["ppid"] == spawned
    assert rows[spawned]["under"] == rows[deeper]["under"] == "controller"
    assert rows[spawned]["name"] and rows[spawned]["started_at"] > 0
    # The name the kernel holds, never the command line: that is where a
    # program's arguments are, and a token passed as a flag with them.
    assert set(rows[spawned]) == {"pid", "ppid", "name", "started_at", "under"}


def test_a_process_is_labelled_by_the_nearest_root_above_it(a_child_with_a_child):
    """Roots are seeded before the walk starts, so reaching one from above
    never relabels it - which is what puts a worker's own children under the
    worker rather than under the controller they also descend from."""
    spawned, deeper = a_child_with_a_child

    found, _truncated = process.descendants({os.getpid(): "controller", spawned: "gw0"})
    rows = {row["pid"]: row for row in found}
    assert rows[deeper]["under"] == "gw0"
    # And a root is not a row of its own walk: it is already described by
    # whatever named it a root.
    assert spawned not in rows


def test_a_walk_stops_at_its_bound_and_says_that_it_did(a_child_with_a_child):
    """A reply about a live machine must have a size that does not depend on
    what the machine is doing, and a short list has to be distinguishable from
    a complete one."""
    found, truncated = process.descendants({os.getpid(): "controller"}, limit=1)
    assert len(found) == 1 and truncated is True

    found, truncated = process.descendants({os.getpid(): "controller"}, limit=0)
    assert found == [] and truncated is True


def test_a_root_with_nothing_under_it_is_no_rows_rather_than_a_failure():
    found, truncated = process.descendants({DEFINITELY_NOT_A_PARENT: "gone"})
    assert found == [] and truncated is False
    assert process.descendants({}) == ([], False)


def test_a_recorded_creation_time_is_checked_only_when_both_ends_know_one():
    """The weaker half of same_process, and weaker on purpose: this is asked
    about a live process a caller is about to read, so False has to mean
    "reused" and nothing else."""
    assert process.creation_time_agrees(os.getpid(), process.creation_time(os.getpid()))
    # A record from before the field existed, or a machine whose procfs
    # belongs to another namespace, leaves the claim unchecked rather than
    # refused - the check catches reuse where reuse can be proven.
    assert process.creation_time_agrees(os.getpid(), None)
    assert process.creation_time_agrees(os.getpid(), "not a time")
    if process.creation_time(os.getpid()) is not None:
        assert not process.creation_time_agrees(os.getpid(), 1.0)
