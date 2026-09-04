"""A client session that is expensive to open, opened by a fixture per test.

Every test in an I/O-bound suite asks for a fresh session, and building one
derives a table of tokens the way somebody once did to make the first
request fast. A third of a second of a core: nothing on one test, a quarter
of an hour of a core over a thousand of them, and paid by every worker at
the same moment when they all start together. It is not a hotspot anybody
looks for, because the suite is 99% waiting - it is a burst, and the
profile's timeline is what finds it.
"""

from __future__ import annotations

import hashlib
import time


class Session:
    def __init__(self, tokens: int = 350_000) -> None:
        self.tokens = {
            index: hashlib.sha256(b"token-%d" % index).hexdigest() for index in range(tokens)
        }

    def request(self, seconds: float) -> dict:
        """The I/O the suite is made of: a wait, and a small answer."""
        time.sleep(seconds)
        return {"status": "ok"}
