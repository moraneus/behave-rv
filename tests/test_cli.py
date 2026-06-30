"""The CLI driver: run a .feature policy over a recorded trace, no Python policy
construction in the path. Exercises the example files end to end.
"""

from behave_rv.__main__ import main

STEPS = "examples/order_steps.py"
TRACE = "examples/order_trace.jsonl"


def test_cli_runs_a_feature_policy_over_a_recorded_trace(capsys):
    rc = main(["--steps", STEPS, "--policy", "examples/order_authorized.feature",
               "--trace", TRACE])
    out = capsys.readouterr().out

    assert rc == 0
    assert '"order_id": "A"' in out and '"verdict": "satisfied"' in out
    assert '"order_id": "B"' in out and '"verdict": "violated"' in out
    assert '"order_id": "C"' in out and '"verdict": "pending"' in out
    assert "✗ Then" in out  # the violation rendered as the authored scenario


def test_cli_rephrasing_runs_identically(capsys):
    rc = main(["--steps", STEPS, "--policy", "examples/order_authorized_reworded.feature",
               "--trace", TRACE])
    out = capsys.readouterr().out

    assert rc == 0
    assert '"order_id": "B"' in out and '"verdict": "violated"' in out


def test_cli_refuses_a_cross_entity_policy(capsys):
    rc = main(["--steps", STEPS, "--policy", "examples/cross_entity.feature",
               "--trace", TRACE])

    assert rc == 2
    assert "more than one entity key" in capsys.readouterr().err
