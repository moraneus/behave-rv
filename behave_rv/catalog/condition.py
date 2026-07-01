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


def condition_fingerprint(func) -> str:
    """A rename-invariant fingerprint of the step's matching condition.

    Returns "" when the source is unavailable; the diff treats a "" fingerprint
    conservatively (any change away from a known fingerprint surfaces)."""
    fn = _function_ast(func)
    if fn is None:
        return ""
    normalized = _Alpha().visit(fn)
    ast.fix_missing_locations(normalized)
    return hashlib.sha256(ast.dump(normalized).encode()).hexdigest()[:16]


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
