"""The fixed-size record of what a worker is doing right now.

A 256-byte slot is a bound, and the node id is the only field that can reach
it: a parametrized id runs to hundreds of characters routinely. What that bound
must never cost is the record itself - an unparseable slot leaves the reader
with no test, no phase and no counters, and a worker that died mid-call is then
reported as one that died before running anything.
"""

from __future__ import annotations

from pytest_failure_instrumentation.capture.state import (
    ELIDED,
    SLOT_SIZE,
    WorkerState,
    read_state,
)

# The long-but-ordinary shape: a real path, a class, and a parameter set
# carrying content hashes. This is what the slot is sized to hold whole.
HASHED_NODEID = (
    "tests/e2e/test_replay.py::TestLedger::test_settles"
    "[input=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    "-expected=2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae]"
)

# Past anything real by a wide margin, so the elision itself can be tested.
OVERSIZED_NODEID = "tests/matrix/test_grid.py::test_case[" + "-".join(
    f"dimension{index}=abcdef0123456789" for index in range(250)
) + "]"


def state_for(tmp_path, **fields):
    state = WorkerState(tmp_path / "gw0.state", 4242)
    state.update(**fields)
    return read_state(tmp_path / "gw0.state")


def test_the_slot_is_the_same_size_whatever_is_in_it(tmp_path):
    state = WorkerState(tmp_path / "gw0.state", 4242)
    for nodeid in ("t.py::test_a", HASHED_NODEID, "t.py::test_b[" + "x" * 20000 + "]"):
        state.update(nodeid=nodeid, phase="call")
        assert (tmp_path / "gw0.state").stat().st_size == SLOT_SIZE


def test_a_short_node_id_is_recorded_as_it_is(tmp_path):
    record = state_for(tmp_path, nodeid="t.py::test_a", phase="setup")
    assert record["nodeid"] == "t.py::test_a"
    assert record["phase"] == "setup"


def test_a_long_id_with_hashes_in_it_is_kept_whole(tmp_path):
    """The slot is sized for the ids people actually have. A suite keyed on
    content hashes produces nothing else, and an id cut short there names the
    test but not which case of it failed."""
    record = state_for(
        tmp_path, nodeid=HASHED_NODEID, phase="call", tests_started=7, tests_finished=6
    )
    assert record["nodeid"] == HASHED_NODEID
    assert record["phase"] == "call"
    assert record["tests_started"] == 7
    assert record["tests_finished"] == 6
    assert record["pid"] == 4242


def test_an_id_past_even_that_keeps_both_of_its_ends(tmp_path):
    """Cutting the tail loses which case it was - a hash, a timestamp, an
    account id all live at the end. Cutting the head loses the module the
    incident is attributed to. So an id too long for the slot gives up its
    middle, and says that it did."""
    record = state_for(tmp_path, nodeid=OVERSIZED_NODEID, phase="call")
    head, _, tail = record["nodeid"].partition(ELIDED)
    assert head and tail
    assert OVERSIZED_NODEID.startswith(head)
    assert OVERSIZED_NODEID.endswith(tail)
    assert record["phase"] == "call"


def test_an_id_that_escapes_to_more_bytes_than_it_has_characters(tmp_path):
    """Quotes and non-ASCII parameters cost several bytes each once encoded.
    Subtracting an overflow in characters over-trims them - far enough, on a
    fully non-ASCII id, to throw away the module name as well."""
    for parameter in ('"\\' * 2600, "é中文" * 2000, "\n\t" * 3000):
        record = state_for(tmp_path, nodeid=f"t.py::test_x[{parameter}]", phase="call")
        assert record, parameter
        assert record["nodeid"].startswith("t.py::test_x["), record["nodeid"]


