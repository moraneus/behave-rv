"""The built-in dashboard: a sink + tap + stdlib HTTP server, readable while
the app runs."""

import json
import urllib.request

from behave_rv.compile.compiler import compile_feature
from behave_rv.dashboard import Dashboard
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource

from tests.test_mutation_gaps import BEFORE_POLICY, basic_registry, ev


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode()


def test_dashboard_records_verdicts_and_serves_state():
    policies = compile_feature(BEFORE_POLICY, basic_registry())
    dashboard = Dashboard(policies)
    url = dashboard.start(port=0)
    try:
        src = InProcessSource()
        for e in [ev(1.0, "authorized", key="A"), ev(2.0, "paid", key="A"),
                  ev(3.0, "paid", key="B")]:
            src.emit(dashboard.tap(e))
        Engine(policies, grace=0).run(src, sink=dashboard.sink)

        status, body = _get(url + "/api/state")
        assert status == 200
        state = json.loads(body)
        assert state["counts"] == {"events": 3, "verdicts": 2, "violations": 1}
        (policy,) = state["policies"]
        assert policy["policy"] == "paid after authorized"
        cells = {c["entity"]: c["verdict"] for c in policy["cells"]}
        assert cells == {"order_id=A": "satisfied", "order_id=B": "violated"}
        (violation,) = state["violations"]
        assert "✗" in violation["explanation"]
        assert 'an order is "authorized"' in violation["explanation"]
        assert len(state["events"]) == 3

        status, page = _get(url + "/")
        assert status == 200 and "behave_rv live monitor" in page
    finally:
        dashboard.stop()


def test_dashboard_forwards_to_a_chained_sink_and_handles_handbuilt_policies():
    from behave_rv.compile.automaton import never as make_never

    hand_built = make_never("no-bad", correlation_key="order_id",
                            event_types={"order.status"},
                            bad=lambda e: e.payload.get("status") == "bad")
    received = []
    dashboard = Dashboard([hand_built], forward=received.append)
    src = InProcessSource()
    src.emit(Event("order.status", 1.0, {"order_id": "X"}, {"status": "bad"}, "t"))
    Engine([hand_built], grace=0).run(src, sink=dashboard.sink)

    assert [v.verdict for v in received] == ["violated"]     # the chain worked
    state = dashboard.state()
    (violation,) = state["violations"]
    assert violation["explanation"] is None                  # no authored scenario: graceful


