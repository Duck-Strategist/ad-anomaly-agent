"""两段时间对比（SPEC Q20）。纯函数测试，不需要数据库。"""

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from adagent.semantic.catalog import load_catalog
from adagent.semantic.mql import (MQL, MQLError, TimeSpec, compare_window, previous_window, resolve_window,
                                  validate_against_catalog)

SUN = date(2026, 11, 8)
QUESTIONS = Path(__file__).resolve().parent.parent / "eval" / "questions"


def _cw(time: dict, today: date = SUN) -> tuple[str, str]:
    s, e = compare_window(TimeSpec.model_validate(time), today)
    return s.isoformat(), e.isoformat()


@pytest.mark.parametrize("time, expected", [
    # 按月：上个月同期，整月对整月
    ({"range": "calendar", "unit": "month", "offset": -1, "compare": "previous_period"}, ("2026-09-01", "2026-09-30")),
    ({"range": "calendar", "unit": "month", "offset": 0, "compare": "previous_period"}, ("2026-10-01", "2026-10-08")),
    ({"range": "custom", "start": "2026-10-01", "end": "2026-10-31", "compare": "previous_period"}, ("2026-09-01", "2026-09-30")),
    # 自然周：上一周同期
    ({"range": "calendar", "unit": "week", "offset": -2, "compare": "previous_period"}, ("2026-10-12", "2026-10-18")),
    ({"range": "this_week", "compare": "previous_period"}, ("2026-10-26", "2026-11-01")),
    # 滚动窗口、半个月的 custom：紧挨着的等长前一段（行为不变）
    ({"range": "last_7_days", "compare": "previous_period"}, ("2026-10-26", "2026-11-01")),
    ({"range": "custom", "start": "2026-10-01", "end": "2026-10-07", "compare": "previous_period"}, ("2026-09-24", "2026-09-30")),
    # 指定的对比期
    ({"range": "custom", "start": "2026-11-01", "end": "2026-11-07",
      "compare": {"range": "custom", "start": "2026-10-01", "end": "2026-10-07"}}, ("2026-10-01", "2026-10-07")),
    ({"range": "calendar", "unit": "month", "offset": 0,
      "compare": {"range": "calendar", "unit": "month", "offset": -1}}, ("2026-10-01", "2026-10-31")),   # 明确要整月
])
def test_compare_window(time, expected):
    assert _cw(time) == expected


def test_same_period_mid_week_and_month_end_clipping():
    wed = date(2026, 11, 4)
    assert _cw({"range": "this_week", "compare": "previous_period"}, wed) == ("2026-10-26", "2026-10-28")
    # 3-31 到今天 → 对不上的日子截到 2 月最后一天
    assert _cw({"range": "custom", "start": "2027-03-31", "end": "2027-03-31", "compare": "previous_period"},
               date(2027, 3, 31)) == ("2027-03-30", "2027-03-30")   # 单日不是整月，取前一天
    assert _cw({"range": "calendar", "unit": "month", "offset": 0, "compare": "previous_period"},
               date(2027, 3, 31)) == ("2027-02-01", "2027-02-28")
    assert _cw({"range": "calendar", "unit": "month", "offset": 0, "compare": "previous_period"},
               date(2028, 3, 30)) == ("2028-02-01", "2028-02-29")   # 闰年
    assert _cw({"range": "calendar", "unit": "month", "offset": -1, "compare": "previous_period"},
               date(2027, 3, 15)) == ("2027-01-01", "2027-01-31")   # 2 月整月 → 1 月整月


def _mql(time: dict) -> MQL:
    return MQL.model_validate({"metrics": ["ad_spend"], "time": time})


def test_compare_must_be_before_main_period():
    cat = load_catalog()
    swapped = {"range": "custom", "start": "2026-09-01", "end": "2026-09-30",
               "compare": {"range": "custom", "start": "2026-10-01", "end": "2026-10-31"}}
    with pytest.raises(MQLError, match="must end before"):
        validate_against_catalog(_mql(swapped), cat)
    overlap = {"range": "custom", "start": "2026-10-01", "end": "2026-10-31",
               "compare": {"range": "custom", "start": "2026-09-15", "end": "2026-10-05"}}
    with pytest.raises(MQLError, match="must end before"):
        validate_against_catalog(_mql(overlap), cat)


def test_compare_period_cannot_nest_compare():
    with pytest.raises(ValidationError):
        TimeSpec.model_validate({"range": "last_week", "compare": {"range": "last_week", "compare": "previous_period"}})


def _reference_times():
    for f in sorted(QUESTIONS.glob("*.yaml")):
        if f.stem == "m3_compare_holdout":   # Q20 之后出的题，本来就按新规则写
            continue
        for q in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
            t = (q.get("reference_mql") or {}).get("time")
            if t and t.get("compare") == "previous_period":
                yield f"{f.stem}:{q['id']}", t


@pytest.mark.parametrize("qid, time", list(_reference_times()))
def test_existing_questions_keep_their_comparison_period(qid, time):
    """SPEC Q20 事先算过的影响范围：已有题目的标准 MQL 里，没有一道是按月对比，所以对比期一天都不应该变。"""
    t = TimeSpec.model_validate(time)
    assert not (t.range == "calendar" and t.unit == "month")
    assert compare_window(t, SUN) == previous_window(*resolve_window(t, SUN)), qid
