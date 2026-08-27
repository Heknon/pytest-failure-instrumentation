"""One per run, whose *absence* is the finding.

Every other incident says something went wrong. This one says the process that
was doing the reporting reached the end - so a run with no summary is a run
whose controller died, which nothing inside that process can tell you.

Emitted for a single-process run as much as a distributed one; it is not a
report about workers.
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
    #: Whether the run was distributed. The same summary is emitted either
    #: way, and a reader triaging one needs to know which kinds could have
    #: arrived: a single-process run has no workers to lose.
    distributed: bool = False
    #: How many of them ended the session. pytest's exit status at session
    #: finish is sometimes reported before INTERNAL_ERROR is applied, so a run
    #: killed by an internal error can still show 0 above; this contradicts it.
    run_ending_incidents: int = 0

    def headline(self) -> str:
        # No owner, because nothing failed: printing owner=unknown here reads
        # as an unattributed failure rather than as a clean end of run.
        return f"[{self.kind}] {self.verdict}  severity={self.severity}"

    def details(self) -> list[str]:
        mode = "distributed" if self.distributed else "single process"
        if not self.raised:
            return [f"no incidents ({mode})"]
        line = f"{self.raised} incident(s) over {len(self.incidents)} distinct fingerprint(s)"
        if self.run_ending_incidents:
            line += f", {self.run_ending_incidents} run-ending"
        return [f"{line} ({mode})"]


def build(
    exitstatus: int,
    seen: dict[str, int],
    raised: int,
    suppressed: int,
    run_ending: int = 0,
    distributed: bool = False,
) -> RunSummaryIncident:
    return RunSummaryIncident(
        worker="controller" if distributed else "main",
        verdict="RUN_FINISHED",
        confidence="high",
        exitstatus=int(exitstatus),
        incidents=dict(seen),
        raised=raised,
        duplicates_suppressed=suppressed,
        run_ending_incidents=run_ending,
        distributed=distributed,
        evidence=[
            f"run ended with exit status {exitstatus}",
            f"{raised} incident(s) raised, {suppressed} duplicate(s) suppressed "
            "by fingerprint",
        ]
        + (
            [
                f"{run_ending} of them were raised as run-ending, and the run "
                "still reached session finish: either the condition resolved - "
                "a wedged worker that came back is the usual one - or the exit "
                "status above is what pytest reported before it applied "
                "INTERNAL_ERROR"
            ]
            # Only when the exit status does not already show it: pytest applies
            # INTERNAL_ERROR after some paths through sessionfinish and before
            # others, so the note is a correction, not a caption.
            #
            # And it says "raised as run-ending" rather than "ended the
            # session", because this line only exists at all in a run that
            # reached session finish. run_ending is the inference the evidence
            # supported when the incident was raised - a worker silent past the
            # threshold has handed xdist work it will never give back - and a
            # summary is the one thing emitted late enough to know that it did
            # not happen. Asserting it here reported a run that passed as one
            # that could not complete.
            if run_ending and exitstatus == 0
            else []
        ),
    )
