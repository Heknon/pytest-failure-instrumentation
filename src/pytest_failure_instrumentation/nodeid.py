"""The hash that identifies a node id the stored copy cannot.

A node id is written down in a dozen places here, and in several of them it is
not written down whole. The worker's state slot is a fixed size and trims the
id to fit (:mod:`.capture.state` says why the *id* gives up its middle rather
than the record giving up its shape); the resource inventory caps it at a
kilobyte; a consumer's own column has a width of its own. Node ids have no
bound at all - a parametrize over file contents, a fixture that puts a payload
in the id, a matrix of a dozen dimensions, and the id is kilobytes.

What elision costs is identity. Two parametrized cases whose ids differ only
in the part that was dropped elide to the same string, and nothing downstream
can tell them apart or tell either from a short id that happens to look like
it. So every node id this package records is recorded twice: the id itself,
elided where something had to give, for a person to read - and beside it the
sha256 of the *whole* id, 64 characters however long the id was, for a machine
to join on, count by and match against its own collection.

The hash is always of the complete id. Where a record elides, the elision
happens after this is taken, which is the whole reason the two travel
together: a hash of the trimmed text would identify nothing that exists.

sha256 rather than something shorter: this is an identity somebody else's
database keys on and their pipeline groups by, so it has to stay
collision-free over every id of every run, not merely over one alert.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

#: Named once, so a consumer reading a payload can find out what the hex is
#: without reading this module.
ALGORITHM = "sha256"

#: Hex characters in one. Fixed, which is the point - it is the bound the node
#: id itself does not have.
HASH_LENGTH = 64


def hash_of(nodeid: str | None) -> str | None:
    """The sha256 of one *whole* node id, or None when there is no id.

    ``None`` rather than the hash of the empty string: a worker between tests
    has no test to identify, and a constant hash in that column is an id every
    idle worker in the fleet would share - which is exactly the join a
    consumer would then make.
    """
    if not nodeid:
        return None
    return hashlib.sha256(nodeid.encode("utf-8")).hexdigest()


def hashes_of(nodeids: Iterable[str]) -> list[str]:
    """The hashes of a list of node ids, positionally beside it.

    Same length and same order as what it was given, so index *n* of one list
    always describes index *n* of the other. An empty id keeps its place as an
    empty string rather than dropping out and shifting everything after it.
    """
    return [hash_of(nodeid) or "" for nodeid in nodeids]
