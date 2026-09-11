"""The hash that travels beside every node id.

A node id has no length bound and every place this package writes one down
does: the worker's state slot is a fixed size, the resource inventory caps the
id at a kilobyte, and somebody's database column has a width of its own. What
the elision costs is identity - two parametrized cases whose ids differ only
in the part that was dropped store as the same text - and this is what buys it
back.
"""

from __future__ import annotations

import hashlib
import importlib
import pkgutil

import pytest
from pydantic import BaseModel

from pytest_failure_instrumentation.incidents.collection import (
    CollectionMismatchIncident,
    CollectionVariant,
    UnstableParameters,
)
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident
from pytest_failure_instrumentation.incidents.internal_error import InternalErrorIncident
from pytest_failure_instrumentation.incidents.profile import (
    CpuBurstIncident,
    CpuHotspotIncident,
    MemoryProfileIncident,
)
from pytest_failure_instrumentation.incidents.stall import WorkerStallIncident
from pytest_failure_instrumentation.nodeid import (
    ALGORITHM,
    HASH_LENGTH,
    hash_of,
    hashes_of,
)

NODEID = "tests/e2e/test_replay.py::TestLedger::test_settles[input=9f86d081]"


def test_the_hash_is_the_sha256_of_the_id():
    """Spelled out rather than compared against the function that produced it:
    this value is what somebody else's table is keyed on, and a change to how
    it is computed orphans every row they have."""
    assert hash_of(NODEID) == hashlib.sha256(NODEID.encode("utf-8")).hexdigest()
    assert ALGORITHM == "sha256"


def test_the_hash_is_the_same_width_whatever_the_id_was():
    """The whole point: the id is unbounded and this is not, so a consumer can
    give it a column and know nothing will ever be trimmed out of it."""
    for nodeid in ("t.py::test_a", NODEID, "t.py::test_b[" + "x" * 200_000 + "]"):
        assert len(hash_of(nodeid)) == HASH_LENGTH


def test_two_ids_that_differ_only_where_elision_cuts_stay_different():
    """The failure this exists to prevent. A fixed-size slot keeps both ends
    of an over-long id and drops the middle, so two cases that differ in the
    middle alone are stored as the same string - and the hash is then the only
    thing that can still tell them apart."""
    left = "t.py::test_case[" + "a" * 9000 + "-left-" + "z" * 9000 + "]"
    right = "t.py::test_case[" + "a" * 9000 + "-right-" + "z" * 9000 + "]"
    assert hash_of(left) != hash_of(right)


def test_no_id_hashes_to_nothing_rather_than_to_a_constant():
    """A worker between tests has no test to identify. The hash of the empty
    string would be a real-looking id shared by every idle worker in the
    fleet, which is exactly the join a consumer would then make."""
    assert hash_of(None) is None
    assert hash_of("") is None


def test_a_list_of_hashes_keeps_its_places():
    """Read positionally against the id list beside it, so an id that hashes
    to nothing has to hold its index rather than drop out and shift the rest."""
    assert hashes_of(["t.py::test_a", "", "t.py::test_b"]) == [
        hash_of("t.py::test_a"),
        "",
        hash_of("t.py::test_b"),
    ]
    assert hashes_of([]) == []


def test_non_ascii_ids_hash_as_utf_8():
    """A parametrize over text produces these routinely, and the encoding has
    to be the one every other reader will assume."""
    nodeid = "t.py::test_x[é中文]"
    assert hash_of(nodeid) == hashlib.sha256(nodeid.encode("utf-8")).hexdigest()


# --- the payload contract ----------------------------------------------

#: Every field in the incident payload that holds a node id, and the field
#: holding its hash. This table is the contract: a node id stored without one
#: is a row nobody can join, because the text may have been cut to fit and two
#: cases that differ only in the cut part are then indistinguishable.
NODE_ID_FIELDS = [
    (WorkerDeathIncident, "test_in_flight", "test_in_flight_hash"),
    (WorkerDeathIncident, "last_test", "last_test_hash"),
    (WorkerStallIncident, "test_in_flight", "test_in_flight_hash"),
    (WorkerStallIncident, "last_test", "last_test_hash"),
    (InternalErrorIncident, "test_in_flight", "test_in_flight_hash"),
    (CpuHotspotIncident, "tests", "test_hashes"),
    (CpuBurstIncident, "nodeid", "nodeid_hash"),
    (CpuBurstIncident, "tests", "test_hashes"),
    (MemoryProfileIncident, "nodeid", "nodeid_hash"),
    (MemoryProfileIncident, "tests", "test_hashes"),
    (CollectionMismatchIncident, "unstable_tests", "unstable_test_hashes"),
    (CollectionVariant, "missing", "missing_hashes"),
    (CollectionVariant, "extra", "extra_hashes"),
    (CollectionVariant, "first_divergence", "first_divergence_hashes"),
    (UnstableParameters, "test", "test_hash"),
]


@pytest.mark.parametrize(
    ("model", "field", "companion"),
    NODE_ID_FIELDS,
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_every_node_id_in_the_payload_has_a_hash_beside_it(model, field, companion):
    assert field in model.model_fields
    assert companion in model.model_fields


@pytest.mark.parametrize(
    ("model", "field", "companion"),
    NODE_ID_FIELDS,
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_a_list_of_ids_gets_a_list_of_hashes_and_a_single_id_a_single_hash(
    model, field, companion
):
    """Read positionally against each other, so the two have to be the same
    shape - a list of ids with one hash beside it says nothing about which."""
    identifiers = model.model_fields[field].annotation
    hashes = model.model_fields[companion].annotation
    assert ("list" in str(identifiers)) == ("list" in str(hashes))


def test_no_hash_field_is_left_without_the_id_it_names():
    """The other direction of the same contract: a hash column with no text
    beside it is unreadable by a person, which is what the text is for."""
    paired = {(model, companion) for model, _, companion in NODE_ID_FIELDS}
    for model, _, _ in NODE_ID_FIELDS:
        for name in model.model_fields:
            if name.endswith(("_hash", "_hashes")):
                assert (model, name) in paired, f"{model.__name__}.{name}"


#: Field names that hold a node id wherever they turn up in the payload. The
#: guard below is what makes the table above a contract rather than a snapshot:
#: a kind that grows one of these later fails here until it grows the hash too.
NODE_ID_NAMES = {
    "extra",
    "first_divergence",
    "last_test",
    "missing",
    "nodeid",
    "test",
    "test_in_flight",
    "tests",
    "unstable_tests",
}


def _payload_models():
    """Every pydantic model the incident payload is built from."""
    package = importlib.import_module("pytest_failure_instrumentation.incidents")
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
            ):
                yield value


def test_a_node_id_field_added_later_is_not_forgotten():
    covered = {(model, field) for model, field, _ in NODE_ID_FIELDS}
    for model in _payload_models():
        for name, field in model.model_fields.items():
            # By name and by type: `MemoryGrowth.tests` is how many tests the
            # growth was measured over, which is a number and not an id.
            if name in NODE_ID_NAMES and "str" in str(field.annotation):
                assert (model, name) in covered, (
                    f"{model.__name__}.{name} holds a node id and has no hash "
                    "beside it; add the pair to NODE_ID_FIELDS once it does"
                )
