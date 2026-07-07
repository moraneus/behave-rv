"""An independent, deliberately simple oracle for the operator verdicts.

It computes the verdict per correlation key by direct definition straight from
SEMANTICS.md, over the whole trace. It shares NO evaluation code with the engine:
it imports only the Event data type and re-derives everything (canonical ordering,
the operator state machines, the global clock horizon) by hand. Its independence
is the point -- if it shared a bug with the engine, the check would prove nothing.

A policy is a plain dict:
  {"operator": "never",  "correlation_key": ("order_id",), "bad": s}
  {"operator": "before", "correlation_key": ("order_id",), "prior": s, "trigger": s}
  {"operator": "within", "correlation_key": ("order_id",), "trigger": s,
   "response": s, "seconds": n}
Predicates are "payload['status'] == s", matching the registered example step.
"""

from __future__ import annotations

from math import inf

from behave_rv.events.event import Event


def decisive_for_key(policy: dict, events: list[Event], horizon):
    """(verdict, decisive_events) for one key's canonical events, by direct
    definition, mirroring which events each operator uses to decide.

    never:  the bad event.
    before: the trigger, plus the first prior when satisfied.
    within: the arming trigger, plus the response when satisfied.
    """
    op = policy["operator"]

    if op == "never":
        for e in events:
            if e.payload.get("status") == policy["bad"]:
                return "violated", [e]
        return "pending", []

    if op == "scoped_never":
        open_ = False
        opening = None
        for e in events:
            s = e.payload.get("status")
            if not open_ and s == policy["scope"]:
                open_ = True
                opening = e
            elif open_ and policy.get("close") is not None and s == policy["close"]:
                open_ = False
                opening = None
            if open_ and s == policy["bad"]:
                return "violated", ([e] if opening is e else [opening, e])
        return "pending", []

    if op == "once":
        for e in events:
            if e.payload.get("status") == policy["good"]:
                return "satisfied", [e]
        return "pending", []

    if op == "historically":
        for e in events:
            if e.payload.get("status") != policy["phi"]:
                return "violated", [e]
        return "pending", []

    if op == "previously":
        prev_phi = False
        prev_ev = None
        for e in events:
            if e.payload.get("status") == policy["trigger"]:
                return ("satisfied", [prev_ev, e]) if prev_phi else ("violated", [e])
            prev_ev = e
            prev_phi = e.payload.get("status") == policy["prior"]
        return "pending", []

    if op == "since":
        s = False
        started = False
        anchor = None
        for e in events:
            psi = e.payload.get("status") == policy["psi"]
            phi = e.payload.get("status") == policy["phi"]
            new_s = psi or (phi and s)
            if psi:
                anchor = e
            if new_s and not started:
                started = True
            if started and s and not new_s:
                return "violated", [anchor, e]
            s = new_s
        return "pending", []

    if op == "before":
        prior_ev = None
        for e in events:
            if e.payload.get("status") == policy["prior"] and prior_ev is None:
                prior_ev = e
            if e.payload.get("status") == policy["trigger"]:
                return ("satisfied", [prior_ev, e]) if prior_ev is not None else ("violated", [e])
        return "pending", []

    if op == "within":
        arm = None
        deadline = None
        for e in events:
            if arm is not None and e.event_time >= deadline:
                return "violated", [arm]
            if arm is None and e.payload.get("status") == policy["trigger"]:
                arm = e
                deadline = e.event_time + policy["seconds"]
            elif arm is not None and e.payload.get("status") == policy["response"]:
                return "satisfied", [arm, e]
        if arm is not None and horizon is not None and horizon >= deadline:
            return "violated", [arm]
        return "pending", []

    raise ValueError(f"unknown operator {op!r}")


def canonical_sorted(events: list[Event]) -> list[Event]:
    return sorted(
        events,
        key=lambda e: (
            e.event_time,
            e.type,
            repr(sorted(e.bindings.items())),
            repr(sorted(e.payload.items())),
            e.source,
        ),
    )


def _key_of(event: Event, correlation_key: tuple[str, ...]):
    try:
        return tuple(event.bindings[f] for f in correlation_key)
    except KeyError:
        return None


def _status(event: Event):
    return event.payload.get("status")


