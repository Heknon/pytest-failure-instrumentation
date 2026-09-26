"""Attributes a test carries into the evidence, for a reader to group by.

A product's dashboards are cut by things this package cannot know: which team
owns a test, which component it exercises, which suite it belongs to. They are
facts about the *test*, and a test exists only where it runs - a worker, under
xdist - while everything that reads them runs somewhere else: the controller
raising an incident, a later run recovering a killed one, the sampler, the
metrics adapter. So they are read once per test, where the test is, and
written into the worker's state slot beside the node id, which every one of
those readers already reads. A death in setup, a run killed outright and a
wedged worker all carry them, because the slot is written as the test begins
and outlives the process that wrote it.

The test report would have been the other road, and it misses exactly the
cases this package exists for: a worker that dies in setup never sends one,
and a killed controller takes whatever it had been sent with it.

Two sources, most specific last:

- ``pytest_failure_test_attributes(item)``, implemented in a conftest or a
  plugin. Called through the item's own hook proxy, so a conftest answers only
  for the tests beneath it, and the one nearest the test is heard last.
- ``failure_test_attributes``: marker names whose first argument is the value
  (``@pytest.mark.team("payments")``), or ``"true"`` for a marker with none.
  A marker is on the test itself, so it has the last word over a conftest's
  default for its directory.

They are dimensions, not data: a metric grows a series for every distinct
value, so the set is bounded here - a key is a short name, a value a short
string, and the whole of it fits in :data:`MAX_BYTES` of a slot that must still
hold the node id. Anything over is dropped and said once rather than cut: a
truncated value is a different value, and two tests would share a tag neither
of them has.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Optional

from .config import advise

#: The record key, in a state slot and in every payload that carries them.
KEY = "test_attributes"

#: How many a test may carry. A dashboard cuts by a handful of dimensions;
#: a dict with dozens of keys is a record, and belongs in the report.
MAX_KEYS = 16
MAX_KEY_LENGTH = 64
MAX_VALUE_LENGTH = 128
#: What the whole set may cost the slot, encoded. The slot is 5 KiB and the
#: node ids are the only other fields that can grow, so this leaves them
#: nearly two thousand characters each before either has to be elided.
MAX_BYTES = 1024

#: A name a metric attribute, a column and a log field can all take as is.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")


class Collector:
    """Reads one test's attributes, on whichever process runs it.

    One per recorder. Whether anything implements the hook is decided at the
    first test rather than at construction: a recorder is built at configure
    time, and the conftests that implement it are loaded by collection, which
    has finished by the time the first test is set up.
    """

    def __init__(self, markers: tuple[str, ...] = ()) -> None:
        self.markers = tuple(markers)
        self.hooked: Optional[bool] = None
        #: What has been said already, so a problem with a key that every
        #: test has is one warning for the run rather than one per test.
        self._said: set[str] = set()

    def __call__(self, item: Any) -> dict[str, str]:
        """This test's attributes, bounded; empty when nothing asks for any.

        Never raises. A test is recorded without attributes rather than not
        at all - the rule every write on this path keeps.
        """
        try:
            if self.hooked is None:
                self.hooked = _implemented(item)
            if not self.markers and not self.hooked:
                return {}
            return self._bounded(self._gather(item))
        except Exception as failure:  # noqa: BLE001 - never break a test
            self._say("gather", f"test attributes could not be read: {failure!r}")
            return {}

    def _gather(self, item: Any) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if self.hooked:
            try:
                answers = item.ihook.pytest_failure_test_attributes(item=item)
            except Exception as failure:  # noqa: BLE001 - the product's code
                self._say(
                    "raised",
                    f"pytest_failure_test_attributes raised {failure!r}; tests it "
                    "raises for are recorded without its attributes",
                )
                answers = []
            # pluggy calls the plugin registered last first, and a conftest
            # nearer the test is registered after one further out - so the
            # answers arrive nearest first, and are applied the other way.
            for answer in reversed(answers or []):
                if isinstance(answer, Mapping):
                    found.update(answer)
                elif answer is not None:
                    self._say(
                        "mapping",
                        "pytest_failure_test_attributes returned a "
                        f"{type(answer).__name__}, not a mapping; it is ignored",
                    )
        for name in self.markers:
            marker = item.get_closest_marker(name)
            if marker is not None:
                found[name] = marker.args[0] if marker.args else True
        return found

    def _bounded(self, found: dict[str, Any]) -> dict[str, str]:
        kept: dict[str, str] = {}
        for key in sorted(found, key=str):
            if not isinstance(key, str) or len(key) > MAX_KEY_LENGTH or not _NAME.match(key):
                self._say(
                    f"key:{key!r}",
                    f"test attribute {key!r} is not a name of up to {MAX_KEY_LENGTH} "
                    "letters, digits, '_', '.' or '-' starting with a letter or '_'; "
                    "it is dropped",
                )
                continue
            value = _text(found[key])
            if value is None:
                self._say(
                    f"value:{key}",
                    f"test attribute {key!r} has a value that is not a string, number "
                    f"or bool of 1 to {MAX_VALUE_LENGTH} characters; that value is dropped",
                )
                continue
            if len(kept) == MAX_KEYS:
                self._say("keys", f"a test carries at most {MAX_KEYS} attributes; the rest are dropped")
                break
            # Measured as the slot will encode it, escapes and separators
            # included, rather than estimated: at most sixteen small dumps,
            # once per test, and only in a run that tags its tests.
            if len(json.dumps({**kept, key: value}).encode("utf-8")) > MAX_BYTES:
                self._say(
                    "bytes",
                    f"a test's attributes are held to {MAX_BYTES} bytes; those past "
                    "it are dropped",
                )
                continue
            kept[key] = value
        return kept

    def _say(self, reason: str, message: str) -> None:
        if reason in self._said:
            return
        self._said.add(reason)
        advise(message)


def _implemented(item: Any) -> bool:
    hook = getattr(getattr(item, "config", None), "hook", None)
    caller = getattr(hook, "pytest_failure_test_attributes", None)
    return bool(caller is not None and caller.get_hookimpls())


def _text(value: Any) -> Optional[str]:
    """The value as the string every reader will see, or None."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float, str)):
        text = str(value)
    else:
        return None
    if not text or len(text) > MAX_VALUE_LENGTH:
        return None
    return text


def of(record: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """The attributes in a state record, or none.

    A record is a file on disk, read back by another process and perhaps by a
    later version: anything that is not a flat map of strings is treated as
    absent rather than trusted.
    """
    raw = (record or {}).get(KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def in_flight(record: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """:func:`of`, for a record that names a test in flight - and none for
    one that does not, whatever it still holds: tags belong to a test, and a
    worker between tests is running nobody's."""
    if not (record or {}).get("nodeid"):
        return {}
    return of(record)
