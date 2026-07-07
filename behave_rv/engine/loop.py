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
from time import monotonic
from typing import Optional

from behave_rv.compile.automaton import Policy, set_predicate_error_collector
from behave_rv.engine.dispatch import Dispatcher
from behave_rv.engine.instance import Instance
from behave_rv.engine.timers import TimerQueue
from behave_rv.events.event import Event
from behave_rv.events.watermark import ReorderBuffer, usable_time
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


class ErrorLog:
    """The one convention for contain-record-continue errors: a count, the
    first exception, and per-occurrence sources. Used identically by the sink
    and predicate families so the two surfaces cannot drift apart."""

    def __init__(self) -> None:
        self.count = 0
        self.first: Optional[Exception] = None
        self.sources: list = []

    def record(self, exc: Exception, source) -> None:
        self.count += 1
        if self.first is None:
            self.first = exc
        self.sources.append(source)


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
        self.observed_values: set[tuple[str, str, str]] = set()  # (event_type, field, value)
        self.retired_keys: list[tuple[str, ...]] = []
        self.reclaimed_keys: list[tuple[str, ...]] = []
        self.verdicts_delivered = 0
        self._sink_log = ErrorLog()
        self._predicate_log = ErrorLog()

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
        self.observed_values = set()
        self.retired_keys = []
        self.reclaimed_keys = []
        self.verdicts_delivered = 0
        self._sink_log = ErrorLog()
        self._predicate_log = ErrorLog()

        pred_errors: list = []
        previous_collector = set_predicate_error_collector(pred_errors)

        if sink is None:
            deliver = verdicts.append
        else:
            emitfn = sink.emit if hasattr(sink, "emit") else sink

            def deliver(verdict: Verdict) -> None:
                self.verdicts_delivered += 1
                try:
                    emitfn(verdict)
                except Exception as exc:  # a failing sink must not stop evaluation
                    self._sink_log.record(exc, verdict.policy_id)

        buffer = ReorderBuffer(self._grace) if self._grace > 0 else None
        state = (instances, deadlines, ttl_timers, deliver, pred_errors)

        if getattr(source, "live", False) and hasattr(source, "next_event"):
            self._run_live(source, buffer, state)
        else:
            stream = self._ordered(source, buffer) if buffer is not None else source.events()
            for event in stream:
                self._handle_event(event, *state)

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
        set_predicate_error_collector(previous_collector)
        return verdicts

    def _handle_event(self, event, instances, deadlines, ttl_timers, deliver,
                      pred_errors) -> None:
        """Apply one admitted event: liveness harvest, due timers, quiescence,
        dispatch to the candidate policies' instances, terminal retirement."""
        now = event.event_time
        if not usable_time(now):
            # unusable event_time -- non-finite or not a number at all
            # (grace=0 path; the buffer rejects these on the default path)
            self.invalid_events += 1
            self.dropped_invalid.append(event)
            return
        self.observed_types.add(event.type)  # liveness harvest: this type was seen
        for field, value in event.payload.items():
            # value-level liveness: scalar payload values only (str/int/float/
            # bool, compared as strings, matching how phrasing params arrive)
            if isinstance(value, (str, int, float, bool)):
                self.observed_values.add((event.type, field, str(value)))
        self._fire_due_deadlines(now, instances, deadlines, deliver)
        self._reclaim_quiescent(now, instances, ttl_timers)

        for policy in self._dispatcher.candidates(event):
            key = Dispatcher.key_of(policy, event)
            if key is None:
                continue
            instance = self._instance_for(policy, key, instances)
            instance.witness(event)
            monitor = instance.monitor
            snapshot = dict(monitor.__dict__)  # small fixed-size scalar state
            try:
                status = monitor.on_event(event)
            except Exception as exc:
                # atomic per-event state: a raise that propagates through
                # on_event (a monitor-internal bug, or a raw programmatic
                # predicate outside the compiler's per-call containment)
                # leaves the monitor EXACTLY as it was -- the event is
                # not-applied for this monitor, never partially applied.
                monitor.__dict__.clear()
                monitor.__dict__.update(snapshot)
                self._record_predicate_error(policy.policy_id, exc)
                status = None
            for err in pred_errors:
                # per-predicate containment: each raised predicate was
                # no-match for that call alone; record it here with the
                # policy context, and let the rest of the event's handling
                # stand as evaluated
                self._record_predicate_error(policy.policy_id, err)
            pred_errors.clear()
            if status is not None:
                deliver(self._verdict(instance, status, event, now))
            else:
                self._reschedule(instance, instances, deadlines, ttl_timers)

        if event.type in self._terminal:
            self._retire_entity(event, instances, deliver)

    def _run_live(self, source, buffer, state) -> None:
        """The live loop: block for the next event OR until the nearest armed
        deadline matures on the wall clock, whichever comes first.

        Event time on a live source is assumed to progress at wall rate,
        anchored at the last clock-front advance. A wall fire is a VIRTUAL
        CLOCK TICK at event time ``deadline + grace``: it advances the
        watermark exactly as a real tick would (so later events older than the
        deadline are late and flagged -- committed-plus-flagged), releases
        buffered stragglers first (preserving canonical order for everything
        already admitted), and fires the due deadlines through the same timer
        path, so the verdict's ``at`` is the deadline's event time. Consumption
        stays single-threaded: no timer thread, no busy loop.
        """
        from behave_rv.events.sources.subscription import CLOSED

        instances, deadlines, ttl_timers, deliver, pred_errors = state
        front = float("-inf")     # highest admitted event time
        anchor_wall: Optional[float] = None   # wall instant when front last advanced

        while True:
            # the next wall moment anything is waiting for: an armed deadline
            # maturing, or a buffered event ageing past the grace window (on an
            # idle stream nothing else would ever release it)
            targets = []
            next_deadline = deadlines.peek()
            if next_deadline is not None:
                targets.append(next_deadline + self._grace)
            if buffer is not None:
                oldest = buffer.peek_oldest()
                if oldest is not None:
                    targets.append(oldest + self._grace)
            wait = None
            if targets and anchor_wall is not None:
                estimate = front + (monotonic() - anchor_wall)
                wait = max(min(targets) - estimate, 0.0)

            item = source.next_event(wait)
            if item is CLOSED:
                break
            if item is None:
                # wall fire: behave as if a clock tick at the matured target
                # arrived, except no event is dispatched and nothing joins the
                # trace (the epsilon clears the strict release boundary)
                tick = min(targets) + 1e-9
                if buffer is not None:
                    buffer.advance_clock(tick)
                    for released in buffer.releasable():
                        self._handle_event(released, *state)
                if tick > front:
                    front = tick
                    anchor_wall = monotonic()
                self._fire_due_deadlines(tick, instances, deadlines, deliver)
                continue

            if buffer is not None:
                buffer.push(item)
                if buffer.clock_front > front:
                    front = buffer.clock_front
                    anchor_wall = monotonic()
                for released in buffer.releasable():
                    self._handle_event(released, *state)
            else:
                if usable_time(item.event_time) and item.event_time > front:
                    front = item.event_time
                    anchor_wall = monotonic()
                self._handle_event(item, *state)

        if buffer is not None:
            for released in buffer.flush():
                self._handle_event(released, *state)

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
        if deadline is not None and isfinite(deadline):
            # a non-finite deadline (programmatic seconds=inf, or overflow) can
            # never fire; never let it into the timer heap
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

    # -- error recording ------------------------------------------------------

    def _record_predicate_error(self, policy_id: str, exc: Exception) -> None:
        self._predicate_log.record(exc, (policy_id, getattr(exc, "step_id", None)))

    # stable public names, one shape per family: <family>_errors / first_<family>_error
    # / <family>_error_sources
    @property
    def sink_errors(self) -> int:
        return self._sink_log.count

    @property
    def first_sink_error(self) -> Optional[Exception]:
        return self._sink_log.first

    @property
    def sink_error_sources(self) -> list:
        return self._sink_log.sources

    @property
    def predicate_errors(self) -> int:
        return self._predicate_log.count

    @property
    def first_predicate_error(self) -> Optional[Exception]:
        return self._predicate_log.first

    @property
    def predicate_error_sources(self) -> list:
        return self._predicate_log.sources

    # -- verdict construction ----------------------------------------------

    def _verdict(
        self, instance: Instance, status: str, trigger: Optional[Event], at: float
    ) -> Verdict:
        try:
            deciding = instance.monitor.deciding_events()
        except Exception as exc:
            # a monitor-internal raise here must not kill the run; the verdict
            # stands, its deciding evidence is just absent and the error visible
            self._record_predicate_error(instance.policy_id, exc)
            deciding = []
        return Verdict(
            policy_id=instance.policy_id,
            entity_key=dict(instance.entity_key),
            verdict=status,
            trigger_event=trigger,
            witnessing_trace=instance.witnessing_trace(),
            at=at,
            deciding_events=deciding,
        )
