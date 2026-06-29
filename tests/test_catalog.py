"""Phase 2: RV step decorators + catalog with signatures, persisted and reloaded.

The signature starts simple: event type, referenced fields (derived from the
phrasing placeholders), and correlation key. Human policies bind to ``step_id``,
not phrasing, so the id is author-assigned and stable across renames.
"""

import pytest

from behave_rv.catalog.entry import CatalogEntry, StepSignature
from behave_rv.catalog.registry import StepRegistry
from behave_rv.catalog.store import load_catalog, save_catalog


@pytest.fixture
def reg():
    return StepRegistry()


# --- decorators register catalog entries -----------------------------------


def test_trigger_registers_a_catalog_entry(reg):
    @reg.trigger('an order is "{status}"', step_id="order.is",
                 event_type="order.status", correlation_key="order_id")
    def _t(ctx, event, status):
        return event.payload.get("status") == status

    (entry,) = reg.entries()
    assert entry.step_id == "order.is"
    assert entry.kind == "trigger"
    assert entry.phrasing == 'an order is "{status}"'


def test_scope_and_obligation_record_their_kind(reg):
    @reg.scope('after an order is "{status}"', step_id="order.after",
               event_type="order.status", correlation_key="order_id")
    def _s(ctx, event, status):
        return True

    @reg.obligation('it must have been "{status}" before', step_id="order.was",
                    event_type="order.status", correlation_key="order_id")
    def _o(ctx, event, status):
        return True

    kinds = {e.step_id: e.kind for e in reg.entries()}
    assert kinds == {"order.after": "scope", "order.was": "obligation"}


def test_decorated_step_remains_a_callable_predicate(reg):
    @reg.trigger('an order is "{status}"', step_id="order.is",
                 event_type="order.status", correlation_key="order_id")
    def _t(ctx, event, status):
        return event.payload.get("status") == status

    class FakeEvent:
        payload = {"status": "cancelled"}

    assert _t(None, FakeEvent(), "cancelled") is True
    assert _t(None, FakeEvent(), "placed") is False


def test_step_id_is_never_reused(reg):
    @reg.trigger("a", step_id="dup", event_type="e", correlation_key="k")
    def _a(ctx, event):
        return True

    with pytest.raises(ValueError, match="dup"):
        @reg.trigger("b", step_id="dup", event_type="e", correlation_key="k")
        def _b(ctx, event):
            return True


# --- signature derivation --------------------------------------------------


def test_signature_derives_referenced_fields_from_placeholders(reg):
    @reg.trigger('an order is "{status}"', step_id="order.is",
                 event_type="order.status", correlation_key="order_id")
    def _t(ctx, event, status):
        return True

    sig = reg.get("order.is").signature
    assert sig.event_type == "order.status"
    assert sig.referenced_fields == {"status"}
    assert sig.correlation_key == ("order_id",)


def test_signature_handles_typed_and_multiple_placeholders(reg):
    @reg.trigger("paid {amount:d} for {item}", step_id="pay",
                 event_type="payment", correlation_key="order_id")
    def _t(ctx, event, amount, item):
        return True

    assert reg.get("pay").signature.referenced_fields == {"amount", "item"}


def test_composite_correlation_key_is_a_tuple(reg):
    @reg.trigger("x", step_id="c", event_type="e",
                 correlation_key=("tenant_id", "order_id"))
    def _t(ctx, event):
        return True

    assert reg.get("c").signature.correlation_key == ("tenant_id", "order_id")


# --- persistence -----------------------------------------------------------


def test_catalog_entry_dict_round_trip():
    entry = CatalogEntry(
        step_id="order.is",
        phrasing='an order is "{status}"',
        kind="trigger",
        signature=StepSignature(
            event_type="order.status",
            trigger_condition='an order is "{status}"',
            payload_fields={},
            referenced_fields={"status"},
            correlation_key=("order_id",),
        ),
        provenance="llm",
        observed=False,
        version=1,
    )
    assert CatalogEntry.from_dict(entry.to_dict()) == entry


def test_module_level_decorators_register_into_the_default_registry():
    from behave_rv.steps import default_registry, trigger

    @trigger("module level {x}", step_id="mod.lvl",
             event_type="e", correlation_key="k")
    def _t(ctx, event, x):
        return True

    entry = default_registry.get("mod.lvl")
    assert entry.kind == "trigger"
    assert entry.signature.referenced_fields == {"x"}


def test_catalog_save_and_reload_preserves_entries(reg, tmp_path):
    @reg.trigger('an order is "{status}"', step_id="order.is",
                 event_type="order.status", correlation_key="order_id")
    def _t(ctx, event, status):
        return True

    @reg.obligation('it must have been "{status}" before', step_id="order.was",
                    event_type="order.status", correlation_key="order_id")
    def _o(ctx, event, status):
        return True

    path = tmp_path / "catalog.json"
    save_catalog(path, reg.entries())

    assert load_catalog(path) == reg.entries()
