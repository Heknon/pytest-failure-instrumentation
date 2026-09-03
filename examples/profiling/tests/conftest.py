
import pytest
from demo_product.poller import Poller


def pytest_failure_incident(incident):
    """Where the findings go: a JSON line each, beside the terminal output."""
    with open("incidents.jsonl", "a", encoding="utf-8") as handle:
        handle.write(incident.model_dump_json() + "\n")


@pytest.fixture(scope="session")
def status_poller():
    """A watcher somebody added once and nobody remembers is running."""
    poller = Poller()
    poller.start()
    yield poller
    poller.stop()
