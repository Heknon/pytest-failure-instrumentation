"""The profile analysis against records built by hand.

Every rule in :mod:`pytest_failure_instrumentation.profile.analysis` is a
threshold over numbers a sampler wrote, so each one is checked here with the
numbers chosen to sit on either side of it - no sampling, no clock, no
subprocess. The integration tests in test_profile.py check that a real run
produces records these rules fire on.
"""

from __future__ import annotations

import sysconfig
from typing import Any

from pytest_failure_instrumentation.analysis.attribution import Attributor
from pytest_failure_instrumentation.profile import analysis
from pytest_failure_instrumentation.profile.analysis import Thresholds, analyse, speedscope

# Taken from the interpreter rather than written down: the attributor knows
# the stdlib by where sysconfig says it is, and a hardcoded /usr/lib path only
# looks like the stdlib on the machine it was written on. The installed paths
# below are recognised by their site-packages segment wherever they sit.
STDLIB = sysconfig.get_paths()["stdlib"].replace("\\", "/")

PRODUCT = "/srv/product/imaging.py"
TEST = "/srv/tests/test_screens.py"
LIBRARY = f"{STDLIB}/json/encoder.py"
PILLOW = "/usr/lib/python3.11/site-packages/PIL/Image.py"
RUNTIME = "/usr/lib/python3.11/site-packages/_pytest/python.py"

attributor = Attributor(("product",))


def frame(file: str, line: int, function: str) -> str:
    return f"{file}|{line}|{function}"


def record(
    nodeid: str | None,
    stacks: list[dict[str, Any]],
    frames: list[str],
    *,
    worker: str = "gw0",
    rss: tuple[int, int, int] = (100, 100, 100),
    heap: tuple[int, int] | None = None,
    blocks: tuple[int, int] | None = None,
    rss_at: dict[str, int] | None = None,
    gc_seconds: float = 0.0,
    native: list[dict[str, Any]] | None = None,
    cpu_s: float | None = None,
    growth: list[dict[str, Any]] | None = None,
    allocator: tuple[dict[str, int], dict[str, int]] | None = None,
    threads: int | None = None,
) -> dict[str, Any]:
    before, peak, after = rss
    sampled = sum(stack.get("cpu_ns", 0) for stack in stacks) / 1e9
    return {
        "record": "test" if nodeid else "background",
        "worker": worker,
        "nodeid": nodeid,
        "wall_s": 10.0,
        "cpu_s": sampled if cpu_s is None else cpu_s,
        "rss_before_mb": before,
        "rss_after_mb": after,
        "rss_peak_mb": peak,
        "rss_at": rss_at or {},
        "heap_before_mb": heap[0] if heap else None,
        "heap_after_mb": heap[1] if heap else None,
        "blocks_before": blocks[0] if blocks else None,
        "blocks_after": blocks[1] if blocks else None,
        "allocator_before": allocator[0] if allocator else None,
        "allocator_after": allocator[1] if allocator else None,
        "threads": threads,
        "cpus": 4,
        "gc": {"seconds": gc_seconds, "collections": 3, "by_generation": [1, 1, 1]},
        "cpu_weighted": True,
        "thread_clock": "thread-clock",
        "frames": frames,
        "stacks": stacks,
        "growth": growth or [],
        "native_threads": native or [],
    }


def stack(indexes: list[int], cpu_s: float, *, thread: str = "MainThread", background: bool = False) -> dict[str, Any]:
    return {
        "phase": "call",
        "thread": thread,
        "background": background,
        "frames": indexes,
        "cpu_ns": int(cpu_s * 1e9),
        "wall_ns": int(cpu_s * 1e9),
        "samples": int(cpu_s * 50),
    }


def findings_of(report: analysis.Report, verdict: str) -> list[analysis.Finding]:
    return [finding for finding in report.findings if finding.verdict == verdict]


# -- CPU ------------------------------------------------------------------------


class TestBlame:
    def test_a_python_loop_in_the_product_is_charged_to_its_own_function(self) -> None:
        frames = [frame(PRODUCT, 14, "is_images_different"), frame(TEST, 30, "test_screen"), frame(RUNTIME, 100, "pytest_pyfunc_call")]
        report = analyse([record("t::a", [stack([0, 1, 2], 8.0), stack([1, 2], 0.4)], frames)], attributor)

        (finding,) = findings_of(report, "PYTHON_CODE")
        assert finding.frame is not None
        assert finding.frame.function == "is_images_different"
        assert finding.frame.owner == "product"
        assert finding.share_percent == 95.2
        assert finding.self_share_percent == 100.0
        assert finding.hottest_lines == [(14, 100.0)]
        # The stack the engine attributes starts at the blamed frame.
        assert finding.stack[0].startswith(f'  File "{PRODUCT}", line 14')

    def test_time_under_a_library_is_charged_to_the_product_caller(self) -> None:
        frames = [
            frame(LIBRARY, 300, "_iterencode_dict"),
            frame(LIBRARY, 200, "iterencode"),
            frame(PRODUCT, 22, "render_report"),
            frame(TEST, 10, "test_report"),
        ]
        report = analyse([record("t::a", [stack([0, 1, 2, 3], 6.0)], frames)], attributor)

        (finding,) = findings_of(report, "LIBRARY_CALL")
        assert finding.frame is not None and finding.frame.function == "render_report"
        assert finding.below is not None
        assert finding.below.file == LIBRARY
        assert finding.below.function == "_iterencode_dict"
        assert finding.self_share_percent == 0.0
        assert not findings_of(report, "PYTHON_CODE")

    def test_a_dependency_is_blamed_only_when_nobody_else_is_on_the_stack(self) -> None:
        frames = [frame(PILLOW, 50, "resize"), frame(RUNTIME, 100, "pytest_pyfunc_call")]
        report = analyse([record("t::a", [stack([0, 1], 4.0)], frames)], attributor)

        (finding,) = report.findings
        assert finding.frame is not None and finding.frame.owner == "third-party"

    def test_a_comprehension_is_folded_into_its_function(self) -> None:
        frames = [frame(PRODUCT, 40, "build.<locals>.<listcomp>"), frame(PRODUCT, 40, "build")]
        report = analyse([record("t::a", [stack([0, 1], 4.0)], frames)], attributor)

        (finding,) = report.findings
        assert finding.frame is not None and finding.frame.function == "build"
        assert finding.self_share_percent == 100.0

    def test_the_samplers_own_callback_is_never_blamed(self) -> None:
        own = "/env/lib/python3.11/site-packages/pytest_failure_instrumentation/profile/sampler.py"
        frames = [frame(own, 300, "Sampler._on_gc"), frame(PRODUCT, 14, "build_graph")]
        report = analyse([record("t::a", [stack([0, 1], 4.0)], frames)], attributor)

        (finding,) = report.findings
        assert finding.frame is not None and finding.frame.function == "build_graph"
        assert finding.self_share_percent == 100.0

    def test_the_frame_is_on_the_hottest_line_not_the_heaviest_stack(self) -> None:
        # Line 14 costs 6 s over three callers at 2 s each; line 20 costs 3 s
        # from one. The heaviest single stack is on line 20, but "mostly line
        # 14" must point the reader, and the engine's stack, at line 14.
        frames = [
            frame(PRODUCT, 14, "hot"),
            frame(PRODUCT, 20, "hot"),
            frame(TEST, 30, "test_one"),
            frame(TEST, 40, "test_two"),
            frame(TEST, 50, "test_three"),
        ]
        stacks = [stack([0, 2], 2.0), stack([0, 3], 2.0), stack([0, 4], 2.0), stack([1, 2], 3.0)]
        report = analyse([record("t::a", stacks, frames)], attributor)

        (finding,) = findings_of(report, "PYTHON_CODE")
        assert finding.hottest_lines == [(14, 66.7), (20, 33.3)]
        assert finding.frame is not None and finding.frame.line == 14
        assert finding.stack[0].startswith(f'  File "{PRODUCT}", line 14 in hot')
        assert any(line == "Look at: imaging.py:14" for line in finding.evidence)

    def test_the_attributor_answers_a_path_once(self) -> None:
        # Every frame of every record is asked about, and the answer for a
        # path never changes, so it is worked out once per path.
        class Counting(Attributor):
            classified = 0

            def _classify(self, path: str) -> str:
                Counting.classified += 1
                return super()._classify(path)

        counting = Counting(("product",))
        frames = [frame(PRODUCT, 14, "hot"), frame(TEST, 30, "test_x")]
        records = [record(f"t::a[{case}]", [stack([0, 1], 1.0)], frames) for case in range(5)]
        report = analyse(records, counting)

        assert findings_of(report, "PYTHON_CODE")
        assert Counting.classified == 2
        assert counting.owner_of(PRODUCT) == "product" and counting.owner_of(TEST) == "customer-code"
        assert Counting.classified == 2


