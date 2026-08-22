"""pytest raised an exception in its own machinery, and the run is over.

The controller's copy of this is second-hand: xdist relays a worker's internal
error as a flat string and re-raises it on the controller, so the INTERNALERROR
block names xdist's frame rather than the failure. The worker records the real
one as it happens; this prefers that, and says which it used.

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

    def stack_lines(self) -> tuple[list[str], bool]:
        return self.detail.splitlines(), True  # a traceback: outermost first

    def suspect_nodeid(self) -> str | None:
        return self.test_in_flight

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict, self.exception]

    def details(self) -> list[str]:
        lines = [self.exception] if self.exception else []
        if self.test_in_flight:
            lines.append(f"in flight {self.test_in_flight}")
        return lines


def build(excrepr: object, directory: Path, worker: str | None = None) -> InternalErrorIncident:
    text = str(excrepr)
    captured = _captured(directory)
    if worker is not None:
        captured = [item for item in captured if item["worker"] == worker]
    chosen = captured[0] if captured else None
    detail = chosen["detail"] if chosen else text

    if chosen:
        evidence = ["captured on the worker itself, as it was raised"]
    elif "worker_internal_error" in text:
        # xdist relays a worker's internal error by re-raising it inside
        # worker_internal_error; that frame is the tell.
        evidence = [
            "relayed from a worker through xdist's re-raise; worker attribution "
            "is unreliable without worker-side capture"
        ]
    else:
        evidence = ["raised on the controller itself and captured first-hand"]

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


def _exception_line(detail: str) -> str:
    found = ""
    for line in detail.splitlines():
        stripped = line.strip()
        if EXCEPTION_LINE.match(stripped):
            found = stripped
    return found[:300]


def _captured(directory: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.events")):
        events = event_log.read_events(path)
        for event in event_log.internal_errors(events):
            results.append(
                {
                    "worker": path.stem,
                    "detail": event.get("detail", ""),
                    "test_in_flight": event.get("nodeid"),
                }
            )
    return results
