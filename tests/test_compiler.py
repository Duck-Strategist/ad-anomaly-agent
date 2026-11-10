"""编译器测试。

1. 单元测试（不连数据库）：权限注入、参数绑定、对比查询等结构性保证。
2. 集成测试（需要数据库）：编译器结果 vs 独立手写的 SQL vs 用 Python 直接读 CSV 计算。
   三条路径互相独立，结果一致才说明口径正确（对应 SPEC "标准答案制作流程"里的交叉验证）。
"""

import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from adagent.db import reader_conn
from adagent.semantic.catalog import load_catalog
from adagent.semantic.compiler import PermissionDenied, compile_mql
from adagent.semantic.executor import query_metrics
from adagent.semantic.mql import MQL, MQLError

CAT = load_catalog()
OUT = Path(__file__).resolve().parent.parent / "datagen" / "out"


def compile_(q: dict, shops=("US", "UK", "JP")):
    return compile_mql(MQL.model_validate(q), CAT, list(shops))


# ======================= 单元测试 =======================

ALL_SHAPES = [
    {"metrics": ["roas"], "time": {"range": "last_7_days"}},
    {"metrics": ["refund_rate", "aov"], "dimensions": ["sku"], "time": {"range": "last_week"}},
    {"metrics": ["tacos", "net_roas"], "dimensions": ["shop", "week"], "time": {"range": "last_30_days"}},
    {"metrics": ["net_roas", "cvr"], "dimensions": ["ad_group"], "filters": {"ad_group": ["UK-AG05"]},
     "time": {"range": "custom", "start": "2026-10-12", "end": "2026-10-18"}},
]


@pytest.mark.parametrize("q", ALL_SHAPES)
def test_every_subquery_has_permission_filter(q):
    sql = compile_(q).current.sql
    n_subqueries = sql.count("\n    FROM ") - sql.count("FROM net_ad a")  # net 子查询不直接读表
    assert sql.count("sh.shop_id = ANY(%(allowed_shops)s)") == n_subqueries


@pytest.mark.parametrize("q", ALL_SHAPES)
def test_order_side_always_excludes_soft_deleted(q):
    sql = compile_(q).current.sql
    assert sql.count("FROM orders o") == sql.count("o.deleted_at IS NULL")


def test_values_are_bound_not_inlined():
    evil = "US-SKU01'; DROP TABLE orders; --"
    c = compile_({"metrics": ["roas"], "filters": {"sku": [evil]}, "time": {"range": "last_7_days"}})
    assert evil not in c.current.sql
    assert c.current.params["ad_sku"] == [evil]


def test_permission_denied_for_unauthorized_shop():
    with pytest.raises(PermissionDenied):
        compile_({"metrics": ["ad_spend"], "filters": {"shop": ["UK"]}, "time": {"range": "last_week"}}, shops=["US"])


def test_no_shop_filter_defaults_to_allowed_shops():
    c = compile_({"metrics": ["ad_spend"], "time": {"range": "last_week"}}, shops=["US"])
    assert c.current.params["allowed_shops"] == ["US"]


def test_previous_period_has_no_limit():
    c = compile_({"metrics": ["roas"], "dimensions": ["ad_group"], "limit": 3,
                  "time": {"range": "last_7_days", "compare": "previous_period"}})
    assert "LIMIT" in c.current.sql and "LIMIT" not in c.previous.sql
    assert c.previous.window == (date(2026, 10, 26), date(2026, 11, 1))


def test_order_metric_by_campaign_rejected():
    with pytest.raises(MQLError, match="cannot be split"):
        compile_({"metrics": ["refund_rate"], "dimensions": ["campaign"], "time": {"range": "last_7_days"}})


def test_refund_incomplete_flag():
    recent = compile_({"metrics": ["net_sales"], "time": {"range": "last_7_days"}})
    old = compile_({"metrics": ["net_sales"], "time": {"range": "custom", "start": "2026-09-01", "end": "2026-09-07"}})
    ads_only = compile_({"metrics": ["roas"], "time": {"range": "last_7_days"}})
    assert recent.current.refund_incomplete and not old.current.refund_incomplete
    assert not ads_only.current.refund_incomplete


# ======================= 集成测试：编译器 vs 手写 SQL vs CSV =======================

def db_value(sql: str, params: dict) -> Decimal:
    with reader_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def agent_value(q: dict, user="u_admin"):
    r = query_metrics(q, user)
    assert r["ok"], r
    return r["rows"]


@pytest.fixture(scope="module")
def csv_orders():
    """用 Python 直接从生成器的 CSV 计算（完全不经过 SQL）。"""
    if not OUT.exists():
        pytest.skip("datagen/out not found; run `python -m datagen.generate`")
    rd = lambda n: list(csv.reader(open(OUT / f"{n}.csv", encoding="utf-8")))
    tz = {r[0]: ZoneInfo(r[4]) for r in rd("shops")}
    cur = {r[0]: r[3] for r in rd("shops")}
    fx = {(date.fromisoformat(r[0]), r[1]): Decimal(r[2]) for r in rd("fx_rates")}
    orders = {}
    for oid, shop, created, _, deleted in rd("orders"):
        if not deleted:
            orders[oid] = (shop, datetime.fromisoformat(created).astimezone(tz[shop]).date())
    items = {}
    gross = defaultdict(Decimal)
    for iid, oid, sku, qty, price in rd("order_items"):
        if oid in orders:
            shop, d = orders[oid]
            items[iid] = (shop, sku, d)
            gross[(shop, sku, d)] += int(qty) * Decimal(price) * fx[(d, cur[shop])]
    refund = defaultdict(Decimal)
    for _, iid, amt, _ in rd("refunds"):
        if iid in items:
            shop, sku, d = items[iid]
            refund[(shop, sku, d)] += Decimal(amt) * fx[(d, cur[shop])]
    return gross, refund


