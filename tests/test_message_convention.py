"""Every incident renders to one shape, and this holds it.

The convention is in ``incidents.base`` and in the README under "How an
incident reads". In short: the first line says what happened in words and
ends with a ``[kind VERDICT, owner, severity]`` tag; every later line is a
measurement, what it means by construction, or a place to look; sentences
start with a capital and end with a full stop; there is no field vocabulary,
no bullet, nothing said twice. One real incident per kind goes through the
rules here, built by the builder that produces it in a run wherever that
builder needs nothing but files.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from pytest_failure_instrumentation.analysis import classify
from pytest_failure_instrumentation.analysis.attribution import Attributor
from pytest_failure_instrumentation.analysis.collection import CollectionTracker
from pytest_failure_instrumentation.capture.state import WorkerState
from pytest_failure_instrumentation.incidents import (
    collection,
    internal_error,
    profile,
    stack_server,
    stall,
    summary,
)
from pytest_failure_instrumentation.incidents.base import Frame, Incident
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident
from pytest_failure_instrumentation.profile import analysis

TAG = re.compile(r"^(?P<summary>[A-Za-z0-9].+?)   \[(?P<tag>[a-z_]+ [A-Z_0-9]+(, [a-z-]+){1,2}(, run-ending)?)\]$")

#: Names that are lowercase by convention and may open a sentence.
LOWERCASE_NAMES = ("pytest", "xdist", "glibc", "py-spy", "tracemalloc", "malloc_trim", "faulthandler")

#: Words that belong to the implementation, not to a reader.
FORBIDDEN = ("· ", "blamed on", "no stack;", "in flight", " beat ", "severity=", "owner=", "climb", "charged to")

FRAME = Frame(file="/srv/app/yourcore/engine.py", line=6, function="native_call", module="engine", owner="product")


def enriched(incident: Incident, *, owner: str = "product", severity: str = "critical", frame: Frame | None = FRAME) -> Incident:
    """What the engine fills in after the builder, without the engine."""
    incident.owner = owner
    incident.severity = severity
    incident.blamed_frame = frame
    incident.run_ending = incident.ends_this_run()
    return incident


def a_death() -> Incident:
    incident = WorkerDeathIncident(
        worker="gw1", worker_pid=805, exit_status=-11, exit_status_source="waitid",
        test_in_flight="test_api.py::test_thing", phase="call", tests_started=3, tests_finished=2,
        crash_stack=["Fatal Python error: Segmentation fault", "Current thread 0x1 (most recent call first):", '  File "/srv/app/yourcore/engine.py", line 6 in native_call'],
        crash_stack_age_seconds=0.2,
    )
    incident.verdict, incident.confidence, incident.evidence = classify.of(incident)
    return enriched(incident)


def a_kill() -> Incident:
    incident = WorkerDeathIncident(
        worker="gw2", worker_pid=901, exit_status=-9, exit_status_source="waitid",
        last_test="test_api.py::test_other", tests_started=4, tests_finished=4,
        rss_mb_at_death=3900, system_available_mb=120, cgroup_oom_kills_since_start=1,
    )
    incident.verdict, incident.confidence, incident.evidence = classify.of(incident)
    incident.suspect_owner = "customer-code"
    incident.suspect_basis = incident.suspect_basis_for("test_api.py")
    return enriched(incident, owner="unknown", severity="needs-triage", frame=None)


def a_stall(tmp_path: Path) -> Incident:
    events = tmp_path / "gw3.events"
    now = time.time()
    with events.open("w") as handle:
        handle.write(json.dumps({"event": "worker_start", "pid": os.getpid(), "time": now - 100}) + "\n")
        for offset in (60, 40, 20, 5):
            handle.write(json.dumps({"event": "heartbeat", "time": now - offset, "cpu_seconds": 1.0}) + "\n")
    state = WorkerState(tmp_path / "gw3.state", os.getpid())
    state.update(nodeid="test_api.py::test_waits", phase="call")
    incident = stall.build("gw3", tmp_path, silent_for=45.0, interval=5.0, stack_probe=False)
    assert incident is not None
    return enriched(incident, owner="customer-code", severity="informational", frame=None)


def a_mismatch(tmp_path: Path) -> Incident:
    tracker = CollectionTracker()
    tracker.record("gw0", ["test_a.py::test_one", "test_a.py::test_two", "test_b.py::test_three"])
    tracker.record("gw1", ["test_a.py::test_one", "test_a.py::test_two", "test_b.py::test_three"])
    tracker.record("gw2", ["test_a.py::test_one", "test_b.py::test_three"])
    incident = collection.build(tracker, tmp_path)
    incident.suspect_owner = "customer-code"
    incident.suspect_basis = incident.suspect_basis_for("test_a.py")
    return enriched(incident, owner="unknown", severity="needs-triage", frame=None)


def an_internal_error(tmp_path: Path) -> Incident:
    text = "Traceback (most recent call last):\n  File \"loadscope.py\", line 275, in _assign_work_unit\nKeyError: <WorkerController gw1>"
    incident = internal_error.build(text, tmp_path, distributed=False)
    return enriched(incident, owner="runtime", severity="high", frame=Frame(file="/x/loadscope.py", line=275, function="_assign_work_unit", module="loadscope", owner="runtime"))


def a_summary() -> Incident:
    incident = summary.build(exitstatus=0, seen={"abc": 2, "def": 1}, raised=2, suppressed=1, run_ending=1, distributed=True)
    return enriched(incident, owner="unknown", severity="informational", frame=None)


def a_stack_server() -> Incident:
    incident = stack_server.build("PORT_TAKEN", "127.0.0.1", 8080, "port 8080 is held by something that is not a stack server (Address already in use); pass --callstack-port with an unused port, or leave it off entirely and let one be drawn")
    return enriched(incident, owner="runtime", severity="informational", frame=None)


def profile_findings() -> list[Incident]:
    product = "/srv/product/imaging.py"
    frames = [f"{product}|14|compare", "/srv/tests/test_screens.py|30|test_screens"]
    records = [
        {
            "record": "test", "worker": "gw0", "nodeid": f"tests/test_screens.py::test_screens[{case}]",
            "wall_s": 10.0, "cpu_s": 6.0, "rss_before_mb": 100 + 200 * case, "rss_after_mb": 300 + 200 * case,
            "rss_peak_mb": 300 + 200 * case, "rss_at": {"call_start": 100 + 200 * case, "call_end": 300 + 200 * case},
            "heap_before_mb": 50, "heap_after_mb": 250 + 200 * case, "blocks_before": 1000, "blocks_after": 5000,
            "gc": {"seconds": 0.0, "collections": 0, "by_generation": [0, 0, 0]}, "cpu_weighted": True,
            "thread_clock": "thread-clock", "frames": frames,
            "stacks": [{"phase": "call", "thread": "MainThread", "background": False, "frames": [0, 1], "cpu_ns": int(6e9), "wall_ns": int(6e9), "samples": 300}],
            "growth": [{"thread": "MainThread", "frames": [0, 1], "mb": 200}],
            "native_threads": [], "timeline": [[100 * n, int(1e8), 300, "call", "MainThread", [0, 1]] for n in range(1, 60)],
        }
        for case in range(2)
    ]
    report = analysis.analyse(records, Attributor(("product",)), analysis.Thresholds(retained_mb=100))
    assert report.findings, "the hand-built records should cross a threshold"
    incidents = [profile.build(finding, "controller") for finding in report.findings]
    return [enriched(incident, owner="product", severity="informational") for incident in incidents]


@pytest.fixture
def every_kind(tmp_path: Path) -> list[Incident]:
    return [
        a_death(), a_kill(), a_stall(tmp_path), a_mismatch(tmp_path), an_internal_error(tmp_path),
        a_summary(), a_stack_server(), *profile_findings(),
    ]


def test_every_incident_renders_to_the_one_shape(every_kind: list[Incident]) -> None:
    kinds = {incident.kind for incident in every_kind}
    assert kinds >= {"worker_death", "worker_stall", "collection_mismatch", "internal_error", "run_summary", "stack_server_unavailable", "cpu_hotspot", "cpu_burst", "memory_profile"}
    for incident in every_kind:
        text = str(incident)
        lines = text.splitlines()
        first = lines[0]
        match = TAG.match(first)
        assert match, f"{incident.kind}: the first line must say what happened and end with a tag:\n{first}"
        assert match.group("tag").startswith(f"{incident.kind} {incident.verdict}"), first
        assert not first.startswith("["), f"{incident.kind}: the first line is a sentence, not a field dump"
        for lower in FORBIDDEN:
            assert lower not in text, f"{incident.kind}: {lower!r} is implementation vocabulary:\n{text}"
        body = [line[4:] for line in lines[1:]]
        assert all(line.startswith("    ") for line in lines[1:]), f"{incident.kind}: body lines are indented four spaces"
        for line in body:
            if line.startswith((" ", "\t")):
                continue  # a sub-row of a table: a diff line, a parameter sample
            assert line[0].isupper() or line[0].isdigit() or line.startswith(LOWERCASE_NAMES), f"{incident.kind}: a line starts with a capital:\n{line}"
            if not line.startswith("Look at:"):
                assert line.endswith("."), f"{incident.kind}: a line ends with a full stop:\n{line}"
        assert len(set(body)) == len(body), f"{incident.kind}: nothing is said twice:\n{text}"


def test_a_lead_says_where_the_owner_came_from(every_kind: list[Incident]) -> None:
    (kill,) = [incident for incident in every_kind if incident.verdict == "OOM_KILLED"]
    assert str(kill).splitlines()[1].strip() == (
        "No stack was captured; the owner is taken from the last test this worker finished, "
        "test_api.py; nothing was running when it died (customer-code)."
    )


def test_the_death_headline_names_worker_signal_test_and_frame(every_kind: list[Incident]) -> None:
    (death,) = [incident for incident in every_kind if incident.verdict == "NATIVE_CRASH"]
    assert str(death).splitlines()[0] == (
        "Worker gw1 crashed with SIGSEGV (segmentation fault in native code) while running "
        "test_api.py::test_thing (call), in native_call (engine.py:6)   "
        "[worker_death NATIVE_CRASH, product, critical]"
    )


def test_a_look_at_line_points_at_something_the_tool_has(every_kind: list[Incident]) -> None:
    for incident in every_kind:
        for line in incident.evidence:
            if line.startswith("Look at:"):
                target = line[len("Look at:"):].strip()
                assert target, line
                assert not re.search(r"\b(should|must|fix|change|set|make)\b", target.lower().split(",")[0].split(".")[0]) or "failure_" in target or "--" in target, (
                    f"{incident.kind}: a Look at line names a place, not a change:\n{line}"
                )
