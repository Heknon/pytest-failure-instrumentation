"""Module-level state that outlives the test that filled it."""

from __future__ import annotations

_RESULTS: list[bytes] = []


def remember(result: bytes) -> None:
    _RESULTS.append(result)


def remembered() -> int:
    return len(_RESULTS)
