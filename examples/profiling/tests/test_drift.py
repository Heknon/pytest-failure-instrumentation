"""Scenario 12: the leak no single test shows. Forty tests each keep three
megabytes - a response cached by node id, never evicted - which is under
every per-test threshold there is. Expected: it is counted into the worker's
memory_profile STEADY_GROWTH finding, which names this test among what the
worker accumulated and, with --profile-allocations, the line that holds it."""

import pytest
from demo_product import cache


@pytest.mark.parametrize("case", range(40))
def test_response_is_cached(case):
    cache.remember(b"response" * 400_000)  # ~3 MB, written, so it is resident
    assert cache.remembered() > case
