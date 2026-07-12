"""The stability panel's backend calls: real mechanism, asserted outcomes."""

from demo.order_service.stability import MARQUEE, apply_change


def test_rename_function_is_absorbed_and_the_violation_still_caught():
    result = apply_change("rename_function")
    assert result["diff_statuses"] == {"order.status.is": "unchanged"}
    assert result["breaks"] == []
    assert result["liveness"] == []
    assert result["verdict_flips"] == {}                 # nothing changed behavior
    assert result["marquee"]["before"] == "violated"
    assert result["marquee"]["after"] == "violated"      # still caught


def test_rename_field_breaks_scoped_with_the_contract_diff():
    result = apply_change("rename_field")
    assert result["diff_statuses"] == {"order.status.is": "changed"}
    assert len(result["breaks"]) == 11                   # every policy uses the step
    assert {b["policy"] for b in result["breaks"]} >= {MARQUEE}
    assert "'state': 'any'" in result["breaks"][0]["detail"]
    # and the seeded fault is no longer caught -- which is why the break matters
    assert result["marquee"] == {"policy": MARQUEE,
                                 "before": "violated", "after": "pending"}


def test_rename_value_shows_the_silent_failure_and_the_liveness_alarm():
    result = apply_change("rename_value")
    assert result["breaks"] == []                        # the diff is HONESTLY silent
    assert result["marquee"]["before"] == "violated"
    assert result["marquee"]["after"] == "pending"       # the policy went quiet...
    assert result["liveness"]                            # ...and liveness speaks
    assert any("status='paid'" in m for m in result["liveness"])


def test_helper_change_is_now_detected_by_the_call_graph_fingerprint():
    result = apply_change("helper_change")
    assert result["diff_statuses"] == {"order.status.is": "changed"}
    assert len(result["breaks"]) == 11
    assert any("helper" in b["detail"] for b in result["breaks"])
    assert result["marquee"]["before"] == "violated"
    assert result["marquee"]["after"] == "pending"       # dormant -- which is
    #                                     exactly why the break matters


def test_helper_via_value_is_the_honest_boundary():
    result = apply_change("helper_via_value")
    assert result["diff_statuses"] == {"order.status.is": "unchanged"}
    assert result["breaks"] == [] and result["liveness"] == []
    assert result["marquee"]["before"] == "violated"
    assert result["marquee"]["after"] == "pending"       # dormant, nobody spoke
    assert "_check" in result["unresolved_calls"]        # ...but the boundary shows
    assert "residual boundary" in result["narrative"]
