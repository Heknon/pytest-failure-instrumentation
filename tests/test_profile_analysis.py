"""The profile analysis against records built by hand.

Every rule in :mod:`pytest_failure_instrumentation.profile.analysis` is a
threshold over numbers a sampler wrote, so each one is checked here with the
numbers chosen to sit on either side of it - no sampling, no clock, no
subprocess. The integration tests in test_profile.py check that a real run
produces records these rules fire on.
"""

from __future__ import annotations

from typing import Any

from pytest_failure_instrumentation.analysis.attribution import Attributor
from pytest_failure_instrumentation.profile import analysis
from pytest_failure_instrumentation.profile.analysis import Thresholds, analyse, speedscope

PRODUCT = "/srv/product/imaging.py"
TEST = "/srv/tests/test_screens.py"
LIBRARY = "/usr/lib/python3.11/json/encoder.py"
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
        assert any("+160 MB more in use" in line for line in finding.evidence)

    def test_memory_kept_by_a_fixture_names_setup(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(100, 260, 260), heap=(10, 170),
                    rss_at={"setup_start": 100, "setup_end": 258, "call_start": 258, "call_end": 260, "teardown_start": 260, "teardown_end": 260})],
            attributor,
        )

        (finding,) = findings_of(report, "RETAINED_AFTER_TEST")
        assert finding.phase == "setup"
        assert any("fixture" in line for line in finding.evidence)

    def test_memory_kept_but_freed_is_the_allocator_not_a_leak(self) -> None:
        report = analyse(
            [record("t::a", [], [], rss=(100, 400, 350), heap=(10, 12), blocks=(1000, 1200))],
            attributor,
        )

        (finding,) = findings_of(report, "HEAP_NOT_RETURNED")
        assert finding.delta_mb == 250
        assert not findings_of(report, "RETAINED_AFTER_TEST")

    def test_without_heap_readings_kept_memory_is_still_retained(self) -> None:
        report = analyse([record("t::a", [], [], rss=(100, 260, 260))], attributor)

        assert [finding.verdict for finding in report.findings] == ["RETAINED_AFTER_TEST"]

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
            "300 MB of the 320 MB climb happened under imaging.py:14 in load_everything "
            "(product), called from test_screens.py:30 in test_export"
        )
        assert any(expected in line for line in finding.evidence)

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
        assert any("4100 MB of it was still there" in line for line in finding.evidence)

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
        assert "understates" in finding.evidence[0]


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
        assert any("parametrisation of t::leaks" in line for line in finding.evidence)
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
