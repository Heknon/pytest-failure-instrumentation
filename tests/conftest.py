"""Harness for the integration tests.

Each one runs a real pytest in a subprocess - under xdist where the kind needs
it - and reads back what the plugin raised. The inner run writes incidents as
JSON so the outer run can parse them into models, which also exercises the
round-trip on every scenario rather than in one test of its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pytest_failure_instrumentation.incidents.base import Incident
from pytest_failure_instrumentation.incidents.registry import parse

pytest_plugins = ["pytester"]

INCIDENT_FILE = "incidents.jsonl"

INNER_CONFTEST = f"""
import json


def pytest_failure_incident(incident):
    with open({INCIDENT_FILE!r}, "a") as handle:
        handle.write(incident.model_dump_json() + "\\n")
"""


class Runner:
    def __init__(self, pytester: pytest.Pytester) -> None:
        self.pytester = pytester

    def run(self, *arguments: str, timeout: float = 300.0) -> list[Incident]:
        try:
            self.pytester.runpytest_subprocess(*arguments, timeout=timeout)
        except self.pytester.TimeoutExpired:  # type: ignore[attr-defined]
            # A wedged worker is the point of some of these; the incident is
            # already on disk by then.
            pass
        return self.incidents()

    def incidents(self) -> list[Incident]:
        path = self.pytester.path / INCIDENT_FILE
        if not path.exists():
            return []
        found = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            incident = parse(raw)
            # Every scenario doubles as a round-trip test: what a database
            # would store has to come back as the model it was written from.
            assert incident.model_dump() == raw, incident.kind
            found.append(incident)
        return found

    @staticmethod
    def of_kind(incidents: list[Incident], kind: str) -> list[Incident]:
        return [incident for incident in incidents if incident.kind == kind]

    @staticmethod
    def only(incidents: list[Incident], kind: str) -> Any:
        matching = Runner.of_kind(incidents, kind)
        assert len(matching) == 1, f"expected one {kind}, got {[i.kind for i in incidents]}"
        return matching[0]


@pytest.fixture
def runner(pytester: pytest.Pytester) -> Runner:
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makeini(
        """
        [pytest]
        failure_packages = victim
        failure_product_version = 1.2.3
        """
    )
    pytester.makepyfile(
        victim="""
        import ctypes
        import os


        def native_call(pointer):
            return ctypes.string_at(pointer)


        def hard_exit(code):
            os._exit(code)


        def break_pytest():
            raise RuntimeError("victim broke pytest's machinery")
        """
    )
    return Runner(pytester)


@pytest.fixture
def distributed(runner: Runner) -> Runner:
    if not _has_xdist():
        pytest.skip("pytest-xdist is not installed")
    return runner


def _has_xdist() -> bool:
    try:
        import xdist  # noqa: F401
    except ImportError:
        return False
    return True


needs_xdist = pytest.mark.skipif(not _has_xdist(), reason="needs pytest-xdist")
