"""The discriminated union: every incident that can reach the hook.

``kind`` is the discriminator, so a stored row parses back into the model it
was written from without the reader having to know which fields to expect:

    from pytest_failure_instrumentation.incidents.registry import parse

    incident = parse(json.loads(row))     # -> WorkerDeathIncident, ...
    print(incident)                       # the alert text, again

Two kinds documented on the hook are absent here on purpose - ``worker_stall``
and ``collection_mismatch`` are not yet raised, and a union member with no
producer would promise a payload that never arrives.
"""

from __future__ import annotations

from typing import Annotated, Any, Union

from pydantic import Field, TypeAdapter

from .base import Capabilities, Frame, Incident
from .death import WorkerDeathIncident
from .internal_error import InternalErrorIncident
from .summary import RunSummaryIncident

AnyIncident = Annotated[
    Union[WorkerDeathIncident, InternalErrorIncident, RunSummaryIncident],
    Field(discriminator="kind"),
]

_adapter: TypeAdapter[Any] = TypeAdapter(AnyIncident)


def parse(row: Any) -> Incident:
    """A dict, or JSON-decoded database row, back into its own model."""
    return _adapter.validate_python(row)


def json_schema() -> dict[str, Any]:
    """The payload contract, for a schema registry or a table migration."""
    return _adapter.json_schema()


__all__ = [
    "AnyIncident",
    "Capabilities",
    "Frame",
    "Incident",
    "InternalErrorIncident",
    "RunSummaryIncident",
    "WorkerDeathIncident",
    "json_schema",
    "parse",
]
