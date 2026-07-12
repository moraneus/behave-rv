"""Capture the body-level trigger condition of an RV step in its signature.

The signature must reflect *under what condition a step matches an event*, not
just the event type and field set. Otherwise a change to the matching logic
inside the function body (e.g. adding ``and amount > 0``) would move the step's
meaning with no Break -- a hole in the exact guarantee the catalog exists to
provide.

We do not attempt full semantic analysis of arbitrary Python. We compute a sound
over-approximation: an alpha-normalized fingerprint of the function body that is

* invariant to identifier names and formatting (a pure rename stays silent), and
* sensitive to structure and constants (changing the condition changes it).

What it CAN detect: added/removed/changed comparisons, guards, boolean structure,
changed string/number literals, changed attribute access, changed payload keys.
What it CANNOT detect as equivalent (so it surfaces a Break -- bias toward
surfacing): structure-preserving-but-semantically-equal refactors such as
introducing a temporary variable, reordering ``and`` operands, or extracting a
helper. What it cannot see at all: logic hidden behind a called helper whose own
source is elsewhere, and conditions whose source is unavailable (returns "").
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from typing import Optional


def _function_ast(func) -> ast.AST | None:
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return None
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


class _Alpha(ast.NodeTransformer):
    """Rename identifiers to positional canonical names; strip the def name and
    decorators (both representational). Attribute names and constants are kept."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def _canon(self, name: str) -> str:
        if name not in self._map:
            self._map[name] = f"v{len(self._map)}"
        return self._map[name]

    def visit_FunctionDef(self, node: ast.FunctionDef):
        node.name = "_"
        node.decorator_list = []
        node.returns = None
        for arg in (*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs):
            arg.arg = self._canon(arg.arg)
            arg.annotation = None
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name):
        node.id = self._canon(node.id)
        return node


def _own_hash(fn: ast.AST, binding_params: Optional[str]) -> str:
    """Hash one function's own normalized AST. ``binding_params`` is appended
    for the PREDICATE only: its placeholder-bound parameter names are contract
    (the compiler calls ``func(**params)`` by name -- the B7 lesson). Helper
    parameters are internal representation and are alpha-normalized away like
    any local."""
    normalized = _Alpha().visit(fn)
    ast.fix_missing_locations(normalized)
    payload = _stable_dump(normalized)
    if binding_params is not None:
        payload += f"|params:{binding_params}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _binding_params(fn: ast.AST) -> str:
    return ",".join(
        arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args)[2:]
    ) + "|" + ",".join(sorted(arg.arg for arg in fn.args.kwonlyargs))


def _resolve_calls(func, tree: ast.AST):
    """Statically resolve this function's call sites, deliberately narrowly.

    Resolved: plain-name calls to functions in the same module (via
    ``__globals__``), and ``module.attr`` calls where the module is imported
    here and the target function lives in the same top-level package as the
    predicate. Everything else -- dynamic dispatch, getattr, functions passed
    as values, object methods, builtins/stdlib/third-party, and resolvable
    functions whose source is unavailable (lambdas) -- lands in the unresolved
    list: visible, never silently ignored.
    """
    globals_ = getattr(func, "__globals__", {}) or {}
    own_module = getattr(func, "__module__", "") or ""
    own_package = own_module.split(".")[0]
    resolved: list = []
    unresolved: list[str] = []
    # walk the BODY only: decorator calls are representational (stripped from
    # the hash) and must not pollute the unresolved-calls visibility list
    body_nodes = [n for stmt in getattr(tree, "body", []) for n in ast.walk(stmt)]
    for node in body_nodes:
        if not isinstance(node, ast.Call):
            continue
        candidate, name = None, "<dynamic>"
        if isinstance(node.func, ast.Name):
            name = node.func.id
            target = globals_.get(name)
            if inspect.isfunction(target):
                target_module = getattr(target, "__module__", "") or ""
                if target_module == own_module or (
                        own_package and target_module.split(".")[0] == own_package):
                    candidate = target
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if isinstance(node.func.value, ast.Name):
                name = f"{node.func.value.id}.{node.func.attr}"
                base = globals_.get(node.func.value.id)
                if inspect.ismodule(base):
                    target = getattr(base, node.func.attr, None)
                    if inspect.isfunction(target):
                        target_module = getattr(target, "__module__", "") or ""
                        if own_package and target_module.split(".")[0] == own_package:
                            candidate = target
        if candidate is not None and _function_ast(candidate) is not None:
            resolved.append(candidate)
        else:
            unresolved.append(name)
    return resolved, unresolved


