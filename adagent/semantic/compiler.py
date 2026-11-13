"""MQL → SQL 编译器（纯函数，不连数据库，方便单元测试）。

生成的 SQL 结构：
    WITH ad  AS (广告日报 + 汇率，按请求维度聚合)          -- 需要广告侧度量时
       , ord AS (订单 + 订单行 + 退款 + 汇率，按请求维度聚合) -- 需要订单侧度量时
       , net AS (按 SKU 折算退款后的广告销售额)              -- 需要 net_roas 时
    SELECT 维度, 派生指标（先求和再相除）
    FROM ad FULL OUTER JOIN ord USING (维度) ...

规则（每一条都对应一个数据陷阱或安全要求）：
  - 两侧分别聚合到相同粒度后再 JOIN，避免扇出
  - 金额按行乘以当天汇率后再求和（汇率每天不同）
  - 订单按店铺本地日期切日（created_at 存的是 UTC）
  - 订单排除软删除；退款先按订单行聚合再 JOIN（一行可能有多笔退款）
  - 每个子查询都强制加 shop_id = ANY(%(allowed_shops)s)
  - 过滤条件里的商品 / 广告组 / 广告活动编号，属于无权限店铺的直接拒绝，不存在的报错（见 check_id_filters）
  - 所有来自 MQL 的值都走参数绑定；拼进 SQL 的只有目录里预先写好的表达式
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import date, timedelta

from .catalog import Catalog
from .mql import MQL, MQLError, previous_window, resolve_window, validate_against_catalog


class PermissionDenied(MQLError):
    """请求了无权限的店铺。不应让模型重试，而应如实告诉用户。"""


ID_FILTERS = ("sku", "ad_group", "campaign")


@dataclass
class IdIndex:
    """编号归属，由执行器查库得到后传进来（编译器本身不连数据库）。

    owners:     过滤条件里出现的编号 → 所属店铺；查不到的编号不在里面
    accessible: 有权限店铺下该类编号的全集，只在有编号查不到时才填，用来给"你是不是想找"的候选
    """
    owners: dict[str, dict[str, str]] = field(default_factory=dict)
    accessible: dict[str, list[str]] = field(default_factory=dict)


def check_id_filters(mql: MQL, allowed_shops: list[str], ids: IdIndex | None) -> None:
    """第一层权限检查的补充（2026-11-11，评测题 P08 核对时发现）。

    只检查 shop 过滤时，别家店的编号会被 SQL 层的店铺过滤静默滤掉，返回"花费 0"——没有泄露数据，
    但用户拿到的是一个看起来正常的错误数字。这里在编译前就把它拒掉：
      - 编号属于无权限店铺 → PermissionDenied（不重试，如实告诉用户）
      - 编号不存在         → MQLError（多半是笔误，交给模型自修正；候选只从有权限的店铺里找，不泄露别家编号）
    归属以数据库外键为准，不从编号前缀推断（真实 SKU 是卖家自己起的名字，没有店铺前缀）。
    """
    requested = {k: getattr(mql.filters, k) or [] for k in ID_FILTERS}
    if not any(requested.values()):
        return
    if ids is None:   # 程序错误：调用方没查归属。宁可报错，也不放行（fail closed）
        raise ValueError("IdIndex is required when MQL filters by sku / ad_group / campaign")
    denied, unknown = [], []
    for kind, values in requested.items():
        for v in values:
            owner = ids.owners.get(kind, {}).get(v)
            if owner is None:
                unknown.append((kind, v))
            elif owner not in allowed_shops:
                denied.append(f"{kind} {v} (belongs to shop {owner})")
    if denied:
        raise PermissionDenied([f"no permission for {', '.join(denied)}. "
                                f"You can access: {', '.join(sorted(allowed_shops))}"])
    if unknown:
        errors = []
        for kind, v in unknown:
            close = difflib.get_close_matches(v, ids.accessible.get(kind, []), n=3, cutoff=0.6)
            errors.append(f"{kind} {v} does not exist" + (f"; did you mean: {', '.join(close)}" if close else ""))
        raise MQLError(errors)


@dataclass
class CompiledQuery:
    sql: str
    params: dict
    window: tuple[date, date]
    columns: list[str]
    refund_incomplete: bool = False


@dataclass
class CompiledMQL:
    current: CompiledQuery
    previous: CompiledQuery | None = None
    dimensions: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)


# 各维度在两侧数据里对应的 SQL 表达式
_AD_DIM = {
    "date": "s.stat_date",
    "week": "date_trunc('week', s.stat_date)::date",
    "shop": "sh.shop_id",
    "sku": "g.sku_id",
    "ad_group": "g.ad_group_id",
    "campaign": "g.campaign_id",
}
_ORD_DIM = {
    "date": "ld.local_date",
    "week": "date_trunc('week', ld.local_date)::date",
    "shop": "sh.shop_id",
    "sku": "oi.sku_id",
}
# 过滤条件：MQL 的 filters 字段 → 两侧的列
_AD_FILTER = {"shop": "sh.shop_id", "sku": "g.sku_id", "ad_group": "g.ad_group_id", "campaign": "g.campaign_id"}
_ORD_FILTER = {"shop": "sh.shop_id", "sku": "oi.sku_id"}

_AD_FROM = """ad_daily_stats s
      JOIN ad_groups g     ON g.ad_group_id = s.ad_group_id
      JOIN ad_campaigns c  ON c.campaign_id = g.campaign_id
      JOIN shops sh        ON sh.shop_id = c.shop_id
      JOIN fx_rates fx     ON fx.rate_date = s.stat_date AND fx.currency = sh.currency"""

_ORD_FROM = """orders o
      JOIN shops sh        ON sh.shop_id = o.shop_id
      CROSS JOIN LATERAL (SELECT (o.created_at AT TIME ZONE sh.timezone)::date AS local_date) ld
      JOIN order_items oi  ON oi.order_id = o.order_id
      LEFT JOIN (SELECT item_id, SUM(amount) AS amount FROM refunds GROUP BY item_id) r
                           ON r.item_id = oi.item_id
      JOIN fx_rates fx     ON fx.rate_date = ld.local_date AND fx.currency = sh.currency"""


def _measure_sql(cat: Catalog, name: str) -> str:
    m = cat.measures[name]
    expr = f"({m['expr']}) * fx.rate_to_usd" if m.get("money") else m["expr"]
    if m["agg"] == "sum":
        return f"SUM({expr})"
    if m["agg"] == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    raise AssertionError(m["agg"])


def _formula_sql(formula: str) -> str:
    """目录里的公式只有三种形式：x、x / y、x - y。除法分母用 NULLIF 防止除零。"""
    parts = formula.split()
    if len(parts) == 1:
        return f"COALESCE({parts[0]}, 0)"
    a, op, b = parts
    if op == "/":
        return f"{a}::numeric / NULLIF({b}, 0)"
    if op == "-":
        return f"COALESCE({a}, 0) - COALESCE({b}, 0)"
    raise AssertionError(formula)


def _where(base: list[str], filters: dict, colmap: dict, params: dict, prefix: str) -> str:
    conds = list(base)
    for key, values in filters.items():
        if values and key in colmap:
            pname = f"{prefix}_{key}"
            params[pname] = list(values)
            conds.append(f"{colmap[key]} = ANY(%({pname})s)")
    return "\n      AND ".join(conds)


def _select_dims(dims: list[str], dimmap: dict) -> list[str]:
    return [f"{dimmap[d]} AS {d}" for d in dims]


def _compile_window(mql: MQL, cat: Catalog, allowed: list[str], start: date, end: date,
                    apply_limit: bool = True) -> CompiledQuery:
    dims = mql.dimensions
    filters = mql.filters.model_dump()
    params: dict = {"allowed_shops": allowed, "start": start, "end": end}

    needed = {m for metric in mql.metrics for m in cat.measures_of(metric)}
    ad_measures = sorted(m for m in needed if cat.measures[m]["source"] == "ad")
    ord_measures = sorted(m for m in needed if cat.measures[m]["source"] == "order")
    need_net = "net_ad_sales" in needed

    ctes: list[tuple[str, list[str]]] = []   # (名字, 输出的度量列)
    sql_ctes: list[str] = []
    group_by = lambda n: f"\n    GROUP BY {', '.join(str(i) for i in range(1, n + 1))}" if n else ""

    ad_where = _where(["sh.shop_id = ANY(%(allowed_shops)s)",          # 权限：强制注入
                       "s.stat_date BETWEEN %(start)s AND %(end)s"], filters, _AD_FILTER, params, "ad")
    ord_where = _where(["sh.shop_id = ANY(%(allowed_shops)s)",         # 权限：强制注入
                        "o.deleted_at IS NULL",                        # 陷阱：软删除
                        "ld.local_date BETWEEN %(start)s AND %(end)s"],  # 陷阱：按店铺本地日期切日
                       filters, _ORD_FILTER, params, "ord")

    if ad_measures:
        cols = _select_dims(dims, _AD_DIM) + [f"{_measure_sql(cat, m)} AS {m}" for m in ad_measures]
        sql_ctes.append(f"ad AS (\n    SELECT {', '.join(cols)}\n    FROM {_AD_FROM}\n    WHERE {ad_where}{group_by(len(dims))}\n)")
        ctes.append(("ad", ad_measures))

    if ord_measures:
        cols = _select_dims(dims, _ORD_DIM) + [f"{_measure_sql(cat, m)} AS {m}" for m in ord_measures]
        sql_ctes.append(f"ord AS (\n    SELECT {', '.join(cols)}\n    FROM {_ORD_FROM}\n    WHERE {ord_where}{group_by(len(dims))}\n)")
        ctes.append(("ord", ord_measures))

    if need_net:
        # 在"请求维度 + SKU"的粒度上，用该 SKU 同一统计单元的退款率折算广告销售额，再汇总到请求维度。
        # 订单侧只按时间维度 + SKU 聚合（SKU 已唯一确定店铺 / 广告组 / 广告活动）。
        time_dims = [d for d in dims if d in ("date", "week")]
        a_cols = _select_dims(dims, _AD_DIM) + ["g.sku_id AS sku_key",
                                               f"{_measure_sql(cat, 'ad_sales')} AS ad_sales"]
        o_cols = _select_dims(time_dims, _ORD_DIM) + ["oi.sku_id AS sku_key",
                                                      f"{_measure_sql(cat, 'gross_sales')} AS gross_sales",
                                                      f"{_measure_sql(cat, 'refund_amount')} AS refund_amount"]
        join_on = " AND ".join([f"o.{d} = a.{d}" for d in time_dims] + ["o.sku_key = a.sku_key"])
        outer_dims = [f"a.{d}" for d in dims]
        net_cols = outer_dims + ["SUM(a.ad_sales * (1 - COALESCE(o.refund_amount / NULLIF(o.gross_sales, 0), 0))) AS net_ad_sales"]
        sql_ctes.append(
            f"net_ad AS (\n    SELECT {', '.join(a_cols)}\n    FROM {_AD_FROM}\n    WHERE {ad_where}{group_by(len(dims) + 1)}\n)"
        )
        sql_ctes.append(
            f"net_ord AS (\n    SELECT {', '.join(o_cols)}\n    FROM {_ORD_FROM}\n    WHERE {ord_where}{group_by(len(time_dims) + 1)}\n)"
        )
        sql_ctes.append(
            f"net AS (\n    SELECT {', '.join(net_cols)}\n    FROM net_ad a LEFT JOIN net_ord o ON {join_on}{group_by(len(dims))}\n)"
        )
        ctes.append(("net", ["net_ad_sales"]))

    # 最外层：多个子查询按维度 FULL OUTER JOIN（没有维度时每个子查询只有一行，用 CROSS JOIN）
    from_sql = ctes[0][0]
    for name, _ in ctes[1:]:
        from_sql += f"\nFULL OUTER JOIN {name} USING ({', '.join(dims)})" if dims else f"\nCROSS JOIN {name}"

    metric_cols = []
    for m in mql.metrics:
        spec = cat.metrics[m]
        expr = _formula_sql(spec["formula"])
        # 金额保留 4 位：单次点击花费只有 0.5 美元左右，保留 2 位会带来约 1% 的误差（2026-11-11 发现）
        digits = {"ratio": 6, "money": 4}.get(spec["unit"])
        metric_cols.append(f"ROUND(({expr})::numeric, {digits}) AS {m}" if digits else f"{expr} AS {m}")

    order = (f"{mql.order_by.metric} {'DESC' if mql.order_by.desc else 'ASC'} NULLS LAST"
             if mql.order_by else (", ".join(dims) if dims else None))
    # 对比查询的上期不排序、不加 LIMIT：它只为本期的每一行提供对比值，截断会导致对不上
    if apply_limit:
        params["limit"] = mql.limit
    else:
        order = None
    sql = (f"WITH {(chr(10) + ', ').join(sql_ctes)}\n"
           f"SELECT {', '.join(dims + metric_cols)}\nFROM {from_sql}"
           + (f"\nORDER BY {order}" if order else "") + ("\nLIMIT %(limit)s" if apply_limit else ""))

    # 只有用到退款的度量才需要提示"近期退款未入账"（订单数、销售额本身不受影响）
    refund_incomplete = bool("refund_amount" in needed or need_net) and end > cat.today - timedelta(cat.refund_settle_days)
    return CompiledQuery(sql=sql, params=params, window=(start, end),
                         columns=dims + list(mql.metrics), refund_incomplete=refund_incomplete)


def compile_mql(mql: MQL, cat: Catalog, allowed_shops: list[str], ids: IdIndex | None = None) -> CompiledMQL:
    """allowed_shops 由服务端根据 user_id 查权限表得到，绝不来自 MQL 或模型。
    ids：过滤条件里编号的归属（执行器查库得到）；MQL 按编号过滤时必须提供。"""
    validate_against_catalog(mql, cat)

    if not allowed_shops:
        raise PermissionDenied(["you do not have access to any shop"])
    if mql.filters.shop:
        denied = sorted(set(mql.filters.shop) - set(allowed_shops))
        if denied:
            raise PermissionDenied([f"no permission for shop(s): {', '.join(denied)}. "
                                    f"You can access: {', '.join(sorted(allowed_shops))}"])
    check_id_filters(mql, allowed_shops, ids)

    start, end = resolve_window(mql.time, cat.today)
    current = _compile_window(mql, cat, sorted(allowed_shops), start, end)
    previous = None
    if mql.time.compare == "previous_period":
        ps, pe = previous_window(start, end)
        previous = _compile_window(mql, cat, sorted(allowed_shops), ps, pe, apply_limit=False)
    return CompiledMQL(current=current, previous=previous, dimensions=list(mql.dimensions), metrics=list(mql.metrics))