def test_dashboard_unknown_path_is_404():
    dashboard = Dashboard([])
    url = dashboard.start(port=0)
    try:
        import urllib.error
        try:
            urllib.request.urlopen(url + "/nope", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        dashboard.stop()


def test_dashboard_stability_strip_reports_in_sync(tmp_path):
    from behave_rv.catalog.store import save_catalog

    registry = basic_registry()
    policies = compile_feature(BEFORE_POLICY, registry)
    catalog = tmp_path / "catalog.json"
    save_catalog(catalog, registry.entries())

    dashboard = Dashboard(policies, registry=registry, catalog=catalog)
    stability = dashboard.state()["stability"]
    assert stability["status"] == "ok"
    assert stability["breaks"] == []
    assert stability["statuses"] == {"order.status.is": "unchanged"}


def test_dashboard_stability_strip_names_silently_broken_policies(tmp_path):
    from behave_rv.catalog.registry import StepRegistry
    from behave_rv.catalog.store import save_catalog

    baseline = basic_registry()
    catalog = tmp_path / "catalog.json"
    save_catalog(catalog, baseline.entries())

    # the code changed: the step now reads a renamed payload field
    changed = StepRegistry()

    @changed.trigger('an order is "{status}"', step_id="order.status.is",
                     event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("state") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    policies = compile_feature(BEFORE_POLICY, changed)
    dashboard = Dashboard(policies, registry=changed, catalog=catalog)
    stability = dashboard.state()["stability"]
    assert stability["status"] == "breaks"
    assert stability["breaks"][0]["policy"] == "paid after authorized"
    assert "'state': 'any'" in stability["breaks"][0]["detail"]


def test_dashboard_flags_policies_with_no_matching_events():
    policies = compile_feature(BEFORE_POLICY, basic_registry())
    dashboard = Dashboard(policies)
    # events flow, but never the policy's event type: the runtime smell of a
    # policy disconnected from the stream
    dashboard.tap(Event("something.else", 1.0, {"order_id": "A"}, {}, "t"))
    (policy,) = dashboard.state()["policies"]
    assert policy["unobserved"] is True
    # one matching event clears the warning
    dashboard.tap(ev(2.0, "paid"))
    (policy,) = dashboard.state()["policies"]
    assert policy["unobserved"] is False


APP_FOR_DASHBOARD = '''
from behave_rv.events.event import Event

STATUS = "order.status"


class OrderService:
    def __init__(self, emit, clock):
        self._emit = emit
        self._clock = clock

    def set_status(self, order_id, status):
        self._emit(Event(STATUS, self._clock(), {"order_id": order_id},
                         {"status": status}, "orders"))

    def pay(self, order_id, amount):
        if amount > 0:
            self.set_status(order_id, "paid")
'''


def _two_sided_workspace(tmp_path):
    from behave_rv.catalog.app_surface import analyze_app
    from behave_rv.catalog.store import save_catalog

    registry = basic_registry()
    policies = compile_feature(BEFORE_POLICY, registry)
    app = tmp_path / "app.py"
    app.write_text(APP_FOR_DASHBOARD)
    catalog = tmp_path / "catalog.json"
    save_catalog(catalog, registry.entries(), app_surface=analyze_app([app]))
    return registry, policies, app, catalog


def test_dashboard_strip_covers_both_sides_when_in_sync(tmp_path):
    registry, policies, app, catalog = _two_sided_workspace(tmp_path)
    dashboard = Dashboard(policies, registry=registry, catalog=catalog, app=[app])
    stability = dashboard.state()["stability"]
    assert stability["status"] == "ok"
    assert stability["app"]["checked"] is True
    assert set(stability["app"]["statuses"].values()) == {"unchanged"}


def test_dashboard_strip_shows_a_core_code_change_as_an_app_risk(tmp_path):
    registry, policies, app, catalog = _two_sided_workspace(tmp_path)
    # the core-code change: the guard before the payment emission moves
    app.write_text(APP_FOR_DASHBOARD.replace("if amount > 0:", "if amount > 10:"))
    dashboard = Dashboard(policies, registry=registry, catalog=catalog, app=[app])
    stability = dashboard.state()["stability"]
    assert stability["status"] == "risks"
    (risk,) = stability["app"]["risks"]
    assert "OrderService.pay" in risk["detail"]              # names the function
    assert "paid after authorized" in risk["policies"]       # names the policy


def test_dashboard_strip_shows_an_app_interface_break_as_a_break(tmp_path):
    registry, policies, app, catalog = _two_sided_workspace(tmp_path)
    app.write_text(APP_FOR_DASHBOARD.replace('{"status": status}', '{"state": status}'))
    dashboard = Dashboard(policies, registry=registry, catalog=catalog, app=[app])
    stability = dashboard.state()["stability"]
    assert stability["status"] == "breaks"
    (broken,) = stability["app"]["breaks"]
    assert "'status'" in broken["detail"] and "'state'" in broken["detail"]


def test_dashboard_strip_hints_when_the_app_side_is_not_enabled(tmp_path):
    from behave_rv.catalog.store import save_catalog

    registry = basic_registry()
    policies = compile_feature(BEFORE_POLICY, registry)
    catalog = tmp_path / "catalog.json"
    save_catalog(catalog, registry.entries())                # no app_surface
    app = tmp_path / "app.py"
    app.write_text(APP_FOR_DASHBOARD)
    dashboard = Dashboard(policies, registry=registry, catalog=catalog, app=[app])
    stability = dashboard.state()["stability"]
    assert stability["status"] == "ok"                       # never a false alarm
    assert stability["app"]["checked"] is False
    assert "catalog save --app" in stability["app"]["detail"]
