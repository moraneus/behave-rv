"""A built-in live monitor dashboard: open a browser at your running app and
watch every policy's verdicts, per entity, with the rendered explanation for
each violation -- while the app runs.

Standard library only (``http.server``), so the core package stays
dependency-light. The dashboard is a SINK plus an optional event TAP:

    from behave_rv.dashboard import Dashboard

    dashboard = Dashboard(policies)
    url = dashboard.start(port=7007)          # http server on its own thread
    print("monitor:", url)

    engine = Engine(policies, terminal_event_types={"order.done"})
    engine.run(source, sink=dashboard.sink)   # live delivery -> dashboard

Optionally feed it the raw event stream too (``source.push(dashboard.tap(e))``
or call ``dashboard.tap(event)`` wherever you emit) and the page shows the
live event feed next to the verdicts.

Threading contract, same as every sink: ``sink``/``tap`` are called from the
engine's or the app's thread and only record into a lock-protected store; the
HTTP server thread only reads snapshots. Chain your own sink with
``Dashboard(policies, forward=my_sink)``.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from behave_rv.verdict.explain import explain_verdict, safe_value


class Dashboard:
    def __init__(self, policies, *, forward=None, history: int = 300,
                 registry=None, catalog=None, app=None):
        policies = list(policies)
        self._policies = policies
        self._by_id = {p.policy_id: p for p in policies}
        self._policy_order = [p.policy_id for p in policies]
        self._forward = forward
        self._lock = threading.Lock()
        self._status: dict = {}                 # (policy_id, entity) -> cell
        self._violations: deque = deque(maxlen=history)
        self._events: deque = deque(maxlen=history)
        self._observed_types: set = set()       # event types seen via tap()
        self._counts = {"events": 0, "verdicts": 0, "violations": 0}
        self._server = None
        self._thread = None
        # Optional stability check: pass the registry the policies were
        # compiled against plus the committed catalog path, and the dashboard
        # runs the contract diff once at construction (code cannot change
        # inside a running process) and displays the result -- so "could a
        # policy be silently dead?" is answered on the same page as the
        # verdicts. With ``app=`` (the application's .py files/dirs) the check
        # covers BOTH sides of the event boundary: step contracts AND the
        # app's emit sites, so a core-code change that can move what the
        # monitor observes shows on the page before any verdict does. Same
        # mechanism as `python -m behave_rv catalog diff [--app ...]`.
        self._stability = None
        if registry is not None and catalog is not None:
            self._stability = self._check_stability(registry, catalog, app)

    def _check_stability(self, registry, catalog_path, app_paths) -> dict:
        from behave_rv.catalog.diff import classify_changes
        from behave_rv.catalog.store import load_catalog
        from behave_rv.notify.channel import notifications, uses_from_policies

        try:
            committed = load_catalog(catalog_path)
        except (FileNotFoundError, ValueError) as exc:
            return {"status": "error", "detail": str(exc), "breaks": [], "app": None}
        current = registry.entries()
        notes = notifications(committed, current,
                              uses_from_policies(self._policies))
        changes = classify_changes(committed, current)
        result = {
            "status": "breaks" if notes.breaks else "ok",
            "statuses": {c.step_id: c.status for c in changes},
            "breaks": [{"policy": b.policy_id, "step": b.step_id,
                        "detail": b.detail} for b in notes.breaks],
            "detail": None,
            "app": self._check_app_surface(catalog_path, app_paths, current)
                   if app_paths else None,
        }
        app = result["app"]
        if app and app.get("checked"):
            if app["breaks"]:
                result["status"] = "breaks"
            elif app["risks"] and result["status"] == "ok":
                result["status"] = "risks"
        return result

    def _check_app_surface(self, catalog_path, app_paths, entries) -> dict:
        """The app side of the contract: diff the application's CURRENT emit
        sites against the committed app_surface, scoped to the running
        policies -- so a core-code change that can move what the monitor
        observes is shown on the page, not only in the CLI."""
        from behave_rv.catalog.app_surface import (
            APP_REMOVED, BEHAVIOR_RISK, INTERFACE_BREAK,
            analyze_app, classify_app_changes, policies_at_risk,
        )
        from behave_rv.catalog.store import load_app_surface

        committed_sites = load_app_surface(catalog_path)
        if committed_sites is None:
            return {"checked": False,
                    "detail": "catalog has no app_surface section; run "
                              "'catalog save --app ...' to enable this check"}
        changes = classify_app_changes(committed_sites, analyze_app(app_paths))
        breaks, risks = [], []
        for change in changes:
            bucket = {INTERFACE_BREAK: breaks, APP_REMOVED: breaks,
                      BEHAVIOR_RISK: risks}.get(change.status)
            if bucket is None:
                continue
            site = change.new or change.old
            scoped = policies_at_risk(change, entries, self._policies)
            if scoped is None:
                affected = ["(dynamic event type — review ALL policies)"]
            else:
                direct, coupled = scoped
                affected = direct + [f"{pid} (event-time coupling)"
                                     for pid in coupled]
            bucket.append({"site": change.site_id,
                           "event": site.event_type,
                           "detail": change.detail,
                           "policies": affected})
        return {"checked": True,
                "statuses": {c.site_id: c.status for c in changes},
                "breaks": breaks, "risks": risks, "detail": None}

    # -- the write side: called from the engine's / the app's thread ---------

    def sink(self, verdict) -> None:
        """A verdict sink (pass as ``engine.run(source, sink=dashboard.sink)``)."""
        policy = self._by_id.get(verdict.policy_id)
        entity = ", ".join(f"{k}={safe_value(v)}"
                           for k, v in verdict.entity_key.items())
        explanation = None
        if verdict.verdict == "violated" and policy is not None \
                and policy.authored_scenario is not None:
            explanation = explain_verdict(verdict, policy.authored_scenario,
                                          policy.failing_step_index)
        cell = {"policy": verdict.policy_id, "entity": entity,
                "verdict": verdict.verdict, "at": verdict.at,
                "explanation": explanation}
        with self._lock:
            self._counts["verdicts"] += 1
            self._status[(verdict.policy_id, entity)] = cell
            if verdict.verdict == "violated":
                self._counts["violations"] += 1
                self._violations.appendleft(cell)
        if self._forward is not None:
            self._forward(verdict)

    def tap(self, event):
        """Optional event hook: record and return the event unchanged, so it
        composes with any emit chain (``source.push(dashboard.tap(event))``).
        Tapping also powers the per-policy "no matching events observed"
        warning -- the runtime half of silent-failure visibility."""
        with self._lock:
            self._counts["events"] += 1
            self._observed_types.add(event.type)
            self._events.appendleft({
                "t": round(event.event_time, 3),
                "type": safe_value(event.type),
                "entity": ", ".join(f"{k}={safe_value(v)}"
                                    for k, v in event.bindings.items()),
                "payload": safe_value(str(event.payload)),
            })
        return event

    # -- the read side: snapshots for the HTTP thread -------------------------

    def state(self) -> dict:
        with self._lock:
            per_policy = {pid: [] for pid in self._policy_order}
            for (pid, _entity), cell in self._status.items():
                if pid in per_policy:
                    per_policy[pid].append(cell)
            for cells in per_policy.values():
                cells.sort(key=lambda c: c["entity"])
            # a policy is "unobserved" when events ARE flowing but none of
            # its event types has appeared -- the runtime smell of a policy
            # disconnected from the stream (see STABILITY.md)
            unobserved = {
                p.policy_id
                for p in self._policies
                if self._counts["events"] > 0
                and self._observed_types.isdisjoint(p.event_types)
            }
            return {
                "counts": dict(self._counts),
                "stability": self._stability,
                "policies": [{"policy": pid, "cells": per_policy[pid],
                              "unobserved": pid in unobserved}
                             for pid in self._policy_order],
                "violations": list(self._violations),
                "events": list(self._events),
            }

    # -- the server ------------------------------------------------------------

    def start(self, port: int = 7007, host: str = "127.0.0.1") -> str:
        """Serve the dashboard on a daemon thread; returns the URL.
        ``port=0`` picks a free port (useful for tests)."""
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/state":
                    body = json.dumps(dashboard.state()).encode()
                    content_type = "application/json"
                elif self.path == "/":
                    body = _PAGE.encode()
                    content_type = "text/html; charset=utf-8"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep the app's stdout clean
                pass

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return f"http://{host}:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>behave_rv · live monitor</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'\
 viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='24' fill='%230f172a'/%3E\
%3Ctext x='50' y='74' font-size='68' text-anchor='middle' fill='white'%3E%E2%9C%93%3C\
/text%3E%3C/svg%3E">
<style>
  :root { --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f1f5f9;
          --surface:#fff; --ok:#16a34a; --bad:#dc2626; --pend:#94a3b8;
          --radius:12px; --shadow:0 1px 2px rgba(15,23,42,.05),0 4px 16px rgba(15,23,42,.06); }
  * { box-sizing:border-box; margin:0; }
  body { font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--ink); }
  header { background:var(--surface); border-bottom:1px solid var(--line);
           padding:12px 24px; display:flex; align-items:baseline; gap:14px; }
  h1 { font-size:16px; } header span { color:var(--muted); font-size:12.5px; }
  #stats { margin-left:auto; display:flex; gap:16px; font-size:12.5px; color:var(--muted); }
  #stats b { color:var(--ink); }
  main { display:grid; grid-template-columns:minmax(340px,1fr) minmax(380px,1.2fr);
         gap:14px; padding:14px 24px; align-items:start; }
  @media (max-width:900px) { main { grid-template-columns:1fr; } }
  .panel { background:var(--surface); border:1px solid var(--line);
           border-radius:var(--radius); box-shadow:var(--shadow); padding:12px 14px; }
  .panel h2 { font-size:11px; text-transform:uppercase; letter-spacing:.08em;
              color:var(--muted); margin-bottom:10px; }
  .policy { padding:8px 0; border-bottom:1px solid #f1f5f9; }
  .policy .name { font-weight:600; font-size:13px; margin-bottom:4px; }
  .badge { display:inline-block; margin:2px 4px 2px 0; padding:2px 9px;
           border-radius:999px; font:11px ui-monospace,monospace; font-weight:700;
           color:#fff; background:var(--pend); }
  .badge.satisfied { background:var(--ok); } .badge.violated { background:var(--bad); }
  .none { color:var(--muted); font-size:12px; }
  .violation { border:1px solid #fecaca; border-left:3px solid var(--bad);
               border-radius:8px; padding:8px 10px; margin-bottom:8px; background:#fef2f2; }
  .violation .head { color:#991b1b; font-weight:650; font-size:12.5px; margin-bottom:4px; }
  .violation pre { font:11px/1.5 ui-monospace,monospace; white-space:pre-wrap; color:#334155; }
  #violations:empty::after { content:"No violations."; color:var(--muted); font-size:12.5px; }
  #events { font:11.5px/1.7 ui-monospace,monospace; max-height:40vh; overflow:auto; }
  #events div { border-bottom:1px solid #f1f5f9; white-space:nowrap; overflow:hidden;
                text-overflow:ellipsis; }
  #events .tm { color:var(--muted); }
  #events:empty::after { content:"No events tapped (optional: feed dashboard.tap).";
                         color:var(--muted); }
  #stability { margin:12px 24px 0; border-radius:10px; padding:9px 14px;
               font-size:12.5px; font-weight:550; border:1px solid; display:none; }
  #stability.ok { display:block; background:#dcfce7; border-color:#86efac; color:#14532d; }
  #stability.breaks, #stability.error { display:block; background:#fee2e2;
               border-color:#fca5a5; color:#7f1d1d; }
  #stability.risks { display:block; background:#fef3c7; border-color:#fcd34d;
               color:#78350f; }
  #stability pre { font:11px/1.5 ui-monospace,monospace; white-space:pre-wrap; margin-top:6px; }
  .stale { display:inline-block; margin-left:6px; padding:1px 8px; border-radius:999px;
           font-size:10.5px; font-weight:700; background:#fef3c7; color:#92400e; }
</style>
</head>
<body>
<header>
  <h1>behave_rv live monitor</h1><span>polls every 1.5s</span>
  <div id="stats"></div>
</header>
<div id="stability"></div>
<main>
  <div>
    <div class="panel"><h2>Policies · per-entity verdicts</h2><div id="policies"></div></div>
    <div class="panel" style="margin-top:14px"><h2>Event feed</h2><div id="events"></div></div>
  </div>
  <div class="panel"><h2>Violations, explained</h2><div id="violations"></div></div>
</main>
<script>
async function refresh() {
  let s;
  try { s = await (await fetch('/api/state')).json(); } catch (e) { return; }
  document.getElementById('stats').innerHTML =
    `<span>events <b>${s.counts.events}</b></span>` +
    `<span>verdicts <b>${s.counts.verdicts}</b></span>` +
    `<span>violations <b>${s.counts.violations}</b></span>`;
  const strip = document.getElementById('stability');
  if (s.stability) {
    strip.className = s.stability.status;
    const app = s.stability.app;
    const appLine = a =>
      `${a.site} (event '${a.event}')\\n   ${a.detail}\\n   policies at risk: `
      + (a.policies.length ? a.policies.join(', ') : 'none compiled observes it');
    if (s.stability.status === 'ok') {
      strip.textContent = 'contract check: catalog in sync'
        + (app && app.checked
           ? ' on both sides \u2014 steps AND the app emit sites'
           : ' \u2014 no policy can be silently broken by a step change')
        + ' (checked at startup)'
        + (app && !app.checked ? ' \u00b7 app side not enabled: ' + app.detail : '');
    } else if (s.stability.status === 'error') {
      strip.textContent = 'contract check FAILED to run: ' + s.stability.detail;
    } else {
      strip.textContent = '';
      const stepBreaks = s.stability.breaks;
      const appBreaks = app && app.checked ? app.breaks : [];
      const appRisks = app && app.checked ? app.risks : [];
      const head = document.createElement('div');
      head.textContent = 'contract check: '
        + (stepBreaks.length + appBreaks.length
           ? `${stepBreaks.length + appBreaks.length} BREAK(S)`
             + (appRisks.length ? ` + ${appRisks.length} app behavior risk(s)` : '')
             + ' \u2014 these policies may be silently dead or wrong; regenerate'
             + ' the catalog only if the change was intended'
           : `${appRisks.length} APP BEHAVIOR RISK(S) \u2014 core-code logic on`
             + ' an emit path changed; the named policies may be affected before'
             + ' any verdict shows it');
      strip.appendChild(head);
      const lines = []
        .concat(stepBreaks.map(b => `\u2717 ${b.policy}  via ${b.step}\\n   ${b.detail}`))
        .concat(appBreaks.map(a => '\u2717 ' + appLine(a)))
        .concat(appRisks.map(a => '! ' + appLine(a)));
      const pre = document.createElement('pre');
      pre.textContent = lines.join('\\n');
      strip.appendChild(pre);
    }
  }
  const policies = document.getElementById('policies');
  policies.textContent = '';
  for (const p of s.policies) {
    const d = document.createElement('div');
    d.className = 'policy';
    const name = document.createElement('div');
    name.className = 'name'; name.textContent = p.policy;
    if (p.unobserved) {
      const w = document.createElement('span');
      w.className = 'stale';
      w.textContent = '\u26a0 no matching events observed';
      name.appendChild(w);
    }
    d.appendChild(name);
    if (!p.cells.length) {
      const n = document.createElement('span');
      n.className = 'none'; n.textContent = 'no verdicts yet (pending)';
      d.appendChild(n);
    }
    for (const c of p.cells) {
      const b = document.createElement('span');
      b.className = 'badge ' + c.verdict;
      b.textContent = `${c.entity} · ${c.verdict}`;
      d.appendChild(b);
    }
    policies.appendChild(d);
  }
  const violations = document.getElementById('violations');
  violations.textContent = '';
  for (const v of s.violations) {
    const d = document.createElement('div');
    d.className = 'violation';
    const h = document.createElement('div');
    h.className = 'head'; h.textContent = `\\u2717 ${v.entity} \\u2014 ${v.policy}`;
    d.appendChild(h);
    if (v.explanation) {
      const pre = document.createElement('pre');
      pre.textContent = v.explanation;
      d.appendChild(pre);
    }
    violations.appendChild(d);
  }
  const events = document.getElementById('events');
  events.textContent = '';
  for (const e of s.events) {
    const d = document.createElement('div');
    d.innerHTML = `<span class="tm"></span> <b></b> <span></span>`;
    d.children[0].textContent = 't=' + e.t;
    d.children[1].textContent = e.entity;
    d.children[2].textContent = e.type + ' ' + e.payload;
    events.appendChild(d);
  }
}
refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""
