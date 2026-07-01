"""The event loop. Pulls events from the source and drives the per-key monitors.

The same pipeline runs over live and replay sources. For each incoming event the
loop first fires any deadlines the advancing event time has passed (a timeout is
the absence of an event, so it must be checked before the next event is handled)
and reclaims any instances that have gone quiescent past their TTL, then
dispatches the event to the candidate policies' per-key instances. A monitor
returning a status produces a :class:`Verdict`.

Garbage collection has two tiers. Primary: an explicit terminal event the agent
exposed retires every instance for that entity and lets each monitor emit a final
verdict (the entity's lifetime is definitively over). Fallback: a quiescence TTL
silently reclaims instances of entities with no declared terminal event. Either
way the witnessing trace is dropped when the instance retires.

This module is deterministic and contains no language model: the same trace
produces the same verdicts every time.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from behave_rv.compile.automaton import Policy
from behave_rv.engine.dispatch import Dispatcher
from behave_rv.engine.instance import Instance
from behave_rv.engine.timers import TimerQueue
from behave_rv.events.event import Event
from behave_rv.events.watermark import ReorderBuffer
from behave_rv.verdict.record import Verdict

InstanceId = tuple[str, tuple[str, ...]]

# Correct event-time ordering is the DEFAULT. A verification tool's job is a
# trustworthy verdict, so the safe behavior must be what a user gets for free;
# the ordering-ignorant path (grace=0) is now an explicit opt-in. The window is
# in the same units as Event.event_time (seconds): 5s comfortably exceeds the
# sub-second-to-few-seconds reordering lag typical of distributed / telemetry /
# log sources while staying a "short window", and for bounded replay the
# end-of-stream flush corrects any residual ordering within the run. Raise it for
# laggier sources; set grace=0 for the fast path.
DEFAULT_GRACE = 5.0


class _Source:
    def events(self) -> Iterable[Event]:  # pragma: no cover - structural typing aid
        ...


class Engine:
    def __init__(
        self,
        policies: Iterable[Policy],
        *,
        terminal_event_types: Iterable[str] = (),
        quiescence_ttl: Optional[float] = None,
        grace: float = DEFAULT_GRACE,
    ) -> None:
        self._policies = {p.policy_id: p for p in policies}
        self._dispatcher = Dispatcher(self._policies.values())
        self._terminal = frozenset(terminal_event_types)
        self._ttl = quiescence_ttl
        self._grace = grace
        # observability, populated by run()
        self.live_instances = 0
        self.reclaimed = 0
        self.late_events = 0
        self.dropped_late: list[Event] = []
        self.observed_types: set[str] = set()
        self.retired_keys: list[tuple[str, ...]] = []
        self.reclaimed_keys: list[tuple[str, ...]] = []

    def run(self, source: _Source, *, emit_pending: bool = False) -> list[Verdict]:
        """Run the loop to exhaustion over ``source`` and collect verdicts.

        With ``emit_pending`` (useful for replay), every instance still open when
        the recorded stream ends is reported as a three-valued ``pending`` verdict.
        """
        instances: dict[InstanceId, Instance] = {}
        deadlines = TimerQueue()
        ttl_timers = TimerQueue()
        verdicts: list[Verdict] = []
        self.reclaimed = 0
        self.late_events = 0
        self.dropped_late = []
        self.observed_types = set()
        self.retired_keys = []
        self.reclaimed_keys = []

        buffer = ReorderBuffer(self._grace) if self._grace > 0 else None
        stream = self._ordered(source, buffer) if buffer is not None else source.events()

        for event in stream:
            now = event.event_time
            self.observed_types.add(event.type)  # liveness harvest: this type was seen
            self._fire_due_deadlines(now, instances, deadlines, verdicts)
            self._reclaim_quiescent(now, instances, ttl_timers)

            for policy in self._dispatcher.candidates(event):
                key = Dispatcher.key_of(policy, event)
                if key is None:
                    continue
                instance = self._instance_for(policy, key, instances)
                instance.witness(event)
                status = instance.monitor.on_event(event)
                if status is not None:
                    verdicts.append(self._verdict(instance, status, event, now))
                else:
                    self._reschedule(instance, instances, deadlines, ttl_timers)

            if event.type in self._terminal:
                self._retire_entity(event, instances, verdicts)

        if buffer is not None:
            self.dropped_late = list(buffer.late)
            self.late_events = len(buffer.late)

        if emit_pending:
            for instance in instances.values():
                if not instance.monitor.settled:
                    verdicts.append(
                        self._verdict(
                            instance,
                            "pending",
                            instance.monitor.trigger_event,
                            instance.last_activity,
                        )
                    )

        self.live_instances = len(instances)
        return verdicts

    @staticmethod
    def _ordered(source: _Source, buffer: ReorderBuffer):
        """Yield events in event-time order using the reordering window.

        Within the grace window late arrivals are sorted back into place; an event
        that arrives after the watermark has passed it is dropped from the ordered
        stream and recorded on ``buffer.late``.
        """
        for raw in source.events():
            buffer.push(raw)
            yield from buffer.releasable()
        yield from buffer.flush()

    # -- dispatch helpers ---------------------------------------------------

    def _instance_for(
        self, policy: Policy, key: tuple[str, ...], instances: dict[InstanceId, Instance]
    ) -> Instance:
        instance_id = (policy.policy_id, key)
        instance = instances.get(instance_id)
        if instance is None:
            instance = Instance(
                policy_id=policy.policy_id,
                entity_key=dict(zip(policy.correlation_key, key)),
                monitor=policy.monitor_factory(),
            )
            instances[instance_id] = instance
        return instance

    def _reschedule(
        self,
        instance: Instance,
        instances: dict[InstanceId, Instance],
        deadlines: TimerQueue,
        ttl_timers: TimerQueue,
    ) -> None:
        instance_id = (instance.policy_id, tuple(instance.entity_key.values()))
        deadline = instance.monitor.next_deadline()
        if deadline is not None:
            deadlines.schedule(deadline, instance_id)
        if self._ttl is not None:
            ttl_timers.schedule(instance.last_activity + self._ttl, instance_id)

    # -- garbage collection -------------------------------------------------

    def _fire_due_deadlines(
        self,
        now: float,
        instances: dict[InstanceId, Instance],
        deadlines: TimerQueue,
        verdicts: list[Verdict],
    ) -> None:
        for when, instance_id in deadlines.due(now):
            instance = instances.get(instance_id)
            if instance is None:
                continue
            status = instance.monitor.on_timeout(when)
            if status is not None:
                verdicts.append(
                    self._verdict(instance, status, instance.monitor.trigger_event, when)
                )

    def _reclaim_quiescent(
        self, now: float, instances: dict[InstanceId, Instance], ttl_timers: TimerQueue
    ) -> None:
        if self._ttl is None:
            return
        for _, instance_id in ttl_timers.due(now):
            instance = instances.get(instance_id)
            if instance is None:
                continue
            # Validate against live state: a refreshed instance has a later timer.
            if now - instance.last_activity >= self._ttl:
                del instances[instance_id]  # drops the witnessing trace
                self.reclaimed += 1
                self.reclaimed_keys.append(instance_id[1])

    def _retire_entity(
        self, event: Event, instances: dict[InstanceId, Instance], verdicts: list[Verdict]
    ) -> None:
        for policy in self._policies.values():
            key = Dispatcher.key_of(policy, event)
            if key is None:
                continue
            instance = instances.pop((policy.policy_id, key), None)  # drops the trace
            if instance is None:
                continue
            self.retired_keys.append(key)
            status = instance.monitor.on_terminal()
            if status is not None:
                verdicts.append(
                    self._verdict(instance, status, instance.monitor.trigger_event, event.event_time)
                )

    # -- verdict construction ----------------------------------------------

    @staticmethod
    def _verdict(
        instance: Instance, status: str, trigger: Optional[Event], at: float
    ) -> Verdict:
        return Verdict(
            policy_id=instance.policy_id,
            entity_key=dict(instance.entity_key),
            verdict=status,
            trigger_event=trigger,
            witnessing_trace=instance.witnessing_trace(),
            at=at,
        )
