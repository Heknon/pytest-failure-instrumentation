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


def test_a_degraded_collection_mismatch_still_reports_the_run_as_over():
    """ends_this_run() reads the variants, and a degraded incident has none.

    Falling through to "the run carried on" understated a run xdist had
    already aborted - and the flag is what raises a framework defect above
    informational, so the alert was quieter as well as wrong.
    """
    degraded = CollectionMismatchIncident.degraded("controller", KeyError("digest"))
    assert degraded.variants == []
    assert degraded.ends_this_run() is True


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


# -- how a collection mismatch reads -------------------------------------

MANY_WORKERS = CollectionMismatchIncident(
    worker="controller",
    verdict="COLLECTION_MEMBERSHIP_DIFFERS",
    variant_count=2,
    worker_count=20,
    variants=[
        {
            "digest": "aaa",
            "workers": [f"gw{n}" for n in range(17)],
            "worker_count": 17,
            "test_count": 300,
            "role": "baseline",
            "kind": "baseline",
        },
        {
            "digest": "bbb",
            "workers": ["gw3", "gw11", "gw19"],
            "worker_count": 3,
            "test_count": 259,
            "missing_count": 50,
            "extra_count": 9,
            "missing": [f"test_legacy.py::m{n}" for n in range(50)],
            "extra": [f"test_shipping.py::e{n}" for n in range(9)],
            "modules": ["test_legacy.py", "test_shipping.py"],
            "module_count": 2,
        },
    ],
)


def test_the_headline_leads_with_scale_not_with_a_hash():
    first = str(MANY_WORKERS).splitlines()[1].strip()
    assert first == "20 workers produced 2 different collections"


def test_the_baseline_says_it_is_the_reference_rather_than_a_finding():
    baseline = str(MANY_WORKERS).splitlines()[2].strip()
    assert baseline.startswith("baseline: 17 workers collected 300 tests")


def test_a_variant_reads_as_a_sentence_about_what_it_did():
    assert "3 workers differ: 50 missing, 9 extra (gw3, gw11, gw19)" in str(MANY_WORKERS)


def test_the_text_shows_a_sample_in_diff_notation():
    rendered = str(MANY_WORKERS)
    assert "- test_legacy.py::m0" in rendered
    assert "+ test_shipping.py::e0" in rendered
    # Three per side in the text, however many the payload carries.
    assert rendered.count("        - ") == 3
    assert rendered.count("        + ") == 3


def test_the_text_says_how_much_it_is_not_showing():
    """A sample that looks like the whole story is worse than no sample."""
    # 59 differing ids, six of them printed.
    assert "and 53 more" in str(MANY_WORKERS)


def test_the_payload_carries_every_differing_id_even_though_the_text_does_not():
    """The alert is read by a person; the payload is read by a database, and
    the files on disk are on a machine you may never see again."""
    odd = MANY_WORKERS.variants[1]
    assert len(odd.missing) == odd.missing_count == 50
    assert len(odd.extra) == odd.extra_count == 9


def test_several_modules_move_off_the_headline():
    lines = [line.strip() for line in str(MANY_WORKERS).splitlines()]
    assert "across 2 modules: test_legacy.py, test_shipping.py" in lines
    # ...and stay out of the sentence naming the workers.
    assert "test_legacy.py, test_shipping.py (gw3" not in str(MANY_WORKERS)


def test_one_module_stays_in_the_sentence():
    single = CollectionMismatchIncident(
        worker="controller",
        worker_count=2,
        variant_count=2,
        variants=[
            {"digest": "aaa", "workers": ["gw0"], "worker_count": 1, "role": "baseline"},
            {
                "digest": "bbb",
                "workers": ["gw1"],
                "worker_count": 1,
                "missing_count": 1,
                "missing": ["test_orders.py::a"],
                "modules": ["test_orders.py"],
                "module_count": 1,
            },
        ],
    )
    assert "1 worker is missing 1 test, in test_orders.py (gw1)" in str(single)


def test_unstable_parameters_replace_the_variant_rows_entirely():
    """One near-identical block per worker, every one of them the same finding
    said differently, is worse than no detail at all."""
    incident = CollectionMismatchIncident(
        worker="controller",
        verdict="COLLECTION_PARAMETERS_UNSTABLE",
        worker_count=8,
        variant_count=8,
        parameters_unstable=True,
        unstable_tests=["test_pricing.py::test_rounding"],
        variants=[
            {"digest": f"d{n}", "workers": [f"gw{n}"], "worker_count": 1}
            for n in range(8)
        ],
    )
    rendered = str(incident)
    assert "only the parameter values differ" in rendered
    assert "test_pricing.py::test_rounding" in rendered
    assert "missing" not in rendered
    assert "baseline" not in rendered


def test_the_fingerprint_of_unstable_parameters_does_not_move_between_runs():
    """The ids change every run; the test names do not."""
    def incident(names):
        return CollectionMismatchIncident(
            worker="controller",
            verdict="COLLECTION_PARAMETERS_UNSTABLE",
            parameters_unstable=True,
            unstable_tests=names,
        )

    same = incident(["test_pricing.py::test_rounding"])
    again = incident(["test_pricing.py::test_rounding"])
    assert same.fingerprint_parts() == again.fingerprint_parts()
    assert same.fingerprint_parts() != incident(["test_other.py::test_x"]).fingerprint_parts()


def test_the_values_appear_under_the_test_that_produced_them():
    incident = CollectionMismatchIncident(
        worker="controller",
        verdict="COLLECTION_PARAMETERS_UNSTABLE",
        worker_count=6,
        variant_count=6,
        parameters_unstable=True,
        unstable_tests=["test_billing.py::test_invoice"],
        parameter_samples=[
            {
                "test": "test_billing.py::test_invoice",
                "workers": [
                    {"worker": "gw0", "values": ["acct-1791", "acct-3471"]},
                    {"worker": "gw1", "values": ["acct-2186", "acct-2542"]},
                ],
            }
        ],
    )
    lines = [line.strip() for line in str(incident).splitlines()]
    assert "test_billing.py::test_invoice" in lines
    assert "gw0 collected acct-1791, acct-3471" in lines
    assert "gw1 collected acct-2186, acct-2542" in lines


def test_a_partial_collection_report_says_it_is_partial():
    """Which variant holds the majority is only settled once every worker has
    voted, so a report assembled without them all must not read as final."""
    partial = CollectionMismatchIncident(
        worker="controller",
        worker_count=3,
        variant_count=2,
        complete=False,
    )
    assert "of the workers that answered" in str(partial)

    whole = CollectionMismatchIncident(
        worker="controller", worker_count=8, variant_count=2
    )
    assert "of the workers that answered" not in str(whole)


def test_a_stall_says_why_it_has_no_stack_rather_than_blaming_the_platform():
    silent = WorkerStallIncident(
        worker="gw1",
        stack_probed=False,
        stack_unavailable_reason="failure_stack_probe is off, so the worker "
        "was left undisturbed",
    )
    assert "no stack: failure_stack_probe is off" in str(silent)

    # Nothing to explain when a stack was obtained.
    answered = WorkerStallIncident(worker="gw1", stack=["  File \"x.py\", line 1 in f"])
    assert "no stack:" not in str(answered)
