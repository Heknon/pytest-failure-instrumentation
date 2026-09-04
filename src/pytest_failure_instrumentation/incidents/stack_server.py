"""The live-stack server could not start, and nothing else would have said so.

Every other kind reports something that went wrong with the run. This one
reports that a thing the run was *asked* to provide is not there - which is a
different claim, and worth making for the same reason the rest of this package
exists: an absence nobody announces gets read as a negative result.

Somebody switched the server on deliberately. If the port turns out to be held
by a dev server, or cannot be bound at all, the run continues perfectly well
and their UI simply shows nothing forever - with no error anywhere, because
from the outside "no server" and "no tests running" look identical. The status
string on the service says what happened, and nothing reads status strings.

**Not raised when another of our own sessions holds the port.** That is the
named mode working exactly as designed: this session stands down, waits, and
takes over when the holder exits. Reporting it would turn the ordinary case
into an alert and teach people to ignore the kind.

The run is unaffected, so this is owned by the runtime and scored
informational: no test is at fault, nothing is broken, and what is lost is a
diagnostic that somebody has to decide whether to reconfigure.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import ConfigDict

from .base import Incident

#: A port held by something that is not one of ours - a dev server, a proxy,
#: whatever happened to be there. Recoverable by naming a different one.
PORT_TAKEN = "PORT_TAKEN"

#: The address could not be bound at all: a host that is not an interface on
#: this machine, or a sandbox that forbids listening. Naming another port does
#: not help, so the two are separate verdicts rather than one with a message.
BIND_REFUSED = "BIND_REFUSED"


class StackServerIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    ends_run: ClassVar[bool] = False

    kind: Literal["stack_server_unavailable"] = "stack_server_unavailable"

    #: What was asked for. 0 means a port was to be drawn, which makes a
    #: failure here a bind problem rather than a contention one.
    requested_port: int = 0
    host: str = ""
    #: Whether a port was drawn or named, because it changes what to do next.
    drawn: bool = True
    #: The service's own account of it, which names the option to change.
    detail: str = ""

    def owner_when_unattributable(self) -> Optional[str]:
        """Nobody's test is at fault and there is no stack to say otherwise.

        Left to attribution this would be "unknown", which means "we could not
        tell" and is scored needs-triage - and here it was known before the
        incident was built.
        """
        return "runtime"

    def fingerprint_parts(self) -> list[str]:
        """One alert per address per run, not one per retry.

        A named port held by a stranger is re-probed every few seconds for the
        life of the run. The service reports it once, and this is the second
        guard: the same port failing the same way is the same incident.
        """
        return [self.kind, self.verdict, self.host, str(self.requested_port)]

    def summary(self) -> str:
        where = "a drawn port" if self.drawn else f"port {self.requested_port}"
        return f"No live stack view this run: {self.host} could not serve on {where}"

    def details(self) -> list[str]:
        # The service's own account: what held the port or refused the bind,
        # and the option that names a different one. One line, capitalised
        # and closed like the rest.
        if not self.detail:
            return []
        text = self.detail.strip()
        return [text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")]


def build(
    verdict: str, host: str, requested_port: int, detail: str
) -> StackServerIncident:
    drawn = requested_port == 0
    return StackServerIncident(
        worker="controller",
        verdict=verdict,
        # The service knows exactly what happened - it either bound or it did
        # not, and it either recognised the holder or it did not.
        confidence="high",
        host=host,
        requested_port=requested_port,
        drawn=drawn,
        detail=detail,
        # The detail is a ``details()`` line and not an evidence line as
        # well. The alert text is the product here, and a fact printed twice
        # reads as two findings.
        evidence=[
            "The run itself is unaffected; only the live view is missing.",
        ],
    )
