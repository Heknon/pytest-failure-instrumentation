"""What every incident carries, and how one renders itself as an alert.

Every kind carries different evidence. A segfault's resident memory explains
nothing; a collection mismatch has no exit status at all; a run summary has
neither. So the payload is a model per kind rather than one wide dict with most
of its keys empty, and ``kind`` is the discriminator that tells them apart -
see :mod:`.registry` for parsing a stored row back into the right model.

Field types are deliberately loose - ``str``, not ``Literal``. This code runs
while something is already going wrong, and a validation error raised out of a
crash-reporting path would end the very run it exists to explain. What is
strict is the *shape*: ``extra="forbid"`` means a builder that invents a field
is caught in development rather than writing a column nobody reads.

Nothing here is imported by the worker-side capture path. Pydantic is loaded on
the controller, and only once something has already died.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field


class Frame(BaseModel):
    """One line of a stack, with the answer to "whose code is this"."""

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int
    function: str
    module: str
    owner: str

    def __str__(self) -> str:
        return f"{Path(self.file).name}:{self.line} in {self.function}"


class CgroupMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_mb: Optional[int] = None
    peak_mb: Optional[int] = None
    max_mb: Optional[int] = None


class Capabilities(BaseModel):
    """What the machine could measure, so a reader can tell "nothing went
    wrong" from "unmeasurable here"."""

    model_config = ConfigDict(extra="allow")

    platform: str = ""
    system: str = ""
    python: str = ""
    resident_memory: str = "unavailable"
    system_memory: str = "unavailable"
    cgroup_oom_counter: bool = False
    exit_status: str = "unavailable"
    live_stack: bool = False
    #: Whether a process can be read from *outside* it, which is the only way
    #: to get a stack out of a worker whose GIL is held by native code.
    #: Declared rather than left to ``extra="allow"`` so that it reaches the
    #: JSON schema a consumer migrates their table from.
    external_stack: str = "unavailable"
    psutil: bool = False


class Incident(BaseModel):
    """The fields every kind shares. Subclass per kind; never emitted itself."""

    model_config = ConfigDict(extra="forbid")

    #: True for kinds that end the session rather than costing it one worker.
    #: xdist replaces a dead worker and carries on; it cannot carry on past an
    #: internal error, and cannot finish at all while a worker is wedged. Read
    #: by severity, which raises a framework-owned defect that ends the run
    #: above informational - no test is at fault, so nothing else will ever
    #: surface it. Kinds where it depends on the instance override
    #: ``ends_this_run``.
    ends_run: ClassVar[bool] = False

    kind: str
    worker: str
    verdict: str = "UNKNOWN"
    confidence: str = "low"
    evidence: list[str] = Field(default_factory=list)

    # Filled by the engine, after the builder: identical for every kind, and
    # none of it knowable while the kind-specific facts are being gathered.
    severity: str = "needs-triage"
    owner: str = "unknown"
    run_ending: bool = False
    fingerprint: str = ""
    run_id: str = "unknown"
    raised_at: float = 0.0
    product_version: Optional[str] = None
    capabilities: Optional[Capabilities] = None
    top_frame: Optional[Frame] = None
    blamed_frame: Optional[Frame] = None
    #: A lead, not a conclusion - kept apart from ``owner`` so a guess never
    #: sits in the column a reader takes for a finding.
    suspect_owner: Optional[str] = None
    suspect_basis: Optional[str] = None

    # -- what the engine asks each kind ----------------------------------

    def ends_this_run(self) -> bool:
        """Whether *this* incident ended the session. Constant for most kinds."""
        return type(self).ends_run

    def owner_when_unattributable(self) -> Optional[str]:
        """Who owns this when there is no stack to work it out from.

        Attribution reads frames, and a kind with no frames gets "unknown" -
        which means "we could not tell" and is scored ``needs-triage``. For a
        kind that fails without ever running anybody's code, that is not
        uncertainty being reported honestly, it is a certainty being thrown
        away: nobody's test is at fault and the answer was known before the
        incident was built.

        Only consulted when attribution came back unknown, so a kind that does
        have a stack is never overridden by a guess made in its absence.
        """
        return None

    def raw_stack(self) -> list[str]:
        """Everything captured, as text, whatever kind this is.

        For a caller adding the stack to its own alert. Deliberately kept out
        of ``__str__``: forty frames turn a readable incident into a wall, and
        whether they belong in an alert is a decision only the caller can
        make. Which field holds them differs per kind, and this is so that
        nobody has to switch on ``kind`` to find out.
        """
        return []

    def blame_stack(self) -> tuple[list[str], bool]:
        """The text to attribute *this failure* to, and whether it reads
        outermost first. faulthandler prints deepest first; tracebacks do not.

        Not the same as :meth:`raw_stack`, and the difference matters: a dump
        that did not come from the failure is still worth handing to a reader
        as context, and must still never be blamed for it.
        """
        return [], False

    def suspect_nodeid(self) -> str | None:
        """A test to fall back to when no stack named anybody."""
        return None

    def suspect_basis_for(self, path: str) -> str:
        """Why that test points at whoever it points at. A guess has to say
        what kind of guess it is."""
        return f"owner of the test in flight ({path})"

    def fingerprint_parts(self) -> list[str]:
        """What makes this incident the same incident as another one.

        Excludes worker id, pid, timings and memory figures: the same defect on
        gw3 today and gw17 tomorrow is one incident with a count, not two rows.
        """
        return [self.kind, self.verdict]

    # -- rendering -------------------------------------------------------

    def details(self) -> list[str]:
        """The kind's own facts, one line each. Overridden per kind."""
        return []

    def headline(self) -> str:
        line = (
            f"[{self.kind}] {self.verdict}  severity={self.severity}  "
            f"owner={self.owner}"
        )
        return line + "  run-ending" if self.run_ending else line

    def __str__(self) -> str:
        """The alert body. One incident, readable without opening the record."""
        lines = [self.headline()]
        if self.blamed_frame is not None:
            lines.append(f"blamed on {self.blamed_frame}")
        elif self.suspect_owner:
            lines.append(f"no stack; suspect {self.suspect_owner} ({self.suspect_basis})")
        lines.extend(self.details())
        lines.extend(f"· {item}" for item in self.evidence)
        return "\n    ".join(lines)

    # -- failure of the instrumentation itself ---------------------------

    @classmethod
    def degraded(cls, worker: str, failure: BaseException, context: str = "") -> Incident:
        """An incident saying only that gathering the real one failed.

        Every field below the two required ones has a default precisely so this
        works for any kind. Losing the detail is acceptable; letting the
        reporting path raise is not - it would surface as an INTERNALERROR and
        end the customer's run.
        """
        evidence = [
            f"the instrumentation failed while building this incident: {failure!r}",
            "the facts below are all that survived; the underlying failure was real",
        ]
        if context:
            evidence.append(context)
        # `kind` is deliberately not defaulted on the base - a subclass that
        # forgot it would otherwise be emitted under a meaningless one, and
        # test_the_base_is_never_emitted_directly holds that line. Every
        # concrete kind supplies it as a Literal default, so the call below is
        # complete for every class this is ever reached on; mypy only sees the
        # base, where it is not.
        return cls(  # type: ignore[call-arg]
            worker=worker, verdict="INSTRUMENTATION_FAILED", evidence=evidence
        )


def frame_from(raw: dict[str, Any] | None) -> Frame | None:
    return Frame(**raw) if raw else None
