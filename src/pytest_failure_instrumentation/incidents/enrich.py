"""What every incident gets after its kind has said its piece.

Blame, severity, fingerprint, the run id, the capabilities of the machine:
identical for every kind, and none of it knowable while the kind-specific
facts are being gathered. It lived in the engine, which is the ordinary
caller. It is a function because the engine is no longer the only one: a run
whose controller was killed is reported by the sidecar that outlived it - see
:mod:`.reporter` - and an incident that reaches a consumer by that road has to
carry exactly what one raised through the hook would.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .. import probes
from ..analysis import fingerprint as fingerprint_of
from ..analysis import severity as severity_of
from ..analysis.attribution import Attributor
from .base import UNSET_RUN_ID, Capabilities, Incident, frame_from


def enrich(
    incident: Incident,
    attributor: Attributor,
    product_version: Optional[str],
    run_id: str,
) -> None:
    """Fill in everything that is the same whatever kind this is.

    ``run_id`` is what an incident that has not settled its own is stamped
    with. A death recovered from a directory somebody else left behind
    carries the id of the run that died and is left alone: it is the key a
    consumer joins on, and the run that merely found it is not the run it
    happened in.
    """
    lines, reverse = incident.blame_stack()
    blame = attributor.blame(lines, reverse=reverse)
    incident.top_frame = frame_from(blame["top_frame"])
    incident.blamed_frame = frame_from(blame["blamed_frame"])
    incident.owner = blame["owner"] or "unknown"
    if incident.owner == "unknown":
        # A kind that fails before anybody's code runs knows its own owner;
        # attribution had no frames to find it from.
        incident.owner = incident.owner_when_unattributable() or "unknown"

    nodeid = incident.suspect_nodeid()
    if incident.owner == "unknown" and nodeid:
        path = str(nodeid).split("::")[0]
        if path:
            incident.suspect_owner = attributor.owner_of(str(Path(path).resolve()))
            incident.suspect_basis = incident.suspect_basis_for(path)

    incident.run_ending = incident.ends_this_run()
    severity, why = severity_of.of(
        incident.kind, incident.owner, incident.verdict,
        incident.confidence, incident.run_ending,
    )
    incident.severity = severity
    if why:
        incident.evidence.append(why)

    incident.fingerprint = fingerprint_of.of(incident, incident.blamed_frame)
    if incident.run_id == UNSET_RUN_ID:
        incident.run_id = run_id
    incident.raised_at = time.time()
    incident.capabilities = Capabilities(**probes.capabilities())
    incident.product_version = product_version
