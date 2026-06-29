"""The normalized event. Everything the engine sees is an :class:`Event`.

Two rules that are not optional:

* ``event_time`` comes from the event or span timestamp, never from when the
  engine received it. Under any lag this is the difference between correct and
  silently wrong deadline and ordering checks.
* ``bindings`` carries the correlation key. It is how the engine separates one
  entity from another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str                      # stable event type identity, not a display name
    event_time: float              # seconds, taken from the source, not from receipt time
    bindings: dict[str, str]       # correlation key values, e.g. {"order_id": "4471"}
    payload: dict[str, Any]        # the observable fields a step may reference
    source: str                    # which adapter produced it (provenance)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable mapping. Used by the replay recorder/source."""
        return {
            "type": self.type,
            "event_time": self.event_time,
            "bindings": self.bindings,
            "payload": self.payload,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            type=data["type"],
            event_time=data["event_time"],
            bindings=data["bindings"],
            payload=data["payload"],
            source=data["source"],
        )
