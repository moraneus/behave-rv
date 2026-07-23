"""Session-service demo: the mock service runs live, behave_rv monitors it, and
the browser shows events, per-entity verdict badges, and violation
explanations as they are decided.

Threading contract (respected as documented by the library): the mock flows
run on their own threads and push into the QueueSource (push is thread-safe);
the ENGINE consumes single-threaded; the SINK runs on the engine's consumer
thread and only enqueues JSON onto the SSE broadcaster -- it never touches
Flask or any shared monitor state.

Run:  python -m demo.session_service.app   (then open the printed URL)
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

from demo.session_service.service import FLOWS, TERMINAL_TYPE, SessionService
from demo.session_service.steps import build_registry, load_policies

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
                       "entity": event.bindings.get("user_id", "-"),
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
                       "entity": verdict.entity_key["user_id"],
                       "verdict": verdict.verdict, "at": round(verdict.at, 2),
                       "explanation": explanation})


# Event times are seconds since app start, not Unix epoch. Reads better in the
# UI, and it sidesteps a library finding: the live loop's wall-fire epsilon
# (loop.py: tick = target + 1e-9) is absolute, so at epoch magnitudes (~1.8e9)
# it falls below double precision and buffered events are never released.
_START = time.time()
service = SessionService(emit, clock=lambda: time.time() - _START)
engine = Engine(policies, terminal_event_types={TERMINAL_TYPE}, grace=0.5)
threading.Thread(target=lambda: engine.run(source, sink=sink), daemon=True).start()


@app.route("/")
def index():
    buttons = [{"id": k, "label": label, "kind": kind}
               for k, (label, kind, _) in FLOWS.items()]
    return render_template("index.html", title="Session Service",
                           accent="#7c3aed", buttons=buttons,
                           policies=sorted(by_id))


# -- the interactive board: the user IS the mock ---------------------------
# The board deliberately never enforces the session rules. Any button works at
# any time; the policies decide what was legal. The ONE piece of real app
# logic that does run is the lockout counter: the third "fail login" click
# emits locked by itself. User state here is display-only.

BOARD_ACTIONS = {"login": "login_ok", "act": "action", "logout": "logout",
                 "unlock": "unlocked", "review": "review", "flag": "flagged",
                 "lock": "locked"}

BOARD_UI = {
    "brand": "Authly", "tagline": "sessions and lockouts verified live by behave_rv",
    "noun": "session", "avatar": "\U0001f464", "placeholder": "Sign in as…",
    "create_label": "New session", "base": "/user",
    "primary": [["login", "login ok"], ["fail", "fail login"], ["act", "do action"]],
    "more": [["logout", "logout", False], ["review", "review", False],
             ["unlock", "unlock", False], ["flag", "flag", False],
             ["lock", "force lock", True], ["end", "end session", True]],
    "pills": {"new": "muted", "login_ok": "ok", "login_fail": "warn",
              "locked": "danger", "unlocked": "info", "action": "info",
              "logout": "muted", "review": "violet", "flagged": "warn",
              "ended": "muted"},
    "gone": ["ended"],
    "hint": ("Three failed logins lock the account - that is real app logic, watch "
             "the third click. This app never blocks anything else: act while locked, "
             "act after logout, or force a lock with no failed attempt before it. "
             "Leave a lock unreviewed and the review window fires at 8s on the wall "
             "clock. End the session to settle 'eventually logs out'."),
}

users: dict[str, dict] = {}
users_lock = threading.Lock()


@app.route("/board")
def board():
    with users_lock:
        current = sorted(users.values(), key=lambda u: u["id"])
    policy_cards = [{"name": p.policy_id,
                     "steps": [f"{s.keyword} {s.name}" for s in p.authored_scenario.steps]}
                    for p in policies]
    return render_template("board.html", ui=BOARD_UI, accent="#7c3aed",
                           entities=current, policy_cards=policy_cards)


@app.route("/user", methods=["POST"])
def create_user():
    label = ((request.get_json(silent=True) or {}).get("label") or "").strip()
    uid = f"USR-{next(counter)}"
    with users_lock:
        # a session exists before it emits anything; its first event is its
        # first action, which is exactly what the login policies are about
        users[uid] = {"id": uid, "label": label or "Guest", "status": "new"}
        snapshot = dict(users[uid])
    broadcast.publish({"kind": "entity", **snapshot})
    return {"ok": True, "id": uid}


@app.route("/user/<uid>/<action>", methods=["POST"])
def user_action(uid, action):
    if action not in BOARD_ACTIONS and action not in ("fail", "end"):
        return {"ok": False, "error": "unknown action"}, 400
    with users_lock:
        user = users.get(uid)
        if user is None:
            return {"ok": False, "error": "unknown user"}, 404
    if action == "fail":
        locked = service.fail_login(uid)
        status = "locked" if locked else "login_fail"
    elif action == "end":
        service.end_session(uid)
        status = "ended"
    else:
        status = BOARD_ACTIONS[action]
        service.act(uid, status)
    with users_lock:
        user["status"] = status
        snapshot = dict(user)
    broadcast.publish({"kind": "entity", **snapshot})
    return {"ok": True}


@app.route("/action/<name>", methods=["POST"])
def action(name):
    label, kind, flow = FLOWS[name]
    uid = f"USR-{next(counter)}"
    threading.Thread(target=getattr(service, flow), args=(uid,), daemon=True).start()
    return {"ok": True, "entity": uid}


@app.route("/stream")
def stream():
    return Response(broadcast.subscribe(), mimetype="text/event-stream")


if __name__ == "__main__":
    print("Session Service demo -> http://127.0.0.1:5002        (scripted flows)")
    print("Authly board         -> http://127.0.0.1:5002/board  (interactive app)")
    app.run(port=5002, threaded=True)
