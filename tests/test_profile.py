"""The profiler against real runs.

Each scenario is a suite written the way the problem is usually written the
first time, run under ``--failure-profile`` in a subprocess through the shared runner,
and read back as the incidents it raised. The thresholds are lowered so that
a scenario can prove its point in a second or two rather than a minute.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest

from pytest_failure_instrumentation import probes
from pytest_failure_instrumentation.profile.sampler import ThreadClock

from .conftest import RERUN_CONFTEST, Runner, needs_xdist

#: A background thread can only be told from the test's thread where the CPU
#: is read per thread; elsewhere the whole process is charged to the test.
needs_thread_cpu = pytest.mark.skipif(
    not ThreadClock().available, reason="no per-thread CPU clock on this platform"
)

def a_core_of_its_own(wanted: float = 0.7, over: float = 0.15) -> None:
    """Skip unless this machine will give one busy thread most of a core.

    A burst is defined as a *rate* - `failure_profile_burst_cores` of a core,
    sustained - so a scenario cannot produce one on a machine that is already
    saturated by something else, however much CPU it is given to burn. That is
    the profiler working: the same run raises `CONTENDED` and says the load was
    not this run's. It is measured here rather than assumed, and just before
    the run rather than at collection, because what a CI runner will give
    varies minute to minute.
    """
    def probe() -> float:
        started, cpu = time.perf_counter(), time.process_time()
        deadline = started + over
        while time.perf_counter() < deadline:
            sum(range(2000))
        return (time.process_time() - cpu) / max(1e-9, time.perf_counter() - started)

    # The best of three short probes rather than one longer one: a single dip
    # - another worker of this suite starting a subprocess, say - would skip a
    # test that the machine can in fact run, and a skip that should not have
    # happened is the failure mode to avoid here.
    cores = max(probe() for _ in range(3))
    if cores < wanted:
        pytest.skip(
            f"this machine gave one busy thread {cores:.2f} cores just now, and a "
            f"burst is {wanted} of one sustained: the scenario cannot produce one here"
        )

PROFILE_INI = """
[pytest]
failure_packages = victim, product
failure_profile_cpu_share = 5
failure_profile_cpu_floor_seconds = 0.25
failure_profile_retained_mb = 40
"""

PRODUCT_MODULE = '''
"""The product under test: one hot loop, one library call, one poller."""
import hashlib
import json
import threading
import time

#: This thread's own CPU where the platform has one, the process's otherwise.
#: Only the poller reads it, and it is the only thread doing any work.
spent = getattr(time, "thread_time", time.process_time)


def compare_pixels(image1, image2, width, height):
    """A per-pixel comparison in Python: the loop nobody notices in a unit test."""
    differing = 0
    for x in range(width):
        for y in range(height):
            offset = (y * width + x) * 3
            if image1[offset:offset + 3] != image2[offset:offset + 3]:
                differing += 1
    return differing


def render(document):
    """The cost is in the library it calls, not in its own line."""
    return json.dumps(document, indent=2, sort_keys=True)


class Poller:
    """A background thread that polls, and can be asked how much it has done.

    The CPU it burns is what a test that watches it is really waiting for, so
    that is what it counts and what `worked` waits on. Sleeping for a stretch
    of wall time instead made the scenario worth whatever share of a core the
    machine happened to have going: on a saturated CI runner the poller got a
    tenth of one, finished the run under the threshold, and the finding this
    is about was never raised.
    """

    def __init__(self):
        self.stop = threading.Event()
        self.cpu_seconds = 0.0
        self.thread = threading.Thread(target=self.run, name="status-poller", daemon=True)

    #: Polls a second, and hashes per poll. A millisecond between polls is
    #: what this said first, and on Windows a millisecond is a request for
    #: one scheduler tick - 15.6 ms - so the thread polled sixty times a
    #: second there against two thousand here and burnt a fortieth of the CPU
    #: it burnt on Linux. Both numbers are above the coarsest tick any of
    #: these platforms has, so the work per second is the same on all of them.
    INTERVAL = 0.02
    PER_POLL = 20

    def run(self):
        payload = b"x" * 400_000
        while not self.stop.wait(self.INTERVAL):
            before = spent()
            for _ in range(self.PER_POLL):
                hashlib.sha256(payload).hexdigest()
            self.cpu_seconds += spent() - before

    def worked(self, seconds, timeout=90.0):
        """Block until this thread has burnt `seconds` more CPU than it had."""
        target = self.cpu_seconds + seconds
        deadline = time.monotonic() + timeout
        while self.cpu_seconds < target and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.cpu_seconds >= target
'''

LOADER_MODULE = '''

def load_everything(records):
    # Reads the whole export before doing anything with it.
    payload = b"".join(b"record %d\\n" % index * 40 for index in range(records))
    return len(payload.decode("ascii").splitlines())
'''


#: The lines of ``compare_pixels``'s inner loop, worked out from the module
#: rather than written down: a line added to the header above moved them, and
#: three tests that named them by number all failed at once for a reason that
#: had nothing to do with what they check.
PIXEL_LOOP_LINES = tuple(
    number
    for number, text in enumerate(PRODUCT_MODULE.splitlines(), 1)
    if text.strip() in (
        "for y in range(height):",
        "offset = (y * width + x) * 3",
        "if image1[offset:offset + 3] != image2[offset:offset + 3]:",
        "differing += 1",
    )
)


def profiled(runner: Runner, *arguments: str, timeout: float = 120.0) -> list[Any]:
    runner.pytester.makeini(PROFILE_INI)
    (runner.pytester.path / "product.py").write_text(PRODUCT_MODULE, encoding="utf-8")
    return runner.run("--failure-profile", "-p", "no:cacheprovider", *arguments, timeout=timeout)


def hotspots(incidents: list[Any]) -> list[Any]:
    return Runner.of_kind(incidents, "cpu_hotspot")


def memory(incidents: list[Any]) -> list[Any]:
    return Runner.of_kind(incidents, "memory_profile")


def named(incidents: list[Any], function: str) -> Any:
    matching = [
        incident
        for incident in hotspots(incidents)
        if incident.blamed_frame is not None and incident.blamed_frame.function == function
    ]
    assert matching, f"no hotspot blamed on {function}: {[str(i) for i in incidents]}"
    return matching[0]


class TestCpu:
    def test_a_python_loop_in_the_product_is_blamed_on_the_product(self, runner: Runner) -> None:
        runner.pytester.makepyfile(
            test_screens="""
            from product import compare_pixels

            WIDTH, HEIGHT = 1280, 720
            A = bytes(WIDTH * HEIGHT * 3)
            B = bytes(WIDTH * HEIGHT * 3)

            def test_screen_settles():
                for _ in range(8):
                    compare_pixels(A, B, WIDTH, HEIGHT)
            """
        )
        incidents = profiled(runner)

        incident = named(incidents, "compare_pixels")
        assert incident.verdict == "PYTHON_CODE"
        assert incident.owner == "product"
        assert incident.severity == "informational"
        assert incident.blamed_frame.file.endswith("product.py")
        # The line is the comparison inside the loop, not the def line and
        # not 0: on 3.11 the frame reports no line at the loop's back edge,
        # and the sampler resolves it from the instruction offset instead.
        assert incident.hottest_lines[0].line in PIXEL_LOOP_LINES
        assert incident.self_share_percent > 90
        assert incident.share_percent > 50
        assert incident.tests == ["test_screens.py::test_screen_settles"]
        assert incident.raw_stack()[0].startswith('  File "')

    def test_time_under_a_library_is_charged_to_the_caller_and_names_the_library(
        self, runner: Runner
    ) -> None:
        runner.pytester.makepyfile(
            test_reports="""
            from product import render

            def test_report():
                document = {"rows": [{"id": i, "name": f"row-{i}", "value": i * 1.5} for i in range(200000)]}
                for _ in range(6):
                    render(document)
            """
        )
        incidents = profiled(runner)

        incident = named(incidents, "render")
        assert incident.owner == "product"
        if incident.verdict == "PYTHON_CODE":
            # From 3.12 `json.dumps` encodes with `indent` in C, so there is no
            # `_iterencode` frame under `render` and the cost is charged to
            # `render`'s own line - which is what the rule says to do with a C
            # call that leaves no frame, and is the same answer to "whose code
            # is this". The library below is what cannot be named there.
            assert incident.below is None
            return
        assert incident.verdict == "LIBRARY_CALL"
        assert incident.below is not None
        # Windows writes the path with backslashes, and the frame carries it
        # as the interpreter reported it.
        assert incident.below.file.replace("\\", "/").endswith("json/encoder.py")
        assert incident.below.owner == "runtime"
        assert incident.self_share_percent < 50

    @needs_thread_cpu
    def test_a_background_thread_is_a_finding_whatever_test_is_running(self, runner: Runner) -> None:
        runner.pytester.makepyfile(
            test_polling="""
            import time
            import pytest
            from product import Poller

            @pytest.fixture(scope="session")
            def poller():
                poller = Poller()
                poller.thread.start()
                yield poller
                poller.stop.set()

            # Each test waits for the poller to do a measured amount of work
            # rather than for a stretch of the clock: the finding is about
            # the CPU it used, and a busy machine must not be able to shrink
            # that without the test noticing.
            def test_one(poller):
                assert poller.worked(0.6)

            def test_two(poller):
                assert poller.worked(0.6)
            """
        )
        incidents = profiled(runner)

        incident = named(incidents, "Poller.run")
        assert incident.verdict == "BACKGROUND_THREAD"
        assert incident.thread == "status-poller"
        assert incident.background_share_percent > 90
        assert incident.owner == "product"
        # The two tests, and the gap between them: the poller runs there too,
        # and the background record is one more window it was charged in.
        assert incident.test_count >= 2

    def test_a_quiet_run_raises_nothing_and_still_writes_records(self, runner: Runner) -> None:
        runner.pytester.makepyfile(
            test_quiet="""
            import json

            def test_small():
                assert json.loads(json.dumps({"a": 1})) == {"a": 1}
            """
        )
        incidents = profiled(runner)

        assert hotspots(incidents) == []
        assert memory(incidents) == []
        assert Runner.only(incidents, "run_summary").raised == 0
        records = list((runner.pytester.path / ".pytest-failures").glob("run-*/*.profile.jsonl"))
        assert len(records) == 1
        kinds = [json.loads(line)["record"] for line in records[0].read_text().splitlines()]
        assert kinds == ["test", "background"]

    def test_the_switch_alone_turns_the_plugin_on(self, runner: Runner) -> None:
        runner.pytester.makepyfile(test_quiet="def test_ok(): pass")
        runner.pytester.makeini(PROFILE_INI)
        (runner.pytester.path / "product.py").write_text(PRODUCT_MODULE, encoding="utf-8")

        # Deliberately without --failure-instrumentation: --failure-profile is a request
        # for the plugin that takes the profile, like --callstack-port.
        result = runner.pytester.runpytest_subprocess("--failure-profile", "-p", "no:cacheprovider")

        result.stdout.fnmatch_lines(["*failure-instrumentation profile*", "*No findings*"])
        assert Runner.of_kind(runner.incidents(), "run_summary")


class TestMemory:
    def test_memory_a_test_keeps_is_retained_and_placed_in_its_phase(self, runner: Runner) -> None:
        # Two modules, because a module fixture is given back in the teardown
        # of the module's last test - which would mask a body that kept the
        # same amount in the same module, and does so honestly: the worker
        # really is no bigger afterwards.
        runner.pytester.makepyfile(
            test_keep_body="""
            KEPT = []

            def test_body_keeps():
                for _ in range(60):
                    KEPT.append(bytearray(1_000_000))
            """,
            test_keep_fixture="""
            import pytest

            @pytest.fixture(scope="module")
            def big_fixture():
                return [bytearray(1_000_000) for _ in range(60)]

            def test_first_use(big_fixture):
                assert len(big_fixture) == 60

            def test_second_use(big_fixture):
                assert len(big_fixture) == 60
            """,
        )
        incidents = profiled(runner)

        by_test = {incident.nodeid: incident for incident in memory(incidents)}
        body = by_test["test_keep_body.py::test_body_keeps"]
        assert body.verdict == "RETAINED_AFTER_TEST"
        assert body.phase == "call"
        assert body.delta_mb >= 40
        # Attributed from the stack that was running while it climbed when one
        # was seen, and from the test's module otherwise.
        assert "customer-code" in (body.owner, body.suspect_owner)
        fixture = by_test["test_keep_fixture.py::test_first_use"]
        assert fixture.verdict == "RETAINED_AFTER_TEST"
        assert fixture.phase == "setup"
        assert "fixture" in " ".join(fixture.evidence)
        # The second use neither kept nor climbed; the last one gives it back.
        assert "test_keep_fixture.py::test_second_use" not in by_test

    def test_memory_freed_on_return_is_a_peak_not_a_leak(self, runner: Runner) -> None:
        runner.pytester.makepyfile(
            test_peak="""
            import time

            def test_peak():
                blob = [bytearray(1_000_000) for _ in range(120)]
                time.sleep(0.8)
                assert len(blob) == 120
            """
        )
        incidents = profiled(runner)

        (incident,) = memory(incidents)
        # RSS can remain elevated after release on every platform. Even
        # mallinfo2 cannot establish liveness across all allocation domains.
        assert incident.verdict in ("TRANSIENT_PEAK", "RETAINED_AFTER_TEST")
        assert incident.nodeid == "test_peak.py::test_peak"
        assert incident.peak_mb >= incident.before_mb + 100

    def test_a_climb_over_the_ceiling_is_blamed_on_the_code_that_climbed(self, runner: Runner) -> None:
        # The ceiling is this test's to choose, so it is set under what the
        # weakest platform measures rather than the scenario grown until it
        # clears a number picked on Linux. `load_everything` builds a big
        # bytes object, decodes it and splits it, and how much of that is
        # resident at once is the interpreter's business: 1450 MB here, 279 MB
        # on the Windows 3.9 cell, which sat under a 300 MB ceiling and got
        # TRANSIENT_PEAK instead. What the test is about - a climb over the
        # ceiling, blamed on the code that climbed - is the same at 150.
        runner.pytester.makeini(PROFILE_INI + "failure_profile_peak_mb = 150\n")
        (runner.pytester.path / "product.py").write_text(PRODUCT_MODULE + LOADER_MODULE, encoding="utf-8")
        runner.pytester.makepyfile(
            test_loading="""
            from product import load_everything

            def test_loads_the_export():
                assert load_everything(450_000) == 18_000_000
            """
        )
        incidents = runner.run("--failure-profile", "-p", "no:cacheprovider", timeout=120.0)

        ceiling = [incident for incident in memory(incidents) if incident.verdict == "PEAK_OVER_CEILING"]
        assert len(ceiling) == 1
        (incident,) = ceiling
        assert incident.nodeid == "test_loading.py::test_loads_the_export"
        assert incident.peak_mb >= 150
        # It climbed to get there rather than starting high. Whether the pages
        # come back to the OS afterwards is the allocator's business and the
        # platform's - see the live-heap row of the platform table - so what is
        # asserted is the climb, not the fall.
        assert incident.peak_mb - incident.before_mb >= 100
        # The code that was running while the memory climbed, owned by the product.
        assert incident.blamed_frame is not None
        assert incident.blamed_frame.function == "load_everything"
        assert incident.owner == "product"
        assert incident.climb_mb > 0
        assert incident.raw_stack()[0].startswith('  File "')
        assert any("increase happened while load_everything (product.py:" in line for line in incident.evidence)

    def test_a_run_of_small_leaks_is_growth(self, runner: Runner) -> None:
        runner.pytester.makepyfile(
            test_leak="""
            import pytest

            LEAKED = []

            @pytest.mark.parametrize("case", range(6))
            def test_leaks(case):
                LEAKED.append(bytearray(12_000_000))
            """
        )
        incidents = profiled(runner)

        growth = [incident for incident in memory(incidents) if incident.verdict == "STEADY_GROWTH"]
        assert len(growth) == 1
        assert growth[0].growth.tests >= 5
        assert 9 <= growth[0].growth.per_test_mb <= 15
        assert any("All of them are cases of test_leak.py::test_leaks." == line for line in growth[0].evidence)
        assert not [incident for incident in memory(incidents) if incident.verdict == "RETAINED_AFTER_TEST"]

    @pytest.mark.skipif(
        probes.allocator_figures()[1] == "unavailable", reason="the arena reading is glibc's malloc_info"
    )
    def test_memory_a_thread_pool_freed_and_the_allocator_kept_is_named_with_the_fix(self, runner: Runner) -> None:
        # Each thread of the pool gets an arena; the payloads are freed and
        # the small index entries between them pin every heap they sit in.
        runner.pytester.makepyfile(
            test_ingest="""
            from concurrent.futures import ThreadPoolExecutor
            import pytest

            INDEX = []

            def parse(seed):
                payloads = []
                for index in range(200):
                    payloads.append(bytearray(60_000 + ((index * seed) % 13) * 5_000))
                    INDEX.append(bytearray(600))
                return len(payloads)

            @pytest.mark.parametrize("batch", range(4))
            def test_ingest(batch):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    assert sum(pool.map(parse, range(1, 9))) == 1600
            """
        )
        incidents = profiled(runner)

        retained = [incident for incident in memory(incidents) if incident.verdict == "ALLOCATOR_RETENTION"]
        assert len(retained) == 1, [str(incident) for incident in memory(incidents)]
        (incident,) = retained
        assert incident.owner == "runtime"
        assert incident.nodeid is None
        assert incident.arenas is not None and incident.arenas > 2
        assert incident.threads is not None and incident.threads >= 5
        assert incident.delta_mb is not None and incident.delta_mb >= 40
        assert any("MALLOC_ARENA_MAX limits how many thread arenas exist" in line for line in incident.evidence)
        assert str(incident).startswith("Memory held by the allocator: worker main has ")
        assert "[memory_profile ALLOCATOR_RETENTION, runtime, informational]" in str(incident).splitlines()[0]
        # Nothing here is called a leak, and nothing is called freed. Both
        # verdicts that would assert the memory *is* one of those - a leak
        # across tests, or a step the allocator kept - stay out of it.
        assert not [
            incident
            for incident in memory(incidents)
            if incident.verdict in ("STEADY_GROWTH", "HEAP_NOT_RETURNED")
        ]
        # A per-test finding may stand beside the worker's now, and where the
        # readings cannot tell live memory from memory the allocator kept it
        # has to say which it cannot tell rather than pick one. This used to
        # assert that no per-test finding was raised at all, which was the
        # inference "small mallinfo2 delta, therefore freed" being read back
        # out of the suite.
        for other in memory(incidents):
            if other.verdict == "ALLOCATOR_RETENTION":
                continue
            assert other.verdict == "RETAINED_AFTER_TEST", str(other)
            assert any(
                "cannot distinguish live allocations from memory retained by the allocator" in line
                for line in other.evidence
            ), str(other)


@needs_xdist
class TestDistributed:
    def test_the_worker_holding_the_fixture_is_named_with_where_it_diverged(
        self, distributed: Runner
    ) -> None:
        distributed.pytester.makepyfile(
            test_heavy="""
            import pytest

            @pytest.fixture(scope="module")
            def big_fixture():
                return [bytearray(1_000_000) for _ in range(150)]

            @pytest.mark.parametrize("case", range(3))
            def test_uses_it(big_fixture, case):
                assert len(big_fixture) == 150
            """,
            test_light="""
            import pytest

            @pytest.mark.parametrize("case", range(3))
            def test_light(case):
                assert case >= 0
            """,
        )
        incidents = profiled(distributed, "-n", "2", "--dist", "loadfile")

        imbalance = [incident for incident in memory(incidents) if incident.verdict == "WORKER_IMBALANCE"]
        assert len(imbalance) == 1
        assert imbalance[0].nodeid == "test_heavy.py::test_uses_it[0]"
        assert set(imbalance[0].worker_rss) == {"gw0", "gw1"}
        assert imbalance[0].peak_mb >= imbalance[0].median_mb * 2
        # And every worker's records were folded in, not only the controller's.
        summary = Runner.only(incidents, "run_summary")
        assert summary.distributed is True

    def test_findings_are_raised_once_on_the_controller(self, distributed: Runner) -> None:
        distributed.pytester.makepyfile(
            test_screens="""
            from product import compare_pixels
            import pytest

            A = bytes(1280 * 720 * 3)

            @pytest.mark.parametrize("case", range(4))
            def test_screen(case):
                for _ in range(3):
                    compare_pixels(A, A, 1280, 720)
            """
        )
        incidents = profiled(distributed, "-n", "2")

        incident = named(incidents, "compare_pixels")
        assert incident.worker == "controller"
        assert incident.test_count == 4
        assert len([i for i in hotspots(incidents) if i.blamed_frame and i.blamed_frame.function == "compare_pixels"]) == 1


class TestReruns:
    def test_a_rerun_is_one_record_and_one_test(self, runner: Runner) -> None:
        """A rerun plugin brackets each attempt with its own logstart and
        logfinish - pytest-rerunfailures does, inside its retry loop - and the
        profile record used to close on logfinish. So a test rerun twice wrote
        three records under one node id, the summary added them up as three
        tests, and a run of two tests was reported as four. The record closes
        at the end of the protocol now, which is once per test however many
        attempts it took, and carries the CPU of all of them.
        """
        runner.pytester.makeconftest(RERUN_CONFTEST)
        runner.pytester.makepyfile(
            test_flaky="""
            import time

            STATE = {"attempts": 0}

            def burn(seconds):
                deadline = time.perf_counter() + seconds
                total = 0
                while time.perf_counter() < deadline:
                    total += sum(range(2000))
                return total

            def test_flaky():
                STATE["attempts"] += 1
                burn(0.4)
                assert STATE["attempts"] > 1

            def test_steady():
                burn(0.4)
            """
        )
        profiled(runner, "test_flaky.py")

        records = [
            json.loads(line)
            for path in (runner.pytester.path / ".pytest-failures").glob("run-*/*.profile.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        tests = [record for record in records if record.get("record") == "test"]
        assert sorted(record["nodeid"] for record in tests) == [
            "test_flaky.py::test_flaky",
            "test_flaky.py::test_steady",
        ]
        # Both attempts' CPU is in the one record, rather than a third of it
        # in each of three: the rerun burnt 0.4s twice against test_steady's
        # once, so it is the heavier of the two by a clear margin.
        by_test = {record["nodeid"]: record for record in tests}
        rerun_cpu = by_test["test_flaky.py::test_flaky"]["cpu_s"]
        steady_cpu = by_test["test_flaky.py::test_steady"]["cpu_s"]
        assert rerun_cpu > steady_cpu * 1.5, (rerun_cpu, steady_cpu)
        assert "Profile: 2 tests" in runner.result.stdout.str()


class TestArtifacts:
    def test_a_flame_graph_is_written_for_every_test_a_finding_names(self, runner: Runner) -> None:
        runner.pytester.makepyfile(
            test_screens="""
            from product import compare_pixels

            A = bytes(1280 * 720 * 3)

            def test_hot():
                for _ in range(8):
                    compare_pixels(A, A, 1280, 720)

            def test_cold():
                assert True
            """
        )
        incidents = profiled(runner)

        named(incidents, "compare_pixels")
        profiles = sorted(path.name for path in (runner.pytester.path / ".pytest-failures").glob("run-*/profiles/*"))
        # Named for the test, with a hash of the full node id so that two
        # names that sanitise alike cannot overwrite each other.
        assert any(re.fullmatch(r"test_screens\.py_test_hot-[0-9a-f]{8}\.speedscope\.json", name) for name in profiles)
        assert any(re.fullmatch(r"background-main-[0-9a-f]{8}\.speedscope\.json", name) for name in profiles)
        assert not any("test_cold" in name for name in profiles)
        document = json.loads(
            next((runner.pytester.path / ".pytest-failures").glob("run-*/profiles/test_screens.py_test_hot-*.speedscope.json")).read_text()
        )
        main = next(profile for profile in document["profiles"] if profile["name"].startswith("MainThread"))
        assert main["unit"] == "nanoseconds"
        assert sum(main["weights"]) > 0
        assert any(frame["name"] == "compare_pixels" for frame in document["shared"]["frames"])


class TestSettings:
    def test_profile_settings_reach_a_worker_and_round_trip(self) -> None:
        from pytest_failure_instrumentation.config import Settings

        settings = Settings(
            profile=True,
            profile_interval=0.05,
            profile_cpu_share=2.5,
            profile_cpu_floor_seconds=0.25,
            profile_retained_mb=64,
            profile_peak_mb=4096,
        )
        copied = Settings.from_payload(settings.as_payload(), worker_count=4)

        assert copied.profile is True
        assert copied.profile_interval == 0.05
        assert copied.profile_cpu_share == 2.5
        assert copied.profile_cpu_floor_seconds == 0.25
        assert copied.profile_retained_mb == 64
        assert copied.profile_peak_mb == 4096

    def test_the_interval_has_a_floor(self) -> None:
        from pytest_failure_instrumentation.config import MIN_PROFILE_INTERVAL, Settings

        assert Settings(profile_interval=0.0).profile_interval == MIN_PROFILE_INTERVAL
        assert Settings(profile_cpu_floor_seconds=-1).profile_cpu_floor_seconds == 0
        assert Settings(profile_retained_mb=0).profile_retained_mb == 1

    def test_the_new_incidents_are_in_the_registry(self) -> None:
        from pytest_failure_instrumentation.incidents.registry import json_schema, parse

        schema = json.dumps(json_schema())
        assert "cpu_hotspot" in schema and "memory_profile" in schema
        row = {"kind": "memory_profile", "worker": "gw0", "verdict": "STEADY_GROWTH", "nodeid": "t::x[0]"}
        incident = parse(row)
        assert incident.fingerprint_parts() == ["memory_profile", "STEADY_GROWTH", "t::x"]


@pytest.mark.parametrize("verdict", ["GC_PRESSURE", "NATIVE_THREADS"])
def test_frameless_hotspots_belong_to_the_runtime(verdict: str) -> None:
    from pytest_failure_instrumentation.incidents.profile import CpuHotspotIncident

    incident = CpuHotspotIncident(worker="controller", verdict=verdict)
    assert incident.owner_when_unattributable() == "runtime"
    assert incident.blame_stack() == ([], False)


CHURN_MODULE = '''

def churn(seconds):
    """A stretch of a core: the CPU step in a test that otherwise waits.

    Measured in CPU rather than in wall time, so that a machine which is busy
    with something else makes this take longer rather than makes it do less.
    """
    import time
    clock = getattr(time, "thread_time", time.process_time)
    deadline = clock() + seconds
    total = 0
    while clock() < deadline:
        total += sum(range(2000))
    return total
'''

BURST_INI = PROFILE_INI + "failure_profile_burst_cores = 0.5\n"


def bursts(incidents: list[Any]) -> list[Any]:
    return Runner.of_kind(incidents, "cpu_burst")


class TestBursts:
    def test_a_long_burst_in_a_waiting_test_is_named_with_when_it_started(self, runner: Runner) -> None:
        a_core_of_its_own()
        runner.pytester.makeini(BURST_INI + "failure_profile_burst_seconds = 1\n")
        (runner.pytester.path / "product.py").write_text(PRODUCT_MODULE + CHURN_MODULE, encoding="utf-8")
        runner.pytester.makepyfile(
            test_index="""
            import time
            from product import churn

            def test_index_is_complete():
                time.sleep(1.5)
                churn(2.5)
                time.sleep(0.5)
            """
        )
        incidents = runner.run("--failure-profile", "-p", "no:cacheprovider", timeout=120.0)

        long = [incident for incident in bursts(incidents) if incident.verdict == "LONG_BURST"]
        assert len(long) == 1, [str(incident) for incident in incidents]
        (incident,) = long
        assert incident.nodeid == "test_index.py::test_index_is_complete"
        assert incident.burst_seconds >= 1.0
        assert incident.started_s >= 0.5
        assert incident.phase == "call"
        assert incident.blamed_frame is not None and incident.blamed_frame.function == "churn"
        assert incident.owner == "product"
        assert incident.severity == "informational"
        assert any("was waiting." in line for line in incident.evidence)
        assert str(incident).startswith("CPU burst: test_index.py::test_index_is_complete ran at ")
        assert "starting" in str(incident).splitlines()[0]

    def test_the_same_fixture_bursting_in_every_test_is_one_recurring_finding(self, runner: Runner) -> None:
        a_core_of_its_own()
        # A burst too short to be raised on its own, five times over.
        runner.pytester.makeini(BURST_INI + "failure_profile_burst_seconds = 30\n")
        (runner.pytester.path / "product.py").write_text(PRODUCT_MODULE + CHURN_MODULE, encoding="utf-8")
        runner.pytester.makepyfile(
            test_sessions="""
            import time
            import pytest
            from product import churn

            @pytest.fixture
            def session():
                return churn(0.6)

            # Seven, against a rule that needs five. Five exactly meant one
            # burst lost to a slow window anywhere in the run took the finding
            # with it, which is what the macOS cell hit.
            @pytest.mark.parametrize("case", range(7))
            def test_request(session, case):
                time.sleep(0.15)
            """
        )
        incidents = runner.run("--failure-profile", "-p", "no:cacheprovider", timeout=120.0)

        # Only the recurring finding is asserted on: a busy runner can add a CONTENDED one.
        found = [incident for incident in bursts(incidents) if incident.verdict == "RECURRING_BURST"]
        assert len(found) == 1, [str(incident) for incident in incidents]
        (incident,) = found
        assert incident.test_count >= 5
        assert incident.phase == "setup"
        assert incident.blamed_frame is not None and incident.blamed_frame.function == "churn"
        assert incident.cpu_seconds >= 1.0
        assert any("It ran during setup of each of those tests." in line for line in incident.evidence)
        assert str(incident).startswith("Repeated CPU burst: ")


class TestAllocationTracing:
    def test_an_existing_tracer_is_a_configuration_error(
        self, runner: Runner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTHONTRACEMALLOC", "3")
        runner.pytester.makepyfile("def test_must_not_run():\n    assert False\n")

        runner.run("--failure-profile-allocations", "-p", "no:xdist")

        assert runner.result.ret == pytest.ExitCode.USAGE_ERROR
        runner.result.stderr.fnmatch_lines(
            [
                "*--failure-profile-allocations requires exclusive ownership of "
                "tracemalloc, but it is already active with depth 3*"
            ]
        )
        assert "test_must_not_run" not in runner.result.stdout.str()

    @needs_xdist
    def test_an_existing_tracer_is_refused_before_the_workers_are_started(
        self, distributed: Runner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same refusal under -n, and the reason it is made where it is.

        The check used to live only where the tracer is started, which under
        xdist is on a worker - so every worker raised it at once, out of
        pytest_configure, and the run printed four pluggy tracebacks and the
        profiler's "nothing in this run was sampling" summary after spending
        the better part of a minute starting the workers it was about to
        refuse. The controller configures before a worker exists, and the
        environment that made this true for a worker is true for it too.
        """
        monkeypatch.setenv("PYTHONTRACEMALLOC", "3")
        distributed.pytester.makepyfile("def test_must_not_run():\n    assert False\n")

        distributed.run("--failure-profile-allocations", "-n", "2")

        assert distributed.result.ret == pytest.ExitCode.USAGE_ERROR
        distributed.result.stderr.fnmatch_lines(
            [
                "*--failure-profile-allocations requires exclusive ownership of "
                "tracemalloc, but it is already active with depth 3*"
            ]
        )
        output = distributed.result.stdout.str()
        assert "test_must_not_run" not in output
        # The point of moving the check: one sentence, not a traceback per
        # worker, and no profile summary for a profile that never started.
        assert "Traceback" not in output
        assert "gw0" not in output
        assert "failure-instrumentation profile" not in output

    def test_tracing_names_the_holders_and_writes_a_memory_flame_graph(self, runner: Runner) -> None:
        runner.pytester.makeini(PROFILE_INI)
        runner.pytester.makepyfile(
            test_alloc="""
            import time

            KEPT = []

            def hold(count):
                return [bytearray(1_000_000) for _ in range(count)]

            def test_peak():
                blob = hold(200)
                time.sleep(1.0)
                assert len(blob) == 200

            def test_keeps():
                KEPT.extend(hold(60))
            """
        )
        # --failure-profile-allocations implies --failure-profile.
        incidents = runner.run("--failure-profile-allocations", "-p", "no:cacheprovider", timeout=120.0)

        by_test = {incident.nodeid: incident for incident in memory(incidents)}
        peak = by_test["test_alloc.py::test_peak"]
        assert peak.verdict == "TRANSIENT_PEAK"
        assert any(line.startswith("Held at the peak:") and "test_alloc.py:6" in line for line in peak.evidence)
        assert any("Figures are from tracemalloc" in line for line in peak.evidence)
        assert peak.blamed_frame is not None and peak.blamed_frame.file.endswith("test_alloc.py")
        kept = by_test["test_alloc.py::test_keeps"]
        assert kept.verdict == "RETAINED_AFTER_TEST"
        assert any(line.startswith("Still held after the test:") and "test_alloc.py:6" in line for line in kept.evidence)
        profiles = sorted(path.name for path in (runner.pytester.path / ".pytest-failures").glob("run-*/profiles/*"))
        assert any(re.fullmatch(r"test_alloc\.py_test_peak-[0-9a-f]{8}\.memory\.speedscope\.json", name) for name in profiles)
        assert any(re.fullmatch(r"test_alloc\.py_test_peak-[0-9a-f]{8}\.speedscope\.json", name) for name in profiles)
        document = json.loads(
            next((runner.pytester.path / ".pytest-failures").glob("run-*/profiles/test_alloc.py_test_peak-*.memory.speedscope.json")).read_text()
        )
        (profile,) = document["profiles"]
        assert profile["unit"] == "bytes"
        # Half of what the test allocated, not all of it: the snapshot is
        # taken when the sampler *notices* the climb, so it is one reading
        # behind the last chunk. Three quarters is what two platforms
        # measured, and a bar set there failed on the one that measured 73%.
        assert sum(profile["weights"]) >= 100 * 1_000_000
        assert any(frame["file"].endswith("test_alloc.py") for frame in document["shared"]["frames"])

