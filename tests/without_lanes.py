"""What a run without lanes leaves behind, reduced to what must never change.

Support for pytest-threadlanes adds keys to state records, heartbeats, rows and
incidents - and only ever where a lane is running. A run without lanes has to
write exactly what 0.13.1 wrote: the same files, the same keys in the same
order, the same values. This module turns a finished run's evidence into that
comparison, with the values that differ between any two runs - clocks, pids,
ids, ages, resident memory - replaced by a placeholder, and everything else
kept as written.

It imports nothing 0.13.1 did not have, so the expected value in
``evidence_without_lanes.json`` was produced by running :func:`normalised`
against 0.13.1 itself, for the suite in ``BASELINE_SUITE`` run in each of
``MODES`` - and can be again, with :func:`main`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: The suite whose evidence is compared: a pass, a failure and a skip, over
#: two modules, so every phase transition and both counters move.
BASELINE_SUITE = {
    "test_first": """
import pytest


def test_passes():
    pass


def test_fails():
    assert False, "on purpose"
""",
    "test_second": """
import pytest


def test_skips():
    pytest.skip("on purpose")
""",
}

#: The runs compared, by name: a run with no workers, one xdist worker, and a
#: run with no workers keeping its stderr - the one facility whose handling
#: of fd 2 lanes change.
MODES = {
    "single": [],
    "n1": ["-n", "1"],
    "tee": ["-o", "failure_capture_output=true"],
}

#: The incident kinds whose models gained a field for lanes, and the keys a
#: dump of each had in 0.13.1 - which, without lanes, it must still have.
INCIDENT_MODELS = {
    "worker_death": "pytest_failure_instrumentation.incidents.death:WorkerDeathIncident",
    "worker_stall": "pytest_failure_instrumentation.incidents.stall:WorkerStallIncident",
}

VOLATILE = "<volatile>"

#: Fields whose values differ between any two runs of the same suite.
_VOLATILE_KEYS = {
    "time", "pid", "created_at", "run_id", "started_at", "updated_at",
    "observed_at", "state_age_s", "heartbeat_age_s", "rss_mb", "cpu_rate",
    "session", "session_id", "directory",
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
    """Each event's keys, in order, and the order of the rare events.

    Heartbeats are counted by the clock, so only their shape is compared; the
    values of the rest describe the machine (its Python, its tracer), which is
    the same for both sides of a comparison but not across machines.
    """
    shapes: dict[str, list[list[str]]] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        name = str(event.get("event"))
        keys = list(event)
        if keys not in shapes.setdefault(name, []):
            shapes[name].append(keys)
        if name != "heartbeat":
            order.append(name)
    return {"shapes": shapes, "order": order}


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
    events = {path.name: _events(path) for path in sorted(directory.glob("*.events"))}
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
        "events": events,
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


def main() -> None:
    """Print the normalised evidence of the run under a base directory, or the
    incidents' keys.

    ``python tests/without_lanes.py <failure_directory>`` and
    ``python tests/without_lanes.py --incidents``: how the expected value in
    ``evidence_without_lanes.json`` was produced, against 0.13.1.
    """
    import sys

    if sys.argv[1] == "--incidents":
        print(json.dumps(incident_keys()))
        return
    base = Path(sys.argv[1])
    runs = sorted(path for path in base.iterdir() if path.is_dir())
    assert len(runs) == 1, runs
    print(json.dumps(normalised(runs[0])))


if __name__ == "__main__":
    main()
