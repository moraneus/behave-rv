"""The event loop. Pulls events from the source and drives the per-key monitors.

The same pipeline runs over live and replay sources. For each incoming event the
loop first fires any deadlines the advancing event time has passed (a timeout is
the absence of an event, so it must be checked before the next event is handled),
then dispatches the event to the candidate policies' per-key instances. A monitor
returning a status produces a :class:`Verdict`.

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
from behave_rv.verdict.record import Verdict

InstanceId = tuple[str, tuple[str, ...]]


class _Source:
    def events(self) -> Iterable[Event]:  # pragma: no cover - structural typing aid
        ...


class Engine:
    def __init__(self, policies: Iterable[Policy]) -> None:
        self._policies = {p.policy_id: p for p in policies}
        self._dispatcher = Dispatcher(self._policies.values())

    def run(self, source: _Source) -> list[Verdict]:
        """Run the loop to exhaustion over ``source`` and collect verdicts.

        Live mode would consume this generator without exhausting it; replay
        returns the full list once the recorded stream ends.
        """
        instances: dict[InstanceId, Instance] = {}
        timers = TimerQueue()
        verdicts: list[Verdict] = []

        for event in source.events():
            self._fire_due_timers(event.event_time, instances, timers, verdicts)

            for policy in self._dispatcher.candidates(event):
                key = Dispatcher.key_of(policy, event)
                if key is None:
                    continue
                instance_id = (policy.policy_id, key)
                instance = instances.get(instance_id)
                if instance is None:
                    instance = Instance(
                        policy_id=policy.policy_id,
                        entity_key=dict(zip(policy.correlation_key, key)),
                        monitor=policy.monitor_factory(),
                    )
                    instances[instance_id] = instance

                instance.witness(event)
                status = instance.monitor.on_event(event)
                if status is not None:
                    verdicts.append(self._verdict(instance, status, event, event.event_time))
                else:
                    self._reschedule(instance_id, instance, timers)

        return verdicts

    # -- helpers ------------------------------------------------------------

    def _fire_due_timers(
        self,
        now: float,
        instances: dict[InstanceId, Instance],
        timers: TimerQueue,
        verdicts: list[Verdict],
    ) -> None:
        for when, instance_id in timers.due(now):
            instance = instances.get(instance_id)
            if instance is None:
                continue
            status = instance.monitor.on_timeout(when)
            if status is not None:
                trigger = instance.monitor.trigger_event
                verdicts.append(self._verdict(instance, status, trigger, when))

    @staticmethod
    def _reschedule(instance_id: InstanceId, instance: Instance, timers: TimerQueue) -> None:
        deadline = instance.monitor.next_deadline()
        if deadline is not None:
            timers.schedule(deadline, instance_id)

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
