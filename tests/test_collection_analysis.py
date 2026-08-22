"""Grouping collections by variant instead of by worker."""

from __future__ import annotations

from pytest_failure_instrumentation.analysis import collection


def identifiers(*names):
    return [f"test_module.py::{name}" for name in names]


def variant_of(summary, worker):
    """Two variants with one worker each are ordered by digest, so a test that
    means "gw1's collection" has to say so."""
    return next(v for v in summary["variants"] if worker in v["workers"])


def test_agreement_is_not_a_mismatch():
    tracker = collection.CollectionTracker()
    for worker in ("gw0", "gw1", "gw2"):
        tracker.record(worker, identifiers("a", "b"))
    assert tracker.has_mismatch is False


def test_sixty_workers_two_variants():
    tracker = collection.CollectionTracker()
    for index in range(59):
        tracker.record(f"gw{index}", identifiers("a", "b", "c"))
    tracker.record("gw59", identifiers("a", "b"))

    assert tracker.has_mismatch is True
    summary = tracker.summarise()
    assert summary["variant_count"] == 2
    assert summary["worker_count"] == 60

    baseline, odd = summary["variants"]
    # The majority leads, so 59 workers are never reported as the deviation.
    assert baseline["worker_count"] == 59
    assert baseline["role"] == "baseline"
    assert odd["workers"] == ["gw59"]
    assert odd["kind"] == "membership"
    assert odd["missing_count"] == 1
    assert odd["missing_sample"] == ["test_module.py::c"]


def test_only_one_id_list_is_kept_per_variant():
    tracker = collection.CollectionTracker()
    for index in range(50):
        tracker.record(f"gw{index}", identifiers("a", "b"))
    # Fifty workers, one collection held.
    assert len(tracker.identifiers_by_digest) == 1
    assert len(tracker.digest_by_worker) == 50


def test_same_tests_different_order_is_reported_as_order():
    tracker = collection.CollectionTracker()
    tracker.record("gw0", identifiers("a", "b", "c"))
    tracker.record("gw1", identifiers("c", "b", "a"))

    summary = tracker.summarise()
    baseline = summary["variants"][0]
    odd = summary["variants"][1]
    assert odd["kind"] == "order"
    assert odd["missing_count"] == 0 and odd["extra_count"] == 0
    assert odd["first_divergence_index"] == 0
    first, second = odd["first_divergence"]
    assert {first, second} == {"test_module.py::a", "test_module.py::c"}
    assert baseline["role"] == "baseline"


def test_a_late_worker_is_marked_as_a_replacement():
    tracker = collection.CollectionTracker()
    tracker.record("gw0", identifiers("a", "b"))
    tracker.record("gw1_replacement", identifiers("a"), replacement=True)


    odd = variant_of(tracker.summarise(), "gw1_replacement")
    assert odd["replacements"] == ["gw1_replacement"]


def test_samples_are_stable_across_runs():
    # Sorted, so worker timing never moves the sample and the fingerprint
    # does not drift between runs of the same defect.
    first = collection.difference(identifiers("a", "b", "c", "d"), identifiers("a"))
    second = collection.difference(identifiers("a", "b", "c", "d"), identifiers("a"))
    assert first["missing_sample"] == second["missing_sample"]
    assert first["missing_sample"] == identifiers("b", "c")[:2] + ["test_module.py::d"][:1]


def test_workers_are_ordered_the_way_they_are_numbered():
    """gw11 sorting before gw3 reads as a mistake at exactly the scale this
    report exists for."""
    tracker = collection.CollectionTracker()
    for name in ("gw19", "gw3", "gw11", "gw2"):
        tracker.record(name, identifiers("a"))
    assert tracker.variants()[0]["workers"] == ["gw2", "gw3", "gw11", "gw19"]


def test_digest_depends_on_order():
    assert collection.digest_of(["a", "b"]) != collection.digest_of(["b", "a"])
