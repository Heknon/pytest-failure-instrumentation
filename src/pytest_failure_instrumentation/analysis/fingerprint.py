"""A stable identity for an incident, so recurrences group instead of pile up.

Deliberately excludes worker id, pid, timings and memory figures: the same
defect on gw3 today and gw17 tomorrow is one incident with a count, not two
rows. What identifies a kind is the kind's business, so the parts come from the
model - see ``Incident.fingerprint_parts``. When there is no frame to hash, the
phase and the test's module stand in, otherwise every stray SIGKILL collapses
into a single meaningless bucket.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..incidents.base import Frame, Incident


def of(incident: Incident, frame: Frame | None) -> str:
    parts = list(incident.fingerprint_parts())
    if frame is not None:
        parts += [frame.function, Path(frame.file).name]
    else:
        nodeid = incident.suspect_nodeid() or ""
        parts += [
            getattr(incident, "phase", None) or "no-phase",
            nodeid.split("::")[0] or "no-test",
        ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
