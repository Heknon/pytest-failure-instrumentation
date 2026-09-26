"""OpenTelemetry metrics: what is counted, and that it survives how a product
sets its provider up.

The adapter's contract is mostly about *when*: a provider set before the run
or during it, a count made before any provider exists, a flush before a short
run is over. Those are checked in real runs, since the global provider can be
set once per process and this one's must not be spent. What is counted is
checked directly, against a provider handed over the way a product that does
not set the global one hands it.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk.metrics")

from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402

from pytest_failure_instrumentation.config import Settings  # noqa: E402
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident  # noqa: E402
from pytest_failure_instrumentation.incidents.summary import RunSummaryIncident  # noqa: E402
from pytest_failure_instrumentation.otel import METER, MetricsAdapter  # noqa: E402

from .conftest import INNER_CONFTEST, needs_xdist  # noqa: E402
from .test_sampling import evidence  # noqa: E402
from .test_single_process import LEAVES_WITHOUT_FINISHING, SUITE  # noqa: E402


def points(reader):
    """Every data point of this plugin's meter the reader collects now, as
    (name, attributes, value). Its own meter only: some SDK releases report
    metrics about themselves through the same reader."""
    found = []
    data = reader.get_metrics_data()
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            if scope.scope.name != METER:
                continue
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    found.append((metric.name, dict(point.attributes), point.value))
    return found


def named(found, name):
    return [(attributes, value) for metric, attributes, value in found if metric == name]


@pytest.fixture
def adapter(tmp_path):
    """An adapter over a run directory, reporting through a provider handed to
    it rather than the global one."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    engine = SimpleNamespace(stop=threading.Event(), directory=tmp_path / "run", session_id="s-1")
    built = MetricsAdapter(engine, Settings(otel_meter_provider=provider))
    yield built, reader, engine
    provider.shutdown()


# -- what is counted -------------------------------------------------------


def test_incidents_are_counted_by_finding_and_by_the_test_s_tags(adapter):
    metrics, reader, _ = adapter
    tagged = WorkerDeathIncident(
        worker="gw0", verdict="SIGKILLED", owner="product", severity="critical",
        test_attributes={"team": "payments", "owner": "someone-else"},
    )
    metrics.pytest_failure_incident(tagged)
    metrics.pytest_failure_incident(tagged)
    metrics.pytest_failure_incident(WorkerDeathIncident(worker="gw1", verdict="OOM_KILLED"))

    counted = named(points(reader), "pytest.failure.incidents")
    by_verdict = {attributes["pytest.failure.verdict"]: (attributes, value) for attributes, value in counted}
    attributes, value = by_verdict["SIGKILLED"]
    assert value == 2
    assert attributes == {
        "pytest.failure.kind": "worker_death",
        "pytest.failure.verdict": "SIGKILLED",
        "pytest.failure.owner": "product",
        "pytest.failure.severity": "critical",
        "pytest.failure.run_ending": False,
        "test.tag.team": "payments",
        # A tag named like a finding sits apart from it rather than over it.
        "test.tag.owner": "someone-else",
    }
    assert by_verdict["OOM_KILLED"][1] == 1
    assert not any(key.startswith("test.tag.") for key in by_verdict["OOM_KILLED"][0])


def test_a_run_summary_is_a_run_and_not_an_incident(adapter):
    metrics, reader, _ = adapter
    metrics.pytest_failure_incident(RunSummaryIncident(worker="main", exitstatus=1, distributed=True))
    found = points(reader)
    assert named(found, "pytest.failure.incidents") == []
    assert named(found, "pytest.failure.runs") == [
        ({"pytest.failure.exit_status": 1, "pytest.failure.distributed": True}, 1)
    ]


