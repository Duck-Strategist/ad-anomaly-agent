"""decompose_roas：确定性的归因分解（不调用 LLM）。

第一层（找主因）：对数乘法分解，精确无残差
    net_roas = CVR × ad_aov ÷ CPC × 退款因子        （退款因子 = net_roas / roas）
    Δln(net_roas) = Δln CVR + Δln ad_aov − Δln CPC + Δln 退款因子

第二层（定位广告组，仅 scope=shop）：按花费加权的贡献度，Σ 贡献 = Δnet_roas（精确）
    贡献_i = [(本期净销售_i − 上期R × 本期花费_i) − (上期净销售_i − 上期R × 上期花费_i)] / 本期总花费

数据全部经 query_metrics 获取，所以权限照常由编译器注入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from ..semantic.executor import query_metrics

# 显著性：用订单量标准化后的变化（订单数的随机误差约为 √n）
#     z = Δln(net_roas) / √(1/本期广告订单 + 1/上期广告订单)
# 下滑同时满足 z ≤ −Z_THRESHOLD 且 |Δln| ≥ MIN_EFFECT 才算显著。
# 为什么不用固定阈值（2026-11-10 实测，非事件周的广告组周环比，n=319）：
#   周订单 20–50 的广告组 |Δln| 的 95 分位 0.42，周订单 >100 的只有 0.09——噪声随订单量变化，固定阈值要么漏报大组、要么误报小组。
#   标准化后非事件下滑的 z：95 分位 −1.73，99 分位 −2.64，最小 −3.46；6 个埋入事件的 z 在 −4.2 到 −10.2 之间。
Z_THRESHOLD = 3.0
MIN_EFFECT = 0.05               # 实际意义下限：变化小于约 5% 即使统计上显著也不报
SECONDARY_SHARE = 0.25          # 占下滑比例 ≥ 25% 的其他因子列为次要原因

FACTORS = ("cvr", "ad_aov", "cpc", "refund")
ACTIONS = {
    "cvr": ("pause_ad_group", "转化率下降：同样的点击带来的订单变少，继续投放会持续亏损"),
    "refund": ("check_product_quality", "退款激增：广告报表的 ROAS 看起来正常，扣除退款后才下降，问题在商品而不是广告"),
    "ad_aov": ("explain_promotion", "广告客单价下降：通常是促销降价导致，转化没有变差，不建议暂停"),
    "cpc": ("review_bids", "单次点击成本上升：竞价变贵，第一版没有调价动作，仅提示"),
}

# 取求和后的基础度量，比率在 Python 里算：查询结果里金额类指标四舍五入到 2 位小数，
# CPC（约 0.8 美元）这样的小金额会因此带来约 0.7% 的误差，破坏分解的精确性
METRICS = ["clicks", "ad_orders", "ad_spend", "ad_sales", "net_roas"]


def derive(base: dict) -> dict:
    """由基础度量算出分解需要的比率。分母为 0 时对应项为 None。"""
    c, o, sp, sa, nr = (_f(base.get(k)) for k in METRICS)
    div = lambda a, b: a / b if a is not None and b else None
    return {"cvr": div(o, c), "ad_aov": div(sa, o), "cpc": div(sp, c), "roas": div(sa, sp),
            "net_roas": nr, "ad_orders": o}


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def previous(self) -> "Window":
        n = (self.end - self.start).days + 1
        return Window(self.start - timedelta(n), self.start - timedelta(1))


def _f(x) -> float | None:
    return None if x is None else float(x)


def log_decompose(prev: dict, cur: dict) -> dict:
    """第一层分解。prev/cur 含 cvr、ad_aov、cpc、roas、net_roas。任何一项缺失或 ≤0 时返回 None（无法取对数）。"""
    def factors(m):
        vals = {k: _f(m.get(k)) for k in ("cvr", "ad_aov", "cpc", "roas", "net_roas")}
        if any(v is None or v <= 0 for v in vals.values()):
            return None
        return {"cvr": vals["cvr"], "ad_aov": vals["ad_aov"], "cpc": vals["cpc"],
                "refund": vals["net_roas"] / vals["roas"], "net_roas": vals["net_roas"]}

    p, c = factors(prev), factors(cur)
    if p is None or c is None:
        return None
    terms = {k: math.log(c[k] / p[k]) for k in FACTORS}
    terms["cpc"] = -terms["cpc"]                       # CPC 在分母：CPC 上升 → 拉低 ROAS
    total = math.log(c["net_roas"] / p["net_roas"])
    return {
        "total_log_change": total,
        "total_change_pct": (math.exp(total) - 1) * 100,
        "net_roas": {"prev": p["net_roas"], "cur": c["net_roas"]},
        "factors": {k: {"prev": p[k], "cur": c[k], "log_contrib": terms[k],
                        "share": terms[k] / total if total else None} for k in FACTORS},
        "residual": total - sum(terms.values()),       # 理论上为 0（只剩浮点 / 四舍五入误差）
    }


def z_score(total_log_change: float, orders_prev: float, orders_cur: float) -> float | None:
    if not orders_prev or not orders_cur:
        return None
    return total_log_change / math.sqrt(1 / orders_cur + 1 / orders_prev)


def judge(decomp: dict, orders_prev: float, orders_cur: float) -> dict:
    """主因 / 次要因判定。只在下滑显著时归因。"""
    total = decomp["total_log_change"]
    z = z_score(total, orders_prev, orders_cur)
    base = {"z": z}
    if z is None or z > -Z_THRESHOLD or total > -MIN_EFFECT:
        return {**base, "significant": False, "main_cause": None, "secondary_causes": []}
    neg = sorted(((k, v["log_contrib"]) for k, v in decomp["factors"].items() if v["log_contrib"] < 0),
                 key=lambda kv: kv[1])
    main = neg[0][0]
    secondary = [k for k, v in neg[1:] if v / total >= SECONDARY_SHARE]
    return {**base, "significant": True, "main_cause": main, "secondary_causes": secondary}


def _query(mql: dict, user_id: str) -> list[dict]:
    r = query_metrics(mql, user_id)
    if not r["ok"]:
        raise PermissionError(r["errors"]) if r["error_type"] == "permission" else RuntimeError(r["errors"])
    return r["rows"]


def _time(w: Window) -> dict:
    return {"range": "custom", "start": w.start.isoformat(), "end": w.end.isoformat(), "compare": "previous_period"}


def _split(row: dict, metrics: list[str]) -> tuple[dict, dict]:
    """compare 查询的一行 → (上期比率, 本期比率)。"""
    return (derive({m: row.get(f"{m}_prev") for m in metrics}), derive({m: row.get(m) for m in metrics}))


def group_contributions(rows: list[dict], shop_prev_net_roas: float) -> list[dict]:
    """第二层：每个广告组对店铺 Δnet_roas 的贡献。rows 含 ad_group、ad_spend(_prev)、net_roas(_prev)。"""
    total_spend_cur = sum(_f(r["ad_spend"]) or 0 for r in rows)
    R = shop_prev_net_roas
    out = []
    for r in rows:
        sp_c, sp_p = _f(r["ad_spend"]) or 0.0, _f(r["ad_spend_prev"]) or 0.0
        net_c = (_f(r["net_roas"]) or 0.0) * sp_c
        net_p = (_f(r["net_roas_prev"]) or 0.0) * sp_p
        contrib = ((net_c - R * sp_c) - (net_p - R * sp_p)) / total_spend_cur
        out.append({"ad_group": r["ad_group"], "contribution": contrib,
                    "spend_cur": sp_c, "spend_prev": sp_p,
                    "net_roas_cur": _f(r["net_roas"]), "net_roas_prev": _f(r["net_roas_prev"])})
    return sorted(out, key=lambda x: x["contribution"])


def decompose_roas(scope: str, target: str, window: Window, user_id: str) -> dict:
    """scope = shop | ad_group；window 为本期，上期自动取紧邻的等长周期。"""
    if scope not in ("shop", "ad_group"):
        raise ValueError(scope)
    flt = {"shop": [target]} if scope == "shop" else {"ad_group": [target]}
    row = _query({"metrics": METRICS, "filters": flt, "time": _time(window)}, user_id)[0]
    prev, cur = _split(row, METRICS)
    decomp = log_decompose(prev, cur)
    result = {"scope": scope, "target": target,
              "window": [window.start.isoformat(), window.end.isoformat()],
              "previous_window": [window.previous.start.isoformat(), window.previous.end.isoformat()]}
    if decomp is None:
        return {**result, "significant": False, "main_cause": None, "note": "本期或上期数据为零，无法分解"}
    result |= {"decomposition": decomp, **judge(decomp, _f(prev["ad_orders"]), _f(cur["ad_orders"]))}

    located = target if scope == "ad_group" else None
    located_judgement = result
    if scope == "shop" and result["significant"]:
        rows = _query({"metrics": ["ad_spend", "net_roas"], "dimensions": ["ad_group"], "filters": flt,
                       "time": _time(window)}, user_id)
        contribs = group_contributions(rows, decomp["net_roas"]["prev"])
        result["ad_group_contributions"] = contribs
        located = contribs[0]["ad_group"]
        # 对贡献最负的广告组单独做一次第一层分解，确认主因
        g = _query({"metrics": METRICS, "filters": {"ad_group": [located]}, "time": _time(window)}, user_id)[0]
        gp, gc = _split(g, METRICS)
        gd = log_decompose(gp, gc)
        if gd is not None:
            gj = judge(gd, _f(gp["ad_orders"]), _f(gc["ad_orders"]))
            if not gj["significant"]:
                # 它已经是店铺下滑的最大来源；自身未达显著时仍取其最负的因子，并在结果里标明
                gj = {**gj, "main_cause": min(FACTORS, key=lambda k: gd["factors"][k]["log_contrib"]),
                      "note": "定位到的广告组自身变化未达显著，主因判断置信度较低"}
            result["located_decomposition"] = {**gd, **gj}
            located_judgement = gj

    if result["significant"]:
        cause = located_judgement["main_cause"]
        action, reason = ACTIONS[cause]
        result["located_ad_group"] = located
        result["located_main_cause"] = cause
        result["recommendation"] = {"action": action, "ad_group_id": located, "reason": reason,
                                    "requires_approval": action == "pause_ad_group"}
    return result
