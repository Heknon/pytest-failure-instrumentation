"""Reporting must preserve IDs and filenames containing undecodable bytes."""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from pytest_failure_instrumentation import Settings
from pytest_failure_instrumentation.analysis import collection, fingerprint
from pytest_failure_instrumentation.capture.file_resources import Scanner
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident
from pytest_failure_instrumentation.incidents.engine import IncidentEngine

from .test_profile_analysis import frame, record, stack


def test_collection_digest_preserves_surrogates_and_existing_identities():
    normal = "test_café.py::test_ok"
    assert collection.digest_of([normal]) == hashlib.sha1(normal.encode() + b"\0").hexdigest()[:12]
    incident = WorkerDeathIncident(worker="gw0", test_in_flight=normal)
    parts = [*incident.fingerprint_parts(), "no-phase", normal.split("::")[0]]
    assert fingerprint.of(incident, None) == hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
    tracker = collection.CollectionTracker()
    tracker.record("gw0", ["test_\udcff.py::test_ok"])
    tracker.record("gw1", ["test_\udcfe.py::test_ok"])
    assert tracker.has_mismatch
    assert tracker.summarise()["variant_count"] == 2


@pytest.mark.parametrize("with_frame", [False, True])
def test_distinct_surrogate_incidents_are_delivered_and_recurrences_deduplicated(pytester, with_frame):
    engine = IncidentEngine(pytester.parseconfig(), Settings(directory=pytester.path / "evidence"))
    received = []
    engine.config = SimpleNamespace(hook=SimpleNamespace(
        pytest_failure_incident=lambda incident: received.append(incident)),
        pluginmanager=engine.config.pluginmanager)
    for suffix in ("\udcff", "\udcfe", "\udcff"):
        path = f"test_{suffix}.py"
        dump = ["Fatal Python error: Segmentation fault",
                f'  File "{path}", line 1 in test_ok'] if with_frame else []
        engine.raise_incident(WorkerDeathIncident(worker="gw0", verdict="NATIVE_CRASH",
            test_in_flight=f"{path}::test_ok", crash_stack=dump))
    assert len(received) == 2
    assert all(incident.fingerprint for incident in received)
    assert not any("Enrichment failed" in line for incident in received for line in incident.evidence)
    assert all(bool(incident.blamed_frame) == with_frame for incident in received)
    assert engine.suppressed == 1


def test_profile_export_keeps_surrogate_and_subsequent_records(pytester):
    engine = IncidentEngine(pytester.parseconfig(), Settings(directory=pytester.path / "evidence"))
    engine.directory.mkdir(parents=True, exist_ok=True)
    nodeids = ["test_\udcff.py::test_hot", "test_\udcfe.py::test_hot", "test_plain.py::test_hot"]
    records = []
    for nodeid in nodeids:
        item = record(nodeid, [stack([0], 1.0)], [frame("test_profile.py", 1, "test_hot")])
        item["memory_stacks"] = [{"frames": [0], "bytes": 4096}]
        records.append(item)
    (engine.directory / "main.profile.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    engine._report_profile()
    documents = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (engine.directory / "profiles").glob("*.json")]
    assert len(documents) == 6
    for nodeid in nodeids:
        matches = [document for document in documents if document["name"] == nodeid]
        assert len(matches) == 2
        assert {p["unit"] for d in matches for p in d["profiles"]} == {"bytes", "nanoseconds"}


@pytest.mark.parametrize("nodeid", [None, "test_repeat.py::test_hot"])
def test_profile_export_preserves_repeated_tests_and_background_windows(pytester, nodeid):
    engine = IncidentEngine(pytester.parseconfig(), Settings(directory=pytester.path / "evidence"))
    engine.directory.mkdir(parents=True, exist_ok=True)
    records = []
    for weight in (1024, 2048):
        item = record(nodeid, [stack([0], weight / 1024)], [frame("test_repeat.py", 1, "test_hot")])
        item["memory_stacks"] = [{"frames": [0], "bytes": weight}]
        records.append(item)
    (engine.directory / "gw0.profile.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    engine._report_profile()
    documents = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (engine.directory / "profiles").glob("*.json")]
    assert len(documents) == 4
    assert sorted(p["weights"][0] for d in documents for p in d["profiles"]
                  if p["unit"] == "bytes") == [1024, 2048]


@pytest.mark.skipif(os.name != "posix", reason="undecodable POSIX filename bytes")
def test_file_inventory_preserves_undecodable_names_across_scans(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    existing = root / "bad_\udcff"
    existing.write_bytes(b"old")
    scanner = Scanner(root, tmp_path / "inventory.sqlite", 100, tmp_path / "excluded", lambda _: None)
    try:
        assert scanner.scan()["baseline"]["file_count"] == 1
        existing.write_bytes(b"grown")
        added = root / "bad_\udcfe"
        added.write_bytes(b"new file")
        result = scanner.scan()
        assert result["status"] == "complete"
        assert result["new_remaining_count"] == 1
        assert result["grown_existing_bytes"] == 2
        assert {entry["path"] for entry in result["largest_growth"]} == {existing.name, added.name}
        existing.unlink()
        assert scanner.scan()["deleted_baseline_count"] == 1
    finally:
        scanner.close()
