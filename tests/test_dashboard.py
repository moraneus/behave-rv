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