class TestThresholds:
    def test_below_the_share_is_not_a_finding(self) -> None:
        frames = [frame(PRODUCT, 1, "small"), frame(PRODUCT, 2, "big")]
        report = analyse(
            [record("t::a", [stack([0], 0.4), stack([1], 9.6)], frames)],
            attributor,
            Thresholds(cpu_share_percent=5.0),
        )

        assert [finding.frame.function for finding in report.findings if finding.frame] == ["big"]
        # But it is still in the ranking, for the terminal table.
        assert [cost.function for cost in report.functions] == ["big", "small"]

    def test_a_short_run_flags_nothing_however_large_the_share(self) -> None:
        frames = [frame(PRODUCT, 1, "only")]
        report = analyse([record("t::a", [stack([0], 0.3)], frames)], attributor, Thresholds(cpu_floor_seconds=0.5))

        assert report.findings == []


class TestThreads:
    def test_cpu_on_another_thread_is_a_background_finding(self) -> None:
        frames = [frame(PRODUCT, 31, "Poller._run"), frame(TEST, 5, "test_x")]
        report = analyse(
            [record("t::a", [stack([0], 6.0, thread="status-poller", background=True), stack([1], 4.0)], frames)],
            attributor,
        )

        (finding,) = findings_of(report, "BACKGROUND_THREAD")
        assert finding.frame is not None and finding.frame.function == "Poller._run"
        assert finding.thread == "status-poller"
        assert finding.background_share_percent == 100.0

    def test_native_threads_are_reported_without_a_frame(self) -> None:
        frames = [frame(TEST, 5, "test_x")]
        report = analyse(
            [record("t::a", [stack([0], 2.0)], frames, native=[{"tid": 7, "name": "grpc_poll", "cpu_ns": int(8e9)}])],
            attributor,
        )

        (finding,) = findings_of(report, "NATIVE_THREADS")
        assert finding.frame is None
        assert finding.share_percent == 80.0
        assert "grpc_poll" in finding.evidence[1]
        assert report.native_cpu_s == 8.0


class TestBetweenTests:
    def test_cpu_between_tests_is_not_counted_as_a_test(self) -> None:
        # A background record has no node id. Its CPU is real and is
        # charged, but it is nobody's test: not counted as one, not offered
        # to the engine as the test to attribute the finding to.
        frames = [frame(PRODUCT, 30, "Poller.run")]
        gap = record(None, [stack([0], 1.0, thread="status-poller")], frames)
        test = record("t::a", [stack([0], 4.0, thread="status-poller", background=True)], frames)
        report = analyse([gap, test], attributor)

        (finding,) = findings_of(report, "BACKGROUND_THREAD")
        assert finding.tests == ["t::a"]
        assert finding.test_count == 1
        assert not any("background on" in nodeid for nodeid in finding.tests)
        assert any("Seen in 1 test: t::a, and between tests." in line for line in finding.evidence)
        (cost,) = report.functions
        assert cost.gap_cpu_ns == int(1.0 * 1e9)
        assert list(cost.tests) == ["t::a"]

    def test_cpu_with_no_test_in_flight_at_all_says_so(self) -> None:
        frames = [frame(PRODUCT, 30, "Poller.run")]
        report = analyse([record(None, [stack([0], 4.0, thread="status-poller")], frames)], attributor)

        (finding,) = findings_of(report, "PYTHON_CODE")
        assert finding.tests == []
        assert finding.test_count == 0
        assert any("Seen only between tests, with no test running." in line for line in finding.evidence)


class TestGarbageCollection:
    def test_the_collectors_share_is_a_finding_naming_the_tests(self) -> None:
        frames = [frame(TEST, 5, "test_x")]
        records = [
            record("t::heavy", [stack([0], 4.0)], frames, gc_seconds=2.0),
            record("t::light", [stack([0], 4.0)], frames, gc_seconds=0.1),
        ]
        report = analyse(records, attributor, Thresholds(gc_share_percent=10.0))

        (finding,) = findings_of(report, "GC_PRESSURE")
        assert finding.tests[0] == "t::heavy"
        assert finding.cpu_seconds == 2.1

    def test_a_quiet_collector_is_not_mentioned(self) -> None:
        frames = [frame(TEST, 5, "test_x")]
        report = analyse([record("t::a", [stack([0], 10.0)], frames, gc_seconds=0.2)], attributor)

        assert not findings_of(report, "GC_PRESSURE")


# -- memory ---------------------------------------------------------------------


