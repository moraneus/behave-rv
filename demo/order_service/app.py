"""Order-service demo: the mock service runs live, behave_rv monitors it, and
the browser shows events, per-entity verdict badges, and violation
explanations as they are decided.

Threading contract (respected as documented by the library): the mock flows
run on their own threads and push into the QueueSource (push is thread-safe);
the ENGINE consumes single-threaded; the SINK runs on the engine's consumer
thread and only enqueues JSON onto the SSE broadcaster -- it never touches
Flask or any shared monitor state.

Run:  python -m demo.order_service.app   (then open the printed URL)
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time

from flask import Flask, Response, render_template, request

from behave_rv.engine.loop import Engine
from behave_rv.events.sources.subscription import QueueSource
from behave_rv.verdict.explain import explain_verdict

from demo.order_service.service import FLOWS, TERMINAL_TYPE, OrderService
from demo.order_service.steps import build_registry, load_policies

app = Flask(__name__)


class Broadcast:
    """Fan out JSON messages to every connected SSE client."""

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def publish(self, message: dict) -> None:
        with self._lock:
            for q in list(self._subscribers):
                q.put(message)

    def subscribe(self):
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                yield f"data: {json.dumps(q.get())}\n\n"
        finally:
            with self._lock:
                self._subscribers.remove(q)


broadcast = Broadcast()
registry = build_registry()
policies = load_policies(registry)
by_id = {p.policy_id: p for p in policies}
source = QueueSource()
counter = itertools.count(1)


def emit(event):
    """The service's tap: feed the engine AND the browser."""
    source.push(event)
    broadcast.publish({"kind": "event", "t": round(event.event_time, 2),
                       "entity": event.bindings.get("order_id", "-"),
                       "etype": event.type,
                       "status": event.payload.get("status", "done")})


def sink(verdict):
    # engine consumer thread: enqueue only, never touch Flask
    policy = by_id[verdict.policy_id]
    explanation = None
    if verdict.verdict == "violated":
        explanation = explain_verdict(verdict, policy.authored_scenario,
                                      policy.failing_step_index)
    broadcast.publish({"kind": "verdict", "policy": verdict.policy_id,
                       "entity": verdict.entity_key["order_id"],
                       "verdict": verdict.verdict, "at": round(verdict.at, 2),
                       "explanation": explanation})


# Event times are seconds since app start, not Unix epoch. Reads better in the
# UI, and it sidesteps a library finding: the live loop's wall-fire epsilon
# (loop.py: tick = target + 1e-9) is absolute, so at epoch magnitudes (~1.8e9)
# it falls below double precision and buffered events are never released.
_START = time.time()
service = OrderService(emit, clock=lambda: time.time() - _START)
engine = Engine(policies, terminal_event_types={TERMINAL_TYPE}, grace=0.5)
threading.Thread(target=lambda: engine.run(source, sink=sink), daemon=True).start()


@app.route("/")
def index():
    buttons = [{"id": k, "label": label, "kind": kind}
               for k, (label, kind, _) in FLOWS.items()]
    return render_template("index.html", title="Order Service",
                           accent="#2563eb", buttons=buttons,
                           policies=sorted(by_id))


# -- the interactive board: the user IS the mock ---------------------------
# The board deliberately never enforces the lifecycle. Any button works at any
# time; the policies decide what was legal. Order state here is display-only.

BOARD_ACTIONS = {"authorize": "authorized", "pay": "paid", "invoice": "invoiced",
                 "ship": "shipped", "deliver": "delivered", "cancel": "cancelled",
                 "refund": "refunded", "flag": "fraud_flagged", "review": "reviewed",
                 "double_charge": "double_charged"}

