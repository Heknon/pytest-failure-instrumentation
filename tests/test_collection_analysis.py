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
    assert odd["missing"] == ["test_module.py::c"]


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


def test_the_whole_difference_travels_not_a_sample_of_it():
    """The collection is unbounded and stays on disk; the difference is what a
    reader needs and is almost always small."""
    result = collection.difference(identifiers(*"abcdefghij"), identifiers("a"))
    assert result["missing_count"] == 9
    assert result["missing"] == identifiers(*"bcdefghij")


def test_an_enormous_difference_is_capped_and_says_so():
    baseline = [f"test_module.py::test_{n:05d}" for n in range(2000)]
    result = collection.difference(baseline, [])
    # The count is the truth; the list is what would fit.
    assert result["missing_count"] == 2000
    assert len(result["missing"]) == collection.IDS_KEPT


def test_ids_are_stable_across_runs():
    # Sorted, so worker timing never reorders them and the fingerprint does
    # not drift between runs of the same defect.
    first = collection.difference(identifiers("a", "b", "c", "d"), identifiers("a"))
    second = collection.difference(identifiers("a", "b", "c", "d"), identifiers("a"))
    assert first["missing"] == second["missing"] == identifiers("b", "c", "d")


def test_workers_are_ordered_the_way_they_are_numbered():
    """gw11 sorting before gw3 reads as a mistake at exactly the scale this
    report exists for."""
    tracker = collection.CollectionTracker()
    for name in ("gw19", "gw3", "gw11", "gw2"):
        tracker.record(name, identifiers("a"))
    assert tracker.variants()[0]["workers"] == ["gw2", "gw3", "gw11", "gw19"]


def test_digest_depends_on_order():
    assert collection.digest_of(["a", "b"]) != collection.digest_of(["b", "a"])


# -- ids that are not stable between workers -----------------------------


def parametrized(worker: int, count: int = 3):
    """One parametrized test whose values are drawn per worker."""
    return [f"test_pricing.py::test_rounding[{worker}.{n}]" for n in range(count)]


def test_the_same_tests_with_different_parameters_are_not_missing_tests():
    tracker = collection.CollectionTracker()
    for worker in range(8):
        tracker.record(f"gw{worker}", identifiers("a", "b") + parametrized(worker))

    assert tracker.has_mismatch is True
    assert tracker.parameters_unstable is True
    # Named without their parameters, which is the only stable part.
    assert tracker.unstable_tests() == ["test_pricing.py::test_rounding"]


def test_genuinely_different_tests_are_not_blamed_on_parameters():
    tracker = collection.CollectionTracker()
    tracker.record("gw0", identifiers("a", "b"))
    tracker.record("gw1", identifiers("a"))
    assert tracker.parameters_unstable is False


def test_agreement_is_never_reported_as_unstable():
    tracker = collection.CollectionTracker()
    for worker in range(4):
        tracker.record(f"gw{worker}", identifiers("a") + parametrized(0))
    assert tracker.parameters_unstable is False


def test_one_variant_per_worker_does_not_hold_one_collection_per_worker():
    """The case the digest exists to avoid: unstable ids turn "a handful of
    variants" into "one per worker", and holding them all is the hundreds of
    megabytes this was designed not to store."""
    tracker = collection.CollectionTracker()
    for worker in range(40):
        tracker.record(f"gw{worker}", identifiers("a") + parametrized(worker, 50))

    assert len(tracker.digest_by_worker) == 40
    assert len(tracker.identifiers_by_digest) == collection.VARIANTS_KEPT
    # The counts stay right for every variant, held or not.
    assert all(entry["test_count"] == 51 for entry in tracker.variants())


def test_a_variant_with_no_list_held_is_reported_as_uncompared():
    """Diffing two lists that were never kept reports "the same tests in a
    different order" - a finding invented out of missing data."""
    tracker = collection.CollectionTracker()
    for worker in range(9):
        tracker.record(f"gw{worker}", identifiers(*"abcdefghij"[: worker + 1]))

    described = tracker.summarise()["variants"]
    dropped = [entry for entry in described if entry["kind"] == "uncompared"]
    assert dropped, "expected some variant to have been dropped"
    for entry in dropped:
        assert entry["compared"] is False
        assert entry.get("missing_count", 0) == 0
        assert entry.get("extra_count", 0) == 0


def test_the_values_each_worker_produced_are_reported_side_by_side():
    """Naming the test says where to look. Seeing the values says what you
    are looking at - float noise reads as a random number, and a row of live
    ids reads as a fetch running at collection time."""
    tracker = collection.CollectionTracker()
    for worker in range(6):
        tracker.record(
            f"gw{worker}",
            identifiers("a")
            + [f"test_billing.py::test_invoice[acct-{worker}{n}]" for n in range(4)],
        )

    samples = tracker.parameter_samples()
    assert len(samples) == 1
    assert samples[0]["test"] == "test_billing.py::test_invoice"

    rows = samples[0]["workers"]
    assert len(rows) == 3, "a few workers, not all of them"
    assert [row["worker"] for row in rows] == ["gw0", "gw1", "gw2"]
    assert rows[0]["values"] == ["acct-00", "acct-01", "acct-02", "acct-03"]
    # Different workers, genuinely different values - which is the finding.
    assert rows[0]["values"] != rows[1]["values"]


def test_a_long_parameter_value_is_truncated_rather_than_wrapping():
    long_value = "x" * 200
    assert len(collection.parameter_of(f"test_a.py::test_b[{long_value}]")) <= 48


def test_a_test_with_no_parameters_contributes_no_values():
    assert collection.without_parameters("test_a.py::test_b") == "test_a.py::test_b"


def test_a_duplicated_id_is_a_difference_not_a_reordering():
    """Set arithmetic cannot see a duplicate at all: the lists differ in length
    while the difference comes out empty, which was reported as "the same N
    tests in a different order" - with an N that contradicted the test count on
    the line above it, and no divergence index, because neither exists.
    """
    baseline = ["m.py::a", "m.py::b"]
    variant = ["m.py::a", "m.py::b", "m.py::a"]

    result = collection.difference(baseline, variant)
    assert result["kind"] == "membership"
    assert result["extra"] == ["m.py::a"]
    assert result["extra_count"] == 1
    assert result["missing_count"] == 0


def test_a_dropped_duplicate_is_reported_as_missing():
    result = collection.difference(["m.py::a", "m.py::a"], ["m.py::a"])
    assert result["kind"] == "membership"
    assert result["missing"] == ["m.py::a"]


def test_a_genuine_reordering_is_still_an_order_difference():
    result = collection.difference(["m.py::a", "m.py::b"], ["m.py::b", "m.py::a"])
    assert result["kind"] == "order"
    assert result["first_divergence_index"] == 0
