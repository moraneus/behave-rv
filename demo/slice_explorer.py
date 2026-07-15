"""The slice explorer: click a line of a demo application and SEE the
dependency slice the app-surface analyser computes for it.

    python -m demo.slice_explorer            # http://127.0.0.1:7010

Pick one of the committed demo applications, click any source line, and the
page shows -- computed by the REAL analyser (`behave_rv.catalog.app_surface`),
not a mock -- which emissions that line can influence, every function in
those emissions' backward slices, the constants and unresolved calls they
carry, and the policies that would be named at risk if the line changed.
Demonstration tool only: it reuses a few of the analyser's internals
(`_index_module`, `_is_anchor`) to place anchors on source lines.

Standard library only, same conventions as behave_rv.dashboard.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from behave_rv.catalog.app_surface import _index_module, _is_anchor, analyze_app

ROOT = Path(__file__).resolve().parents[1]

APPS = {
    "order": ("demo/order_service/service.py", "demo.order_service.steps",
              "e-commerce order lifecycle, 11 policies"),
    "session": ("demo/session_service/service.py", "demo.session_service.steps",
                "authentication with lockout logic, 10 policies"),
    "todo": ("demo/todo_app/service.py", "demo.todo_app.steps",
             "task board with a sync channel, 4 policies"),
    "ticketing": ("examples/ticketing/app_service.py",
                  "examples/ticketing/monitoring/steps.py",
                  "support-ticket service, 6 policies"),
}


def _load_policies(steps_ref: str):
    if steps_ref.endswith(".py"):
        spec = importlib.util.spec_from_file_location("slice_explorer_steps",
                                                      ROOT / steps_ref)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(steps_ref)
    registry = module.build_registry()
    return module.load_policies(registry), registry.entries()


def _function_spans(tree: ast.Module, stem: str) -> list[dict]:
    spans = []

    def add(node, qualname):
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        spans.append({"q": f"{stem}.{qualname}", "start": start,
                      "end": node.end_lineno})

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(item, f"{node.name}.{item.name}")
    return spans


def _constant_lines(tree: ast.Module, stem: str) -> dict[str, int]:
    lines: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    lines[f"{stem}.{target.id}"] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and isinstance(node.value, ast.Constant):
            lines[f"{stem}.{node.target.id}"] = node.lineno
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            lines[f"{stem}.{node.name}.{target.id}"] = item.lineno
    return lines


def _anchor_lines(tree: ast.Module, stem: str) -> dict[str, int]:
    """site_id -> source line, replicating the analyser's per-function walk
    order so the ids match analyze_app's exactly."""
    mod = _index_module(stem, tree)
    lines: dict[str, int] = {}
    for local_qualname, fn in mod.functions.items():
        caller = f"{stem}.{local_qualname}"
        ordinal = 0
        for stmt in getattr(fn, "body", []):
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call) and _is_anchor(node, mod):
                    ordinal += 1
                    lines[f"{caller}#{ordinal}"] = node.lineno
    return lines


def build_app_data(name: str) -> dict:
    file_rel, steps_ref, blurb = APPS[name]
    path = ROOT / file_rel
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    stem = path.stem
    policies, entries = _load_policies(steps_ref)
    sites = analyze_app([path])                      # the REAL analysis
    anchor_lines = _anchor_lines(tree, stem)

    steps_for_type: dict[str, set] = {}
    for entry in entries:
        steps_for_type.setdefault(entry.signature.event_type, set()).add(entry.step_id)

    site_records = []
    sites_by_function: dict[str, list[str]] = {}
    sites_by_constant: dict[str, list[str]] = {}
    for site in sites:
        step_ids = steps_for_type.get(site.event_type, set())
        direct = sorted({p.policy_id for p in policies
                         if set(p.used_step_ids) & step_ids})
        keys = frozenset(k for k in site.binding_keys if not k.startswith("<"))
        coupled = sorted({p.policy_id for p in policies
                          if p.has_deadline and frozenset(p.correlation_key) == keys}
                         - set(direct))
        site_records.append({
            "id": site.site_id, "event_type": site.event_type,
            "function": site.function, "line": anchor_lines.get(site.site_id),
            "members": sorted(site.slice_functions),
            "constants": sorted(site.referenced_constants),
            "unresolved": site.unresolved_calls,
            "policies": {"direct": direct, "coupled": coupled},
        })
        for member in site.slice_functions:
            sites_by_function.setdefault(member, []).append(site.site_id)
        for constant in site.referenced_constants:
            sites_by_constant.setdefault(constant, []).append(site.site_id)

    return {
        "name": name, "file": file_rel, "blurb": blurb,
        "lines": source.splitlines(),
        "spans": _function_spans(tree, stem),
        "constants": _constant_lines(tree, stem),
        "sites": site_records,
        "sites_by_function": sites_by_function,
        "sites_by_constant": sites_by_constant,
    }