BOARD_UI = {
    "brand": "Shoply", "tagline": "an order pipeline verified live by behave_rv",
    "noun": "order", "avatar": "\U0001f6d2", "placeholder": "What's being ordered?",
    "create_label": "New order", "base": "/order",
    "primary": [["authorize", "authorize"], ["pay", "pay"],
                ["ship", "ship"], ["deliver", "deliver"]],
    "more": [["invoice", "invoice", False], ["cancel", "cancel", False],
             ["refund", "refund", False], ["flag", "flag as fraud", False],
             ["review", "review", False], ["double_charge", "charge again", True],
             ["close", "close order", True]],
    "pills": {"created": "muted", "authorized": "info", "paid": "info",
              "invoiced": "violet", "shipped": "info", "delivered": "ok",
              "cancelled": "warn", "refunded": "violet", "fraud_flagged": "warn",
              "reviewed": "ok", "double_charged": "danger", "done": "muted"},
    "gone": ["done"],
    "hint": ("This shop never blocks an action - legality is the monitor's job. "
             "Try paying before authorizing, shipping a cancelled order, or cancelling "
             "and never refunding: the refund window fires at 5s on the wall clock. "
             "Close an undelivered order and watch its 'eventually' obligations settle."),
}

orders: dict[str, dict] = {}
orders_lock = threading.Lock()


@app.route("/board")
def board():
    with orders_lock:
        current = sorted(orders.values(), key=lambda o: o["id"])
    policy_cards = [{"name": p.policy_id,
                     "steps": [f"{s.keyword} {s.name}" for s in p.authored_scenario.steps]}
                    for p in policies]
    return render_template("board.html", ui=BOARD_UI, accent="#2563eb",
                           entities=current, policy_cards=policy_cards)


@app.route("/order", methods=["POST"])
def create_order():
    label = ((request.get_json(silent=True) or {}).get("label") or "").strip()
    oid = f"ORD-{next(counter)}"
    with orders_lock:
        orders[oid] = {"id": oid, "label": label or "Order", "status": "created"}
        snapshot = dict(orders[oid])
    service.act(oid, "created")
    broadcast.publish({"kind": "entity", **snapshot})
    return {"ok": True, "id": oid}


@app.route("/order/<oid>/<action>", methods=["POST"])
def order_action(oid, action):
    if action != "close" and action not in BOARD_ACTIONS:
        return {"ok": False, "error": "unknown action"}, 400
    with orders_lock:
        order = orders.get(oid)
        if order is None:
            return {"ok": False, "error": "unknown order"}, 404
        order["status"] = "done" if action == "close" else BOARD_ACTIONS[action]
        snapshot = dict(order)
    if action == "close":
        service.close(oid)
    else:
        service.act(oid, BOARD_ACTIONS[action])
    broadcast.publish({"kind": "entity", **snapshot})
    return {"ok": True}


# -- the stability panel: real code changes through the real defense stack ----

from demo.order_service.stability import CHANGES, apply_change  # noqa: E402


@app.route("/stability")
def stability_page():
    cards = [{"id": cid, "title": spec["title"], "kind": spec["kind"]}
             for cid, spec in CHANGES.items()]
    return render_template("stability.html", cards=cards)


@app.route("/stability/<change_id>", methods=["POST"])
def stability_apply(change_id):
    if change_id not in CHANGES:
        return {"error": "unknown change"}, 404
    # runs in a sandbox: separate registries and services, never the live engine
    return apply_change(change_id)


@app.route("/action/<name>", methods=["POST"])
def action(name):
    label, kind, flow = FLOWS[name]
    oid = f"ORD-{next(counter)}"
    threading.Thread(target=getattr(service, flow), args=(oid,), daemon=True).start()
    return {"ok": True, "entity": oid}


@app.route("/stream")
def stream():
    return Response(broadcast.subscribe(), mimetype="text/event-stream")


if __name__ == "__main__":
    print("Order Service demo -> http://127.0.0.1:5001            (scripted flows)")
    print("Shoply board       -> http://127.0.0.1:5001/board      (interactive app)")
    print("Stability panel    -> http://127.0.0.1:5001/stability  (code-change defenses)")
    app.run(port=5001, threaded=True)
