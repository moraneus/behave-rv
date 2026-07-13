"""The app-surface analyzer: emit-site discovery, slices, and rename-vs-break."""

from pathlib import Path

from behave_rv.catalog.app_surface import (
    APP_ADDED,
    APP_REMOVED,
    APP_RENAMED,
    APP_UNCHANGED,
    BEHAVIOR_RISK,
    DYNAMIC,
    INTERFACE_BREAK,
    SPLAT,
    EmitSite,
    affected_step_ids,
    analyze_app,
    classify_app_changes,
)

ROOT = Path(__file__).resolve().parents[1]

BASE = '''
from behave_rv.events.event import Event

STATUS = "job.status"
DONE = "job.done"


def _fmt(name):
    return name.strip()


def helper_value(name):
    return _fmt(name)


class JobService:
    def __init__(self, emit, clock):
        self._emit = emit
        self._clock = clock

    def _status(self, job_id, status):
        self._emit(Event(STATUS, self._clock(), {"job_id": job_id},
                         {"status": status}, "jobs"))

    def start(self, job_id, name):
        label = helper_value(name)
        if label:
            self._status(job_id, "started")

    def finish(self, job_id):
        self._status(job_id, "finished")
        self._emit(Event(DONE, self._clock() + 1e-3, {"job_id": job_id}, {}, "jobs"))


def unrelated_report(count):
    return f"{count} jobs"
'''


def _analyze(tmp_path, source, name="jobs_app.py"):
    path = tmp_path / name
    path.write_text(source)
    return analyze_app([path])


def _classify(tmp_path, old_source, new_source):
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir(exist_ok=True), new_dir.mkdir(exist_ok=True)
    return classify_app_changes(_analyze(old_dir, old_source),
                                _analyze(new_dir, new_source))


def _statuses(changes):
    return {c.site_id: c.status for c in changes}


def test_discovery_finds_every_anchor_with_resolved_types(tmp_path):
    sites = {s.site_id: s for s in _analyze(tmp_path, BASE)}
    assert set(sites) == {"jobs_app.JobService._status#1",
                          "jobs_app.JobService.finish#1"}
    assert sites["jobs_app.JobService._status#1"].event_type == "job.status"
    assert sites["jobs_app.JobService.finish#1"].event_type == "job.done"
    assert sites["jobs_app.JobService._status#1"].binding_keys == ["job_id"]
    assert sites["jobs_app.JobService._status#1"].payload_fields == ["status"]
    assert sites["jobs_app.JobService.finish#1"].payload_fields == []


def test_slice_covers_callers_and_their_callees_but_not_unrelated_code(tmp_path):
    (site,) = [s for s in _analyze(tmp_path, BASE)
               if s.site_id == "jobs_app.JobService._status#1"]
    members = set(site.slice_functions)
    assert {"jobs_app.JobService._status", "jobs_app.JobService.start",
            "jobs_app.JobService.finish", "jobs_app.helper_value",
            "jobs_app._fmt"} <= members
    assert "jobs_app.unrelated_report" not in members
    # the injected transport is a declared hole, not a silent one
    assert "self._emit" in site.unresolved_calls


def test_discovery_on_the_committed_ticketing_example(tmp_path):
    sites = analyze_app([ROOT / "examples/ticketing/app_service.py"])
    types = sorted({s.event_type for s in sites})
    assert types == ["ticket.closed", "ticket.priority", "ticket.reply", "ticket.status"]
    (status_site,) = [s for s in sites if s.function == "TicketService._status"]
    assert SPLAT in status_site.payload_fields          # {"status": status, **payload}
    assert status_site.binding_keys == ["ticket_id"]


def test_local_rename_inside_the_emit_path_is_absorbed(tmp_path):
    changed = BASE.replace("label = helper_value(name)", "tag = helper_value(name)") \
                  .replace("if label:", "if tag:")
    assert set(_statuses(_classify(tmp_path, BASE, changed)).values()) == {APP_UNCHANGED}


def test_guard_change_in_a_caller_is_a_behavior_risk_naming_the_function(tmp_path):
    changed = BASE.replace("if label:", "if label and len(label) > 3:")
    by_id = {c.site_id: c for c in _classify(tmp_path, BASE, changed)}
    risk = by_id["jobs_app.JobService._status#1"]
    assert risk.status == BEHAVIOR_RISK
    assert "jobs_app.JobService.start" in risk.detail
    # precision check: the DONE emission in finish() does not depend on start's
    # guard -- start is not in finish's slice -- so that site stays silent
    assert by_id["jobs_app.JobService.finish#1"].status == APP_UNCHANGED


def test_helper_logic_change_two_calls_deep_is_a_behavior_risk(tmp_path):
    changed = BASE.replace("return name.strip()", "return name.strip().lower()")
    by_id = _statuses(_classify(tmp_path, BASE, changed))
    assert by_id["jobs_app.JobService._status#1"] == BEHAVIOR_RISK