def test_workers_are_reported_from_the_run_s_own_files(adapter):
    metrics, reader, engine = adapter
    root = evidence(engine.directory, {"gw0": [1.0, 3.0, 5.0, 7.0], "gw1": [5.0, 5.0, 5.0, 5.0]})
    record = json.loads((root / "gw0.state").read_bytes().rstrip(b"\x00"))
    record["test_attributes"] = {"team": "payments"}
    raw = json.dumps(record).encode()
    (root / "gw0.state").write_bytes(raw + b"\x00" * (5120 - len(raw)))

    found = points(reader)
    rss = {attributes["pytest.failure.worker"]: (attributes, value)
           for attributes, value in named(found, "pytest.failure.worker.memory.rss")}
    assert rss["gw0"] == ({"pytest.failure.worker": "gw0", "test.tag.team": "payments"}, 40 * 1024 * 1024)
    assert rss["gw1"][0] == {"pytest.failure.worker": "gw1"}
    assert {attributes["pytest.failure.worker"] for attributes, _ in named(found, "pytest.failure.worker.cpu.rate")} == {"gw0", "gw1"}
    assert len(named(found, "pytest.failure.worker.heartbeat.age")) == 2
    statuses = dict((attributes["pytest.failure.worker.status"], value)
                    for attributes, value in named(found, "pytest.failure.workers"))
    assert statuses == {"working": 1, "blocked": 1}


def test_a_dead_worker_s_last_figures_are_not_held_on_a_dashboard(adapter, tmp_path):
    """xdist replaces a dead worker under a new name, and its row keeps the
    memory of its last beat: reported, it would stand for the rest of the run."""
    metrics, reader, engine = adapter
    evidence(engine.directory, {"gw0": [1.0, 3.0, 5.0]}, pid=2**22 + 12345)
    found = points(reader)
    assert named(found, "pytest.failure.worker.memory.rss") == []
    assert [attributes for attributes, _ in named(found, "pytest.failure.workers")] == [
        {"pytest.failure.worker.status": "gone"}
    ]


def test_nothing_is_read_once_the_session_is_finishing_and_counts_still_are(adapter):
    metrics, reader, engine = adapter
    evidence(engine.directory, {"gw0": [1.0, 3.0, 5.0]})
    metrics.pytest_failure_incident(WorkerDeathIncident(worker="gw0"))
    engine.stop.set()
    found = points(reader)
    assert named(found, "pytest.failure.worker.memory.rss") == []
    assert named(found, "pytest.failure.workers") == []
    assert len(named(found, "pytest.failure.incidents")) == 1


def test_a_count_that_fails_does_not_cost_the_product_its_incident(adapter, capsys):
    """The adapter shares one hook call with the product's own implementation,
    and pluggy stops at the first one that raises."""
    metrics, reader, _ = adapter
    metrics.pytest_failure_incident(SimpleNamespace(kind="worker_death"))
    assert "could not count an incident" in capsys.readouterr().out
    assert named(points(reader), "pytest.failure.incidents") == []


# -- how a product sets its provider up, in real runs ----------------------

#: A product's setup: the global provider, set on the controller only, with a
#: reader this conftest polls while the run goes and an exporter that only a
#: flush can reach - its interval is ten minutes, and exit is not flushed.
PROVIDER = '''
import json
import threading

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)


def _points(data):
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            if scope.scope.name != "pytest_failure_instrumentation":
                continue  # an SDK that reports on itself, as some releases do
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    yield [metric.name, dict(point.attributes), point.value]


class Exported(MetricExporter):
    def __init__(self):
        super().__init__()

    def export(self, data, timeout_millis=10_000, **kwargs):
        with open("exported.jsonl", "a") as handle:
            for point in _points(data):
                handle.write(json.dumps(point) + "\\n")
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        pass


POLLED = InMemoryMetricReader()
STOP = threading.Event()


def _poll():
    while not STOP.wait(0.2):
        data = POLLED.get_metrics_data()
        if data is None:
            continue
        with open("polled.jsonl", "a") as handle:
            for point in _points(data):
                handle.write(json.dumps(point) + "\\n")


def pytest_configure(config):
    if hasattr(config, "workerinput"):
        return
    metrics.set_meter_provider(MeterProvider(
        metric_readers=[POLLED, PeriodicExportingMetricReader(Exported(), export_interval_millis=600_000)],
        shutdown_on_exit=False,
    ))
    threading.Thread(target=_poll, daemon=True).start()


def pytest_unconfigure(config):
    STOP.set()
'''

