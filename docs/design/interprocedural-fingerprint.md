# Design note: the interprocedural (call-graph) fingerprint

Status: implemented (2026-07-12). Origin: an **external design review** of the
stability mechanism proposed this fix for the C4 helper blind spot; this note
records the review's proposal and rationale as relayed (the review was not
preserved verbatim), plus the implementation decisions made here.

## The problem the review addressed

The stability catalog's measured table had exactly one MISS: **C4** — a
predicate delegates to a helper function, the helper's condition changes, the
policy goes dormant, and nothing speaks. The caller's AST fingerprint is
byte-identical (the body still just calls the helper) and the observed stream
is unchanged, so neither the diff nor liveness can see the change.

## The review's proposal

Extend the fingerprint over a **statically resolvable call graph**,
Merkle-style: a predicate's fingerprint covers the normalized ASTs of the
helpers it (transitively) reaches, so a helper body change moves the caller's
fingerprint exactly as an inline change would.

The review's essential constraint, and the reason this is the right fix and a
full semantic analyzer is the wrong one: **the mechanism's trust model only
tolerates errors in the loud direction.** A conservative structural check that
sometimes alarms on behavior-preserving refactors costs a glance. A clever
analyzer (PDG slicing, semantic equivalence) that can err in the QUIET
direction — deciding a real change is safe — inverts the trust model: the
tool's whole value is that silence means safe. So:

- resolution is deliberately narrow and auditable (same module, same-package
  module attributes), never heuristic;
- everything unresolvable is *visible*, not ignored: the signature records
  the unresolved call sites, so the protection boundary is per-step
  inspectable — the same convention as the existing "source unavailable,
  fingerprint empty, protection weaker" case;
- D3 (extract unchanged logic into a helper) deliberately STAYS a false
  alarm: fixing it needs semantic analysis, which is out of scope for the
  trust-model reason above.

## Implementation decisions (this repo)

1. **Resolution scope**: plain-name calls to functions in the predicate's own
   module (via `__globals__`), and `module.attr` calls where the module is
   imported in that module and the target function's top-level package equals
   the predicate's. No dynamic dispatch, no getattr, no function values, no
   object methods, no builtins/stdlib/third-party — all recorded in
   `unresolved_calls`. A resolvable function whose source is unavailable
   (e.g. a lambda) is also treated as unresolved rather than silently hashed
   as empty.
2. **Flat reachable-set hashing** instead of nested per-edge Merkle: the
   fingerprint combines the predicate's own normalized AST with the SORTED
   SET of the reachable helpers' own normalized-AST hashes. Same detection
   power for a set-equality diff (any reachable body change changes the
   set), trivially cycle-safe (each function contributes once), and
   order-independent (guarded by harness case A6).
3. **Helper names are NOT in the hash** — only body identities. Renaming a
   helper (call site updated) or reordering definitions absorbs, like every
   other rename (harness case A5). Names ARE stored in the catalog
   (`helper_hashes: {qualified_name: hash}`) so a break message can say
   *which* helper changed.
4. **Helper parameters are alpha-normalized like any local.** Only the
   predicate's own placeholder-bound parameters are contract-bearing (the B7
   lesson: the compiler binds phrasing placeholders to them by name); a
   helper's parameter names are internal representation.
5. **Serialization reuses the existing version-stable `_stable_dump`** — not
   forked — so the cross-version stability property (the ast.dump bug found
   by the fresh-clone check) carries over, re-verified with helpers included.
6. **Catalog schema v2.** Old (v1) catalogs are refused on load with a clear
   "recompute with `catalog save`" message — an explained mismatch, never
   spurious breaks.

## The boundary after this change

Covered: same-module and same-package statically resolvable calls, to full
transitive depth. Not covered, and stated: dynamic dispatch and calls through
values (the new C4b harness boundary), cross-package calls, object methods.
Where users see the boundary: `unresolved_calls` in the signature and the
committed catalog.
