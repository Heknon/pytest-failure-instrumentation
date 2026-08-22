"""The payload contract: one model per kind, discriminated on `kind`."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pytest_failure_instrumentation.incidents import registry
from pytest_failure_instrumentation.incidents.base import Frame, Incident
from pytest_failure_instrumentation.incidents.collection import CollectionMismatchIncident
from pytest_failure_instrumentation.incidents.death import WorkerDeathIncident
from pytest_failure_instrumentation.incidents.internal_error import InternalErrorIncident
from pytest_failure_instrumentation.incidents.stall import WorkerStallIncident
from pytest_failure_instrumentation.incidents.summary import RunSummaryIncident

EVERY_KIND = [
    WorkerDeathIncident,
    WorkerStallIncident,
    CollectionMismatchIncident,
    InternalErrorIncident,
    RunSummaryIncident,
]


@pytest.mark.parametrize("model", EVERY_KIND, ids=lambda model: model.__name__)
def test_a_kind_needs_nothing_but_a_worker(model):
    # Everything else has a default, which is what lets `degraded` work for
    # any kind when gathering the real incident fails.
    incident = model(worker="gw1")
    assert incident.kind
    assert str(incident)


@pytest.mark.parametrize("model", EVERY_KIND, ids=lambda model: model.__name__)
def test_round_trip_through_a_database_row(model):
    incident = model(worker="gw1", verdict="SOMETHING", evidence=["a fact"])
    row = json.loads(incident.model_dump_json())
    back = registry.parse(row)
    assert type(back) is model
    assert back.model_dump() == incident.model_dump()
    assert str(back) == str(incident)


def test_the_discriminator_is_kind():
    schema = registry.json_schema()
    assert schema["discriminator"]["propertyName"] == "kind"


def test_an_unknown_kind_is_rejected_rather_than_guessed():
    with pytest.raises(ValidationError):
        registry.parse({"kind": "something_new", "worker": "gw1"})


def test_a_builder_that_invents_a_field_is_caught():
    with pytest.raises(ValidationError):
        WorkerDeathIncident(worker="gw1", exit_code_typo=3)


@pytest.mark.parametrize("model", EVERY_KIND, ids=lambda model: model.__name__)
def test_degraded_says_what_failed_and_does_not_raise(model):
    incident = model.degraded("gw3", KeyError("cgroup"), context="xdist said: down")
    assert incident.verdict == "INSTRUMENTATION_FAILED"
    assert any("KeyError" in line for line in incident.evidence)
    assert any("xdist said: down" in line for line in incident.evidence)


def test_run_ending_is_a_property_of_the_kind():
    assert InternalErrorIncident(worker="gw1").ends_this_run() is True
    assert WorkerStallIncident(worker="gw1").ends_this_run() is True
    assert WorkerDeathIncident(worker="gw1").ends_this_run() is False


def test_a_dropped_replacement_does_not_end_the_run():
    """xdist aborts when the initial collections disagree, but silently drops a
    worker that registers a differing collection after scheduling began."""
    dropped = CollectionMismatchIncident(
        worker="controller",
        variants=[
            {"digest": "aaa", "workers": ["gw0"], "worker_count": 1, "role": "baseline"},
            {
                "digest": "bbb",
                "workers": ["gw1"],
                "worker_count": 1,
                "replacements": ["gw1"],
            },
        ],
    )
    assert dropped.ends_this_run() is False

    aborted = CollectionMismatchIncident(
        worker="controller",
        variants=[
            {"digest": "aaa", "workers": ["gw0"], "worker_count": 1, "role": "baseline"},
            {"digest": "bbb", "workers": ["gw1"], "worker_count": 1},
        ],
    )
    assert aborted.ends_this_run() is True


def test_the_alert_text_leads_with_the_blame():
    incident = WorkerDeathIncident(
        worker="gw1",
        verdict="NATIVE_CRASH",
        severity="critical",
        owner="product",
        test_in_flight="test_api.py::test_thing",
        phase="call",
        evidence=["exit status -11"],
        blamed_frame=Frame(
            file="/srv/app/yourcore/engine.py",
            line=6,
            function="native_call",
            module="engine",
            owner="product",
        ),
    )
    rendered = str(incident).splitlines()
    assert rendered[0] == "[worker_death] NATIVE_CRASH  severity=critical  owner=product"
    assert rendered[1].strip() == "blamed on engine.py:6 in native_call"
    assert "test_api.py::test_thing" in rendered[2]


def test_a_guess_is_never_rendered_as_a_finding():
    incident = WorkerDeathIncident(
        worker="gw1",
        owner="unknown",
        suspect_owner="customer-code",
        suspect_basis="owner of the test in flight (test_api.py)",
    )
    assert "suspect customer-code" in str(incident)
    assert "blamed on" not in str(incident)


def test_the_summary_does_not_pretend_to_have_an_owner():
    rendered = str(RunSummaryIncident(worker="main", verdict="RUN_FINISHED"))
    assert "owner=" not in rendered


def test_the_base_is_never_emitted_directly():
    # It has no kind of its own, so constructing one is a mistake.
    with pytest.raises(ValidationError):
        Incident(worker="gw1")
