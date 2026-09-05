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

from ..config import SOLE_WORKER
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

    def tag(self) -> str:
        # No owner, because nothing failed: an owner slot reading "unknown"
        # here reads as an unattributed failure rather than as a clean end.
        return f"[{self.kind} {self.verdict}, {self.severity}]"

    def summary(self) -> str:
        mode = "distributed" if self.distributed else "single process"
        line = f"Run finished with exit status {self.exitstatus}"
        if not self.raised:
            return f"{line} and no incidents ({mode})"
        counted = f"{self.raised} incident{'s' if self.raised != 1 else ''}"
        distinct = len(self.incidents)
        if distinct != self.raised:
            counted += f" over {distinct} distinct fingerprint{'s' if distinct != 1 else ''}"
        if self.run_ending_incidents:
            counted += f", {self.run_ending_incidents} of them raised as run-ending"
        if self.duplicates_suppressed:
            counted += f", {self.duplicates_suppressed} duplicate{'s' if self.duplicates_suppressed != 1 else ''} suppressed"
        return f"{line}: {counted} ({mode})"


def build(
    exitstatus: int,
    seen: dict[str, int],
    raised: int,
    suppressed: int,
    run_ending: int = 0,
    distributed: bool = False,
) -> RunSummaryIncident:
    return RunSummaryIncident(
        worker="controller" if distributed else SOLE_WORKER,
        verdict="RUN_FINISHED",
        confidence="high",
        exitstatus=int(exitstatus),
        incidents=dict(seen),
        raised=raised,
        duplicates_suppressed=suppressed,
        run_ending_incidents=run_ending,
        distributed=distributed,
        evidence=(
            [
                f"{run_ending} incident{'s were' if run_ending != 1 else ' was'} raised as "
                "run-ending, and the run still reached session finish: either the "
                "condition resolved, as when a wedged worker comes back, or pytest "
                "reported this exit status before applying INTERNAL_ERROR."
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
