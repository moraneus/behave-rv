"""The monitorable surface: the vocabulary policies are written in.

Conventions this file demonstrates (copy them):

* One steps module per monitored domain, next to its policies/ directory.
* ``build_registry()`` is a side-effect-free factory returning a fresh
  registry -- tests and tools get isolation for free, and the CLI
  (``python -m behave_rv ...``) detects and uses it automatically when the
  module registers nothing at import time.
* ``step_id`` naming: ``<domain>.<event>.<what>`` -- stable forever, never
  reused. It is the identity policies bind to across renames.
* The phrasing's ``{status}`` placeholder binds BY NAME to the third
  parameter, so that parameter must be called ``status`` -- it is contract
  (renaming it disconnects every policy; the catalog diff will say so).
* The predicate is pure: read the event, return a boolean, change nothing.
"""

from pathlib import Path

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature

POLICY_DIR = Path(__file__).parent / "policies"


def build_registry() -> StepRegistry:
    registry = StepRegistry()

    @registry.trigger('a ticket is "{status}"', step_id="ticket.status.is",
                      event_type="ticket.status", correlation_key="ticket_id")
    def ticket_is(ctx, event, status):
        if event.type == "ticket.status" and event.payload.get("status") == status:
            ctx.bind(ticket_id=event.bindings["ticket_id"])
            return True
        return False

    return registry


def load_policies(registry: StepRegistry):
    """Compile every .feature in policies/, one file per policy (convention:
    numbered file names keep the ladder readable and diffs stable)."""
    policies = []
    for path in sorted(POLICY_DIR.glob("*.feature")):
        policies.extend(compile_feature(path.read_text(), registry))
    return policies
