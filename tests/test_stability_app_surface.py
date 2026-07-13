"""The E-series app-change catalog, asserted: every declared expectation holds,
no stream change is ever missed, and every false alarm is a declared one."""

import pytest

from behave_rv.catalog.app_surface import APP_ADDED
from tests.stability_app_surface import APP_BASELINE, CASES, change_statuses, run_catalog


@pytest.fixture(scope="module")
def rows():
    return {r.case_id: r for r in run_catalog()}


def test_the_catalog_covers_all_seventeen_cases(rows):
    assert sorted(rows, key=lambda c: int(c[1:])) == [f"E{n}" for n in range(1, 18)]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_each_case_is_detected_at_its_declared_level(case, rows):
    assert rows[case.case_id].detected == case.expect


def test_no_stream_change_is_missed(rows):
    assert [r.case_id for r in rows.values() if r.outcome == "MISS"] == []


def test_every_false_alarm_is_a_declared_by_design_conservatism(rows):
    for row in rows.values():
        if row.outcome == "FALSE ALARM":
            assert row.by_design, f"{row.case_id}: undeclared false alarm"


def test_the_by_design_family_is_exactly_the_declared_one(rows):
    assert [r.case_id for r in rows.values() if r.by_design] == \
        ["E13", "E14", "E15", "E17"]


def test_e10_also_surfaces_the_new_site_as_an_addition():
    # the dominant signal is the risk on the edited method, but the new emit
    # site itself must ALSO appear as an addition (the suggestion channel)
    (e10,) = [c for c in CASES if c.case_id == "E10"]
    assert APP_ADDED in change_statuses(APP_BASELINE, e10.transform(APP_BASELINE))


def test_e10_demonstrates_the_layering_stream_changed_verdicts_same(rows):
    assert rows["E10"].stream_changed and not rows["E10"].verdicts_changed
