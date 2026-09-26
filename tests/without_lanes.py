"""What a run without lanes leaves behind, reduced to what must never change.

Support for pytest-threadlanes adds keys to state records, rows, samples and
incidents - and only ever where a lane is running. A run without lanes has to
write exactly what 0.13.1 wrote: the same files, the same keys in the same
order, the same values, the same slot bytes and the same captured stderr. This
module turns a finished run's evidence into that comparison, with the values
that differ between any two runs - clocks, pids, ids, ages, resident memory -
or between two machines - the interpreter, the memory limit, what the kernel
lets a tracer do - replaced by a placeholder, and everything else kept as
written.

It imports nothing 0.13.1 did not have, so ``evidence_without_lanes.json`` is
produced by running :func:`main` against 0.13.1 itself, for the suite in
``BASELINE_SUITE`` run in each of ``MODES``; never against the code under
test, whose output it exists to check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: The suite whose evidence is compared: a pass, a failure and a skip, over
#: two modules, so every phase transition and both counters move; writes to
#: fd 2, so the stderr tee has something to keep; and one test that outlasts
#: a heartbeat, so a beat names a test.
BASELINE_SUITE = {
    "test_first": """
import os
import time

import pytest


def test_passes():
    os.write(2, b"written to fd 2 by test_passes\\n")


def test_fails():
    os.write(2, b"written to fd 2 by test_fails\\n")
    assert False, "on purpose"


def test_outlasts_a_beat():
    time.sleep(2.5)
""",
    "test_second": """
import pytest


def test_skips():
    pytest.skip("on purpose")
""",
}

#: Every run beats once a second, so a beat lands inside the long test.
COMMON = ["-o", "failure_heartbeat_interval=1"]

#: The runs compared, by name: a run with no workers, one xdist worker, and a
#: run with no workers keeping its stderr - the one facility whose handling
#: of fd 2 lanes change.
MODES = {
    "single": [*COMMON],
    "n1": [*COMMON, "-n", "1"],
    "tee": [*COMMON, "-o", "failure_capture_output=true"],
}

#: The incident kinds whose models gained a field for lanes. Their dump keys
#: are compared as a whole, since a field only a lanes run fills must not
#: appear in a dump without lanes at all.
INCIDENT_MODELS = {
    "worker_death": "pytest_failure_instrumentation.incidents.death:WorkerDeathIncident",
    "worker_stall": "pytest_failure_instrumentation.incidents.stall:WorkerStallIncident",
}

#: The fields 0.14.0 declares for lanes, by model: the only differences its
#: JSON schemas may have from 0.13.1's - see :func:`schemas`.
LANE_PROPERTIES = {
    "WorkerDeathIncident": {"lanes_in_flight"},
    "WorkerStallIncident": {"lanes_in_flight"},
    "SampledWorker": {"process", "thread_name", "thread_id"},
    "Worker": {"process", "thread_name", "thread_id"},
    "ResourceProcess": {"lanes_running"},
}

VOLATILE = "<volatile>"

#: Fields whose values differ between any two runs of the same suite, or
#: between two machines running it.
_VOLATILE_KEYS = {
    "time", "pid", "created_at", "run_id", "started_at", "updated_at",
    "observed_at", "state_age_s", "heartbeat_age_s", "rss_mb", "cpu_rate",
    "session", "session_id", "directory",
    # Events: the machine, not the run.
    "python", "platform", "executable", "capabilities", "traceable_by_parent",
    "limit_mb", "limit_source", "share_mb", "threshold_mb", "threshold_source",
    "controller_witness", "signal_trace", "cpu_seconds",
    # And, in a row, the test clocks, which a live row would carry.
    "phase_started", "test_started",
}


def _record(record: Any) -> Any:
    """A record with its volatile values replaced, keys and order kept."""
    if not isinstance(record, dict):
        return record
    kept = []
    for key, value in record.items():
        if key in _VOLATILE_KEYS:
            value = VOLATILE
        elif key == "why" and isinstance(value, str):
            value = re.sub(r"[0-9]+(\.[0-9]+)?", "N", value)
        elif isinstance(value, dict):
            value = _record(value)
        kept.append([key, value])
    return kept


def _events(path: Path) -> dict[str, Any]:
    """Every event but the beats, as written; and what the beats named.

    The beats are counted by the clock, so how many there are is not
    compared: their shapes are, and which test and phase they named while the
    long test's call was running - which is the one moment a beat is sure to
    land in.
    """
    shapes: dict[str, list[list[str]]] = {}
    written: list[Any] = []
    named: set[tuple[Any, Any]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        name = str(event.get("event"))
        keys = list(event)
        if keys not in shapes.setdefault(name, []):
            shapes[name].append(keys)
        if name == "heartbeat":
            if event.get("phase") == "call":
                named.add((event.get("nodeid"), event.get("phase")))
        else:
            written.append(_record(event))
    return {"shapes": shapes, "events": written, "beats_named": sorted(named)}


def _slot(path: Path) -> dict[str, Any]:
    """A state slot's bytes, as far as they are the same on every run: its
    length, and the record up to its clock."""
    raw = path.read_bytes()
    return {"length": len(raw), "prefix": raw[: raw.find(b'"time"')].decode("utf-8")}


def normalised(directory: Path) -> dict[str, Any]:
    """One run directory's evidence, as it must read without lanes."""
    from pytest_failure_instrumentation import topology
    from pytest_failure_instrumentation.capture.state import read_state
    from pytest_failure_instrumentation.sampling import WorkerSampler

    files = sorted(path.name for path in directory.iterdir())
    states = {
        path.name: _record(read_state(path))
        for path in sorted(directory.glob("*.state"))
    }
    slots = {path.name: _slot(path) for path in sorted(directory.glob("*.state"))}
    events = {path.name: _events(path) for path in sorted(directory.glob("*.events"))}
    outputs = {
        path.name: path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(directory.glob("*.output"))
    }
    described = topology.run(directory) or {}
    run = _record({key: value for key, value in described.items() if key != "workers"})
    rows = [_record(row) for row in described.get("workers", [])]
    sample = WorkerSampler(directory, session_id="s").sample().model_dump(mode="json")
    sampled = [_record(row) for row in sample.pop("workers")]
    schedule_path = directory / "schedule.json"
    schedule = (
        _record(json.loads(schedule_path.read_text(encoding="utf-8")))
        if schedule_path.exists()
        else None
    )
    return {
        "files": files,
        "states": states,
        "slots": slots,
        "events": events,
        "outputs": outputs,
        "run": run,
        "rows": rows,
        "sample": _record(sample),
        "sampled": sampled,
        "schedule": schedule,
    }


