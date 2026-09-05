"""The example's hooks: where the findings go, and the poller fixture.

``incidents.jsonl`` holds one run. It is emptied when the session starts and
appended to as findings arrive, so it never carries a previous run's lines.
"""

import pytest
from demo_product.poller import Poller


def pytest_sessionstart(session):
    # Once, in the process that receives the findings; under xdist that is
    # the controller, and the workers leave the file alone.
    if not hasattr(session.config, "workerinput"):
        open("incidents.jsonl", "w", encoding="utf-8").close()


def pytest_failure_incident(incident):
    """A JSON line per finding, beside the terminal output."""
    with open("incidents.jsonl", "a", encoding="utf-8") as handle:
        handle.write(incident.model_dump_json() + "\n")


@pytest.fixture(scope="session")
def status_poller():
    """A watcher somebody added once and nobody remembers is running."""
    poller = Poller()
    poller.start()
    yield poller
    poller.stop()
