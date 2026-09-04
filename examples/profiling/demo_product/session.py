"""A client session that is expensive to open, opened by a fixture per test.

Every test in an I/O-bound suite asks for a fresh session, and building one
derives a table of tokens the way somebody once did to make the first
request fast - stretched over a few rounds, because that is what you do to a
derived key. Four tenths of a second of a core: nothing on one test, seven
minutes of a core over a thousand of them, and paid by every worker at the
same moment when they all start together. It is not a hotspot anybody looks
for, because the suite is 99% waiting - it is a burst, and the profile's
timeline is what finds it.
"""

from __future__ import annotations

import hashlib
import time

#: Rounds of stretching per token. The cost is here rather than in the size
#: of the table on purpose: a burst is what this module is about, and a
#: bigger table would be a memory finding as well and say two things at once.
ROUNDS = 6


class Session:
    def __init__(self, tokens: int = 150_000) -> None:
        self.tokens = {}
        for index in range(tokens):
            digest = b"token-%d" % index
            for _ in range(ROUNDS):
                digest = hashlib.sha256(digest).digest()
            self.tokens[index] = digest.hex()

    def request(self, seconds: float) -> dict:
        """The I/O the suite is made of: a wait, and a small answer."""
        time.sleep(seconds)
        return {"status": "ok"}
