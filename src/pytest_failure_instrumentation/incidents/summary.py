"""One per run, whose *absence* is the finding.

Every other incident says something went wrong. This one says the controller
reached the end and reported it - so a run with no summary is a run whose
controller died, which no hook can tell you from inside the process.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import ConfigDict, Field

from .base import Incident


class RunSummaryIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    ends_run: ClassVar[bool] = False

    kind: Literal["run_summary"] = "run_summary"

    exitstatus: int = 0
    #: fingerprint -> how many times it was seen this run.
    incidents: dict[str, int] = Field(default_factory=dict)
    raised: int = 0
    duplicates_suppressed: int = 0
    #: How many of them ended the session. pytest's exit status at session
    #: finish is reported before INTERNAL_ERROR is applied, so a run killed by
    #: an internal error can still show 0 above; this is what contradicts it.
    run_ending_incidents: int = 0

    def headline(self) -> str:
        # No owner, because nothing failed: printing owner=unknown here reads
        # as an unattributed failure rather than as a clean end of run.
        return f"[{self.kind}] {self.verdict}  severity={self.severity}"

    def details(self) -> list[str]:
        if not self.raised:
            return ["no incidents"]
        line = f"{self.raised} incident(s) over {len(self.incidents)} distinct fingerprint(s)"
        if self.run_ending_incidents:
            line += f", {self.run_ending_incidents} run-ending"
        return [line]


def build(
    exitstatus: int,
    seen: dict[str, int],
    raised: int,
    suppressed: int,
    run_ending: int = 0,
) -> RunSummaryIncident:
    return RunSummaryIncident(
        worker="controller",
        verdict="RUN_FINISHED",
        confidence="high",
        exitstatus=int(exitstatus),
        incidents=dict(seen),
        raised=raised,
        duplicates_suppressed=suppressed,
        run_ending_incidents=run_ending,
        evidence=[
            f"run ended with exit status {exitstatus}",
            f"{raised} incident(s) raised, {suppressed} duplicate(s) suppressed "
            "by fingerprint",
        ]
        + (
            [
                f"{run_ending} of them ended the session; the exit status above "
                "is what pytest reported at session finish, before it applied "
                "INTERNAL_ERROR"
            ]
            if run_ending
            else []
        ),
    )
