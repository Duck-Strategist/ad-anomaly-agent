"""两段时间对比题的判分（长表转宽表）和 pass^k 统计。不需要数据库。"""

import pytest

from eval.pass_k import pass_k
from eval.scoring import compare_any, long_to_wide

GOLD_WIDE = [{"gross_sales_cur": 120.0, "gross_sales_prev": 100.0, "refund_rate_cur": 0.05, "refund_rate_prev": 0.04}]


def test_wide_candidate_matches_directly():
    cand = [{"gross_sales": 120.0, "gross_sales_prev": 100.0, "gross_sales_change_pct": 20.0,
             "refund_rate": 0.05, "refund_rate_prev": 0.04, "refund_rate_change_pct": 25.0}]
    assert compare_any([(GOLD_WIDE, None)], cand)[0]


@pytest.mark.parametrize("labels", [("本期", "上期"), ("2026-10", "2026-09"), ("2026-10-01", "2026-09-01")])
def test_long_candidate_accepted_only_for_period_questions(labels):
    cand = [{"period": labels[0], "gross_sales": 120.0, "refund_rate": 0.05},
            {"period": labels[1], "gross_sales": 100.0, "refund_rate": 0.04}]
    assert not compare_any([(GOLD_WIDE, None)], cand)[0]
    assert compare_any([(GOLD_WIDE, None)], cand, allow_long=True)[0]


def test_long_candidate_with_start_end_columns_and_a_dimension():
    gold = [{"shop_id": "US", "s_cur": 10.0, "s_prev": 8.0}, {"shop_id": "UK", "s_cur": 5.0, "s_prev": 6.0}]
    cand = [{"shop": "US", "period_start": "2026-10-01", "period_end": "2026-10-31", "sales": 10.0},
            {"shop": "US", "period_start": "2026-09-01", "period_end": "2026-09-30", "sales": 8.0},
            {"shop": "UK", "period_start": "2026-10-01", "period_end": "2026-10-31", "sales": 5.0},
            {"shop": "UK", "period_start": "2026-09-01", "period_end": "2026-09-30", "sales": 6.0}]
    assert compare_any([(gold, None)], cand, allow_long=True)[0]


def test_long_candidate_with_wrong_numbers_fails():
    cand = [{"period": "本期", "gross_sales": 120.0, "refund_rate": 0.05},
            {"period": "上期", "gross_sales": 99.0, "refund_rate": 0.04}]
    assert not compare_any([(GOLD_WIDE, None)], cand, allow_long=True)[0]


def test_long_to_wide_needs_exactly_two_periods():
    three = [{"p": x, "v": float(i)} for i, x in enumerate("abc")]
    assert long_to_wide(three) == []


def test_pass_k_counts_only_all_correct():
    runs = [[{"id": "A", "pass": True}, {"id": "B", "pass": True}, {"id": "C", "pass": False}],
            [{"id": "A", "pass": True}, {"id": "B", "pass": False}, {"id": "C", "pass": False}],
            [{"id": "A", "pass": True}, {"id": "B", "pass": True}, {"id": "C", "pass": False}]]
    s = pass_k(runs)
    assert (s["pass_k"], s["single"], s["answers"]) == (1, 5, 9)
    assert s["unstable"] == ["B"] and s["never"] == ["C"]