class TestRetained:
    def test_memory_kept_and_still_in_use_is_retained(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(100, 260, 260), heap=(10, 170), blocks=(1000, 1100),
                    rss_at={"setup_start": 100, "setup_end": 100, "call_start": 100, "call_end": 260, "teardown_start": 260, "teardown_end": 260})],
            attributor,
        )

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 160
        assert finding.phase == "call"
        assert any(line.startswith("Measured: process") and "Live heap +160 MB" in line for line in finding.evidence)

    def test_memory_kept_by_a_fixture_names_setup(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(100, 260, 260), heap=(10, 170),
                    rss_at={"setup_start": 100, "setup_end": 258, "call_start": 258, "call_end": 260, "teardown_start": 260, "teardown_end": 260})],
            attributor,
        )

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.phase == "setup"
        assert any("during setup, so a fixture allocated it" in line for line in finding.evidence)

    def test_small_heap_growth_does_not_prove_objects_were_freed(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(100, 400, 350), heap=(10, 12), blocks=(1000, 1200))],
            attributor,
        )

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 250
        assert not findings_of(report, "HEAP_NOT_RETURNED")
        assert any("cannot distinguish" in line for line in finding.evidence)

    def test_live_small_objects_are_not_reported_as_freed(self) -> None:
        # Measurements from 450,000 retained 400-character strings: pymalloc
        # arenas account for the RSS increase, while mallinfo2 barely moves.
        retained = record("t::a", [], [], rss=(26, 224, 224),
                          heap=(6, 9), blocks=(137882, 587898),
                          rss_at={"call_start": 26, "call_end": 224})
        report = analyse([retained], attributor)

        assert analysis._still_in_use(retained, 198)[0] is None
        assert not findings_of(report, "HEAP_NOT_RETURNED")
        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 198
        assert finding.phase == "call"
        assert any("cannot distinguish" in line for line in finding.evidence)
        assert not any("were freed" in line or "still in use" in line for line in finding.evidence)

    def test_without_heap_readings_kept_memory_is_still_retained(self) -> None:
        report = analyse([record("t::a", [], [], rss=(100, 260, 260))], attributor)

        assert [finding.verdict for finding in report.findings] == ["RETAINED_AFTER_TEST"]

    def test_a_block_count_alone_cannot_say_the_memory_was_freed(self) -> None:
        # No heap reading - macOS, Windows - and one block more: a numpy
        # array holding the two hundred megabytes is one block. The blocks
        # at their rough size are a floor, and a floor under the bar says
        # nothing, not "none of it in use".
        report = analyse([record("t::a", [], [], rss=(100, 300, 300), blocks=(1000, 1001))], attributor)

        assert not findings_of(report, "HEAP_NOT_RETURNED")
        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 200
        assert not any("none of it" in line for line in finding.evidence)
        assert any("+1 Python allocation blocks" in line for line in finding.evidence)

    def test_large_block_count_does_not_establish_live_bytes(self) -> None:
        # Allocation blocks have variable sizes; a count is not a byte measurement.
        report = analyse([record("t::a", [], [], rss=(100, 300, 300), blocks=(1000, 2_001_000))], attributor)

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 200
        assert any("cannot distinguish" in line for line in finding.evidence)

    def test_under_the_threshold_is_nothing(self) -> None:
        report = analyse([record("t::a", [], [], rss=(100, 150, 150), heap=(0, 50))], attributor, Thresholds(retained_mb=100))

        assert report.findings == []


