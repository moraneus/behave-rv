"""Todo-app demo: the mock task service and its background sync channel run
live, behave_rv monitors both entity types, and the browser shows events,
per-entity verdict badges, and violation explanations as they are decided.

Threading contract (respected as documented by the library): the mock flows
run on their own threads and push into the QueueSource (push is thread-safe);
the ENGINE consumes single-threaded; the SINK runs on the engine's consumer
thread and only enqueues JSON onto the SSE broadcaster -- it never touches
Flask or any shared monitor state.

Run:  python -m demo.todo_app.app   (then open the printed URL)
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time

from flask import Flask, Response, render_template

from behave_rv.engine.loop import Engine
from behave_rv.events.sources.subscription import QueueSource
from behave_rv.verdict.explain import explain_verdict

from demo.todo_app.service import FLOWS, TodoService
from demo.todo_app.steps import build_registry, load_policies

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


def entity_of(bindings: dict) -> str:
    return bindings.get("task_id") or bindings.get("session_id") or "-"


def emit(event):
    """The service's tap: feed the engine AND the browser."""
    source.push(event)
    broadcast.publish({"kind": "event", "t": round(event.event_time, 2),
                       "entity": entity_of(event.bindings),
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
                       "entity": entity_of(verdict.entity_key),
                       "verdict": verdict.verdict, "at": round(verdict.at, 2),
                       "explanation": explanation})


# Event times are seconds since app start, not Unix epoch. Reads better in the
# UI, and it sidesteps a library finding: the live loop's wall-fire epsilon
# (loop.py: tick = target + 1e-9) is absolute, so at epoch magnitudes (~1.8e9)
# it falls below double precision and buffered events are never released.
_START = time.time()
service = TodoService(emit, clock=lambda: time.time() - _START)
engine = Engine(policies, grace=0.5)
threading.Thread(target=lambda: engine.run(source, sink=sink), daemon=True).start()


@app.route("/")
def index():
    buttons = [{"id": k, "label": label, "kind": kind}
               for k, (label, kind, _) in FLOWS.items()]
    return render_template("index.html", title="Todo App",
                           accent="#0d9488", buttons=buttons,
                           policies=sorted(by_id))


@app.route("/action/<name>", methods=["POST"])
def action(name):
    label, kind, flow = FLOWS[name]
    prefix = "SYN" if "sync" in flow else "TSK"
    eid = f"{prefix}-{next(counter)}"
    threading.Thread(target=getattr(service, flow), args=(eid,), daemon=True).start()
    return {"ok": True, "entity": eid}


@app.route("/stream")
def stream():
    return Response(broadcast.subscribe(), mimetype="text/event-stream")


if __name__ == "__main__":
    print("Todo App demo -> http://127.0.0.1:5003")
    app.run(port=5003, threaded=True)
