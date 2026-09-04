"""pytest raised an exception in its own machinery, and the run is over.

The controller's copy of this is second-hand: xdist relays a worker's internal
error as a flat string and re-raises it on the controller, so the INTERNALERROR
block names xdist's frame rather than the failure. The worker records the real
one as it happens; this prefers that, and says which it used.

Preferring the worker's copy is only right when it is a copy of *this* failure.
Three things can put an unrelated record in the evidence directory - a previous
run that left its files behind, a single-process run reading a distributed
run's leftovers, and a second worker that failed earlier in this one - and a
captured event chosen without checking is reported at high confidence, under
the wrong worker's name, describing an exception that did not happen. So a
candidate has to belong to this run and has to match the failure the controller
is holding; otherwise the controller's own text is the honest answer.

pytest also excludes INTERNAL_ERROR from the exit codes that get a terminal
summary, so ``pytest_terminal_summary`` never runs and nothing else reports it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import ConfigDict

from ..capture import events as event_log
from .base import Incident

EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b")


class InternalErrorIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    # Nothing carries on past this: pytest tears the session down.
    ends_run: ClassVar[bool] = True

    kind: Literal["internal_error"] = "internal_error"

    #: The last ``SomeError: message`` line, which is the actual failure.
    exception: str = ""
    #: The traceback, tail-truncated - a recursion error produces megabytes.
    detail: str = ""
    test_in_flight: Optional[str] = None
    #: False when this is xdist's re-raise of a worker's error rather than the
    #: error itself, which is what makes worker attribution unreliable.
    first_hand: bool = True

    def raw_stack(self) -> list[str]:
        return self.detail.splitlines()

    def blame_stack(self) -> tuple[list[str], bool]:
        return self.detail.splitlines(), True  # a traceback: outermost first

    def suspect_nodeid(self) -> str | None:
        return self.test_in_flight

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict, self.exception]

    def summary(self) -> str:
        if self.verdict == "INSTRUMENTATION_FAILED":
            return super().summary()
        line = "pytest hit an internal error"
        if self.exception:
            line += f": {self.exception}"
        if self.test_in_flight:
            line += f", while {self.worker} was running {self.test_in_flight}"
        elif self.worker and self.worker != "controller":
            line += f", on {self.worker}"
        if self.blamed_frame is not None:
            line += f", in {self.blamed_frame.named()}"
        return line


def build(
    excrepr: object,
    directory: Path,
    worker: str | None = None,
    run_id: str | None = None,
    distributed: bool = True,
) -> InternalErrorIncident:
    text = str(excrepr)
    captured = _captured(directory, run_id) if distributed else []
    if worker is not None:
        captured = [item for item in captured if item["worker"] == worker]
    chosen = _matching(captured, text)
    detail = chosen["detail"] if chosen else text

    if chosen:
        evidence = ["Captured on the worker itself, as it was raised."]
    elif "worker_internal_error" in text:
        # xdist relays a worker's internal error by re-raising it inside
        # worker_internal_error; that frame is the tell.
        evidence = [
            "Relayed from a worker through xdist's re-raise; which worker raised it "
            "is uncertain without worker-side capture."
        ]
    else:
        evidence = ["Raised on the controller and captured first-hand."]
    evidence.append("pytest ends the session on an internal error.")
    evidence.append("Look at: the full traceback, kept in the incident's detail field.")

    return InternalErrorIncident(
        worker=(chosen or {}).get("worker") or worker or "controller",
        verdict="INTERNAL_ERROR",
        confidence="high" if chosen else "low",
        exception=_exception_line(detail),
        detail=detail[-4000:],
        test_in_flight=(chosen or {}).get("test_in_flight"),
        first_hand=chosen is not None or "worker_internal_error" not in text,
        evidence=evidence,
    )


def _matching(
    captured: list[dict[str, Any]], text: str
) -> Optional[dict[str, Any]]:
    """The captured record that is this failure, or none of them.

    The exception line is what ties a worker's first-hand record to the string
    the controller was handed. Without that tie a second worker's earlier
    failure - or a file left behind by a previous run - is indistinguishable
    from the real one, and picking the first is picking alphabetically.
    """
    if not captured:
        return None
    wanted = _exception_line(text)
    if wanted:
        matching = [
            item for item in captured if _exception_line(item["detail"]) == wanted
        ]
        if matching:
            return matching[0]
    # No exception line to compare - a bare INTERNALERROR block, say. One
    # candidate is unambiguous; several are a guess, and a guess reported as
    # first-hand is worse than the controller's own second-hand text.
    return captured[0] if len(captured) == 1 else None


def _exception_line(detail: str) -> str:
    found = ""
    for line in detail.splitlines():
        stripped = line.strip()
        if EXCEPTION_LINE.match(stripped):
            found = stripped
    return found[:300]


def _captured(directory: Path, run_id: str | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.events")):
        events = event_log.of_run(event_log.read_events(path), run_id)
        for event in event_log.internal_errors(events):
            results.append(
                {
                    "worker": path.stem,
                    "detail": event.get("detail", ""),
                    "test_in_flight": event.get("nodeid"),
                }
            )
    return results