def test_a_record_stays_readable_as_the_counters_grow_digits(tmp_path):
    """A trimmed id is re-used across the six updates a test makes, so the fit
    has to be re-checked rather than trusted: the counters beside it widen."""
    state = WorkerState(tmp_path / "gw0.state", 4242)
    for count in (9, 99, 999, 9999, 99999, 999999):
        state.tests_started = state.tests_finished = count
        state.update(nodeid=OVERSIZED_NODEID, phase="teardown")
        record = read_state(tmp_path / "gw0.state")
        assert record["tests_started"] == count
        assert record["nodeid"].startswith("tests/matrix/test_grid.py::")


def test_a_missing_file_reads_as_nothing_known(tmp_path):
    assert read_state(tmp_path / "absent.state") == {}


def test_the_test_in_flight_and_the_last_test_are_different_fields(tmp_path):
    """A worker between two tests has no test in flight, and saying it has one
    puts a passing test's name on whatever happened next."""
    state = WorkerState(tmp_path / "gw0.state", 4242)
    state.update(nodeid="t.py::test_a", phase="call")
    state.update(phase=None, nodeid=None)

    record = read_state(tmp_path / "gw0.state")
    assert record["nodeid"] is None
    assert record["last_nodeid"] == "t.py::test_a"


def test_the_last_test_is_elided_too_rather_than_bursting_the_slot(tmp_path):
    state = WorkerState(tmp_path / "gw0.state", 4242)
    state.update(nodeid=OVERSIZED_NODEID, phase="call")
    state.update(phase=None, nodeid=None)

    assert (tmp_path / "gw0.state").stat().st_size == SLOT_SIZE
    record = read_state(tmp_path / "gw0.state")
    assert record["nodeid"] is None
    assert ELIDED in record["last_nodeid"]
    assert record["tests_started"] == 0  # the rest of the record still parsed


def test_a_record_another_run_left_behind_is_not_this_run_s(tmp_path):
    """The directory outlives a run and cleaning it is best-effort - on Windows
    a file somebody still has open cannot be unlinked at all. Read as this
    run's, the record hands over an earlier run's pid, and that pid is what the
    stall probe signals."""
    state = WorkerState(tmp_path / "gw0.state", 4242, run_id="run-earlier")
    state.update(nodeid="t.py::test_a", phase="call")

    assert read_state(tmp_path / "gw0.state", "run-now") == {}
    assert read_state(tmp_path / "gw0.state", "run-earlier")["pid"] == 4242


def test_a_record_with_no_run_id_at_all_is_still_read(tmp_path):
    """A worker the controller never reached with an id wrote a record that
    cannot be placed either way. Dropping it loses real evidence to protect
    against nothing: only a disagreement proves the file is somebody else's."""
    state = WorkerState(tmp_path / "gw0.state", 4242)
    state.update(nodeid="t.py::test_a", phase="call")

    assert read_state(tmp_path / "gw0.state", "run-now")["nodeid"] == "t.py::test_a"


def test_the_tests_clock_is_cleared_with_the_test(tmp_path):
    """Read on the controller as "how long the test had been running when this
    worker died", and matched there against the run's configured timeouts.

    A clock left running past teardown measures the gap since a test that
    already passed, and keeps growing - so an idle worker's ``os._exit(1)``
    reaches any timeout you like and is reported as one, naming that test and
    blaming its owner.
    """
    state = WorkerState(tmp_path / "gw0.state", 4242)
    state.update(nodeid="t.py::test_a", phase="setup", test_started=1000.0, phase_started=1000.0)
    state.update(nodeid="t.py::test_a", phase="call", phase_started=1001.0)

    running = read_state(tmp_path / "gw0.state")
    assert running["test_started"] == 1000.0 and running["phase_started"] == 1001.0

    state.update(phase=None, nodeid=None, phase_started=None, test_started=None)

    idle = read_state(tmp_path / "gw0.state")
    assert idle["test_started"] is None and idle["phase_started"] is None
    assert idle["last_nodeid"] == "t.py::test_a"  # what it was is still kept
