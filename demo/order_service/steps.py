"""The monitorable surface (taps) for the order service demo."""

from pathlib import Path

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature

POLICY_DIR = Path(__file__).parent / "policies"


def build_registry() -> StepRegistry:
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry



def load_policies(registry: StepRegistry):
    policies = []
    for path in sorted(POLICY_DIR.glob("*.feature")):
        policies.extend(compile_feature(path.read_text(), registry))
    return policies
