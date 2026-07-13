"""The measured stability table, asserted: this is the committed form of the
evidence for the central claim. Families A and B must be fully correct, the
liveness disconnects C1-C3 must be caught, C4 is asserted AS the documented
miss (the boundary stated, not hidden), and the conservative false-alarm rate
is recorded."""

import subprocess
import sys

from tests.stability_catalog import CASES, run_catalog


def _by_id():
    return {o.case.case_id: o for o in run_catalog()}


OUTCOMES = _by_id()      # once per session; a few seconds for all 22 cases


def test_every_declared_ground_truth_matches_the_replayed_one():
    for outcome in OUTCOMES.values():
        assert not outcome.notes, f"{outcome.case.case_id}: {outcome.notes}"


def test_family_a_absorbs_are_silent():
    # A5 (helper renamed, body identical) and A6 (helper definitions
    # reordered) joined with the call-graph fingerprint: names and definition
    # order are not hashed, only body identities
    for cid in ("A1", "A2", "A3", "A4", "A5", "A6"):
        assert OUTCOMES[cid].classification == "CORRECT (silent)", cid


def test_family_b_breaks_are_caught_and_scoped():
    for cid in ("B1", "B2", "B3", "B4", "B5", "B6", "B7"):
        assert OUTCOMES[cid].classification == "CORRECT (diff)", cid
    # the scoping proof: two steps share the event type, ONE policy notified
    b6 = OUTCOMES["B6"]
    assert [b.policy_id for b in b6.diff_breaks] == ["no oversized order"]
    assert all(b.step_id == "order.amount.exceeds" for b in b6.diff_breaks)


def test_family_c_disconnects_are_caught_by_liveness():
    for cid in ("C1", "C2", "C3"):
        outcome = OUTCOMES[cid]
        assert outcome.classification == "CORRECT (liveness)", cid
        assert outcome.diff_breaks == []       # and the diff is HONESTLY silent


def test_c4_helper_changes_are_now_detected_by_the_call_graph_fingerprint():
    # This test previously asserted C4 as the documented MISS, xfail-style,
    # with the promise that a mechanism catching it must update docs and test
    # together. That happened: the interprocedural fingerprint
    # (see STABILITY.md) resolves same-module and
    # same-package calls, so the helper's body change moves the caller's
    # fingerprint and the diff breaks. The new documented boundary is C4b
    # (calls through values), asserted below.
    c4 = OUTCOMES["C4"]
    assert c4.behavior_changed is True
    assert c4.classification == "CORRECT (diff)"
    assert c4.diff_breaks
    assert any("helper" in b.detail for b in c4.diff_breaks)


def test_family_d_false_alarm_rate_is_recorded():
    false_alarms = [cid for cid in ("D1", "D2", "D3", "D4")
                    if OUTCOMES[cid].classification == "FALSE ALARM"]
    # conservative by design: all four structural refactors (D4: splitting a
    # helper changes the reachable set) trip the fingerprint; the rate is
    # stated in STABILITY.md, not hidden
    assert false_alarms == ["D1", "D2", "D3", "D4"]
    for cid in false_alarms:
        assert OUTCOMES[cid].behavior_changed is False


def test_c4b_is_the_new_documented_boundary():
    # xfail-style, the same protocol C4 carried: this asserts the MISS. A
    # mechanism that starts resolving calls through values must update the
    # docs' boundary statement alongside this test.
    c4b = OUTCOMES["C4b"]
    assert c4b.behavior_changed is True
    assert c4b.classification == "MISS (documented)"
    assert c4b.diff_breaks == [] and c4b.liveness == []
    # ...and the weaker protection is VISIBLE: the signature records the
    # call site static resolution could not follow
    from tests.stability_catalog import registry_c4b_baseline
    signature = registry_c4b_baseline().entries()[0].signature
    assert "_check" in signature.unresolved_calls


def test_the_catalog_covers_every_declared_case():
    assert {c.case_id for c in CASES} == set(OUTCOMES)
    assert len(CASES) == 22


def test_catalog_save_is_stable_across_processes(tmp_path):
    """The committed artifact must not depend on process state (hash seeds,
    dict order): two independent processes write byte-identical catalogs."""
    script = (
        "from behave_rv.catalog.store import save_catalog\n"
        "from demo.order_service.steps import build_registry\n"
        "import sys\n"
        "save_catalog(sys.argv[1], build_registry().entries())\n"
    )
    for name in ("one.json", "two.json"):
        subprocess.run([sys.executable, "-c", script, str(tmp_path / name)],
                       check=True, cwd=".")
    assert (tmp_path / "one.json").read_bytes() == (tmp_path / "two.json").read_bytes()
