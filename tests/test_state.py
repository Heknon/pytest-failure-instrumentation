"""The fixed-size record of what a worker is doing right now.

A 256-byte slot is a bound, and the node id is the only field that can reach
it: a parametrized id runs to hundreds of characters routinely. What that bound
must never cost is the record itself - an unparseable slot leaves the reader
with no test, no phase and no counters, and a worker that died mid-call is then
reported as one that died before running anything.
"""

from __future__ import annotations

from pytest_failure_instrumentation.capture.state import (
    SLOT_SIZE,
    TRIMMED,
    WorkerState,
    read_state,
)

# The shape that overflows in practice: a real path, a class, and a parameter
# set that spells out every dimension of the case.
LONG_NODEID = (
    "tests/integration/test_billing_reconciliation.py::TestQuarterlyClose"
    "::test_invoice_matrix[currency=EUR-region=emea-tier=enterprise-2024-01-01]"
)


def state_for(tmp_path, **fields):
    state = WorkerState(tmp_path / "gw0.state", 4242)
    state.update(**fields)
    return read_state(tmp_path / "gw0.state")


def test_the_slot_is_the_same_size_whatever_is_in_it(tmp_path):
    state = WorkerState(tmp_path / "gw0.state", 4242)
    for nodeid in ("t.py::test_a", LONG_NODEID, "t.py::test_b[" + "x" * 4000 + "]"):
        state.update(nodeid=nodeid, phase="call")
        assert (tmp_path / "gw0.state").stat().st_size == SLOT_SIZE


def test_a_short_node_id_is_recorded_as_it_is(tmp_path):
    record = state_for(tmp_path, nodeid="t.py::test_a", phase="setup")
    assert record["nodeid"] == "t.py::test_a"
    assert record["phase"] == "setup"


def test_an_oversized_node_id_costs_its_tail_and_nothing_else(tmp_path):
    """The head is what everything reads - the module for attribution and for
    the fingerprint, the test name for the alert - so the tail is what goes."""
    record = state_for(
        tmp_path, nodeid=LONG_NODEID, phase="call", tests_started=7, tests_finished=6
    )
    assert record["nodeid"].endswith(TRIMMED)
    assert LONG_NODEID.startswith(record["nodeid"][: -len(TRIMMED)])
    # And the rest of the record survives intact, which is the whole point:
    # trimming the encoded JSON instead would take these with it.
    assert record["phase"] == "call"
    assert record["tests_started"] == 7
    assert record["tests_finished"] == 6
    assert record["pid"] == 4242


def test_an_id_that_escapes_to_more_bytes_than_it_has_characters(tmp_path):
    """Quotes and non-ASCII parameters cost several bytes each once encoded.
    Subtracting an overflow in characters over-trims them - far enough, on a
    fully non-ASCII id, to throw away the module name as well."""
    for parameter in ('"\\' * 80, "é中文" * 60, "\n\t" * 90):
        record = state_for(tmp_path, nodeid=f"t.py::test_x[{parameter}]", phase="call")
        assert record, parameter
        assert record["nodeid"].startswith("t.py::test_x["), record["nodeid"]


def test_a_record_stays_readable_as_the_counters_grow_digits(tmp_path):
    """A trimmed id is re-used across the six updates a test makes, so the fit
    has to be re-checked rather than trusted: the counters beside it widen."""
    state = WorkerState(tmp_path / "gw0.state", 4242)
    for count in (9, 99, 999, 9999, 99999, 999999):
        state.tests_started = state.tests_finished = count
        state.update(nodeid=LONG_NODEID, phase="teardown")
        record = read_state(tmp_path / "gw0.state")
        assert record["tests_started"] == count
        assert record["nodeid"].startswith("tests/integration/")


def test_a_missing_file_reads_as_nothing_known(tmp_path):
    assert read_state(tmp_path / "absent.state") == {}
