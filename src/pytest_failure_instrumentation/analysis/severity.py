"""Who needs to act, which is not the same as how loud the failure was."""

from __future__ import annotations

#: The verdicts that mean somebody outside the run stopped it, and a witness
#: saw who. No test is at fault, so no test is suspected and nobody is paged.
#:
#: It lives here, in the module with no imports of its own, because the two
#: consequences are drawn in two places - :func:`of` below scores them
#: informational, and ``WorkerDeathIncident.suspect_nodeid`` declines to name
#: a test - and a verdict added to one list and not the other gets half of
#: each. :mod:`.classify` re-exports it for that second reader.
DELIBERATE_STOPS = frozenset(
    {
        "KILLED_BY_PROCESS",
        "KILLED_AFTER_SIGTERM",
        # A run found dead afterwards whose controller had been told to stop.
        # The same cancellation as the two above, witnessed from the other
        # side: the SIGTERM is on the controller's log rather than on this
        # process, and the process ended with the run.
        "RUN_STOPPED",
    }
)

#: Kinds the profiler raises. Findings rather than failures, and scored as such.
PROFILE_KINDS = ("cpu_hotspot", "cpu_burst", "memory_profile")

BY_OWNER = {
    "product": "critical",
    "third-party": "high",
    "customer-code": "informational",
    # Nobody's test is at fault: the framework itself failed. Recorded, not
    # paged - though see the run-ending case below.
    "runtime": "informational",
    "unknown": "needs-triage",
}


def of(
    kind: str, owner: str, verdict: str, confidence: str, ends_run: bool
) -> tuple[str, str | None]:
    """Returns (severity, why) - the reason only when it overrides the default.

    ``ends_run`` is the kind's own declaration (``Incident.ends_run``): xdist
    replaces a dead worker and carries on, but it cannot carry on past an
    internal error, and cannot finish at all while a worker is wedged.
    """
    if kind == "run_summary":
        return "informational", None
    if kind in PROFILE_KINDS:
        # Nothing failed. A hotspot in the product is a flag for somebody to
        # look at, not a page for somebody to answer, whoever owns it.
        return "informational", None
    if verdict.startswith("SIGNAL_") and confidence == "high":
        # A deliberate stop signal, already identified as such. No owner to
        # find and nothing for anyone to do.
        return "informational", None
    if verdict in DELIBERATE_STOPS:
        # Somebody outside the run stopped it, and a witness saw who. That is
        # a cancellation or a timeout, not a defect in anybody's code - the
        # sender is on the incident for whoever wants to take it up with them.
        return "informational", None
    if ends_run and owner == "runtime":
        return "high", (
            "Severity high rather than informational: a defect in the framework ended "
            "the run, and no test is at fault, so nothing else reports it."
        )
    return BY_OWNER.get(owner, "needs-triage"), None
