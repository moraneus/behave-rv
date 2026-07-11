"""The monitorable surface (taps) for the todo app demo.

Two triggers, one per entity type: tasks are keyed by task_id, the background
sync channel by session_id. Each policy uses exactly one key, so both live in
one registry without ever crossing.
"""

from pathlib import Path

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature

POLICY_DIR = Path(__file__).parent / "policies"


def build_registry() -> StepRegistry:
    registry = StepRegistry()

    @registry.trigger('a task is "{status}"', step_id="task.status.is",
                      event_type="task.status", correlation_key="task_id")
    def task_is(ctx, event, status):
        if event.type == "task.status" and event.payload.get("status") == status:
            ctx.bind(task_id=event.bindings["task_id"])
            return True
        return False

    @registry.trigger('a sync is "{status}"', step_id="sync.status.is",
                      event_type="sync.status", correlation_key="session_id")
    def sync_is(ctx, event, status):
        if event.type == "sync.status" and event.payload.get("status") == status:
            ctx.bind(session_id=event.bindings["session_id"])
            return True
        return False

    return registry



def load_policies(registry: StepRegistry):
    policies = []
    for path in sorted(POLICY_DIR.glob("*.feature")):
        policies.extend(compile_feature(path.read_text(), registry))
    return policies