class SliceExplorer:
    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._server = None
        self._thread = None

    def app_data(self, name: str) -> dict:
        if name not in self._cache:
            self._cache[name] = build_app_data(name)
        return self._cache[name]

    def start(self, port: int = 7010, host: str = "127.0.0.1") -> str:
        explorer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    body, content_type = _PAGE.encode(), "text/html; charset=utf-8"
                elif self.path == "/api/apps":
                    listing = [{"name": n, "file": f, "blurb": b}
                               for n, (f, _s, b) in APPS.items()]
                    body, content_type = json.dumps(listing).encode(), "application/json"
                elif self.path.startswith("/api/app/"):
                    name = self.path.rsplit("/", 1)[1]
                    if name not in APPS:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = json.dumps(explorer.app_data(name)).encode()
                    content_type = "application/json"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{host}:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>behave_rv · slice explorer</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='24' fill='%230f172a'/%3E%3Ctext x='50' y='74' font-size='68' text-anchor='middle' fill='white'%3E%E2%9C%82%3C/text%3E%3C/svg%3E">
<style>
  :root { --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f1f5f9;
          --surface:#fff; --member:#fef3c7; --memberline:#f59e0b;
          --anchor:#fee2e2; --anchorline:#dc2626; --const:#ede9fe;
          --radius:12px; }
  * { box-sizing:border-box; margin:0; }
  body { font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--ink); }
  header { background:var(--surface); border-bottom:1px solid var(--line);
           padding:12px 24px; display:flex; align-items:center; gap:14px; }
  h1 { font-size:16px; }
  header span { color:var(--muted); font-size:12.5px; }
  select { font:13px inherit; padding:4px 8px; border:1px solid var(--line);
           border-radius:8px; }
  main { display:grid; grid-template-columns:minmax(480px,1.3fr) minmax(360px,1fr);
         gap:14px; padding:14px 24px; align-items:start; }
  @media (max-width:980px) { main { grid-template-columns:1fr; } }
  .panel { background:var(--surface); border:1px solid var(--line);
           border-radius:var(--radius); padding:12px 14px; }
  .panel h2 { font-size:11px; text-transform:uppercase; letter-spacing:.08em;
              color:var(--muted); margin-bottom:10px; }
  #code { font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
          max-height:78vh; overflow:auto; }
  .ln { display:flex; cursor:pointer; border-radius:4px; white-space:pre; }
  .ln:hover { outline:1px solid #cbd5e1; }
  .ln .no { width:38px; flex:none; text-align:right; padding-right:10px;
            color:#94a3b8; user-select:none; }
  .ln.member { background:var(--member); box-shadow:inset 3px 0 var(--memberline); }
  .ln.anchor { background:var(--anchor); box-shadow:inset 3px 0 var(--anchorline);
               font-weight:600; }
  .ln.const  { background:var(--const); box-shadow:inset 3px 0 #7c3aed; }
  .ln.selected { outline:2px solid #0f172a; }
  .ln.dim { opacity:.45; }
  .tag { display:inline-block; margin:2px 4px 2px 0; padding:1px 8px;
         border-radius:999px; font:11px ui-monospace,monospace; }
  .tag.fn { background:var(--member); } .tag.site { background:var(--anchor); }
  .tag.k { background:var(--const); } .tag.hole { background:#e2e8f0; }
  .tag.pol { background:#dcfce7; } .tag.coupled { background:#fef9c3; }
  #info .block { border-top:1px solid #f1f5f9; padding:8px 0; }
  #info .head { font-weight:650; font-size:13px; margin-bottom:4px; }
  #info .why { color:var(--muted); font-size:12.5px; }
  details { margin-top:12px; }
  summary { cursor:pointer; font-weight:600; font-size:13px; }
  details p { margin:8px 0; font-size:12.5px; color:#334155; }
  .legend span { margin-right:12px; font-size:12px; }
  .sw { display:inline-block; width:10px; height:10px; border-radius:3px;
        margin-right:4px; vertical-align:-1px; }
</style>
</head>
<body>
<header>
  <h1>behave_rv slice explorer</h1>
  <select id="apps"></select>
  <span id="blurb"></span>
  <span style="margin-left:auto">click any line — the REAL analyser's slice, live</span>
</header>
<main>
  <div class="panel">
    <h2 id="filename">source</h2>
    <div class="legend">
      <span><i class="sw" style="background:var(--anchor)"></i>emission (anchor)</span>
      <span><i class="sw" style="background:var(--member)"></i>in the backward slice</span>
      <span><i class="sw" style="background:var(--const)"></i>referenced constant</span>
    </div>
    <div id="code"></div>
  </div>
  <div class="panel">
    <h2>what changing the selected line can affect</h2>
    <div id="info"><span class="why">Select a line on the left.</span></div>
    <details>
      <summary>How does it know how to slice? (the mechanism)</summary>
      <p><b>1. Anchors.</b> Every emission is a construction of behave_rv's
      <code>Event</code> type — a fixed, findable landmark. The analyser parses
      the source (it never imports it) and marks each one.</p>
      <p><b>2. The dependency graph.</b> It then builds, from the syntax tree
      alone: who calls whom (including <code>self.method</code> calls and
      cross-module imports), which methods write each <code>self.attr</code>
      that others read, which module/class constants each function references,
      and which decorators wrap each function.</p>
      <p><b>3. The backward slice.</b> From each anchor it closes over that
      graph to a fixed point: the emitting function, its transitive callers
      (they decide when it runs and with what arguments), the callees of all
      of those (they compute the emitted values), the writers of instance
      state any member reads, plus referenced constants and decorators.
      Everything in the closure can change when or what this anchor emits —
      nothing outside it can.</p>
      <p><b>4. Honest holes.</b> Calls the resolver cannot follow (injected
      callables like <code>self._emit</code>, dynamic dispatch) are listed as
      <i>unresolved</i> rather than silently ignored — the boundary is
      visible, per emission.</p>
      <p><b>5. Policies at risk.</b> Each emission's event type maps to the
      catalogue steps observing it, and each compiled policy records which
      steps it uses — so a flagged slice names exact policies. Deadline
      policies of the same entity are added conservatively: any change to the
      entity's event flow can move event time past a deadline.</p>
      <p>It is a <i>may-affect</i> analysis: it says "review these", never
      "this will violate". Runtime data (config, databases) has no static
      signature; replay and live monitoring are the layers above.</p>
    </details>
  </div>
</main>
<script>
let data = null;
async function loadApps() {
  const apps = await (await fetch('/api/apps')).json();
  const select = document.getElementById('apps');
  for (const app of apps) {
    const option = document.createElement('option');
    option.value = app.name;
    option.textContent = app.name;
    select.appendChild(option);
  }
  select.addEventListener('change', () => loadApp(select.value));
  loadApp(apps[0].name);
}
async function loadApp(name) {
  data = await (await fetch('/api/app/' + name)).json();
  document.getElementById('blurb').textContent = data.blurb;
  document.getElementById('filename').textContent = data.file;
  const code = document.getElementById('code');
  code.textContent = '';
  data.lines.forEach((text, index) => {
    const row = document.createElement('div');
    row.className = 'ln';
    row.dataset.line = index + 1;
    const no = document.createElement('span');
    no.className = 'no';
    no.textContent = index + 1;
    const src = document.createElement('span');
    src.textContent = text.length ? text : ' ';
    row.appendChild(no);
    row.appendChild(src);
    row.addEventListener('click', () => select(index + 1));
    code.appendChild(row);
  });
  document.getElementById('info').textContent = '';
  info('Select a line on the left.');
}
function info(text) {
  const why = document.createElement('span');
  why.className = 'why';
  why.textContent = text;
  document.getElementById('info').appendChild(why);
}
function spanOf(line) {
  return data.spans.find(s => line >= s.start && line <= s.end) || null;
}
function select(line) {
  const rows = document.querySelectorAll('#code .ln');
  rows.forEach(r => r.className = 'ln');
  rows[line - 1].classList.add('selected');
  const panel = document.getElementById('info');
  panel.textContent = '';

  const span = spanOf(line);
  const constant = Object.keys(data.constants)
    .find(key => data.constants[key] === line) || null;
  let siteIds = [];
  let subject = '';
  if (constant && data.sites_by_constant[constant]) {
    siteIds = data.sites_by_constant[constant];
    subject = 'constant ' + constant;
  } else if (span && data.sites_by_function[span.q]) {
    siteIds = data.sites_by_function[span.q];
    subject = 'function ' + span.q;
  } else if (span) {
    subject = 'function ' + span.q;
  }

  if (!siteIds.length) {
    rows.forEach(r => r.classList.add('dim'));
    rows[line - 1].className = 'ln selected';
    info(subject
      ? subject + ' is OUTSIDE every emission’s slice: no dependence path '
        + 'connects it to an Event(...) construction, so changing this line '
        + 'cannot affect what the monitor observes. The analyser stays silent.'
      : 'This line is module scaffolding (imports/blank); pick a line inside '
        + 'a function or a constant.');
    return;
  }

  const sites = data.sites.filter(s => siteIds.includes(s.id));
  const memberSet = new Set();
  const constantSet = new Set();
  sites.forEach(s => {
    s.members.forEach(m => memberSet.add(m));
    s.constants.forEach(k => constantSet.add(k));
  });
  data.spans.forEach(s => {
    if (memberSet.has(s.q)) {
      for (let l = s.start; l <= s.end; l++) rows[l - 1].classList.add('member');
    }
  });
  constantSet.forEach(key => {
    const l = data.constants[key];
    if (l) rows[l - 1].classList.add('const');
  });
  sites.forEach(s => { if (s.line) rows[s.line - 1].classList.add('anchor'); });
  rows[line - 1].classList.add('selected');

  const head = document.createElement('div');
  head.className = 'head';
  head.textContent = 'Changing ' + subject + ' can affect ' + sites.length
    + ' emission(s):';
  panel.appendChild(head);
  for (const site of sites) {
    const block = document.createElement('div');
    block.className = 'block';
    const title = document.createElement('div');
    title.className = 'head';
    title.textContent = site.id + '  →  event "' + site.event_type + '"';
    block.appendChild(title);
    addTags(block, 'backward slice (change any of these and this emission '
      + 'flags):', site.members, 'fn');
    if (site.constants.length)
      addTags(block, 'constants in the slice:', site.constants, 'k');
    if (site.unresolved.length)
      addTags(block, 'declared holes (calls the resolver cannot follow):',
              site.unresolved, 'hole');
    addTags(block, 'policies at risk:', site.policies.direct, 'pol');
    if (site.policies.coupled.length)
      addTags(block, 'deadline policies on the same entity (event-time '
        + 'coupling):', site.policies.coupled, 'coupled');
    panel.appendChild(block);
  }
}
function addTags(parent, label, items, kind) {
  const wrap = document.createElement('div');
  const caption = document.createElement('div');
  caption.className = 'why';
  caption.textContent = label;
  wrap.appendChild(caption);
  if (!items.length) {
    const none = document.createElement('span');
    none.className = 'why';
    none.textContent = '(none)';
    wrap.appendChild(none);
  }
  for (const item of items) {
    const tag = document.createElement('span');
    tag.className = 'tag ' + kind;
    tag.textContent = item;
    wrap.appendChild(tag);
  }
  parent.appendChild(wrap);
}
loadApps();
</script>
</body>
</html>
"""


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7010)
    parser.add_argument("--seconds", type=float, default=600.0,
                        help="how long to serve before shutting down")
    args = parser.parse_args()
    explorer = SliceExplorer()
    print("slice explorer:", explorer.start(port=args.port))
    try:
        import time
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    explorer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
