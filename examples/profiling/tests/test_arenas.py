"""Scenario 13: ALLOCATOR_RETENTION.

Each test ingests a batch on the product's thread pool. Every one frees what
it parsed, and the worker grows anyway: the freed payloads stay mapped in
the thread arenas glibc gave the pool, pinned by the index entries between
them. No test keeps enough to be raised on its own, nothing is in use, so
the drift rule does not count it - and the worker is far bigger at the end
than anything it holds. The finding says which it is, arenas or one
fragmented heap, because MALLOC_ARENA_MAX=2 fixes the first and not the
second. Run this module with that variable set to see the difference.
"""

import pytest
from demo_product import ingest


@pytest.mark.parametrize("batch", range(4))
def test_ingest_batch(batch):
    assert ingest.ingest() == 8 * 300
    assert ingest.indexed() >= 300
