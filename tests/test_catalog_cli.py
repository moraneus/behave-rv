"""The stability CLI: catalog save / catalog diff, the workflow a user or a CI
job actually runs after a code change."""


import pytest

from behave_rv.__main__ import main
from behave_rv.catalog.store import load_catalog
from behave_rv.events.event import Event
from behave_rv.events.sources.replay import record_events

STEPS_V1 = '''
from behave_rv.steps import trigger

@trigger('an order is "{status}"', step_id="cli.order.status",
         event_type="order.status", correlation_key="order_id")
def order_is(ctx, event, status):
    if event.type == "order.status" and event.payload.get("status") == status:
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False
'''

# the payload field the predicate reads is renamed: a contract change
STEPS_FIELD_RENAMED = STEPS_V1.replace('payload.get("status")', 'payload.get("state")')

# the function is renamed and internals reworded: representational only
STEPS_FUNCTION_RENAMED = STEPS_V1.replace("def order_is(ctx, event, status):",
                                          "def order_status_predicate(ctx, event, status):")

STEPS_WITH_NEW_TAP = STEPS_V1 + '''

@trigger('an order return is recorded', step_id="cli.order.return",
         event_type="order.return", correlation_key="order_id")
def order_returned(ctx, event):
    if event.type == "order.return":
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False
'''

POLICY = """
Feature: payment safety
  Scenario: paid after authorized
    When an order is "paid"
    Then an order is "authorized" before
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "steps_v1.py").write_text(STEPS_V1)
    (tmp_path / "steps_field_renamed.py").write_text(STEPS_FIELD_RENAMED)
    (tmp_path / "steps_function_renamed.py").write_text(STEPS_FUNCTION_RENAMED)
    (tmp_path / "steps_new_tap.py").write_text(STEPS_WITH_NEW_TAP)
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "payment.feature").write_text(POLICY)
    return tmp_path


def _save(workspace, capsys):
    rc = main(["catalog", "save", "--steps", str(workspace / "steps_v1.py"),
               "--catalog", str(workspace / "catalog.json")])
    assert rc == 0
    assert "1 step(s) written" in capsys.readouterr().out


def test_catalog_save_writes_a_loadable_artifact(workspace, capsys):
    _save(workspace, capsys)
    (entry,) = load_catalog(workspace / "catalog.json")
    assert entry.step_id == "cli.order.status"
    assert entry.signature.event_type == "order.status"


def test_catalog_diff_clean_when_nothing_changed(workspace, capsys):
    _save(workspace, capsys)
    rc = main(["catalog", "diff", "--steps", str(workspace / "steps_v1.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--policies", str(workspace / "policies")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cli.order.status: unchanged" in out
    assert "ok: no breaks" in out


def test_catalog_diff_absorbs_a_function_rename(workspace, capsys):
    _save(workspace, capsys)
    rc = main(["catalog", "diff", "--steps", str(workspace / "steps_function_renamed.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--policies", str(workspace / "policies")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cli.order.status: unchanged" in out
    assert "BREAKS" not in out


def test_catalog_diff_breaks_on_a_field_rename_and_gates_ci(workspace, capsys):
    _save(workspace, capsys)
    rc = main(["catalog", "diff", "--steps", str(workspace / "steps_field_renamed.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--policies", str(workspace / "policies"),
               "--owner", "payments-team"])
    out = capsys.readouterr().out
    assert rc == 1                                     # the CI gate
    assert "✗ paid after authorized  [payments-team]  via cli.order.status" in out
    assert "'status': 'any'" in out and "'state': 'any'" in out   # the contract diff
    assert "FAIL: 1 break(s)" in out


def test_catalog_diff_reports_liveness_against_a_trace(workspace, capsys):
    _save(workspace, capsys)
    trace = workspace / "trace.jsonl"
    record_events(trace, [
        Event("order.status", 1.0, {"order_id": "A"}, {"status": "AUTHORIZED"}, "app"),
        Event("order.status", 2.0, {"order_id": "A"}, {"status": "PAID"}, "app"),
    ])
    rc = main(["catalog", "diff", "--steps", str(workspace / "steps_v1.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--policies", str(workspace / "policies"),
               "--trace", str(trace)])
    out = capsys.readouterr().out
    assert rc == 0                                     # liveness warns, does not gate
    assert "status='paid'" in out and "status='authorized'" in out
    assert "liveness" in out


def test_catalog_diff_surfaces_new_uncovered_taps_as_suggestions(workspace, capsys):
    _save(workspace, capsys)
    rc = main(["catalog", "diff", "--steps", str(workspace / "steps_new_tap.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--policies", str(workspace / "policies")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "+ cli.order.return" in out and "no policy covers it" in out


def test_catalog_diff_reports_uncompilable_policies_with_the_break(workspace, capsys):
    _save(workspace, capsys)
    empty_steps = workspace / "steps_deleted.py"
    empty_steps.write_text("from behave_rv.steps import trigger\n")
    rc = main(["catalog", "diff", "--steps", str(empty_steps),
               "--catalog", str(workspace / "catalog.json"),
               "--policies", str(workspace / "policies")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "cli.order.status: removed" in out
    assert "UNCOMPILABLE" in out


def test_catalog_save_refuses_an_empty_steps_module(workspace, capsys):
    empty_steps = workspace / "steps_empty.py"
    empty_steps.write_text("x = 1\n")
    rc = main(["catalog", "save", "--steps", str(empty_steps),
               "--catalog", str(workspace / "catalog.json")])
    assert rc == 2
    assert "no steps registered" in capsys.readouterr().err


def test_catalog_diff_missing_artifact_is_a_usage_error(workspace, capsys):
    rc = main(["catalog", "diff", "--steps", str(workspace / "steps_v1.py"),
               "--catalog", str(workspace / "nope.json")])
    assert rc == 2
    assert "error:" in capsys.readouterr().err
