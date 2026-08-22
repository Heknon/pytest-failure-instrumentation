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
    assert odd.missing_sample == ["test_suite.py::test_two"]
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