def fingerprint_bundle(func):
    """(fingerprint, helper_hashes, unresolved_calls) for a step predicate.

    The fingerprint covers the predicate's own normalized AST (binding
    parameters contract-bearing, per B7) plus the SORTED SET of reachable
    helpers' own normalized-AST hashes -- flat reachable-set hashing rather
    than nested Merkle: the same detection power for a set-equality diff, and
    trivially cycle-safe and order-independent. Helper NAMES are not hashed
    (a helper rename absorbs, harness case A5); they are returned in
    ``helper_hashes`` so a break can say WHICH helper changed. Resolution is
    transitive to full depth; cycles contribute each function once.

    Returns ("", {}, []) when the predicate's source is unavailable; the diff
    treats "" conservatively.
    """
    fn = _function_ast(func)
    if fn is None:
        return "", {}, []

    helper_hashes: dict[str, str] = {}
    unresolved: list[str] = []
    visited = {f"{func.__module__}.{func.__qualname__}"}
    queue = [func]
    own = None
    while queue:
        current = queue.pop()
        # resolution FIRST, on a fresh parse: _Alpha normalization mutates the
        # tree in place and renames the very Names resolution looks up
        tree = fn if current is func else _function_ast(current)
        resolved, unres = _resolve_calls(current, tree)
        unresolved.extend(unres)
        for helper in resolved:
            qualname = f"{helper.__module__}.{helper.__qualname__}"
            if qualname not in visited:
                visited.add(qualname)
                queue.append(helper)
        if current is func:
            own = _own_hash(tree, _binding_params(tree))
        else:
            helper_hashes[f"{current.__module__}.{current.__qualname__}"] = \
                _own_hash(tree, None)

    combined = own + "|calls:" + ",".join(sorted(set(helper_hashes.values())))
    fingerprint = hashlib.sha256(combined.encode()).hexdigest()[:16]
    return fingerprint, helper_hashes, sorted(set(unresolved))


def condition_fingerprint(func) -> str:
    """The fingerprint alone; see :func:`fingerprint_bundle` for the full
    contract (helpers reached, unresolved call sites)."""
    return fingerprint_bundle(func)[0]


def _stable_dump(node) -> str:
    """A Python-version-stable AST serialization.

    ``ast.dump`` output changes across interpreter versions (new node fields
    appear, e.g. ``type_params`` in 3.12), so a fingerprint built on it made
    the committed catalog report spurious breaks the moment a teammate or CI
    ran a different Python. Serializing node type names plus only the
    NON-EMPTY fields is stable across versions for any code that does not use
    syntax the older version lacks -- and code that does cannot run there
    anyway."""
    if isinstance(node, ast.AST):
        parts = []
        for name, value in ast.iter_fields(node):
            if value is None or (isinstance(value, list) and not value):
                continue  # absent-vs-empty is exactly where versions disagree
            parts.append(f"{name}={_stable_dump(value)}")
        return f"{node.__class__.__name__}({', '.join(parts)})"
    if isinstance(node, list):
        return "[" + ", ".join(_stable_dump(item) for item in node) + "]"
    return repr(node)


def payload_fields(func) -> dict[str, str]:
    """The payload keys the step reads (``event.payload.get("k")`` / ``["k"]``).

    Types are not inferred soundly, so every field maps to ``"any"``. The point is
    field-level visibility: dropping or adding a read changes the field map."""
    fn = _function_ast(func)
    fields: dict[str, str] = {}
    if fn is None:
        return fields
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "payload"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            fields[node.args[0].value] = "any"
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "payload"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            fields[node.slice.value] = "any"
    return fields
