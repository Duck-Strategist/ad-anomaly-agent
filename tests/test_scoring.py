"""评分器测试：既要判对正确答案，也要判错掉进陷阱的答案。"""

from decimal import Decimal
from pathlib import Path

import pytest

from eval.check_reference import load_questions, run_gold
from eval.scoring import compare_results

QUESTIONS = {q["id"]: q for q in load_questions(Path(__file__).resolve().parent.parent / "eval" / "questions" / "m1.yaml")}


# ---------- 纯单元测试 ----------

def test_column_names_and_order_do_not_matter():
    gold = [{"shop_id": "US", "v": Decimal("10.00")}, {"shop_id": "UK", "v": Decimal("20.00")}]
    cand = [{"x": 20.0, "shop": "UK", "extra": 1.0}, {"x": 10.0, "shop": "US", "extra": 2.0}]
    assert compare_results(gold, cand)[0]


def test_tolerance_and_percent_form():
    gold = [{"ctr": Decimal("0.007234")}]
    assert compare_results(gold, [{"a": 0.00724}])[0]      # 0.08% 误差
    assert compare_results(gold, [{"a": 0.7234}])[0]       # 以百分数给出
    assert not compare_results(gold, [{"a": 0.0074}])[0]   # 2.3% 误差


def test_row_count_mismatch_fails():
    assert not compare_results([{"v": 1}], [{"v": 1}, {"v": 2}])[0]


# ---------- 陷阱题必须能区分对错 ----------
# 每个都是"掉进陷阱"的写法，结果应当被判错

NAIVE = {
    "T01": """SELECT COUNT(*) FROM orders WHERE shop_id = 'US'
              AND (created_at AT TIME ZONE 'America/Los_Angeles')::date BETWEEN '2026-10-26' AND '2026-11-01'""",  # 忘了软删除
    "T04": """SELECT SUM(spend) FROM ad_daily_stats WHERE stat_date BETWEEN '2026-10-26' AND '2026-11-01'""",          # 不换汇
    "T07": """SELECT SUM(oi.qty * oi.unit_price * 0.0067) FROM orders o JOIN order_items oi USING (order_id)
              WHERE o.shop_id = 'JP' AND o.deleted_at IS NULL AND o.created_at::date = '2026-10-01'""",                # 按 UTC 切日
    "T12": """SELECT SUM(r.amount) FROM refunds r JOIN order_items oi USING (item_id) JOIN orders o USING (order_id)
              WHERE o.shop_id = 'US' AND o.deleted_at IS NULL
                AND (r.refunded_at AT TIME ZONE 'America/Los_Angeles')::date BETWEEN '2026-09-01' AND '2026-09-30'""",  # 按退款日归属
    "T10": """SELECT SUM(oi.qty * oi.unit_price * fx.rate_to_usd)
              FROM orders o JOIN order_items oi USING (order_id)
              JOIN fx_rates fx ON fx.currency = 'GBP' AND fx.rate_date = (o.created_at AT TIME ZONE 'Europe/London')::date
              WHERE o.shop_id = 'UK' AND o.deleted_at IS NULL
                AND (o.created_at AT TIME ZONE 'Europe/London')::date BETWEEN '2026-09-01' AND '2026-09-30'""",  # 忽略退款
    "T11": """SELECT SUM(COALESCE(r.amount, 0) * fx.rate_to_usd) / SUM(oi.qty * oi.unit_price * fx.rate_to_usd)
              FROM orders o JOIN order_items oi USING (order_id) LEFT JOIN refunds r ON r.item_id = oi.item_id
              JOIN fx_rates fx ON fx.currency = 'GBP' AND fx.rate_date = (o.created_at AT TIME ZONE 'Europe/London')::date
              WHERE oi.sku_id = 'UK-SKU05' AND o.deleted_at IS NULL
                AND (o.created_at AT TIME ZONE 'Europe/London')::date BETWEEN '2026-10-12' AND '2026-10-18'""",  # 退款直接 JOIN → 扇出
}


@pytest.mark.parametrize("qid", sorted(NAIVE))
def test_naive_answer_is_marked_wrong(qid):
    gold = run_gold(QUESTIONS[qid]["gold_sql"])
    naive = run_gold(NAIVE[qid])
    assert compare_results(gold, gold)[0]
    ok, why = compare_results(gold, naive)
    assert not ok, f"{qid}: naive SQL should fail but scored as correct"
