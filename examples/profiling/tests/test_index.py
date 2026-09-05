"""Scenario 11: one long burst inside a test that is otherwise waiting.
Expected: cpu_burst LONG_BURST blamed on reports.py in build_index, starting
about a second into the call phase - nearly all of the test's CPU in one
stretch, the rest of its duration spent waiting."""

import time

from demo_product.reports import build_document, build_index


def test_index_is_complete():
    time.sleep(1.0)  # fetch the export
    index = build_index(build_document(200_000))
    time.sleep(1.0)  # upload the index
    assert index["a"] == 200_000
