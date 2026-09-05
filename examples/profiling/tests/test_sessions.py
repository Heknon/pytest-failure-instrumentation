"""Scenario 10: an I/O-bound suite whose fixture bursts on every test.
Expected: cpu_burst RECURRING_BURST blamed on session.py in Session.__init__,
in setup, across all six tests - each test is a third of a second of a core
followed by waiting, and no single one of them is worth a look on its own."""

import pytest


@pytest.fixture
def session():
    from demo_product.session import Session

    return Session()


@pytest.mark.parametrize("case", range(6))
def test_request_answers(session, case):
    assert session.request(0.3)["status"] == "ok"
