"""Scenarios 5 to 8, all memory.

* test_keeps_results     RETAINED_AFTER_TEST: the test's body fills a
                         module-level cache that survives it.
* test_big_fixture       RETAINED_AFTER_TEST in setup: a module fixture
                         builds 150 MB and keeps it for the module.
* test_transient_peak    TRANSIENT_PEAK: 300 MB climbed and freed.
* test_leaks_a_little    STEADY_GROWTH: eight parametrisations each keep
                         25 MB, none of them enough to be flagged alone -
                         counted, with test_drift.py's, into the worker's one
                         growth finding, which names both.
* the whole module       WORKER_IMBALANCE under -n 2 --dist loadfile: the
                         worker that gets this file ends far above its sibling.
                         The finding names the test that first pushed that
                         worker past the other, which is whichever big test
                         its share of the files put in front - this module's
                         or test_loading.py's - not this module as such.
"""

import time

import pytest
from demo_product import cache


def test_keeps_results():
    for _ in range(150):
        cache.remember(b"result" * 170_000)  # ~1 MB, and written, so it is resident
    assert cache.remembered() >= 150


@pytest.fixture(scope="module")
def big_fixture():
    return [bytearray(1_000_000) for _ in range(150)]


def test_big_fixture(big_fixture):
    assert len(big_fixture) == 150


def test_transient_peak():
    blob = [bytearray(1_000_000) for _ in range(300)]
    time.sleep(0.6)  # long enough for the sampler to read the peak
    assert len(blob) == 300


_LEAKED = []


@pytest.mark.parametrize("case", range(8))
def test_leaks_a_little(case):
    _LEAKED.append(bytearray(25_000_000))
    assert _LEAKED[-1][case] == 0
