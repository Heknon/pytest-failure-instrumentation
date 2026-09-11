"""Test identity remains coherent while the recorder changes tests."""

from __future__ import annotations

import threading

from pytest_failure_instrumentation import probes
from pytest_failure_instrumentation.capture import heartbeat
from pytest_failure_instrumentation.nodeid import hash_of


def test_a_beat_during_hashing_keeps_the_previous_complete_identity(monkeypatch):
    rows = []
    beat = heartbeat.Heartbeat(lambda event, **row: rows.append(row))
    old = "t.py::test_old"
    new = "t.py::test_new[" + "x" * 200_000 + "]"
    beat.nodeid = old
    entered = threading.Event()
    release = threading.Event()

    def paused_hash(nodeid):
        # Long-input hashlib releases the GIL. Hold that window open without
        # relying on a scheduler race or slowing down the production function.
        entered.set()
        release.wait(5)
        return hash_of(nodeid)

    monkeypatch.setattr(heartbeat, "hash_of", paused_hash)
    monkeypatch.setattr(probes, "resident_megabytes", lambda: (42, "test"))
    writer = threading.Thread(target=setattr, args=(beat, "nodeid", new))
    writer.start()
    try:
        assert entered.wait(5), "the recorder never reached hashing"
        beat._beat()
        assert (rows[-1]["nodeid"], rows[-1]["nodeid_hash"]) == (old, hash_of(old))
    finally:
        release.set()
        writer.join(5)
    assert not writer.is_alive()
    beat._beat()
    assert (rows[-1]["nodeid"], rows[-1]["nodeid_hash"]) == (new, hash_of(new))
    beat.nodeid = None
    beat._beat()
    assert rows[-1]["nodeid"] is None and rows[-1]["nodeid_hash"] is None


def test_memory_observers_use_the_identity_of_the_recorded_beat(monkeypatch):
    rows = []
    observed = []
    old = "t.py::test_old"
    new = "t.py::test_new"

    class Observer:
        def observe(self, resident, nodeid, nodeid_hash):
            observed.append((resident, nodeid, nodeid_hash))

    def record(event, **row):
        rows.append(row)
        # The main thread can move to the next test during event-log I/O.
        beat.nodeid = new
        beat._stop.set()

    beat = heartbeat.Heartbeat(record, interval=1, observers=[Observer()])
    beat.nodeid = old
    monkeypatch.setattr(probes, "resident_megabytes", lambda: (42, "test"))
    monkeypatch.setattr(heartbeat, "TICK_SECONDS", 0.001)
    clock = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: next(clock))
    beat._run()
    assert rows[0]["nodeid"] == old
    assert observed == [(42, old, hash_of(old))]
