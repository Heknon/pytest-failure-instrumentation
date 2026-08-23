"""Workers that disagreed about which tests exist."""

from __future__ import annotations

from .conftest import needs_xdist

pytestmark = needs_xdist

ODD_WORKER_PLUGIN = """
import os


def pytest_collection_modifyitems(session, config, items):
    if os.environ.get("PYTEST_XDIST_WORKER") != "gw1":
        return
    if os.environ.get("ODD") == "membership":
        items[:] = [item for item in items if "test_two" not in item.nodeid]
    elif os.environ.get("ODD") == "order":
        items.reverse()
"""

SUITE = """
def test_one():
    assert True


def test_two():
    assert True


def test_three():
    assert True
"""


def prepare(distributed, mode, monkeypatch):
    monkeypatch.setenv("ODD", mode)
    distributed.pytester.makepyfile(test_suite=SUITE)
    distributed.pytester.makepyfile(odd_plugin=ODD_WORKER_PLUGIN)
    return distributed.run(
        "-n", "2", "-p", "odd_plugin", "test_suite.py", timeout=180
    )


def test_a_missing_test_is_reported_as_membership(distributed, monkeypatch):
    incidents = prepare(distributed, "membership", monkeypatch)

    mismatch = distributed.only(incidents, "collection_mismatch")
    assert mismatch.verdict == "COLLECTION_MEMBERSHIP_DIFFERS"
    assert mismatch.variant_count == 2
    assert mismatch.worker_count == 2
    assert mismatch.run_ending is True

    baseline, odd = mismatch.variants
    assert baseline.role == "baseline"
    assert baseline.test_count == 3
    assert odd.kind == "membership"
    assert odd.missing == ["test_suite.py::test_two"]
    assert odd.modules == ["test_suite.py"]
    # No stack names anybody, but the module that differs has an owner.
    assert mismatch.suspect_owner
    assert "disagreed about" in (mismatch.suspect_basis or "")


def test_the_same_tests_in_a_different_order_is_still_fatal(distributed, monkeypatch):
    incidents = prepare(distributed, "order", monkeypatch)

    mismatch = distributed.only(incidents, "collection_mismatch")
    assert mismatch.verdict == "COLLECTION_ORDER_DIFFERS"
    odd = mismatch.variants[1]
    assert odd.kind == "order"
    assert odd.missing_count == 0 and odd.extra_count == 0
    # The one fact a unified diff of a reordered list destroys. Which of two
    # equal-sized variants is the baseline is arbitrary, so the assertion is
    # about the pair rather than its order.
    assert odd.first_divergence_index == 0
    assert set(odd.first_divergence) == {"test_suite.py::test_one", "test_suite.py::test_three"}


def test_full_id_lists_go_to_disk_not_into_the_payload(distributed, monkeypatch):
    incidents = prepare(distributed, "membership", monkeypatch)

    mismatch = distributed.only(incidents, "collection_mismatch")
    assert len(mismatch.variant_files) == 2
    for digest, path in mismatch.variant_files.items():
        assert digest in path
        written = (distributed.pytester.path / path).read_text(encoding="utf-8")
        assert "test_suite.py::test_one" in written


def test_agreement_raises_nothing(distributed, monkeypatch):
    monkeypatch.delenv("ODD", raising=False)
    distributed.pytester.makepyfile(test_suite=SUITE)
    incidents = distributed.run("-n", "2", "test_suite.py", timeout=180)
    assert distributed.of_kind(incidents, "collection_mismatch") == []


MANY = "".join(f"def test_{index:03d}(): assert True\n" for index in range(20))

ODD_ONE_IN_MANY = """
import os


def pytest_collection_modifyitems(session, config, items):
    if os.environ.get("PYTEST_XDIST_WORKER") != "gw1":
        return
    if os.environ.get("ODD") == "membership":
        items[:] = [item for item in items if "test_002" not in item.nodeid]
    elif os.environ.get("ODD") == "duplicate":
        items.append(items[0])
"""


def prepare_many(distributed, mode, monkeypatch, workers="8"):
    monkeypatch.setenv("ODD", mode)
    distributed.pytester.makepyfile(test_many=MANY)
    distributed.pytester.makepyfile(odd_plugin=ODD_ONE_IN_MANY)
    return distributed.run(
        "-n", workers, "-p", "odd_plugin", "test_many.py", timeout=240
    )


def test_the_majority_is_measured_once_every_worker_has_voted(distributed, monkeypatch):
    """Reporting on the first two differing digests describes whichever two
    workers won the race.

    At sixty workers that is "2 workers produced 2 different collections", and
    the majority-against-minority framing this whole kind exists for never
    happens - the baseline is simply whoever registered first.
    """
    incidents = prepare_many(distributed, "membership", monkeypatch)

    mismatch = distributed.only(incidents, "collection_mismatch")
    assert mismatch.worker_count == 8
    assert mismatch.complete is True
    baseline, odd = mismatch.variants
    assert baseline.worker_count == 7
    assert odd.worker_count == 1 and odd.workers == ["gw1"]
    assert odd.missing == ["test_many.py::test_002"]
    assert "8 workers produced 2 different collections" in str(mismatch)


def test_the_odd_worker_does_not_become_the_baseline(distributed, monkeypatch):
    """With only two collections in hand the tie-break falls to test count, so
    a worker with an *extra* test led the report and the seven correct ones
    were described as missing it."""
    incidents = prepare_many(distributed, "duplicate", monkeypatch)

    mismatch = distributed.only(incidents, "collection_mismatch")
    baseline, odd = mismatch.variants
    assert baseline.worker_count == 7
    assert odd.workers == ["gw1"]
    # A duplicated id is a difference in what was collected, not a reordering:
    # set arithmetic saw nothing here and reported "the same 20 tests in a
    # different order" under a baseline line that said 21.
    assert odd.kind == "membership"
    assert odd.extra == ["test_many.py::test_000"]
    assert "in a different order" not in str(mismatch)
