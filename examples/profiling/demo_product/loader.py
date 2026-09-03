"""A loader that reads the whole thing before doing anything with it.

Streaming would hold one record at a time. This holds every record, and then
a second copy while it decodes them - so the worker climbs by the size of the
input twice over and comes back down when the test returns. Nothing leaks,
nothing fails, and a machine with eight of these workers runs out of memory
on the day eight tests line up.
"""

from __future__ import annotations


def load_everything(records: int) -> int:
    payload = b"".join(b"record %d\n" % index * 40 for index in range(records))
    decoded = payload.decode("ascii").splitlines()
    return len(decoded)
