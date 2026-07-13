"""The committed example projects run exactly as the guide says they do."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*argv):
    return subprocess.run([sys.executable, *argv], capture_output=True,
                          text=True, cwd=ROOT)


def test_quickstart_runs_with_the_documented_output():
    result = _run("examples/quickstart.py")
    assert result.returncode == 0
    assert "{'order_id': 'A-1'} satisfied" in result.stdout
    assert "{'order_id': 'B-7'} violated" in result.stdout
    assert "✗ Then an order is \"authorized\" before" in result.stdout


def test_ticketing_replay_check_finds_exactly_the_seeded_faults():
    result = _run("examples/ticketing/replay_check.py")
    assert result.returncode == 1                      # violations gate the exit code
    assert "41 verdicts, 5 violation(s)" in result.stdout
    # the healthy tickets stay fully green
    assert "violated   T-1" not in result.stdout
    assert "violated   T-4" not in result.stdout
    # every one of the five steps is exercised by at least one verdict
    for policy in ("a ticket may only be resolved after it was assigned",
                   "an opened ticket is assigned within the window",
                   "an escalated ticket must not be closed until resolved",
                   "every ticket is eventually resolved",
                   "a customer reply is answered within the window",
                   "the on-call agent only receives urgent tickets"):
        assert policy in result.stdout


def test_ticketing_committed_catalog_matches_the_code():
    # the example models the real convention: catalog.json is committed and
    # CI diffs it -- so the example's own catalog must never drift
    result = _run("-m", "behave_rv", "catalog", "diff",
                  "--steps", "examples/ticketing/monitoring/steps.py",
                  "--catalog", "examples/ticketing/monitoring/catalog.json",
                  "--policies", "examples/ticketing/monitoring/policies")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: no breaks" in result.stdout