class TestPeak:
    def test_a_climb_that_comes_back_is_transient(self) -> None:
        report = analyse([record("t::a", [], [], rss=(100, 420, 110))], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert finding.peak_mb == 420
        assert finding.delta_mb == 320
        assert finding.frame is None  # nothing was seen climbing

    def test_the_climb_is_blamed_on_the_code_that_was_running(self) -> None:
        frames = [frame(LIBRARY, 5, "loads"), frame(PRODUCT, 14, "load_everything"), frame(TEST, 30, "test_export")]
        growth = [
            {"thread": "MainThread", "frames": [0, 1, 2], "mb": 300},
            {"thread": "MainThread", "frames": [2], "mb": 20},
        ]
        report = analyse([record("t::a", [], frames, rss=(100, 420, 110), growth=growth)], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert finding.frame is not None
        assert finding.frame.function == "load_everything"
        assert finding.frame.owner == "product"
        assert finding.climb_mb == 300
        assert finding.climb_total_mb == 320
        assert finding.stack[0].startswith(f'  File "{PRODUCT}", line 14')
        expected = (
            "Memory rose by 320 MB, summed over every reading that found it higher; 300 MB of that "
            "increase happened while load_everything (imaging.py:14) was running, called from "
            "test_export (test_screens.py:30)."
        )
        assert any(expected == line for line in finding.evidence)

    def test_the_total_is_the_sum_of_the_readings_and_is_called_that(self) -> None:
        # A test that allocates and frees fifty megabytes ten times: every
        # upward reading is charged, so the total is five hundred next to a
        # peak of two hundred and fifty. The number is right; it must not be
        # called the increase.
        frames = [frame(PRODUCT, 3, "churn"), frame(TEST, 8, "test_churns")]
        growth = [{"thread": "MainThread", "frames": [0, 1], "mb": 50}] * 10
        report = analyse([record("t::a", [], frames, rss=(100, 250, 100), growth=growth)], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert finding.climb_mb == 500
        assert finding.climb_total_mb == 500
        assert finding.peak_mb == 250
        expected = (
            "Memory rose by 500 MB, summed over every reading that found it higher; all of that "
            "increase happened while churn (imaging.py:3) was running, called from "
            "test_churns (test_screens.py:8)."
        )
        assert any(expected == line for line in finding.evidence)
        assert not any("500 MB increase" in line for line in finding.evidence)

    def test_a_climb_seen_under_the_runtime_alone_is_unplaced_rather_than_blamed_on_it(self) -> None:
        # The reading landed after the body returned, with pytest's own
        # frames on the stack. Naming them would send the reader to the
        # wrong place, so the climb is unplaced and the suspect owner stands.
        frames = [frame(RUNTIME, 167, "pytest_pyfunc_call"), frame(RUNTIME, 595, "FDCapture.snap")]
        growth = [{"thread": "MainThread", "frames": [1, 0], "mb": 300}]
        report = analyse([record("t::a", [], frames, rss=(100, 420, 110), growth=growth)], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert finding.frame is None
        assert finding.stack == []
        assert finding.climb_mb == 0
        assert finding.climb_total_mb == 300
        assert any("300 MB of the increase could not be attributed to any code." == line for line in finding.evidence)

    def test_a_climb_under_the_runtime_falls_back_to_the_stack_a_tick_earlier(self) -> None:
        frames = [
            frame(RUNTIME, 595, "FDCapture.snap"),
            frame(PRODUCT, 9, "remember"),
            frame(TEST, 22, "test_keeps_results"),
        ]
        growth = [{"thread": "MainThread", "frames": [0], "fallback": [1, 2], "mb": 300}]
        report = analyse([record("t::a", [], frames, rss=(100, 420, 110), growth=growth)], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert finding.frame is not None
        assert finding.frame.function == "remember"
        assert finding.frame.owner == "product"
        assert finding.climb_mb == 300
        assert finding.stack[0].startswith(f'  File "{PRODUCT}", line 9')

    def test_the_ceiling_is_a_finding_whatever_the_test_started_from(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(3000, 4100, 3010))],
            attributor,
            Thresholds(retained_mb=100, peak_mb=4000),
        )

        (finding,) = findings_of(report, "PEAK_OVER_CEILING")
        assert finding.peak_mb == 4100
        assert finding.delta_mb == 1100
        assert not findings_of(report, "TRANSIENT_PEAK")

    def test_a_test_already_over_the_ceiling_is_not_raised_for_sitting_there(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(4050, 4060, 4060))],
            attributor,
            Thresholds(retained_mb=100, peak_mb=4000),
        )

        assert report.findings == []

    def test_a_test_that_keeps_memory_and_crosses_the_ceiling_is_the_ceiling_and_says_so(self) -> None:
        # The size is the finding; that it stayed is said rather than
        # changing the verdict, so the ceiling is never hidden by a leak.
        report = analyse(
            [record("t::a", [], [], rss=(100, 4200, 4200), heap=(0, 4100))],
            attributor,
            Thresholds(retained_mb=100, peak_mb=4000),
        )

        (finding,) = report.findings
        assert finding.verdict == "PEAK_OVER_CEILING"
        assert any("4100 MB of it was still in use after the test." == line for line in finding.evidence)
        assert finding.ceiling_mb == 4000

    def test_memory_kept_in_pages_an_earlier_test_freed_is_still_retained(self) -> None:
        # Resident memory grew 60 MB; the live heap grew 150 MB. The test
        # kept 150 and reused 90 MB the allocator was already holding.
        report = analyse(
            [record("t::a", [], [], rss=(500, 560, 560), heap=(100, 250))],
            attributor,
            Thresholds(retained_mb=100),
        )

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 150
        assert any("lower than the live-heap figure" in line for line in finding.evidence)


class TestGrowth:
    def test_a_run_of_small_steps_is_steady_growth(self) -> None:
        records = []
        rss = 100
        for case in range(6):
            records.append(record(f"t::leaks[{case}]", [], [], rss=(rss, rss + 30, rss + 30)))
            rss += 30
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        (finding,) = findings_of(report, "STEADY_GROWTH")
        assert finding.growth_tests == 6
        assert finding.delta_mb == 180
        assert finding.growth_per_test_mb == 30.0
        assert any("All of them are cases of t::leaks." == line for line in finding.evidence)
        # None of the steps was a finding of its own.
        assert not findings_of(report, "RETAINED_AFTER_TEST")

    def test_growth_hidden_by_reused_pages_is_seen_through_the_heap(self) -> None:
        # Resident memory is flat because every test fills pages the one before
        # it freed; the live heap says what each one kept.
        records = [
            record(f"t::leaks[{case}]", [], [], rss=(800, 800, 800), heap=(100 + 30 * case, 130 + 30 * case))
            for case in range(6)
        ]
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        (finding,) = findings_of(report, "STEADY_GROWTH")
        assert finding.delta_mb == 180
        assert finding.growth_per_test_mb == 30.0

    def test_a_single_big_step_is_not_growth(self) -> None:
        records = [
            record("t::a", [], [], rss=(100, 100, 100)),
            record("t::b", [], [], rss=(100, 300, 300), heap=(0, 200)),
            record("t::c", [], [], rss=(300, 300, 300)),
        ]
        report = analyse(records, attributor)

        assert not findings_of(report, "STEADY_GROWTH")
        assert findings_of(report, "RETAINED_AFTER_TEST")

    def test_too_few_steps_are_not_growth(self) -> None:
        records = [record(f"t::{case}", [], [], rss=(100 + 40 * case, 140 + 40 * case, 140 + 40 * case)) for case in range(3)]
        report = analyse(records, attributor, Thresholds(growth_tests=4))

        assert not findings_of(report, "STEADY_GROWTH")

    def test_tests_that_kept_nothing_by_the_live_figures_are_not_growing(self) -> None:
        # Three tests keep 40 MB each; four keep nothing but fifty blocks,
        # which every test does. Fewer than half grew, so this is three
        # steps, not a drift - however many blocks the others were up by.
        records = [
            record(f"t::keeps[{case}]", [], [], rss=(100 + 40 * case, 140 + 40 * case, 140 + 40 * case), heap=(40 * case, 40 + 40 * case), blocks=(1000, 1050))
            for case in range(3)
        ]
        records += [
            record(f"t::clean[{case}]", [], [], rss=(220, 220, 220), heap=(120, 120), blocks=(1000, 1050))
            for case in range(4)
        ]
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        assert not findings_of(report, "STEADY_GROWTH")

    def test_objects_per_test_is_over_the_tests_where_they_were_counted(self) -> None:
        records = [
            record(f"t::leaks[{case}]", [], [], rss=(100 + 30 * case, 130 + 30 * case, 130 + 30 * case), heap=(30 * case, 30 + 30 * case), blocks=(1000, 1300) if case % 2 else None)
            for case in range(6)
        ]
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        (finding,) = findings_of(report, "STEADY_GROWTH")
        assert finding.growth_objects_per_test == 300

    def test_no_minimum_number_of_tests_is_not_an_error(self) -> None:
        # growth_tests=0 asks for no minimum. A worker whose only test was
        # raised on its own has no rows for the rule, and that is nothing.
        report = analyse([record("t::a", [], [], rss=(100, 300, 300))], attributor, Thresholds(growth_tests=0))

        assert not findings_of(report, "STEADY_GROWTH")
        assert findings_of(report, "RETAINED_AFTER_TEST")


class TestImbalance:
    def test_one_worker_far_above_its_siblings_is_named_with_where_it_diverged(self) -> None:
        records = [
            record("t::a", [], [], worker="gw0", rss=(100, 120, 120)),
            record("t::b", [], [], worker="gw1", rss=(100, 110, 110)),
            record("t::c", [], [], worker="gw2", rss=(100, 130, 130)),
            record("t::d", [], [], worker="gw2", rss=(130, 900, 900), heap=(0, 770)),
            record("t::e", [], [], worker="gw2", rss=(900, 900, 900)),
        ]
        report = analyse(records, attributor, Thresholds(imbalance_ratio=2.0, retained_mb=100))

        (finding,) = findings_of(report, "WORKER_IMBALANCE")
        assert finding.worker == "gw2"
        assert finding.nodeid == "t::d"
        assert finding.peak_mb == 900
        assert finding.median_mb == 115  # the median of its siblings, not of all three
        assert finding.worker_rss == {"gw0": 120, "gw1": 110, "gw2": 900}

    def test_one_worker_is_never_imbalanced(self) -> None:
        report = analyse([record("t::a", [], [], worker="gw0", rss=(100, 900, 900))], attributor)

        assert not findings_of(report, "WORKER_IMBALANCE")

    def test_workers_close_together_are_not_imbalanced(self) -> None:
        records = [
            record("t::a", [], [], worker="gw0", rss=(100, 500, 500), heap=(0, 400)),
            record("t::b", [], [], worker="gw1", rss=(100, 450, 450), heap=(0, 350)),
        ]
        report = analyse(records, attributor)

        assert not findings_of(report, "WORKER_IMBALANCE")


# -- the report and the files ------------------------------------------------------


class TestReport:
    def test_the_totals_reconcile_sampled_against_process_cpu(self) -> None:
        frames = [frame(PRODUCT, 1, "f")]
        records = [
            record("t::a", [stack([0], 3.0)], frames, cpu_s=4.0),
            record(None, [stack([0], 1.0)], frames, cpu_s=1.5),
        ]
        report = analyse(records, attributor)

        assert report.sampled_cpu_s == 4.0
        assert report.process_cpu_s == 5.5
        assert report.tests == 1
        assert report.workers["gw0"]["tests"] == 1

    def test_a_record_without_cpu_weighting_marks_the_report(self) -> None:
        frames = [frame(PRODUCT, 1, "f")]
        entry = record("t::a", [stack([0], 3.0)], frames)
        entry["cpu_weighted"] = False

        assert analyse([entry], attributor).cpu_weighted is False

    def test_speedscope_has_a_profile_per_thread_outermost_first(self) -> None:
        frames = [frame(PRODUCT, 14, "inner"), frame(TEST, 30, "test_x")]
        entry = record("t::a", [stack([0, 1], 2.0), stack([0], 1.0, thread="poller", background=True)], frames)

        document = speedscope(entry, "t::a")

        assert [profile["name"] for profile in document["profiles"]] == ["MainThread (cpu)", "poller (cpu)"]
        main = document["profiles"][0]
        assert main["unit"] == "nanoseconds"
        assert main["samples"] == [[1, 0]]  # speedscope wants the root first
        assert main["weights"] == [int(2e9)]
        assert document["shared"]["frames"][0] == {"name": "inner", "file": PRODUCT, "line": 14}


class TestAllocatorRetention:
    """The worker grew and nothing is using it - the allocator kept it."""

    @staticmethod
    def arenas(free: int, main_free: int, arenas: int = 9, trim: int = 0) -> dict[str, int]:
        return {"arenas": arenas, "free_mb": free, "main_free_mb": main_free, "mapped_mb": free + 20, "trim_mb": trim}

    def churn(
        self,
        count: int = 6,
        step: int = 30,
        free_at_end: int | None = None,
        main_share: float = 0.05,
        arenas: int = 9,
        trim: int = 0,
    ) -> list[dict[str, Any]]:
        # Resident memory climbs `step` a test, the live heap and the object
        # count stay flat, and the allocator's free figure climbs with it.
        rows = []
        for case in range(count):
            before_free = step * case
            after_free = step * (case + 1) if free_at_end is None else min(free_at_end, step * (case + 1))
            rows.append(
                record(
                    f"t::churn[{case}]",
                    [],
                    [],
                    rss=(100 + step * case, 130 + step * case, 130 + step * case),
                    heap=(50, 50),
                    blocks=(1000, 1000),
                    allocator=(
                        self.arenas(before_free, int(before_free * main_share), arenas, trim),
                        self.arenas(after_free, int(after_free * main_share), arenas, trim),
                    ),
                    threads=12,
                )
            )
        return rows

    def test_free_memory_in_the_thread_arenas_names_malloc_arena_max(self) -> None:
        report = analyse(self.churn(), attributor, Thresholds(retained_mb=100))

        assert not findings_of(report, "STEADY_GROWTH")  # nothing in use grew
        (finding,) = findings_of(report, "ALLOCATOR_RETENTION")
        assert finding.worker == "gw0"
        assert finding.nodeid is None
        assert finding.delta_mb == 180
        assert finding.before_mb == 100 and finding.after_mb == 280
        assert finding.arenas == 9 and finding.threads == 12 and finding.cpus == 4
        assert finding.allocator_free_mb == 180
        assert any(
            "Measured: process 100 MB at the start, 280 MB at the end, up 180 MB over 6 tests with 0 MB of that in use."
            == line
            for line in finding.evidence
        )
        assert any("9 arenas existed for up to 12 threads on 4 cores" in line for line in finding.evidence)
        assert any("MALLOC_ARENA_MAX limits how many thread arenas exist" in line for line in finding.evidence)
        assert not any("malloc_trim" in line for line in finding.evidence)

    def test_free_memory_in_the_main_arena_names_a_trim_instead(self) -> None:
        report = analyse(self.churn(main_share=0.9, arenas=1, trim=40), attributor, Thresholds(retained_mb=100))

        (finding,) = findings_of(report, "ALLOCATOR_RETENTION")
        assert finding.trim_mb == 40
        assert any("MALLOC_ARENA_MAX does not affect" in line and "currently 40 MB" in line for line in finding.evidence)
        assert not any("limits how many thread arenas" in line for line in finding.evidence)

    def test_a_main_arena_with_no_free_tail_does_not_recommend_a_trim(self) -> None:
        # The free memory is in the main heap and none of it is at the tail:
        # a trim would return nothing, and saying "currently 0 MB" next to a
        # recommendation to trim is telling the reader to do nothing.
        report = analyse(self.churn(main_share=0.9, arenas=1, trim=0), attributor, Thresholds(retained_mb=100))

        (finding,) = findings_of(report, "ALLOCATOR_RETENTION")
        assert finding.trim_mb == 0
        assert any("MALLOC_ARENA_MAX does not affect" in line and "a trim would return nothing" in line for line in finding.evidence)
        assert not any("malloc_trim(0) releases" in line or "currently 0 MB" in line for line in finding.evidence)

    def test_growth_that_is_in_use_is_not_the_allocators(self) -> None:
        rows = self.churn()
        for case, row in enumerate(rows):
            # The live heap climbs with the resident figure: kept, not freed.
            row["heap_before_mb"], row["heap_after_mb"] = 50 + 30 * case, 80 + 30 * case
        report = analyse(rows, attributor, Thresholds(retained_mb=100))

        assert not findings_of(report, "ALLOCATOR_RETENTION")

    def test_a_gap_the_allocator_does_not_account_for_is_not_raised(self) -> None:
        # Resident memory grew 180 MB with nothing in use, but the allocator
        # says it holds 30 MB free: whatever the rest is, it is not this.
        report = analyse(self.churn(free_at_end=30), attributor, Thresholds(retained_mb=100))

        assert not findings_of(report, "ALLOCATOR_RETENTION")

    def test_under_the_threshold_is_nothing(self) -> None:
        report = analyse(self.churn(count=3), attributor, Thresholds(retained_mb=100))

        assert not findings_of(report, "ALLOCATOR_RETENTION")

    def test_one_finding_for_the_run_names_the_other_workers(self) -> None:
        rows = self.churn()
        others = [dict(row, worker="gw1") for row in self.churn(step=25)]
        report = analyse(rows + others, attributor, Thresholds(retained_mb=100))

        (finding,) = findings_of(report, "ALLOCATOR_RETENTION")
        assert finding.worker == "gw0"
        assert finding.worker_rss == {"gw0": 180, "gw1": 150}
        assert any("The same on gw1 (150 MB)." == line for line in finding.evidence)

    def test_uncertain_test_retention_is_not_hidden_by_worker_allocator_evidence(self) -> None:
        rows = self.churn()
        # Worker-wide free space does not establish what this individual test kept.
        rows[2]["rss_after_mb"] = rows[2]["rss_before_mb"] + 120
        for row in rows[3:]:
            row["rss_before_mb"] += 90
            row["rss_after_mb"] += 90
        report = analyse(rows, attributor, Thresholds(retained_mb=100))

        assert not findings_of(report, "HEAP_NOT_RETURNED")
        assert findings_of(report, "ALLOCATOR_RETENTION")
        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.nodeid == "t::churn[2]"
        assert any("cannot distinguish" in line for line in finding.evidence)

    def test_a_traced_run_is_not_judged_on_its_allocator(self) -> None:
        rows = self.churn()
        for row in rows:
            row["traced"] = {"before_mb": 1, "after_mb": 1, "peak_mb": 1, "tracer_mb": 30}
        report = analyse(rows, attributor, Thresholds(retained_mb=100))

        assert not findings_of(report, "ALLOCATOR_RETENTION")


# -- bursts ---------------------------------------------------------------------


def timeline(*windows: tuple[float, int | None, str | None, str | None, list[int] | None]) -> list[list[Any]]:
    """Timeline entries a tenth of a second apart from (cores, machine
    permille, phase, thread, frame indexes)."""
    entries = []
    for number, (cores, machine, phase, thread, frames) in enumerate(windows, start=1):
        entries.append([number * 100, int(cores * 1e8), machine, phase, thread, frames])
    return entries


def idle(count: int, phase: str | None = "call") -> list[tuple[Any, ...]]:
    return [(0.02, 200, phase, None, None)] * count


def busy(count: int, frames: list[int], *, phase: str = "call", thread: str = "MainThread", cores: float = 1.0, machine: int = 300) -> list[tuple[Any, ...]]:
    return [(cores, machine, phase, thread, frames)] * count


def bursts_of(report: analysis.Report) -> list[analysis.Finding]:
    return [finding for finding in report.findings if finding.kind == "cpu_burst"]


class TestBursts:
    frames = [frame(PRODUCT, 31, "build_index"), frame(TEST, 12, "test_index"), frame(PRODUCT, 18, "Session.__init__")]

    def test_a_long_burst_in_a_waiting_test_is_named_with_when_it_started(self) -> None:
        entry = record("t::index", [stack([0, 1], 2.5)], self.frames, cpu_s=2.6)
        entry["timeline"] = timeline(*idle(5), *busy(25, [0, 1]), *idle(5))
        report = analyse([entry], attributor, Thresholds(burst_seconds=2.0, burst_cores=0.7))

        (finding,) = bursts_of(report)
        assert finding.verdict == "LONG_BURST"
        assert finding.nodeid == "t::index"
        assert finding.started_s == 0.5
        assert finding.burst_seconds == 2.5
        assert finding.cores == 1.0
        assert finding.phase == "call"
        assert finding.frame is not None and finding.frame.function == "build_index"
        assert finding.frame.owner == "product"
        assert finding.stack[0].startswith(f'  File "{PRODUCT}", line 31')
        assert any("was waiting." in line for line in finding.evidence)
        assert any(line.startswith("Running build_index (imaging.py:31)") for line in finding.evidence)
        assert any(line == "Look at: imaging.py:31" for line in finding.evidence)

    def test_a_burst_shorter_than_the_threshold_is_not_a_finding(self) -> None:
        entry = record("t::index", [stack([0, 1], 1.0)], self.frames, cpu_s=1.0)
        entry["timeline"] = timeline(*idle(5), *busy(10, [0, 1]), *idle(5))
        report = analyse([entry], attributor, Thresholds(burst_seconds=2.0))

        assert bursts_of(report) == []

    def test_one_window_as_long_as_a_burst_is_a_burst(self) -> None:
        # A native call held the GIL for five seconds, so the sampler's next
        # tick came five seconds late: one window, five seconds of wall and
        # of CPU. One tick's worth of noise is a tenth of a second, not that.
        entry = record("t::index", [stack([0, 1], 5.0)], self.frames, cpu_s=5.0)
        entry["timeline"] = [
            [100, int(0.02 * 1e8), 200, "call", None, None],
            [5100, int(5e9), 300, "call", "MainThread", [0, 1]],
            *([5200 + 100 * n, int(0.02 * 1e8), 200, "call", None, None] for n in range(5)),
        ]
        report = analyse([entry], attributor, Thresholds(burst_seconds=2.0, burst_cores=0.7))

        (finding,) = bursts_of(report)
        assert finding.verdict == "LONG_BURST"
        assert finding.burst_seconds == 5.0
        assert finding.cores == 1.0
        assert finding.started_s == 0.1
        assert finding.frame is not None and finding.frame.function == "build_index"

    def test_a_single_busy_window_is_noise_however_often_it_recurs(self) -> None:
        records = []
        for case in range(6):
            entry = record(f"t::a[{case}]", [], self.frames, cpu_s=0.1)
            entry["timeline"] = timeline(*busy(1, [2], phase="setup"), *idle(5))
            records.append(entry)
        report = analyse(records, attributor, Thresholds(burst_tests=5))

        assert bursts_of(report) == []

    def test_the_same_function_bursting_in_five_tests_is_one_recurring_finding(self) -> None:
        records = []
        for case in range(5):
            entry = record(f"t::request[{case}]", [stack([2], 0.4)], self.frames, cpu_s=0.4)
            entry["timeline"] = timeline(*busy(4, [2], phase="setup"), *idle(6))
            records.append(entry)
        report = analyse(records, attributor, Thresholds(burst_seconds=2.0, burst_tests=5))

        (finding,) = bursts_of(report)
        assert finding.verdict == "RECURRING_BURST"
        assert finding.test_count == 5
        assert finding.phase == "setup"
        assert finding.frame is not None and finding.frame.function == "Session.__init__"
        assert finding.cpu_seconds == 2.0
        assert finding.burst_seconds == 0.4
        assert any("It ran during setup of each of those tests." in line for line in finding.evidence)
        assert len(finding.tests) == 3

    def test_four_tests_bursting_is_not_yet_recurring(self) -> None:
        records = []
        for case in range(4):
            entry = record(f"t::request[{case}]", [stack([2], 0.4)], self.frames, cpu_s=0.4)
            entry["timeline"] = timeline(*busy(4, [2], phase="setup"), *idle(6))
            records.append(entry)
        report = analyse(records, attributor, Thresholds(burst_seconds=2.0, burst_tests=5))

        assert bursts_of(report) == []

    def test_a_burst_on_another_thread_is_background(self) -> None:
        frames = [frame(PRODUCT, 30, "Poller._run"), frame(TEST, 5, "test_x")]
        entry = record("t::a", [stack([1], 0.1), stack([0], 3.0, thread="status-poller", background=True)], frames)
        entry["timeline"] = timeline(*busy(30, [0], thread="status-poller"))
        gap = record(None, [stack([0], 3.0, thread="status-poller", background=True)], frames)
        gap["timeline"] = timeline(*busy(30, [0], thread="status-poller", phase=None))
        report = analyse([entry, gap], attributor, Thresholds(burst_seconds=2.0))

        # One thread is one finding, however many bursts it had and whether
        # they fell under a test or between two.
        (finding,) = bursts_of(report)
        assert finding.verdict == "BACKGROUND_BURST"
        assert finding.thread == "status-poller"
        assert finding.frame is not None and finding.frame.function == "Poller._run"
        assert any("2 bursts like it on this thread" in line for line in finding.evidence)

    def test_a_burst_between_tests_says_so(self) -> None:
        frames = [frame(PRODUCT, 30, "Poller._run")]
        gap = record(None, [stack([0], 3.0, thread="status-poller", background=True)], frames)
        gap["timeline"] = timeline(*busy(30, [0], thread="status-poller", phase=None))
        report = analyse([gap], attributor, Thresholds(burst_seconds=2.0))

        (finding,) = bursts_of(report)
        assert finding.verdict == "BACKGROUND_BURST"
        assert finding.nodeid is None
        assert any("This thread is not the one running tests." in line for line in finding.evidence)

    def test_a_pinned_machine_and_starved_workers_is_contended(self) -> None:
        records = []
        for case in range(3):
            entry = record(f"t::slow[{case}]", [stack([1], 0.6)], self.frames, cpu_s=0.6)
            entry["cpus"] = 4
            entry["timeline"] = timeline(*busy(30, [1], cores=0.2, machine=960))
            records.append(entry)
        report = analyse(records, attributor, Thresholds(burst_seconds=2.0, burst_cores=0.7))

        (finding,) = bursts_of(report)
        assert finding.verdict == "CONTENDED"
        assert finding.frame is None
        assert finding.cores == 0.2
        assert finding.machine_busy_percent == 100.0
        assert finding.cpus == 4
        assert any(line.startswith("1 worker on 4 cores, so the load was not only this run's workers.") for line in finding.evidence)

    def test_contention_is_not_summed_across_workers(self) -> None:
        # Two workers side by side, each pinned for the whole of a 3 s run:
        # the run was pinned for 3 s, not 6, and for all of it, not twice
        # all of it. The cores are the average of what each got.
        records = []
        for worker, cores in (("gw0", 0.2), ("gw1", 0.4)):
            entry = record(f"t::slow[{worker}]", [stack([1], 0.6)], self.frames, worker=worker, cpu_s=0.6)
            entry["timeline"] = timeline(*busy(30, [1], cores=cores, machine=960))
            records.append(entry)
        report = analyse(records, attributor, Thresholds(burst_seconds=2.0, burst_cores=0.7))

        (finding,) = bursts_of(report)
        assert finding.verdict == "CONTENDED"
        assert finding.burst_seconds == 3.0
        assert finding.machine_busy_percent == 100.0
        assert finding.cores == 0.3
        assert finding.worker_count == 2

    def test_a_machine_busy_for_a_moment_is_not_contended(self) -> None:
        entry = record("t::a", [stack([1], 0.6)], self.frames, cpu_s=0.6)
        entry["timeline"] = timeline(*busy(5, [1], cores=0.2, machine=960), *idle(30))
        report = analyse([entry], attributor)

        assert bursts_of(report) == []

    def test_a_pinned_machine_is_noted_on_a_long_burst(self) -> None:
        entry = record("t::index", [stack([0, 1], 1.0)], self.frames, cpu_s=1.0)
        entry["timeline"] = timeline(*busy(30, [0, 1], cores=0.75, machine=980))
        report = analyse([entry], attributor, Thresholds(burst_seconds=2.0, burst_cores=0.7))

        (finding,) = [finding for finding in bursts_of(report) if finding.verdict == "LONG_BURST"]
        assert finding.machine_busy_percent == 98.0
        assert any("The machine was saturated, so this took longer than its CPU time." in line for line in finding.evidence)

    def test_one_quiet_window_does_not_end_a_burst_but_two_do(self) -> None:
        entry = record("t::index", [stack([0, 1], 2.5)], self.frames, cpu_s=2.6)
        entry["timeline"] = timeline(*busy(12, [0, 1]), *idle(1), *busy(12, [0, 1]), *idle(2), *busy(12, [0, 1]))
        report = analyse([entry], attributor, Thresholds(burst_seconds=2.0))

        (finding,) = bursts_of(report)
        assert finding.verdict == "LONG_BURST"
        assert finding.burst_seconds == 2.5


# -- drift and allocation tracing ---------------------------------------------------


class TestDrift:
    def test_a_fixture_given_back_does_not_cancel_the_drift(self) -> None:
        records = [
            record(f"t::leaks[{case}]", [], [], rss=(100 + 30 * case, 130 + 30 * case, 130 + 30 * case), heap=(30 * case, 30 + 30 * case))
            for case in range(6)
        ]
        records.append(record("t::last", [], [], rss=(280, 280, 130), heap=(180, 30)))
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        (finding,) = findings_of(report, "STEADY_GROWTH")
        assert finding.delta_mb == 180
        assert finding.growth_tests == 6

    def test_pages_the_allocator_kept_are_not_drift(self) -> None:
        # Resident memory climbs thirty a test and none of it is in use.
        records = [
            record(f"t::churn[{case}]", [], [], rss=(100 + 30 * case, 130 + 30 * case, 130 + 30 * case), heap=(50, 50), blocks=(1000, 1000))
            for case in range(6)
        ]
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        assert not findings_of(report, "STEADY_GROWTH")

    def test_drift_names_the_tests_that_carry_it_and_the_objects(self) -> None:
        records = [
            record(f"t::cached[{case}]", [], [], rss=(100 + 20 * case, 120 + 20 * case, 120 + 20 * case), heap=(20 * case, 20 + 20 * case), blocks=(1000, 1400))
            for case in range(6)
        ]
        records += [
            record(f"t::noisy[{case}]", [], [], rss=(220 + 10 * case, 230 + 10 * case, 230 + 10 * case), heap=(120 + 10 * case, 130 + 10 * case), blocks=(1000, 1100))
            for case in range(3)
        ]
        report = analyse(records, attributor, Thresholds(retained_mb=100, growth_tests=4))

        (finding,) = findings_of(report, "STEADY_GROWTH")
        assert finding.delta_mb == 150
        assert finding.growth_objects_per_test == 300
        assert any(line.startswith("Most of it during: t::cached (120 MB over 6 tests), t::noisy (30 MB over 3 tests)") for line in finding.evidence)
        assert any("--failure-profile-allocations" in line for line in finding.evidence)

    def test_drift_with_tracing_on_names_the_lines_holding_it(self) -> None:
        frames = [frame(TEST, 30, ""), frame(PRODUCT, 9, ""), frame(LIBRARY, 5, "")]
        records = [
            record(f"t::cached[{case}]", [], [], rss=(100 + 30 * case, 130 + 30 * case, 130 + 30 * case), heap=(30 * case, 30 + 30 * case))
            for case in range(6)
        ]
        background = record(None, [], frames)
        background["holders_session"] = [{"mb": 176.5, "frames": [0, 1, 2]}]
        report = analyse(records + [background], attributor, Thresholds(retained_mb=100, growth_tests=4))

        (finding,) = findings_of(report, "STEADY_GROWTH")
        assert any(
            line == "Held at the end of the worker: 176.5 MB allocated at encoder.py:5, called from imaging.py:9, test_screens.py:30."
            for line in finding.evidence
        )
        assert finding.frame is not None
        assert finding.frame.file == PRODUCT and finding.frame.owner == "product"
        assert finding.stack[0] == f'  File "{PRODUCT}", line 9 in ?'
        assert not any("--failure-profile-allocations" in line for line in finding.evidence)


class TestAllocationTracing:
    frames = [frame(TEST, 30, ""), frame(PRODUCT, 12, ""), frame(LIBRARY, 5, "")]

    def test_the_holders_at_the_peak_are_evidence_and_the_frame(self) -> None:
        entry = record("t::a", [], self.frames, rss=(100, 420, 110))
        entry["holders_peak"] = [{"mb": 300.5, "frames": [0, 1, 2]}]
        report = analyse([entry], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert any(
            line == "Held at the peak: 300.5 MB allocated at encoder.py:5, called from imaging.py:12, test_screens.py:30."
            for line in finding.evidence
        )
        assert finding.frame is not None and finding.frame.owner == "product"

    def test_holders_under_the_runtime_alone_leave_the_frame_unplaced(self) -> None:
        # tracemalloc saw the memory allocated under pytest's own frames and
        # the stdlib's. That is evidence, but naming re/__init__.py as the
        # frame would send the reader into the runtime.
        frames = [frame(RUNTIME, 167, ""), frame(f"{STDLIB}/re/__init__.py", 5, "")]
        entry = record("t::a", [], frames, rss=(100, 420, 110))
        entry["holders_peak"] = [{"mb": 300.5, "frames": [0, 1]}]
        report = analyse([entry], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert any(line == "Held at the peak: 300.5 MB allocated at __init__.py:5, called from python.py:167." for line in finding.evidence)
        assert finding.frame is None
        assert finding.stack == []
        assert not any(line.startswith("Look at:") for line in finding.evidence)

    def test_a_sampled_stack_outranks_the_holders_for_the_frame(self) -> None:
        frames = self.frames + [frame(PRODUCT, 40, "load_everything")]
        entry = record("t::a", [], frames, rss=(100, 420, 110), growth=[{"thread": "MainThread", "frames": [3], "mb": 300}])
        entry["holders_peak"] = [{"mb": 300.5, "frames": [0, 1, 2]}]
        report = analyse([entry], attributor)

        (finding,) = findings_of(report, "TRANSIENT_PEAK")
        assert finding.frame is not None and finding.frame.function == "load_everything"
        assert any(line.startswith("Held at the peak:") for line in finding.evidence)

    def test_traced_figures_replace_the_resident_ones(self) -> None:
        # Resident memory says 300 MB kept; the tracer says the test freed
        # everything and its own tables are what stayed.
        entry = record("t::a", [], [], rss=(100, 400, 400))
        entry["traced"] = {"before_mb": 0, "after_mb": 5, "peak_mb": 210, "tracer_mb": 250}
        report = analyse([entry], attributor)

        (finding,) = report.findings
        assert finding.verdict == "TRANSIENT_PEAK"
        assert finding.peak_mb == 210 and finding.delta_mb == 210
        assert any("Figures are from tracemalloc" in line and "250 MB" in line for line in finding.evidence)

    def test_traced_memory_kept_is_in_use_by_definition(self) -> None:
        entry = record("t::a", [], self.frames, rss=(100, 400, 400), heap=(0, 0))
        entry["traced"] = {"before_mb": 0, "after_mb": 150, "peak_mb": 150, "tracer_mb": 20}
        entry["holders_kept"] = [{"mb": 149.0, "frames": [0, 1, 2]}]
        report = analyse([entry], attributor)

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.delta_mb == 150
        assert any(line.startswith("Still held after the test: 149.0 MB allocated at encoder.py:5") for line in finding.evidence)
        assert not findings_of(report, "HEAP_NOT_RETURNED")

    def test_memory_speedscope_is_the_allocations_at_the_peak_in_bytes(self) -> None:
        from pytest_failure_instrumentation.profile.analysis import memory_speedscope

        entry = record("t::a", [], self.frames + [frame(TEST, 31, "test_x")])
        entry["memory_stacks"] = [{"frames": [0, 1, 2], "bytes": 4096}, {"frames": [3], "bytes": 512}]

        document = memory_speedscope(entry, "t::a")

        assert document is not None
        (profile,) = document["profiles"]
        assert profile["unit"] == "bytes"
        assert profile["samples"] == [[0, 1, 2], [3]]
        assert profile["weights"] == [4096, 512]
        assert profile["endValue"] == 4608
        assert document["shared"]["frames"][1] == {"name": "imaging.py:12", "file": PRODUCT, "line": 12}
        assert document["shared"]["frames"][3]["name"] == "test_x"
        assert memory_speedscope(record("t::b", [], []), "t::b") is None


class TestTracedRun:
    def test_a_traced_run_raises_memory_findings_and_no_cpu_ones(self) -> None:
        frames = [frame(PRODUCT, 14, "is_images_different")]
        entry = record("t::a", [stack([0], 8.0)], frames, rss=(100, 420, 110))
        entry["allocations"] = True
        entry["timeline"] = timeline(*busy(30, [0]))
        report = analyse([entry], attributor)

        assert report.allocations is True
        assert [finding.verdict for finding in report.findings] == ["TRANSIENT_PEAK"]
        # The ranking is still there for the terminal, cost and all.
        assert [cost.function for cost in report.functions] == ["is_images_different"]
