"""The profiler against real runs.

Each scenario is a suite written the way the problem is usually written the
first time, run under ``--profile`` in a subprocess through the shared runner,
and read back as the incidents it raised. The thresholds are lowered so that
a scenario can prove its point in a second or two rather than a minute.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import Runner, needs_xdist

PROFILE_INI = """
[pytest]
failure_packages = victim, product
failure_profile_cpu_share = 5
failure_profile_retained_mb = 40
"""

PRODUCT_MODULE = '''
"""The product under test: one hot loop, one library call, one poller."""
import hashlib
import json
import threading


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
    def __init__(self):
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="status-poller", daemon=True)

    def run(self):
        payload = b"x" * 400_000
        while not self.stop.wait(0.001):
            hashlib.sha256(payload).hexdigest()
'''


def profiled(runner: Runner, *arguments: str, timeout: float = 120.0) -> list[Any]:
    runner.pytester.makeini(PROFILE_INI)
    (runner.pytester.path / "product.py").write_text(PRODUCT_MODULE, encoding="utf-8")
    return runner.run("--profile", "-p", "no:cacheprovider", *arguments, timeout=timeout)


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
        assert incident.hottest_lines[0].line in (13, 14, 15, 16)
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
                document = {"rows": [{"id": i, "name": f"row-{i}", "value": i * 1.5} for i in range(60000)]}
                for _ in range(4):
                    render(document)
            """
        )
        incidents = profiled(runner)

        incident = named(incidents, "render")
        assert incident.verdict == "LIBRARY_CALL"
        assert incident.owner == "product"
        assert incident.below is not None
        assert incident.below.file.endswith("json/encoder.py")
        assert incident.below.owner == "runtime"
        assert incident.self_share_percent < 50

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

            def test_one(poller):
                time.sleep(1.2)

            def test_two(poller):
                time.sleep(1.2)
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

        # Deliberately without --failure-instrumentation: --profile is a request
        # for the plugin that takes the profile, like --callstack-port.
        result = runner.pytester.runpytest_subprocess("--profile", "-p", "no:cacheprovider")

        result.stdout.fnmatch_lines(["*failure-instrumentation profile*", "*no findings*"])
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
        assert body.suspect_owner == "customer-code"
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
        assert incident.verdict in ("TRANSIENT_PEAK", "HEAP_NOT_RETURNED")
        assert incident.nodeid == "test_peak.py::test_peak"
        assert incident.peak_mb >= incident.before_mb + 100

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
        assert "parametrisation of test_leak.py::test_leaks" in growth[0].evidence[-1]
        assert not [incident for incident in memory(incidents) if incident.verdict == "RETAINED_AFTER_TEST"]


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
        assert "test_screens.py_test_hot.speedscope.json" in profiles
        assert "background-main.speedscope.json" in profiles
        assert not any("test_cold" in name for name in profiles)
        document = json.loads(
            next((runner.pytester.path / ".pytest-failures").glob("run-*/profiles/test_screens.py_test_hot.speedscope.json")).read_text()
        )
        main = next(profile for profile in document["profiles"] if profile["name"].startswith("MainThread"))
        assert main["unit"] == "nanoseconds"
        assert sum(main["weights"]) > 0
        assert any(frame["name"] == "compare_pixels" for frame in document["shared"]["frames"])


class TestSettings:
    def test_profile_settings_reach_a_worker_and_round_trip(self) -> None:
        from pytest_failure_instrumentation.config import Settings

        settings = Settings(profile=True, profile_interval=0.05, profile_cpu_share=2.5, profile_retained_mb=64)
        copied = Settings.from_payload(settings.as_payload(), worker_count=4)

        assert copied.profile is True
        assert copied.profile_interval == 0.05
        assert copied.profile_cpu_share == 2.5
        assert copied.profile_retained_mb == 64

    def test_the_interval_has_a_floor(self) -> None:
        from pytest_failure_instrumentation.config import MIN_PROFILE_INTERVAL, Settings

        assert Settings(profile_interval=0.0).profile_interval == MIN_PROFILE_INTERVAL
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