def oracle_verdicts(trace: list[Event], policy: dict) -> dict:
    """Return {key_value: verdict} for every key that has at least one event."""
    ck = policy["correlation_key"]
    horizon = max((e.event_time for e in trace), default=None)

    groups: dict = {}
    for e in trace:
        k = _key_of(e, ck)
        if k is not None:
            groups.setdefault(k, []).append(e)

    return {
        (k[0] if len(k) == 1 else k): _verdict(policy, canonical_sorted(evs), horizon)
        for k, evs in groups.items()
    }


def admit(arrival_events: list[Event], grace: float):
    """Model the engine's late-drop admission, by definition (SEMANTICS.md).

    Single pass in ARRIVAL order with a global watermark = max_seen - grace. An
    event whose event_time is below the watermark is dropped as late (and does not
    advance max_seen); otherwise it is admitted. Returns (admitted, dropped).
    """
    admitted: list[Event] = []
    dropped: list[Event] = []
    max_seen = -inf
    watermark = -inf
    for e in arrival_events:
        if e.event_time < watermark:
            dropped.append(e)
        else:
            admitted.append(e)
            if e.event_time > max_seen:
                max_seen = e.event_time
            watermark = max_seen - grace
    return admitted, dropped


def oracle_with_admission(arrival_events: list[Event], policy: dict, grace: float):
    """Verdicts and dropped-late set for a given arrival order and grace.

    Admission is modelled by definition (not by calling the engine); the verdict is
    then computed over the admitted events in canonical order.
    """
    admitted, dropped = admit(arrival_events, grace)
    return oracle_verdicts(admitted, policy), dropped


POLICY_EVENT_TYPE = "order.status"


def oracle_lifecycle(arrival_events, policy, grace, terminal_types, ttl):
    """Verdicts, dropped set, retired keys, and reclaimed keys, by direct
    canonical simulation over the admitted trace (independent of the engine).

    Mirrors the engine's per-event step order -- (1) within timeouts, (2)
    quiescence reclaim, (3) dispatch, (4) terminal retire -- applied over the
    ADMITTED events in canonical order (not arrival order). Reclamation is
    timer-driven: an instance is reclaimable only once some dispatch to it emitted
    no verdict (``ever_pending``), matching the engine's TTL scheduling.
    """
    admitted, dropped = admit(arrival_events, grace)
    events = canonical_sorted(admitted)
    ck = policy["correlation_key"]
    op = policy["operator"]
    ptype = policy.get("event_type", POLICY_EVENT_TYPE)
    terminal_types = set(terminal_types)

    inst: dict = {}
    verdicts: list = []
    retired: set = set()
    reclaimed: set = set()

    def keyof(e):
        try:
            return tuple(e.bindings[f] for f in ck)
        except KeyError:
            return None

    for e in events:
        now = e.event_time

        if op == "within":
            for k, st in list(inst.items()):
                if st["armed"] and not st["settled"] and now >= st["deadline"]:
                    verdicts.append((k, "violated", st["deadline"]))
                    st["settled"] = True

        if ttl is not None:
            for k, st in list(inst.items()):
                if st["ever_pending"] and now - st["last_activity"] >= ttl:
                    del inst[k]
                    reclaimed.add(k)

        if e.type == ptype:
            k = keyof(e)
            if k is not None:
                st = inst.get(k)
                if st is None:
                    st = {"settled": False, "seen_prior": False, "armed": False,
                          "deadline": None, "last_activity": now, "ever_pending": False,
                          "prev_phi": False, "since_s": False, "since_started": False,
                          "sc_open": False}
                    inst[k] = st
                st["last_activity"] = now
                produced = False
                if not st["settled"]:
                    s = _status(e)
                    if op == "never":
                        if s == policy["bad"]:
                            verdicts.append((k, "violated", now))
                            st["settled"] = True
                            produced = True
                    elif op == "scoped_never":
                        if not st["sc_open"] and s == policy["scope"]:
                            st["sc_open"] = True
                        elif st["sc_open"] and policy.get("close") is not None \
                                and s == policy["close"]:
                            st["sc_open"] = False
                        if st["sc_open"] and s == policy["bad"]:
                            verdicts.append((k, "violated", now))
                            st["settled"] = True
                            produced = True
                    elif op == "once":
                        if s == policy["good"]:
                            verdicts.append((k, "satisfied", now))
                            st["settled"] = True
                            produced = True
                    elif op == "historically":
                        if s != policy["phi"]:
                            verdicts.append((k, "violated", now))
                            st["settled"] = True
                            produced = True
                    elif op == "previously":
                        if s == policy["trigger"]:
                            verdicts.append((k, "satisfied" if st["prev_phi"] else "violated", now))
                            st["settled"] = True
                            produced = True
                        else:
                            st["prev_phi"] = s == policy["prior"]
                    elif op == "since":
                        new_s = (s == policy["psi"]) or (s == policy["phi"] and st["since_s"])
                        if new_s and not st["since_started"]:
                            st["since_started"] = True
                        if st["since_started"] and st["since_s"] and not new_s:
                            verdicts.append((k, "violated", now))
                            st["settled"] = True
                            produced = True
                        st["since_s"] = new_s
                    elif op == "before":
                        if s == policy["prior"]:
                            st["seen_prior"] = True
                        if s == policy["trigger"]:
                            verdicts.append((k, "satisfied" if st["seen_prior"] else "violated", now))
                            st["settled"] = True
                            produced = True
                    elif op == "within":
                        if not st["armed"] and s == policy["trigger"]:
                            st["armed"] = True
                            st["deadline"] = now + policy["seconds"]
                        elif st["armed"] and s == policy["response"]:
                            verdicts.append((k, "satisfied", now))
                            st["settled"] = True
                            produced = True
                if not produced:
                    st["ever_pending"] = True  # a no-verdict dispatch schedules a TTL timer

        if e.type in terminal_types:
            k = keyof(e)
            if k is not None and k in inst:
                st = inst.pop(k)
                retired.add(k)
                if not st["settled"]:
                    if op in ("never", "scoped_never", "historically", "since"):
                        verdicts.append((k, "satisfied", now))  # safety held to end of life
                    elif op == "once":
                        verdicts.append((k, "violated", now))    # existential never occurred
                    elif op == "within" and st["armed"]:
                        verdicts.append((k, "violated", now))
                    # before, previously (triggered but untriggered): no terminal verdict

    for k, st in inst.items():
        if not st["settled"]:
            verdicts.append((k, "pending", st["last_activity"]))

    return verdicts, dropped, retired, reclaimed


