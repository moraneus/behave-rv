"""The call-graph fingerprint: directed tests for the C4 fix
(docs/design/interprocedural-fingerprint.md)."""

import json

import pytest

from behave_rv.catalog.store import load_catalog, save_catalog
from tests import _fingerprint_fixtures as fx


def test_helper_change_moves_the_callers_fingerprint():
    assert fx.bundle_with(fx.pred, fx.helper_v1)[0] != fx.bundle_with(fx.pred, fx.helper_v2)[0]


def test_helper_formatting_and_local_renames_absorb():
    base = fx.bundle_with(fx.pred, fx.helper_v1)[0]
    assert fx.bundle_with(fx.pred, fx.helper_v1_reformatted)[0] == base
    assert fx.bundle_with(fx.pred, fx.helper_v1_locals_renamed)[0] == base


def test_cycles_contribute_each_function_once():
    fingerprint, helpers, _ = fx.fingerprint_bundle(fx.pred_cyclic)
    assert fingerprint
    assert {k.split(".")[-1] for k in helpers} == {"_cycle_a", "_cycle_b"}


def test_unresolved_calls_are_visible_not_ignored():
    _, helpers, unresolved = fx.fingerprint_bundle(fx.pred_dynamic)
    assert helpers == {}                        # nothing statically resolvable
    assert "check" in unresolved                # the function-valued call site
    assert "get" in unresolved                  # object method, out of scope


def test_transitive_helpers_are_covered():
    fingerprint, helpers, _ = fx.fingerprint_bundle(fx.pred_deep)
    # _inner delegates through the _INNER module global -- which resolution
    # follows (a global holding a function IS the C4 pattern), so the closure
    # reaches three functions, inner_v1 included
    assert {k.split(".")[-1] for k in helpers} == {"_outer", "_inner", "inner_v1"}
    assert fx.bundle_with(fx.pred_deep, inner=fx.inner_v2)[0] != fingerprint


def test_v1_catalogs_are_refused_with_recompute_guidance(tmp_path):
    from demo.order_service.steps import build_registry

    path = tmp_path / "catalog.json"
    save_catalog(path, build_registry().entries())
    document = json.loads(path.read_text())
    document["catalog_format_version"] = 1
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="catalog save"):
        load_catalog(path)


def test_v1_catalog_is_a_clean_cli_error_not_spurious_breaks(tmp_path, capsys):
    from behave_rv.__main__ import main
    from demo.order_service.steps import build_registry

    path = tmp_path / "catalog.json"
    save_catalog(path, build_registry().entries())
    document = json.loads(path.read_text())
    document["catalog_format_version"] = 1
    path.write_text(json.dumps(document))
    rc = main(["catalog", "diff", "--steps", "demo/order_service/steps.py",
               "--catalog", str(path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "format v1" in err and "spurious breaks" in err


def test_same_package_module_attribute_calls_resolve():
    # the second resolution form: module.attr where the module is a
    # same-top-level-package sibling (mutation spot-check exposed this branch
    # as untested)
    fingerprint, helpers, unresolved = fx.fingerprint_bundle(fx.pred_pkg_attr)
    assert fingerprint
    assert set(helpers) == {"tests._pkg_helper_mod.check"}
    assert unresolved == ["get"]     # the helper's own method call, transitively visible


def test_cross_package_and_third_party_calls_stay_unresolved():
    _, helpers, unresolved = fx.fingerprint_bundle(fx.pred_third_party)
    assert helpers == {}
    assert "third_party_by_name" in unresolved       # by-name import of parse.parse
    assert "parse.parse" in unresolved               # module-attr, other package
    assert "json.dumps" in unresolved                # stdlib module-attr
    assert "str" in unresolved and "bool" in unresolved   # builtins, per design


def test_dynamic_call_forms_are_unresolved_and_never_crash():
    _, helpers, unresolved = fx.fingerprint_bundle(fx.pred_dynamic_forms)
    assert helpers == {}
    assert "<dynamic>" in unresolved                 # the subscripted call
    assert "_lambda_helper" in unresolved            # resolvable name, no source


def test_same_package_by_name_imports_resolve():
    # the Name branch's package clause: a by-name import from a sibling module
    # (different __module__, same top-level package) must resolve
    fingerprint, helpers, _ = fx.fingerprint_bundle(fx.pred_pkg_by_name)
    assert fingerprint
    assert set(helpers) == {"tests._pkg_helper_mod.check"}