TAGGING = '''

def pytest_failure_test_attributes(item):
    return {"team": "platform"}
'''


def read(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def metrics_ini(*extra: str) -> str:
    return "\n".join([
        "[pytest]", "failure_otel = true", "failure_heartbeat_interval = 1",
        "failure_test_attributes = team", "markers =", "    team(name): owner", *extra,
    ]) + "\n"


@needs_xdist
@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_a_distributed_run_is_counted_and_flushed_before_it_ends(runner):
    runner.pytester.makeconftest(INNER_CONFTEST + PROVIDER + TAGGING)
    runner.pytester.makeini(metrics_ini())
    runner.pytester.makepyfile(test_mixed="""
        import os
        import signal
        import time

        import pytest


        @pytest.fixture
        def dies():
            os.kill(os.getpid(), signal.SIGKILL)


        @pytest.mark.team("payments")
        def test_dies_in_setup(dies):
            pass


        def test_takes_a_while():
            time.sleep(3)
    """)
    runner.run("-n", "2")
    root = runner.pytester.path

    exported = read(root / "exported.jsonl")
    incidents = [(attributes, value) for name, attributes, value in exported if name == "pytest.failure.incidents"]
    assert [(attributes["pytest.failure.verdict"], attributes["test.tag.team"], value)
            for attributes, value in incidents] == [("SIGKILLED", "payments", 1)]
    assert [(attributes, value) for name, attributes, value in exported if name == "pytest.failure.runs"] == [
        ({"pytest.failure.exit_status": 1, "pytest.failure.distributed": True}, 1)
    ]
    # Read while the run went: the worker in the long test, with its tags.
    polled = read(root / "polled.jsonl")
    assert any(
        name == "pytest.failure.worker.memory.rss" and attributes.get("test.tag.team") == "platform"
        for name, attributes, _ in polled
    ), polled[-20:]
    assert "no OpenTelemetry MeterProvider" not in runner.result.stdout.str()


def test_a_provider_set_after_an_incident_still_counts_it(runner):
    """The reason every instrument is observable. The incidents of a killed
    run are recovered as the session starts - before a conftest below the
    rootdir is loaded, so before this one's provider exists. A counter's add
    at that moment is dropped without a word; a tally read at collection is
    not."""
    runner.pytester.makepyfile(test_gone=LEAVES_WITHOUT_FINISHING)
    runner.run("test_gone.py", "-o", "failure_kill_trace=false")
    (runner.pytester.path / "test_gone.py").unlink()

    runner.pytester.makeini(metrics_ini())
    below = runner.pytester.mkdir("below")
    (below / "conftest.py").write_text(PROVIDER)
    (below / "test_after.py").write_text(SUITE)
    runner.run()

    exported = read(runner.pytester.path / "exported.jsonl")
    assert [(attributes["pytest.failure.kind"], value) for name, attributes, value in exported
            if name == "pytest.failure.incidents"] == [("worker_death", 1)]


@needs_xdist
def test_a_provider_the_controller_never_loads_is_said(runner):
    """Under xdist only the workers collect, so a conftest below the rootdir
    never runs on the controller that reports - and an empty dashboard would
    be read as a quiet week."""
    runner.pytester.makeini(metrics_ini())
    below = runner.pytester.mkdir("below")
    (below / "conftest.py").write_text(PROVIDER)
    (below / "test_after.py").write_text(SUITE)
    runner.run("-n", "1")
    assert "no OpenTelemetry MeterProvider" in runner.result.stdout.str()
    assert not (runner.pytester.path / "exported.jsonl").exists()


def test_without_opentelemetry_only_the_metrics_are_off(runner):
    runner.pytester.makeconftest(INNER_CONFTEST + '''
import sys


def pytest_configure(config):
    sys.modules["opentelemetry"] = None
''')
    runner.pytester.makeini(metrics_ini())
    runner.pytester.makepyfile(test_fine=SUITE)
    incidents = runner.run()
    said = runner.result.stdout.str() + runner.result.stderr.str()
    assert "install pytest-failure-instrumentation[otel]" in said
    runner.only(incidents, "run_summary")
