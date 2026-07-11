"""The monitorable surface (taps) for the session service demo."""

from pathlib import Path

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature

POLICY_DIR = Path(__file__).parent / "policies"


def build_registry() -> StepRegistry:
    registry = StepRegistry()

    @registry.trigger('a user is "{status}"', step_id="user.status.is",
                      event_type="session.status", correlation_key="user_id")
    def user_is(ctx, event, status):
        if event.type == "session.status" and event.payload.get("status") == status:
            ctx.bind(user_id=event.bindings["user_id"])
            return True
        return False

    return registry



def load_policies(registry: StepRegistry):
    policies = []
    for path in sorted(POLICY_DIR.glob("*.feature")):
        policies.extend(compile_feature(path.read_text(), registry))
    return policies
