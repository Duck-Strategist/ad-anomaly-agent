"""相对时间换算（SPEC Q19）。纯函数测试，不需要数据库。"""

from datetime import date

import pytest
from pydantic import ValidationError

from adagent.nl2mql import build_system_prompt
from adagent.semantic.catalog import load_catalog
from adagent.semantic.mql import (MQL, OutOfRange, TimeSpec, calendar_window, check_data_range, coverage_notes,
                                  resolve_window, time_rules)
from adagent.workflow import ROUTE_PROMPT

SUN = date(2026, 11, 8)   # 数据里的"今天"，周日


@pytest.mark.parametrize("unit, offset, expected", [
    ("day", 0, ("2026-11-08", "2026-11-08")),
    ("day", -1, ("2026-11-07", "2026-11-07")),
    ("week", 0, ("2026-11-02", "2026-11-08")),
    ("week", -1, ("2026-10-26", "2026-11-01")),
    ("week", -2, ("2026-10-19", "2026-10-25")),
    ("week", -3, ("2026-10-12", "2026-10-18")),
    ("month", 0, ("2026-11-01", "2026-11-08")),     # 本月到今天为止
    ("month", -1, ("2026-10-01", "2026-10-31")),
    ("month", -2, ("2026-09-01", "2026-09-30")),
    ("month", -3, ("2026-08-01", "2026-08-31")),
])
def test_calendar_window_on_sunday(unit, offset, expected):
    s, e = calendar_window(unit, offset, SUN)
    assert (s.isoformat(), e.isoformat()) == expected


def test_calendar_window_mid_week_and_year_boundary():
    wed = date(2026, 11, 4)
    assert calendar_window("week", 0, wed) == (date(2026, 11, 2), wed)          # 本周只到今天
    jan = date(2027, 1, 15)
    assert calendar_window("month", -1, jan) == (date(2026, 12, 1), date(2026, 12, 31))
    assert calendar_window("month", -2, jan) == (date(2026, 11, 1), date(2026, 11, 30))
    assert calendar_window("month", -1, date(2026, 3, 31)) == (date(2026, 2, 1), date(2026, 2, 28))


def test_calendar_agrees_with_existing_week_ranges():
    for name, offset in (("this_week", 0), ("last_week", -1)):
        assert resolve_window(TimeSpec(range=name), SUN) == calendar_window("week", offset, SUN)


@pytest.mark.parametrize("spec", [
    {"range": "calendar", "unit": "week"},                       # 缺 offset
    {"range": "calendar", "offset": -1},                         # 缺 unit
    {"range": "calendar", "unit": "week", "offset": 1},          # 不能指向未来
    {"range": "calendar", "unit": "week", "offset": -13},        # 超出上限
    {"range": "calendar", "unit": "year", "offset": -1},         # 不支持的单位
    {"range": "last_week", "unit": "week", "offset": -1},        # unit/offset 只属于 calendar
    {"range": "calendar", "unit": "week", "offset": -1, "start": "2026-10-01", "end": "2026-10-02"},
])
def test_invalid_calendar_specs_rejected(spec):
    with pytest.raises(ValidationError):
        TimeSpec.model_validate(spec)


def _mql(time: dict) -> MQL:
    return MQL.model_validate({"metrics": ["ad_spend"], "time": time})


def test_out_of_range_whole_window_rejected():
    cat = load_catalog()
    with pytest.raises(OutOfRange):
        check_data_range(_mql({"range": "custom", "start": "2025-10-01", "end": "2025-10-31"}), cat)
    with pytest.raises(OutOfRange):   # 本期有数据，但对比的上一期整段在数据之前
        check_data_range(_mql({"range": "custom", "start": "2026-08-11", "end": "2026-08-20",
                               "compare": "previous_period"}), cat)


def test_partially_covered_window_allowed_and_disclosed():
    cat = load_catalog()
    m = _mql({"range": "calendar", "unit": "month", "offset": -3})   # 8 月，数据从 8-11 开始
    check_data_range(m, cat)                                          # 不报错
    s, e = resolve_window(m.time, cat.today)
    [note] = coverage_notes(s, e, cat.data_start)
    assert "2026-08-01 ~ 2026-08-31" in note and "实际只覆盖 2026-08-11 ~ 2026-08-31" in note
    assert coverage_notes(date(2026, 10, 19), date(2026, 10, 25), cat.data_start) == ["统计区间：2026-10-19 ~ 2026-10-25"]


def test_prompts_share_one_time_rules_source():
    cat = load_catalog()
    rules = time_rules(cat.today)
    assert "周日" in rules                      # 星期由代码算，不再写死
    assert rules in build_system_prompt(cat)
    assert rules in ROUTE_PROMPT.format(time_rules=rules)
