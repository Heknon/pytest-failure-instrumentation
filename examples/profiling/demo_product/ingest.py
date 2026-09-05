"""An ingest path that parses records on a thread pool and indexes them.

Every worker thread that allocates is handed a malloc arena of its own by
glibc, up to eight per core. The parsed payloads are freed as soon as they
are indexed, but the index entry - a few hundred bytes - stays, and it sits
in whichever arena's heap the thread was using. A heap cannot shrink below
its highest live chunk, so each arena keeps the freed payloads around it
mapped: the process grows by the payloads' size and holds almost none of it.
"""

from concurrent.futures import ThreadPoolExecutor

_INDEX: list[bytearray] = []


def _parse(seed: int, count: int) -> int:
    payloads = []
    for index in range(count):
        # Under the allocator's mmap threshold, so it comes from the arena.
        payloads.append(bytearray(60_000 + ((index * seed) % 13) * 5_000))
        # The entry that survives, between the payloads that do not.
        _INDEX.append(bytearray(600))
    return len(payloads)


def ingest(batches: int = 8, records: int = 300) -> int:
    """Parse ``batches`` batches of ``records`` each, one batch per thread."""
    with ThreadPoolExecutor(max_workers=batches) as pool:
        return sum(pool.map(lambda seed: _parse(seed + 1, records), range(batches)))


def indexed() -> int:
    return len(_INDEX)
