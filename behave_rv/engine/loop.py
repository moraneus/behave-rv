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
from math import isfinite
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
        policies = list(policies)
        ids = [p.policy_id for p in policies]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(
                f"duplicate policy_id(s) {sorted(duplicates)!r}: every policy needs a "
                "unique id, or one policy would silently replace the other"
            )
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
        self.invalid_events = 0
        self.dropped_invalid: list[Event] = []
        self.observed_types: set[str] = set()
        self.retired_keys: list[tuple[str, ...]] = []
        self.reclaimed_keys: list[tuple[str, ...]] = []
        self.verdicts_delivered = 0
        self.sink_errors = 0
        self.first_sink_error: Optional[Exception] = None
        self.predicate_errors = 0
        self.first_predicate_error: Optional[Exception] = None
        self.predicate_error_sources: list[tuple[str, Optional[str]]] = []  # (policy_id, step_id)

    def run(
        self,
        source: _Source,
        *,
        emit_pending: bool = False,
        sink=None,
    ) -> list[Verdict]:
        """Run the loop to exhaustion over ``source`` and collect verdicts.

        With ``emit_pending`` (useful for replay), every instance still open when
        the recorded stream ends is reported as a three-valued ``pending`` verdict.

        With ``sink`` (a callable, or an object with ``emit``), every verdict is
        delivered the moment it is decided, before the next event is processed,
        in the same deterministic order the batch list would have. A sink-supplied
        run does NOT also accumulate the list (the memory point of a sink) and
        returns []; ``engine.verdicts_delivered`` counts deliveries. A sink that
        raises is recorded (``sink_errors`` / ``first_sink_error``) and evaluation
        continues -- a broken alert channel must not kill the monitor.
        """
        instances: dict[InstanceId, Instance] = {}
        deadlines = TimerQueue()
        ttl_timers = TimerQueue()
        verdicts: list[Verdict] = []
        self.reclaimed = 0
        self.late_events = 0
        self.dropped_late = []
        self.invalid_events = 0
        self.dropped_invalid = []
        self.observed_types = set()
        self.retired_keys = []
        self.reclaimed_keys = []
        self.verdicts_delivered = 0
        self.sink_errors = 0
        self.first_sink_error = None
        self.predicate_errors = 0
        self.first_predicate_error = None
        self.predicate_error_sources = []

        if sink is None:
            deliver = verdicts.append
        else:
            emitfn = sink.emit if hasattr(sink, "emit") else sink

            def deliver(verdict: Verdict) -> None:
                self.verdicts_delivered += 1
                try:
                    emitfn(verdict)
                except Exception as exc:  # a failing sink must not stop evaluation
                    self.sink_errors += 1
                    if self.first_sink_error is None:
                        self.first_sink_error = exc

        buffer = ReorderBuffer(self._grace) if self._grace > 0 else None
        stream = self._ordered(source, buffer) if buffer is not None else source.events()

        for event in stream:
            now = event.event_time
            if not isfinite(now):
                # non-finite event_time (grace=0 path; the buffer rejects these
                # before they reach here on the default path)
                self.invalid_events += 1
                self.dropped_invalid.append(event)
                continue
            self.observed_types.add(event.type)  # liveness harvest: this type was seen
            self._fire_due_deadlines(now, instances, deadlines, deliver)
            self._reclaim_quiescent(now, instances, ttl_timers)

            for policy in self._dispatcher.candidates(event):
                key = Dispatcher.key_of(policy, event)
                if key is None:
                    continue
                instance = self._instance_for(policy, key, instances)
                instance.witness(event)
                try:
                    status = instance.monitor.on_event(event)
                except Exception as exc:
                    # a broken predicate matches nothing; contain, record, continue
                    # (mirrors the sink-failure policy: a step author's bug must not
                    # kill the monitor or disturb any other policy or instance)
                    self.predicate_errors += 1
                    if self.first_predicate_error is None:
                        self.first_predicate_error = exc
                    self.predicate_error_sources.append(
                        (policy.policy_id, getattr(exc, "step_id", None)))
                    status = None
                if status is not None:
                    deliver(self._verdict(instance, status, event, now))
                else:
                    self._reschedule(instance, instances, deadlines, ttl_timers)

            if event.type in self._terminal:
                self._retire_entity(event, instances, deliver)

        if buffer is not None:
            self.dropped_late = list(buffer.late)
            self.late_events = len(buffer.late)
            self.dropped_invalid = list(buffer.invalid)
            self.invalid_events = len(buffer.invalid)

        if emit_pending:
            for instance in instances.values():
                if not instance.monitor.settled:
                    deliver(
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
        deliver,
    ) -> None:
        for when, instance_id in deadlines.due(now):
            instance = instances.get(instance_id)
            if instance is None:
                continue
            status = instance.monitor.on_timeout(when)
            if status is not None:
                deliver(
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
        self, event: Event, instances: dict[InstanceId, Instance], deliver
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
                deliver(
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
            deciding_events=instance.monitor.deciding_events(),
        )
