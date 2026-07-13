"""The app side of the stability CLI: catalog save/diff --app, the build-time
check that catches application logic changes before any runtime violation."""

import json

import pytest

from behave_rv.__main__ import main
from behave_rv.catalog.store import load_app_surface, load_catalog

STEPS = '''
from behave_rv.steps import trigger

@trigger('a job is "{status}"', step_id="cli.job.status",
         event_type="job.status", correlation_key="job_id")
def job_is(ctx, event, status):
    if event.type == "job.status" and event.payload.get("status") == status:
        ctx.bind(job_id=event.bindings["job_id"])
        return True
    return False
'''

APP_V1 = '''
from behave_rv.events.event import Event

STATUS = "job.status"


class JobService:
    def __init__(self, emit, clock):
        self._emit = emit
        self._clock = clock

    def _status(self, job_id, status):
        self._emit(Event(STATUS, self._clock(), {"job_id": job_id},
                         {"status": status}, "jobs"))

    def start(self, job_id, priority):
        if priority != "low":
            self._status(job_id, "started")

    def finish(self, job_id):
        self._status(job_id, "finished")
'''

# an app LOGIC change: the condition under which "started" is emitted moves
APP_GUARD_CHANGED = APP_V1.replace('if priority != "low":', 'if priority == "high":')

# an app INTERFACE change: the payload field every policy step reads is renamed
APP_FIELD_RENAMED = APP_V1.replace('{"status": status}', '{"state": status}')

POLICY = """
Feature: job safety
  Scenario: finished only after started
    When a job is "finished"
    Then a job is "started" before
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "steps.py").write_text(STEPS)
    (tmp_path / "app.py").write_text(APP_V1)      # tests EDIT this file in place,
    policies = tmp_path / "policies"              # exactly like a real change
    policies.mkdir()
    (policies / "jobs.feature").write_text(POLICY)
    return tmp_path


def _save(workspace, capsys):
    rc = main(["catalog", "save", "--steps", str(workspace / "steps.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--app", str(workspace / "app.py")])
    assert rc == 0
    assert "app emit site(s)" in capsys.readouterr().out


def _diff(workspace, new_app_source, *extra):
    (workspace / "app.py").write_text(new_app_source)
    return main(["catalog", "diff", "--steps", str(workspace / "steps.py"),
                 "--catalog", str(workspace / "catalog.json"),
                 "--policies", str(workspace / "policies"),
                 "--app", str(workspace / "app.py"), *extra])


def test_save_with_app_persists_the_emit_sites(workspace, capsys):
    _save(workspace, capsys)
    sites = load_app_surface(workspace / "catalog.json")
    assert [s.event_type for s in sites] == ["job.status"]
    assert sites[0].payload_fields == ["status"]


def test_diff_clean_when_the_app_is_untouched(workspace, capsys):
    _save(workspace, capsys)
    rc = _diff(workspace, APP_V1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "app surface diff" in out and ": unchanged" in out
    assert "ok: no breaks" in out


def test_app_logic_change_is_a_scoped_behavior_risk(workspace, capsys):
    _save(workspace, capsys)
    rc = _diff(workspace, APP_GUARD_CHANGED)
    out = capsys.readouterr().out
    assert rc == 0                                     # a risk warns by default
    assert "APP BEHAVIOR RISKS" in out
    assert "JobService.start" in out                   # names the changed function
    assert "policies at risk: finished only after started" in out
    assert "1 app behavior risk(s)" in out


def test_fail_on_app_risk_promotes_the_risk_to_a_ci_gate(workspace, capsys):
    _save(workspace, capsys)
    rc = _diff(workspace, APP_GUARD_CHANGED, "--fail-on-app-risk")
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL: 1 break(s)" in out and "promoted by --fail-on-app-risk" in out


def test_app_payload_field_rename_is_a_break_and_gates_ci(workspace, capsys):
    _save(workspace, capsys)
    rc = _diff(workspace, APP_FIELD_RENAMED)
    out = capsys.readouterr().out
    assert rc == 1
    assert "APP BREAKS" in out
    assert "'status'" in out and "'state'" in out
    assert "policies at risk: finished only after started" in out


def test_diff_without_a_committed_app_surface_hints_instead_of_guessing(workspace, capsys):
    rc = main(["catalog", "save", "--steps", str(workspace / "steps.py"),
               "--catalog", str(workspace / "catalog.json")])   # no --app
    assert rc == 0
    capsys.readouterr()
    rc = _diff(workspace, APP_GUARD_CHANGED)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no app_surface section" in out and "catalog save --app" in out


def test_v2_catalogs_stay_readable_with_an_absent_app_surface(workspace, capsys):
    _save(workspace, capsys)
    path = workspace / "catalog.json"
    document = json.loads(path.read_text())
    document.pop("app_surface")
    document["catalog_format_version"] = 2
    path.write_text(json.dumps(document))
    assert load_catalog(path)[0].step_id == "cli.job.status"
    assert load_app_surface(path) is None


def test_save_warns_when_the_app_paths_yield_no_anchors(workspace, capsys):
    (workspace / "no_events.py").write_text("def f():\n    return 1\n")
    rc = main(["catalog", "save", "--steps", str(workspace / "steps.py"),
               "--catalog", str(workspace / "catalog.json"),
               "--app", str(workspace / "no_events.py")])
    assert rc == 0
    assert "no emit sites found" in capsys.readouterr().err