def test_payload_key_rename_is_an_interface_break(tmp_path):
    changed = BASE.replace('{"status": status}', '{"state": status}')
    by_id = {c.site_id: c for c in _classify(tmp_path, BASE, changed)}
    broken = by_id["jobs_app.JobService._status#1"]
    assert broken.status == INTERFACE_BREAK
    assert "'status'" in broken.detail and "'state'" in broken.detail


def test_event_type_constant_change_is_an_interface_break(tmp_path):
    changed = BASE.replace('STATUS = "job.status"', 'STATUS = "job.state"')
    by_id = _statuses(_classify(tmp_path, BASE, changed))
    assert by_id["jobs_app.JobService._status#1"] == INTERFACE_BREAK


def test_deleted_and_added_emit_sites_are_reported(tmp_path):
    removed = BASE.replace(
        '        self._emit(Event(DONE, self._clock() + 1e-3, {"job_id": job_id}, {}, "jobs"))\n',
        "")
    statuses = _statuses(_classify(tmp_path, BASE, removed))
    assert statuses["jobs_app.JobService.finish#1"] == APP_REMOVED
    assert _statuses(_classify(tmp_path, removed, BASE))[
        "jobs_app.JobService.finish#1"] == APP_ADDED


def test_class_rename_is_absorbed_silently(tmp_path):
    changed = BASE.replace("class JobService:", "class JobRunner:")
    changes = _statuses(_classify(tmp_path, BASE, changed))
    assert changes["jobs_app.JobRunner._status#1"] == APP_RENAMED
    assert changes["jobs_app.JobRunner.finish#1"] == APP_RENAMED
    assert APP_REMOVED not in changes.values()


def test_emit_path_function_rename_flags_conservatively_as_risk_not_removal(tmp_path):
    # callable identity is emission-order contract (see _AppAlpha), so this
    # cannot be proven representational -- it must flag, but as a risk to
    # glance at, never as a removal-level break
    changed = BASE.replace("def _status(", "def _transition(") \
                  .replace("self._status(", "self._transition(")
    changes = _statuses(_classify(tmp_path, BASE, changed))
    assert changes["jobs_app.JobService._transition#1"] == BEHAVIOR_RISK
    assert APP_REMOVED not in changes.values()
    assert INTERFACE_BREAK not in changes.values()


def test_reordering_two_emissions_flags(tmp_path):
    # emission order is contract (a "before" policy hangs on it); occurrence-
    # order alpha canonicalization must NOT absorb a swap of two calls
    changed = BASE.replace(
        """        self._status(job_id, "finished")
        self._emit(Event(DONE, self._clock() + 1e-3, {"job_id": job_id}, {}, "jobs"))""",
        """        self._emit(Event(DONE, self._clock() + 1e-3, {"job_id": job_id}, {}, "jobs"))
        self._status(job_id, "finished")""")
    statuses = _statuses(_classify(tmp_path, BASE, changed))
    assert statuses["jobs_app.JobService._status#1"] == BEHAVIOR_RISK


def test_change_outside_every_slice_stays_silent(tmp_path):
    changed = BASE.replace('return f"{count} jobs"', 'return f"jobs: {count}"')
    assert set(_statuses(_classify(tmp_path, BASE, changed)).values()) == {APP_UNCHANGED}


def test_dynamic_event_type_is_a_declared_marker_not_a_guess(tmp_path):
    source = BASE.replace("Event(STATUS,", "Event(compute_type(),")
    (site,) = [s for s in _analyze(tmp_path, source)
               if s.function == "JobService._status"]
    assert site.event_type == DYNAMIC


def test_affected_steps_scope_by_event_type():
    class _Sig:
        def __init__(self, event_type):
            self.event_type = event_type

    class _Entry:
        def __init__(self, step_id, event_type):
            self.step_id, self.signature = step_id, _Sig(event_type)

    site = EmitSite(site_id="m.f#1", module="m", function="f", event_type="job.status",
                    binding_keys=[], payload_fields=[], slice_functions={},
                    slice_fingerprint="x")
    entries = [_Entry("jobs.status.is", "job.status"), _Entry("other", "job.done")]
    assert affected_step_ids(site, entries) == ["jobs.status.is"]


def test_sites_round_trip_through_dicts(tmp_path):
    sites = _analyze(tmp_path, BASE)
    assert [EmitSite.from_dict(s.to_dict()) for s in sites] == sites


def test_docstring_edit_on_an_emit_path_is_absorbed(tmp_path):
    # found on real history: a docstring-only commit flagged as behavior-risk
    # before docstrings were stripped from the normalization
    changed = BASE.replace(
        'def start(self, job_id, name):',
        'def start(self, job_id, name):\n        """Queue the job for work."""')
    assert set(_statuses(_classify(tmp_path, BASE, changed)).values()) == {APP_UNCHANGED}
