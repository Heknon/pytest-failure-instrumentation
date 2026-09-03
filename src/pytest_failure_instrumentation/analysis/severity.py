"""Who needs to act, which is not the same as how loud the failure was."""

from __future__ import annotations

#: Kinds the profiler raises. Findings rather than failures, and scored as such.
PROFILE_KINDS = ("cpu_hotspot", "memory_profile")

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
    if ends_run and owner == "runtime":
        return "high", (
            "raised above informational: a framework-owned defect that ended the "
            "run - no test is at fault, so nothing else will surface it"
        )
    return BY_OWNER.get(owner, "needs-triage"), None
