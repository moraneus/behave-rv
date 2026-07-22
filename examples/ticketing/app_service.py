"""The application under monitoring: a tiny ticketing system.

This file represents YOUR business logic. Notice what monitoring asks of it:
almost nothing. The service takes an ``emit`` callable and calls it once per
observable state change -- that is the entire integration. It never imports
the engine, never knows about policies, and its logic is not reshaped.

Two conventions worth copying:

* ``emit`` and ``clock`` are injected, so the same service runs live (real
  clock, events flowing to the engine) and in tests (fake clock, events
  collected in a list) with identical behavior.
* Event times are SERVICE-RELATIVE seconds (the caller passes a clock that
  starts near zero), which is what live wall-clock deadlines want -- see the
  Gotchas section of the guide.
"""

from __future__ import annotations

import time

from behave_rv.events.event import Event

EVENT_TYPE = "ticket.status"      # the ticket lifecycle
PRIORITY_TYPE = "ticket.priority"  # priority changes
REPLY_TYPE = "ticket.reply"       # the conversation (direction: inbound/outbound)
TERMINAL_TYPE = "ticket.closed"   # ends a ticket's life: settles its policies


class TicketService:
    def __init__(self, emit, clock=time.time):
        self._emit = emit
        self._clock = clock

    def _status(self, ticket_id: str, status: str, **payload) -> None:
        """The tap: one normalized event per state change, nothing more."""
        self._emit(Event(EVENT_TYPE, self._clock(), {"ticket_id": ticket_id},
                         {"status": status, **payload}, "ticketing"))

    # -- the business operations ------------------------------------------

    def open_ticket(self, ticket_id: str, title: str) -> None:
        self._status(ticket_id, "opened", title=title)

    def assign(self, ticket_id: str, agent: str) -> None:
        self._status(ticket_id, "assigned", agent=agent)

    def escalate(self, ticket_id: str) -> None:
        self._status(ticket_id, "escalated")

    def resolve(self, ticket_id: str) -> None:
        self._status(ticket_id, "resolved")

    def set_priority(self, ticket_id: str, level: str) -> None:
        self._emit(Event(PRIORITY_TYPE, self._clock(), {"ticket_id": ticket_id},
                         {"level": level}, "ticketing"))

    def customer_reply(self, ticket_id: str) -> None:
        self._emit(Event(REPLY_TYPE, self._clock(), {"ticket_id": ticket_id},
                         {"direction": "inbound"}, "ticketing"))

    def agent_reply(self, ticket_id: str) -> None:
        self._emit(Event(REPLY_TYPE, self._clock(), {"ticket_id": ticket_id},
                         {"direction": "outbound"}, "ticketing"))

    def close(self, ticket_id: str) -> None:
        # the observable state change first (policies can talk about it)...
        self._status(ticket_id, "closed")
        # ...then the terminal event, a moment LATER: ordered actions need
        # distinct timestamps (equal times are ordered canonically, and the
        # terminal must not overtake the status it follows -- see the guide's
        # Gotchas)
        self._emit(Event(TERMINAL_TYPE, self._clock() + 1e-3,
                         {"ticket_id": ticket_id}, {}, "ticketing"))
