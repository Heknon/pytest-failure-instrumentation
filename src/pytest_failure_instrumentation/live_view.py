"""What a product needs to reach the live-stack server, delivered once it is up.

The server is started at session start and binds on a thread of its own, so
there is no moment in a hook where a product could reliably ask "is it serving,
and on what?". A drawn port makes that worse rather than better: the number is
one nobody can guess, which is the point of drawing it, and it means the
address cannot be configured ahead of the run at either end.

So the run says so when it happens. :func:`hookspec.pytest_failure_server_ready`
is called with one of these the moment the server is serving, and everything
needed to talk to it is on it - which is the whole payload's reason to exist,
since a UI that has to assemble a URL out of settings it half-knows is a UI
that gets it wrong on the run where the port was drawn.

**The run id is deliberately not here.** At the moment the server binds, xdist
has usually not built its node manager yet, so this run's real id does not
exist - and stamping the stand-in onto a row a product will join against would
be a key that matches nothing, quietly. What is stable from the first moment is
the evidence directory, which is what ``/workers`` reports on, and ``/workers``
carries the run id per directory as soon as the first worker beats. So the join
is directory now, run id when there is one.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class LiveStackServer(BaseModel):
    """Where the live view is, and what it takes to be let in."""

    model_config = ConfigDict(extra="forbid")

    #: The service name this answers ``/identity`` with. Pinned in the payload
    #: so a product can tell it is talking to one of ours rather than to
    #: whatever else has since taken the address.
    service: str = ""
    #: The version of this package, which is what dates the wire format.
    version: str = ""

    #: Ready to have ``/workers`` or ``/stack`` appended - already bracketed if
    #: the host is an IPv6 literal, which is the part hand-assembly gets wrong.
    url: str = ""
    host: str = ""
    #: What got *bound*, never what was asked for. A drawn port is requested as
    #: 0 and a caller that stored the request would store a 0.
    port: int = 0

    #: The process serving. Under xdist this is the controller, which is not
    #: any of the pids ``/workers`` reports on.
    pid: int = 0

    #: This run's evidence directory, and the join key to use until there is a
    #: run id - see the module docstring. ``None`` when the run was not writing
    #: evidence at all, in which case ``/workers`` has nothing to report and
    #: only ``/stack`` is useful.
    directory: Optional[str] = None
    #: What names that directory. Stable from the first moment of the run,
    #: unlike the run id.
    session_id: str = ""

    def endpoint(self, path: str) -> str:
        """``url`` and ``path`` joined without caring who brought the slash."""
        return f"{self.url.rstrip('/')}/{path.lstrip('/')}"
