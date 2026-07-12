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