def incident_keys() -> dict[str, list[str]]:
    """The keys a dump of each model in :data:`INCIDENT_MODELS` has, in order."""
    import importlib

    keys = {}
    for kind, path in INCIDENT_MODELS.items():
        module, name = path.split(":")
        model = getattr(importlib.import_module(module), name)
        keys[kind] = list(json.loads(model(worker="gw0").model_dump_json()))
    return keys


#: A resource sample as the server serves it, for a process without lanes.
RESOURCE_BATCH = {
    "sequence": 1, "observed_at": 1.0, "elapsed_s": 0.5,
    "host": {"metrics": {"cpu_cores": 1.0}}, "cgroup": {},
    "processes": [{
        "pid": 12, "created_at": 1.0, "name": "python", "parent_pid": 1,
        "worker": "gw0", "role": "worker", "worker_exited": False,
        "nodeid": "t.py::test_a", "nodeid_hash": "h", "phase": "call",
        "observed_at": 1.0, "metrics": {"rss_mb": 10.0}, "unavailable": {},
    }],
}


#: And a ``/workers`` answer without lanes.
WORKERS_SNAPSHOT = {
    "served_by": {"service": "s", "pid": 1}, "observed_at": 1.0,
    "runs": [{
        "session": "run-1", "run_id": "r", "controller": {"pid": 1, "alive": True},
        "workers": [{
            "worker": "gw0", "pid": 12, "nodeid": "t.py::test_a", "nodeid_elided": False,
            "nodeid_hash": "h", "phase": "call", "status": "blocked", "why": "w",
            "tests_started": 1, "tests_finished": 0, "cpu_rate": 0.0,
        }],
    }],
}


def client_dumps() -> dict[str, Any]:
    """The client's models, parsed from a payload without lanes and dumped:
    a consumer re-serving them must serve what it did. Empty without httpx."""
    try:
        from pytest_failure_instrumentation.client import ResourceBatch, WorkersSnapshot
    except ImportError:
        return {}
    parsed = ResourceBatch.model_validate(RESOURCE_BATCH)
    workers = WorkersSnapshot.model_validate(WORKERS_SNAPSHOT)
    return {
        "resources": json.loads(parsed.model_dump_json()),
        "workers": json.loads(workers.model_dump_json()),
        "workers_python": workers.model_dump(),
    }


def schemas() -> dict[str, Any]:
    """The JSON schemas of the payloads, in both modes, with the properties
    0.14.0 declares for lanes taken out - so what is left must be 0.13.1's.

    The client's models only where httpx is installed; the key is absent
    otherwise, and the comparison skips it.
    """
    from pytest_failure_instrumentation.incidents import registry
    from pytest_failure_instrumentation.sampling import WorkerSample

    found: dict[str, Any] = {
        "incidents": registry._adapter.json_schema(mode="validation"),
        "incidents_serialized": registry._adapter.json_schema(mode="serialization"),
        "sample": WorkerSample.model_json_schema(mode="validation"),
        "sample_serialized": WorkerSample.model_json_schema(mode="serialization"),
    }
    try:
        from pytest_failure_instrumentation.client import ResourceHistory, WorkersSnapshot
    except ImportError:
        pass
    else:
        for name, model in (("workers", WorkersSnapshot), ("resources", ResourceHistory)):
            found[f"client_{name}"] = model.model_json_schema(mode="validation")
            found[f"client_{name}_serialized"] = model.model_json_schema(mode="serialization")
    return _without_lane_properties(found)


def _without_lane_properties(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_without_lane_properties(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned = {key: _without_lane_properties(value) for key, value in schema.items()}
    for model, names in LANE_PROPERTIES.items():
        definition = cleaned.get("$defs", {}).get(model)
        if definition is None and cleaned.get("title") == model:
            definition = cleaned
        if isinstance(definition, dict):
            for name in names:
                definition.get("properties", {}).pop(name, None)
    return cleaned


def main() -> None:
    """Print the normalised evidence of the run under a base directory, or the
    incidents' keys and the schemas.

    ``python tests/without_lanes.py <failure_directory>`` and
    ``python tests/without_lanes.py --models``, run against 0.13.1.
    """
    import sys

    if sys.argv[1] == "--models":
        import pydantic

        print(json.dumps({
            "incidents": incident_keys(),
            "schemas": schemas(),
            "client_dumps": client_dumps(),
            # A schema is pydantic's rendering as much as the model's.
            "pydantic": pydantic.VERSION,
        }))
        return
    base = Path(sys.argv[1])
    runs = sorted(path for path in base.iterdir() if path.is_dir())
    assert len(runs) == 1, runs
    print(json.dumps(normalised(runs[0])))


if __name__ == "__main__":
    main()