def window_sum(table, shop=None, sku=None, start=None, end=None):
    return sum(v for (s, k, d), v in table.items()
               if (shop is None or s == shop) and (sku is None or k == sku) and start <= d <= end)


def test_jp_gross_sales_three_ways(csv_orders):
    """日本店：时区（UTC+9）+ 日元换算 + 软删除，三条路径对比。"""
    gross, _ = csv_orders
    start, end = date(2026, 9, 28), date(2026, 10, 4)
    rows = agent_value({"metrics": ["gross_sales"], "filters": {"shop": ["JP"]},
                        "time": {"range": "custom", "start": str(start), "end": str(end)}})
    hand = db_value("""
        SELECT SUM(oi.qty * oi.unit_price * fx.rate_to_usd)
        FROM order_items oi
        JOIN (SELECT o.order_id, (o.created_at AT TIME ZONE 'Asia/Tokyo')::date AS d
              FROM orders o WHERE o.shop_id = 'JP' AND o.deleted_at IS NULL) x ON x.order_id = oi.order_id
        JOIN fx_rates fx ON fx.rate_date = x.d AND fx.currency = 'JPY'
        WHERE x.d BETWEEN %(s)s AND %(e)s""", {"s": start, "e": end})
    py = window_sum(gross, shop="JP", start=start, end=end)
    assert rows[0]["gross_sales"] == round(hand, 2) == round(py, 2)


def test_uk_refund_rate_during_refund_spike_three_ways(csv_orders):
    """事件 E3：英国店 SKU05 退款激增；退款归属下单日，一行多笔退款不扇出。"""
    gross, refund = csv_orders
    start, end = date(2026, 10, 12), date(2026, 10, 18)
    rows = agent_value({"metrics": ["refund_rate"], "filters": {"sku": ["UK-SKU05"]},
                        "time": {"range": "custom", "start": str(start), "end": str(end)}})
    hand = db_value("""
        WITH lines AS (
            SELECT oi.item_id, oi.qty * oi.unit_price AS amt,
                   (o.created_at AT TIME ZONE 'Europe/London')::date AS d
            FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
            WHERE oi.sku_id = 'UK-SKU05' AND o.deleted_at IS NULL)
        SELECT SUM(COALESCE((SELECT SUM(r.amount) FROM refunds r WHERE r.item_id = l.item_id), 0) * fx.rate_to_usd)
             / SUM(l.amt * fx.rate_to_usd)
        FROM lines l JOIN fx_rates fx ON fx.rate_date = l.d AND fx.currency = 'GBP'
        WHERE l.d BETWEEN %(s)s AND %(e)s""", {"s": start, "e": end})
    py = window_sum(refund, sku="UK-SKU05", start=start, end=end) / window_sum(gross, sku="UK-SKU05", start=start, end=end)
    assert rows[0]["refund_rate"] == round(hand, 4) == round(py, 4)
    assert rows[0]["refund_rate"] > Decimal("0.2")   # 事件确实埋进去了


def test_us_ad_roas_by_ad_group_matches_hand_sql():
    rows = agent_value({"metrics": ["roas"], "dimensions": ["ad_group"], "filters": {"shop": ["US"]},
                        "time": {"range": "last_7_days"}})
    got = {r["ad_group"]: r["roas"] for r in rows}
    with reader_conn() as conn:
        hand = dict(conn.execute("""
            SELECT ad_group_id, ROUND(SUM(ad_sales) / SUM(spend), 4)   -- 同币种，汇率约掉
            FROM ad_daily_stats WHERE ad_group_id LIKE 'US-%%' AND stat_date BETWEEN '2026-11-02' AND '2026-11-08'
            GROUP BY 1""").fetchall())
    assert got == hand


def test_soft_delete_trap_actually_matters():
    """如果不过滤软删除，结果会不同——说明这个陷阱在数据里是真实存在的。"""
    rows = agent_value({"metrics": ["order_count"], "filters": {"shop": ["US"]}, "time": {"range": "last_week"}})
    naive = db_value("""SELECT COUNT(*) FROM orders WHERE shop_id = 'US'
                        AND (created_at AT TIME ZONE 'America/Los_Angeles')::date BETWEEN '2026-10-26' AND '2026-11-01'""", {})
    assert naive > rows[0]["order_count"]


def test_reader_permission_for_single_shop_user():
    r = query_metrics({"metrics": ["ad_spend"], "dimensions": ["shop"], "time": {"range": "last_week"}}, "u_ops_us")
    assert r["ok"] and [row["shop"] for row in r["rows"]] == ["US"]
    r = query_metrics({"metrics": ["ad_spend"], "filters": {"shop": ["JP"]}, "time": {"range": "last_week"}}, "u_ops_us")
    assert not r["ok"] and r["error_type"] == "permission"