def _verdict(policy: dict, events: list[Event], horizon) -> str:
    op = policy["operator"]

    if op == "never":
        bad = policy["bad"]
        return "violated" if any(_status(e) == bad for e in events) else "pending"

    if op == "scoped_never":
        # scope state updates BEFORE the forbidden check on the same event
        open_ = False
        for e in events:
            s = _status(e)
            if not open_ and s == policy["scope"]:
                open_ = True
            elif open_ and policy.get("close") is not None and s == policy["close"]:
                open_ = False
            if open_ and s == policy["bad"]:
                return "violated"
        return "pending"

    if op == "once":
        good = policy["good"]
        return "satisfied" if any(_status(e) == good for e in events) else "pending"

    if op == "historically":
        phi = policy["phi"]
        return "violated" if any(_status(e) != phi for e in events) else "pending"

    if op == "previously":
        prior, trigger = policy["prior"], policy["trigger"]
        prev_phi = False
        for e in events:
            if _status(e) == trigger:
                return "satisfied" if prev_phi else "violated"
            prev_phi = _status(e) == prior
        return "pending"

    if op == "since":
        phi, psi = policy["phi"], policy["psi"]
        s = False
        started = False
        for e in events:
            new_s = (_status(e) == psi) or (_status(e) == phi and s)
            if new_s and not started:
                started = True
            if started and s and not new_s:
                return "violated"
            s = new_s
        return "pending"

    if op == "before":
        prior, trigger = policy["prior"], policy["trigger"]
        seen_prior = False
        for e in events:
            if _status(e) == prior:
                seen_prior = True
            if _status(e) == trigger:
                return "satisfied" if seen_prior else "violated"
        return "pending"

    if op == "within":
        trigger, response, seconds = policy["trigger"], policy["response"], policy["seconds"]
        armed = False
        deadline = None
        for e in events:
            if armed and e.event_time >= deadline:
                return "violated"
            if not armed and _status(e) == trigger:
                armed = True
                deadline = e.event_time + seconds
            elif armed and _status(e) == response:
                return "satisfied"
        if armed and horizon is not None and horizon >= deadline:
            return "violated"
        return "pending"

    raise ValueError(f"unknown operator {op!r}")
