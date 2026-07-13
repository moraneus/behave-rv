"""Static change-impact analysis of the APPLICATION's monitorable surface.

The catalog (entry/diff/condition) makes the STEP side of the event boundary a
versioned contract. This module does the same for the APP side: it finds every
``Event(...)`` construction site in the application's source (the anchors our
in-process exposure convention guarantees are syntactically visible), computes
a function-granularity backward slice for each -- every function that can
participate in an execution ending at that emission -- and fingerprints it, so
a code change that MAY alter when or what the app emits is surfaced at build
time, scoped to the policies observing that event type, before any violation
occurs at runtime.

Lineage and the deliberate modifications (see STABILITY.md for the measured
E-series):

* Backward program slicing from an output statement (Ferrante-Ottenstein-
  Warren PDGs; Horwitz-Reps-Binkley interprocedural slicing). Modified to
  FUNCTION granularity with context-insensitive closure over the call graph
  instead of summary edges: the slice of an emit site is the emitting
  function, its transitive callers (they decide when it runs and with what
  arguments), and the transitive callees of all of those (they compute the
  values). Precision is lost only toward over-approximation -- extra
  warnings, never missed ones.
* Chianti-style change impact analysis (Ren et al.), with runtime-verification
  policies in the role Chianti gives regression tests: a changed slice maps
  through event type -> catalog steps -> ``used_step_ids`` -> the policies at
  risk.
* The rename-vs-break discipline of the step catalog, via the same
  alpha-normalized version-stable AST hashing (``condition._Alpha`` /
  ``_stable_dump``), with one app-side tightening (see ``_AppAlpha``): called
  names are preserved because emission ORDER is contract. Local, parameter,
  and class renames absorb silently; renaming a function on an emit path
  flags conservatively.

Everything is pure AST -- application modules are NEVER imported (they may
have side effects). The resolvable fragment is deliberately narrow and every
hole is declared, not swallowed: calls the resolver cannot follow land in
``unresolved_calls``, dynamic event types / non-literal payloads become
``<dynamic>`` markers, and ``**splat`` payload keys become ``<splat>``.
Emissions that do not construct ``behave_rv``'s ``Event`` under a recognizable
import are OUTSIDE the analysis (the CLI warns when a target yields no
anchors at all).
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from behave_rv.catalog.condition import _Alpha, _stable_dump

_BUILTIN_NAMES = frozenset(dir(builtins))
_EVENT_PARAMS = ("type", "event_time", "bindings", "payload", "source")

DYNAMIC = "<dynamic>"
SPLAT = "<splat>"

# classification statuses (app side)
APP_UNCHANGED = "unchanged"
APP_RENAMED = "renamed"        # absorbed silently, like a step rename
BEHAVIOR_RISK = "behavior-risk"  # interface intact, slice logic changed
INTERFACE_BREAK = "interface-break"
APP_REMOVED = "removed"
APP_ADDED = "added"


@dataclass
class EmitSite:
    """One ``Event(...)`` construction site and its fingerprinted slice."""

    site_id: str                    # "<module>.<qualname>#<k>" (k-th Event call in the function)
    module: str                     # file stem
    function: str                   # qualname of the enclosing function
    event_type: str                 # resolved literal/constant, or "<dynamic>"
    binding_keys: list[str]         # sorted; may contain markers
    payload_fields: list[str]       # sorted; may contain "<splat>"/"<dynamic>"
    slice_functions: dict[str, str]  # qualname -> alpha-normalized own-hash
    slice_fingerprint: str          # hash of member hashes + referenced constants
    unresolved_calls: list[str] = field(default_factory=list)
    # module-level constants any slice member reads, name -> repr(value): a
    # LIMIT = 10 participating in emission logic lives outside every function
    # body, so it must be fingerprinted separately (found by the mutation
    # campaign, docs/APP_SURFACE_EVALUATION.md)
    referenced_constants: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "module": self.module,
            "function": self.function,
            "event_type": self.event_type,
            "binding_keys": list(self.binding_keys),
            "payload_fields": list(self.payload_fields),
            "slice_functions": dict(sorted(self.slice_functions.items())),
            "slice_fingerprint": self.slice_fingerprint,
            "unresolved_calls": sorted(self.unresolved_calls),
            "referenced_constants": dict(sorted(self.referenced_constants.items())),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmitSite":
        return cls(
            site_id=data["site_id"],
            module=data["module"],
            function=data["function"],
            event_type=data["event_type"],
            binding_keys=list(data["binding_keys"]),
            payload_fields=list(data["payload_fields"]),
            slice_functions=dict(data["slice_functions"]),
            slice_fingerprint=data["slice_fingerprint"],
            unresolved_calls=list(data["unresolved_calls"]),
            referenced_constants=dict(data.get("referenced_constants", {})),
        )


# -- per-module indexing --------------------------------------------------------


class _AppAlpha(_Alpha):
    """Step-side alpha normalization, with one app-side tightening: the NAME of
    a called function is preserved rather than canonicalized. Occurrence-order
    canonical names would absorb a REORDER of two distinct calls -- harmless
    for pure step predicates, but app calls emit, and emission order is
    contract (a ``before`` policy hangs on it). The cost is deliberate and
    conservative: renaming a function on an emit path flags as behavior-risk
    instead of absorbing (local/parameter/class renames still absorb)."""

    visit_AsyncFunctionDef = _Alpha.visit_FunctionDef

    def visit_Call(self, node: ast.Call):
        preserved = node.func.id if isinstance(node.func, ast.Name) else None
        self.generic_visit(node)
        if preserved is not None:
            node.func.id = preserved
        return node


def _strip_docstrings(node: ast.AST) -> None:
    """Docstrings are Expr(Constant) statements, so the hash would flag an edit
    to one -- but a docstring cannot change emission behavior (found on real
    history: a docstring-only commit flagged as behavior-risk). Representational,
    so stripped. Code reading ``__doc__`` at runtime is outside the fragment."""
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = child.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                child.body = body[1:] or [ast.Pass()]


def _norm_hash(fn: ast.AST) -> str:
    clone = copy.deepcopy(fn)              # _Alpha mutates in place
    _strip_docstrings(clone)
    normalized = _AppAlpha().visit(clone)
    ast.fix_missing_locations(normalized)
    return hashlib.sha256(_stable_dump(normalized).encode()).hexdigest()[:16]


@dataclass
class _Module:
    stem: str
    constants: dict[str, object] = field(default_factory=dict)  # NAME -> scalar value
    class_constants: dict[tuple[str, str], object] = field(default_factory=dict)
    functions: dict[str, ast.AST] = field(default_factory=dict)  # local qualname -> node
    methods_of: dict[str, set[str]] = field(default_factory=dict)  # class -> method names
    event_names: set[str] = field(default_factory=set)   # local names bound to Event
    event_modules: set[str] = field(default_factory=set)  # names whose .Event is the anchor
    from_imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    plain_imports: dict[str, str] = field(default_factory=dict)


def _index_module(stem: str, tree: ast.Module) -> _Module:
    mod = _Module(stem=stem)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    mod.constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and isinstance(node.value, ast.Constant):
            mod.constants[node.target.id] = node.value.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod.functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            mod.methods_of[node.name] = set()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mod.functions[f"{node.name}.{item.name}"] = item
                    mod.methods_of[node.name].add(item.name)
                elif isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            mod.class_constants[(node.name, target.id)] = item.value.value
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module.startswith("behave_rv") and alias.name == "Event":
                    mod.event_names.add(local)
                elif node.module.startswith("behave_rv") and alias.name == "event":
                    mod.event_modules.add(local)
                else:
                    mod.from_imports[local] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod.plain_imports[alias.asname or alias.name.split(".")[0]] = alias.name
    return mod


def _is_anchor(call: ast.Call, mod: _Module) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in mod.event_names
    return (isinstance(f, ast.Attribute) and f.attr == "Event"
            and isinstance(f.value, ast.Name) and f.value.id in mod.event_modules)


def _resolve_call(call: ast.Call, mod: _Module, class_name: Optional[str],
                  modules: dict[str, _Module]) -> tuple[Optional[str], Optional[str]]:
    """(resolved global qualname, unresolved display name). Anchors and builtins
    return (None, None): they are neither edges nor holes."""
    f = call.func
    if _is_anchor(call, mod):
        return None, None
    if isinstance(f, ast.Name):
        name = f.id
        if name in mod.functions:
            return f"{mod.stem}.{name}", None
        if name in mod.from_imports:
            source_module, original = mod.from_imports[name]
            target = modules.get(source_module.split(".")[-1])
            if target is not None and original in target.functions:
                return f"{target.stem}.{original}", None
        if name in _BUILTIN_NAMES:
            return None, None
        return None, name
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        base, attr = f.value.id, f.attr
        if base == "self" and class_name is not None \
                and attr in mod.methods_of.get(class_name, ()):
            return f"{mod.stem}.{class_name}.{attr}", None
        if base in mod.plain_imports:
            target = modules.get(mod.plain_imports[base].split(".")[-1])
            if target is not None and attr in target.functions:
                return f"{target.stem}.{attr}", None
        return None, f"{base}.{attr}"
    return None, DYNAMIC


# -- the emitted interface: type, binding keys, payload keys ---------------------


def _event_args(call: ast.Call) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for position, arg in enumerate(call.args[: len(_EVENT_PARAMS)]):
        out[_EVENT_PARAMS[position]] = arg
    for keyword in call.keywords:
        if keyword.arg:
            out[keyword.arg] = keyword.value
    return out


def _resolve_type(node: Optional[ast.AST], mod: _Module) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and isinstance(mod.constants.get(node.id), str):
        return mod.constants[node.id]
    return DYNAMIC


def _dict_keys(node: Optional[ast.AST]) -> list[str]:
    if not isinstance(node, ast.Dict):
        return [DYNAMIC]
    keys: list[str] = []
    for key in node.keys:
        if key is None:
            keys.append(SPLAT)
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.append(key.value)
        else:
            keys.append(DYNAMIC)
    return sorted(keys)


# -- the analysis ----------------------------------------------------------------


def analyze_app(paths) -> list[EmitSite]:
    """Discover and fingerprint every emit site under the given .py files/dirs."""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])

    modules: dict[str, _Module] = {}
    trees: dict[str, ast.Module] = {}
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        modules[file.stem] = _index_module(file.stem, tree)
        trees[file.stem] = tree

    # call graph over global qualnames, plus unresolved holes per function
    edges: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    unresolved_of: dict[str, set[str]] = defaultdict(set)
    const_reads: dict[str, set[str]] = defaultdict(set)   # fn -> qualified const keys
    const_values: dict[str, str] = {}                      # qualified key -> repr(value)
    attr_reads: dict[str, set[str]] = defaultdict(set)    # method -> self.X read
    setters: dict[tuple[str, str], set[str]] = defaultdict(set)  # (class, attr) -> methods
    class_of: dict[str, str] = {}                          # method -> global class name
    anchors: list[tuple[str, str, ast.Call]] = []   # (global fn qualname, stem, call)
    for stem, mod in modules.items():
        for local_qualname, fn in mod.functions.items():
            caller = f"{stem}.{local_qualname}"
            class_name = local_qualname.split(".")[0] if "." in local_qualname else None
            if class_name is not None:
                class_of[caller] = f"{stem}.{class_name}"
            for stmt in getattr(fn, "body", []):
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Name) and node.id in mod.constants:
                        key = f"{stem}.{node.id}"
                        const_reads[caller].add(key)
                        const_values[key] = repr(mod.constants[node.id])
                    elif isinstance(node, ast.Attribute) \
                            and isinstance(node.value, ast.Name) \
                            and node.value.id == "self" and class_name is not None:
                        # a store defines instance state; a load depends on it
                        if isinstance(node.ctx, ast.Store):
                            setters[(f"{stem}.{class_name}", node.attr)].add(caller)
                        else:
                            attr_reads[caller].add(node.attr)
                            # a class-level constant is state too (same hole
                            # class as module constants: no function body owns
                            # its value)
                            if (class_name, node.attr) in mod.class_constants:
                                key = f"{stem}.{class_name}.{node.attr}"
                                const_reads[caller].add(key)
                                const_values[key] = repr(
                                    mod.class_constants[(class_name, node.attr)])
                    if not isinstance(node, ast.Call):
                        continue
                    if _is_anchor(node, mod):
                        anchors.append((caller, stem, node))
                        continue
                    resolved, hole = _resolve_call(node, mod, class_name, modules)
                    if resolved is not None:
                        edges[caller].add(resolved)
                        reverse[resolved].add(caller)
                    elif hole is not None:
                        unresolved_of[caller].add(hole)

    all_functions = {f"{stem}.{qn}": fn
                     for stem, mod in modules.items() for qn, fn in mod.functions.items()}
    hash_cache: dict[str, str] = {}

    def own_hash(qualname: str) -> str:
        if qualname not in hash_cache:
            hash_cache[qualname] = _norm_hash(all_functions[qualname])
        return hash_cache[qualname]

    def closure(seeds: set[str], graph: dict[str, set[str]]) -> set[str]:
        seen, queue = set(seeds), list(seeds)
        while queue:
            for neighbor in graph.get(queue.pop(), ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        return seen

    slice_cache: dict[str, set[str]] = {}

    def slice_of(function: str) -> set[str]:
        """Fixpoint of: callers of the seeds (they decide when/with what), their
        callees (they compute values), and -- because emit-path state flows
        through instance attributes (self._emit itself is one; found by the
        mutation campaign) -- every method that ASSIGNS a self-attribute some
        member reads, which then gets the same caller/callee treatment."""
        if function not in slice_cache:
            seeds = {function}
            while True:
                members = closure(closure(seeds, reverse), edges)
                writers = set()
                for member in members:
                    cls = class_of.get(member)
                    if cls is None:
                        continue
                    for attr in attr_reads.get(member, ()):
                        writers |= setters.get((cls, attr), set())
                if writers <= members:
                    slice_cache[function] = members
                    break
                seeds |= writers
        return slice_cache[function]

    sites: list[EmitSite] = []
    ordinal: dict[str, int] = defaultdict(int)
    for caller, stem, call in anchors:
        ordinal[caller] += 1
        mod = modules[stem]
        args = _event_args(call)
        members = slice_of(caller)
        slice_functions = {qn: own_hash(qn) for qn in sorted(members)}
        constants: dict[str, str] = {}
        for member in members:
            for key in const_reads.get(member, ()):
                constants[key] = const_values[key]
        fingerprint = hashlib.sha256(
            (",".join(sorted(set(slice_functions.values())))
             + "|consts:" + ",".join(f"{k}={v}" for k, v in sorted(constants.items()))
             ).encode()).hexdigest()[:16]
        holes = sorted(set().union(*(unresolved_of.get(qn, set()) for qn in members)))
        sites.append(EmitSite(
            site_id=f"{caller}#{ordinal[caller]}",
            module=stem,
            function=caller[len(stem) + 1:],
            event_type=_resolve_type(args.get("type"), mod),
            binding_keys=_dict_keys(args.get("bindings")),
            payload_fields=_dict_keys(args.get("payload")),
            slice_functions=slice_functions,
            slice_fingerprint=fingerprint,
            unresolved_calls=holes,
            referenced_constants=constants,
        ))
    return sorted(sites, key=lambda s: s.site_id)


# -- diff and classification ------------------------------------------------------


@dataclass(frozen=True)
class AppChange:
    site_id: str
    status: str
    old: Optional[EmitSite]
    new: Optional[EmitSite]
    detail: str = ""


def _interface(site: EmitSite) -> tuple:
    return (site.event_type, tuple(site.binding_keys), tuple(site.payload_fields))


def _describe_interface(old: EmitSite, new: EmitSite) -> str:
    parts = []
    if old.event_type != new.event_type:
        parts.append(f"event_type {old.event_type!r} -> {new.event_type!r}")
    if old.binding_keys != new.binding_keys:
        parts.append(f"binding_keys {old.binding_keys} -> {new.binding_keys}")
    if old.payload_fields != new.payload_fields:
        parts.append(f"payload_fields {old.payload_fields} -> {new.payload_fields}")
    return "; ".join(parts)


def _describe_risk(old: EmitSite, new: EmitSite) -> str:
    changed = sorted(qn for qn in old.slice_functions.keys() & new.slice_functions.keys()
                     if old.slice_functions[qn] != new.slice_functions[qn])
    gone = sorted(old.slice_functions.keys() - new.slice_functions.keys())
    added = sorted(new.slice_functions.keys() - old.slice_functions.keys())
    parts = []
    if changed:
        parts.append("function(s) in the emit slice changed: " + ", ".join(changed))
    if gone or added:
        parts.append(f"emit-slice membership changed (-{gone or '[]'} +{added or '[]'})")
    const_moved = sorted(
        name for name in old.referenced_constants.keys() | new.referenced_constants.keys()
        if old.referenced_constants.get(name) != new.referenced_constants.get(name))
    if const_moved:
        parts.append("module constant(s) in the emit slice changed: " + ", ".join(const_moved))
    new_holes = sorted(set(new.unresolved_calls) - set(old.unresolved_calls))
    if new_holes:
        parts.append("new unresolved call(s) in the slice: " + ", ".join(new_holes))
    return "; ".join(parts) or "slice fingerprint changed"


def classify_app_changes(old_sites: list[EmitSite],
                         new_sites: list[EmitSite]) -> list[AppChange]:
    """Classify every emit site across two versions of the app surface.

    First pass matches by ``site_id``. Orphans are then re-paired twice: sites
    whose interface AND slice fingerprint are identical are a pure move (e.g. a
    class rename) -- absorbed silently, like a step rename; sites matching on
    interface alone are the emitting function renamed and/or changed, which
    cannot be proven representational, so they flag as behavior-risk rather
    than as a removal. What remains is genuinely removed or added.
    """
    old_by = {s.site_id: s for s in old_sites}
    new_by = {s.site_id: s for s in new_sites}
    changes: list[AppChange] = []
    orphans_old = [s for s in old_sites if s.site_id not in new_by]
    orphans_new = [s for s in new_sites if s.site_id not in old_by]

    for site_id in sorted(set(old_by) & set(new_by)):
        o, n = old_by[site_id], new_by[site_id]
        if _interface(o) != _interface(n):
            changes.append(AppChange(site_id, INTERFACE_BREAK, o, n, _describe_interface(o, n)))
        elif o.slice_fingerprint != n.slice_fingerprint:
            changes.append(AppChange(site_id, BEHAVIOR_RISK, o, n, _describe_risk(o, n)))
        else:
            changes.append(AppChange(site_id, APP_UNCHANGED, o, n))

    unclaimed = list(orphans_new)
    unmatched_old = []
    for o in orphans_old:                      # exact matches claim first: a pure move
        n = next((s for s in unclaimed
                  if _interface(s) == _interface(o)
                  and s.slice_fingerprint == o.slice_fingerprint), None)
        if n is not None:
            unclaimed.remove(n)
            changes.append(AppChange(n.site_id, APP_RENAMED, o, n,
                                     f"emitting function moved: {o.site_id} -> {n.site_id}"))
        else:
            unmatched_old.append(o)
    for o in unmatched_old:                    # interface-only: rename and/or change
        n = next((s for s in unclaimed if _interface(s) == _interface(o)), None)
        if n is not None:
            unclaimed.remove(n)
            changes.append(AppChange(
                n.site_id, BEHAVIOR_RISK, o, n,
                f"emitting function renamed and/or changed ({o.site_id} -> "
                f"{n.site_id}); cannot be proven representational"))
        else:
            changes.append(AppChange(o.site_id, APP_REMOVED, o, None,
                                     f"emit site removed (event {o.event_type!r})"))
    for n in unclaimed:
        changes.append(AppChange(n.site_id, APP_ADDED, None, n,
                                 f"new emit site (event {n.event_type!r})"))
    return sorted(changes, key=lambda c: c.site_id)


def affected_step_ids(site: EmitSite, entries) -> list[str]:
    """Catalog steps observing this site's event type -- the scoping hop from an
    app change to the policies at risk. A ``<dynamic>`` type cannot be scoped;
    the caller must report that as unscopable rather than guessing."""
    return sorted(e.step_id for e in entries if e.signature.event_type == site.event_type)


def scope_step_ids(change: AppChange, entries) -> Optional[list[str]]:
    """Steps at risk for a classified change, over BOTH sides: an event type
    that changed value must alert the policies observing the OLD type (their
    events stop arriving) as well as the new one (found by the scoping
    experiment: scoping only the new side reported a mutated type to nobody).
    ``None`` means unscopable (a dynamic type on either side)."""
    sites = [s for s in (change.old, change.new) if s is not None]
    if any(s.event_type == DYNAMIC for s in sites):
        return None
    out: set = set()
    for site in sites:
        out.update(affected_step_ids(site, entries))
    return sorted(out)
