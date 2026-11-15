"""归因解释：数字和结论由代码按模板生成，模型只写最后一段"建议和说明"（2026-11-14，SPEC Q21）。

为什么：数字本来就是 decompose_roas 算的，让模型转述只多一个出错点；编数字、说反方向是审批场景下最危险的错误。
模板覆盖哪些形态：
  - 无显著下滑（店铺：附带下钻扫描结果；广告组）
  - 直接问广告组 / 商品，显著下滑
  - 店铺显著 → 下滑最多的广告组（可能缺少自己的分解、可能置信度较低、主因可能和店铺不同）
  - 店铺不显著、下钻才发现（可能另有其他广告组）
  - 可选部件：次要因、抵消因素、置信度提醒、建议动作、退款未完全入账提醒
数据不足（insufficient_data）在工作流里提前返回，不进入这里。
decompose_roas 出现这里不认识的字段或取值时抛 TemplateError：宁可报错，也不静默漏说（tests/test_explain.py）。
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

from .decompose import ACTIONS, FACTORS, SECONDARY_SHARE

CAUSE_NAMES = {"cvr": "转化率下降", "ad_aov": "广告客单价下降", "cpc": "点击成本上升", "refund": "退款增加"}
FACTOR_NAMES = {"cvr": "转化率", "ad_aov": "广告客单价", "cpc": "点击成本", "refund": "退款扣减比例"}

ACTION_TEXT = {
    "pause_ad_group": "建议暂停广告组 {ag}（执行前需要你审批）。",
    "check_product_quality": "建议检查广告组 {ag} 对应商品的质量和退款原因，不建议暂停广告。",
    "explain_promotion": "建议确认广告组 {ag} 的商品近期是否降价促销；转化没有变差，不建议暂停。",
    "review_bids": "建议检查广告组 {ag} 的竞价设置；系统暂不支持自动调价。",
}

# decompose_roas 结果里模板认识的字段。新增字段必须先在这里登记并决定怎么说（或明确不说）
RESULT_KEYS = {"scope", "target", "window", "previous_window", "decomposition", "z", "significant", "main_cause",
               "secondary_causes", "ad_group_contributions", "located_decomposition", "found_by", "drilldown",
               "shop_significant", "note", "located_ad_group", "located_main_cause", "recommendation"}
DECOMP_KEYS = {"total_log_change", "total_change_pct", "net_roas", "factors", "residual",
               "z", "significant", "main_cause", "secondary_causes", "note"}
FACTOR_KEYS = {"prev", "cur", "log_contrib", "share"}
DRILLDOWN_KEYS = {"groups_scanned", "z_threshold", "findings"}
RECOMMENDATION_KEYS = {"action", "ad_group_id", "reason", "requires_approval"}


class TemplateError(ValueError):
    """归因结果的字段或形态超出了模板覆盖的范围。"""


# ---------------- 校验 ----------------

def _check_keys(d: dict, known: set[str], where: str) -> None:
    unknown = set(d) - known
    if unknown:
        raise TemplateError(f"{where}: unknown fields {sorted(unknown)}")


def _check_decomposition(d: dict, where: str) -> None:
    _check_keys(d, DECOMP_KEYS, where)
    if set(d["factors"]) != set(FACTORS):
        raise TemplateError(f"{where}: factors {sorted(d['factors'])} != {sorted(FACTORS)}")
    for k, f in d["factors"].items():
        _check_keys(f, FACTOR_KEYS, f"{where}.factors.{k}")
    causes = [d.get("main_cause"), *d.get("secondary_causes", [])]
    if any(c is not None and c not in FACTORS for c in causes):
        raise TemplateError(f"{where}: unknown cause in {causes}")


def validate(r: dict) -> None:
    if r.get("insufficient_data"):
        raise TemplateError("insufficient_data must be handled before explanation")
    _check_keys(r, RESULT_KEYS, "result")
    if r["scope"] not in ("shop", "ad_group"):
        raise TemplateError(f"unknown scope {r['scope']!r}")
    if r.get("found_by") not in (None, "shop", "drilldown"):
        raise TemplateError(f"unknown found_by {r['found_by']!r}")
    _check_decomposition(r["decomposition"], "decomposition")
    if "located_decomposition" in r:
        _check_decomposition(r["located_decomposition"], "located_decomposition")
    if "drilldown" in r:
        _check_keys(r["drilldown"], DRILLDOWN_KEYS, "drilldown")
    rec = r.get("recommendation")
    if rec is not None:
        _check_keys(rec, RECOMMENDATION_KEYS, "recommendation")
        if rec["action"] not in ACTION_TEXT:
            raise TemplateError(f"unknown action {rec['action']!r}")
    if r["significant"]:
        if r.get("located_main_cause") not in FACTORS or not r.get("located_ad_group") or rec is None:
            raise TemplateError("significant result without located cause / ad group / recommendation")
        if r["scope"] == "shop" and r.get("found_by") is None:
            raise TemplateError("significant shop result without found_by")
    elif r.get("found_by") or rec is not None:
        raise TemplateError("non-significant result with found_by / recommendation")


# ---------------- 格式 ----------------

def _pct(x: float) -> str:
    x = round(x, 1)
    return "0.0%" if x == 0 else f"{x:+.1f}%"


def _value(k: str, v: float) -> str:
    if k == "cvr":
        return f"{v * 100:.2f}%"
    if k == "ad_aov":
        return f"${v:.2f}"
    if k == "cpc":
        return f"${v:.3f}"
    return f"{_shown(k, v) * 100:.1f}%"      # refund：退款因子 = net_roas / roas = 1 − 退款扣减比例


def _effect(log_contrib: float) -> str:
    return _pct((math.exp(log_contrib) - 1) * 100)


def _shown(k: str, v: float) -> float:
    """卖家看到的那个数（升 / 降按它判断）。退款因子越小、退款扣减比例越大，方向相反。"""
    return max(0.0, 1 - v) if k == "refund" else v


def _factor_sentence(k: str, f: dict) -> str:
    a, b = _value(k, f["prev"]), _value(k, f["cur"])
    up = _shown(k, f["cur"]) > _shown(k, f["prev"])
    move = "基本不变" if a == b else f"从 {a} {'升' if up else '降'}到 {b}"
    return f"{FACTOR_NAMES[k]}{move}，单看这一项，净 ROAS 变化 {_effect(f['log_contrib'])}"


def _roas_change(obj: str, d: dict, verb: str = "降到") -> str:
    nr = d["net_roas"]
    return f"{obj}的净 ROAS 从 {nr['prev']:.2f} {verb} {nr['cur']:.2f}（{_pct(d['total_change_pct'])}）"


def _cause_lines(d: dict, main: str, secondary: list[str], who: str = "") -> list[str]:
    lines = [f"{who}主因：{CAUSE_NAMES[main]}。{_factor_sentence(main, d['factors'][main])}。"]
    for k in secondary:
        lines.append(f"{who}次要因：{CAUSE_NAMES[k]}。{_factor_sentence(k, d['factors'][k])}。")
    total = abs(d["total_log_change"])
    for k in FACTORS:
        f = d["factors"][k]
        if f["log_contrib"] > 0 and total and f["log_contrib"] / total >= SECONDARY_SHARE:
            lines.append(f"{who}抵消因素：{_factor_sentence(k, f)}，部分抵消了下滑。")
    return lines


def _obj(scope: str, target: str) -> str:
    return f"店铺 {target} " if scope == "shop" else f"广告组 {target} "


def basis_line(r: dict) -> str:
    w, pw = r["window"], r["previous_window"]
    return (f"分析口径：净 ROAS（扣除退款后的广告投入产出比）；"
            f"对比窗口：{w[0]}~{w[1]} vs {pw[0]}~{pw[1]}。")


def _refund_line(r: dict, today: date, settle_days: int) -> str | None:
    """同 compiler 的 refund_incomplete：窗口结束日在 today − settle_days 之后，退款可能还没全部发生（SPEC Q11）。"""
    end = date.fromisoformat(r["window"][1])
    if end <= today - timedelta(settle_days):
        return None
    first = max(date.fromisoformat(r["previous_window"][0]), today - timedelta(settle_days - 1))
    return (f"数据提醒：{first.isoformat()} 及之后下单的订单，退款可能还没全部发生，"
            f"这部分的退款扣减偏少，净 ROAS 可能偏高。")


# ---------------- 模板 ----------------

def render_facts(r: dict, today: date, settle_days: int) -> str:
    """decompose_roas 的结果 → 卖家看到的结论段（不含模型写的建议）。"""
    validate(r)
    d = r["decomposition"]
    obj = _obj(r["scope"], r["target"])
    lines = [basis_line(r)]

    if not r["significant"]:
        why = "，考虑到订单量，这个幅度仍在正常波动范围内" if d["total_change_pct"] < 0 else ""
        lines.append(f"结论：{_roas_change(obj, d, '变为')}，没有达到显著下滑的标准{why}，不需要处理。")
        if "drilldown" in r:
            lines.append(f"已逐个检查该店铺的 {r['drilldown']['groups_scanned']} 个广告组，也没有发现显著下滑的广告组。")

    elif r["scope"] == "ad_group":
        lines.append(f"结论：{_roas_change(obj, d)}，属于显著下滑。")
        lines += _cause_lines(d, r["main_cause"], r.get("secondary_causes", []))

    elif r["found_by"] == "shop":
        ag = r["located_ad_group"]
        lines.append(f"结论：{_roas_change(obj, d)}，属于显著下滑。")
        lines += _cause_lines(d, r["main_cause"], r.get("secondary_causes", []), "店铺层面")
        shop_delta = d["net_roas"]["cur"] - d["net_roas"]["prev"]
        contrib = next((c["contribution"] for c in r.get("ad_group_contributions", []) if c["ad_group"] == ag), None)
        share = ""
        if contrib is not None and shop_delta:
            s = contrib / shop_delta * 100
            share = f"，占店铺净 ROAS 下降幅度的 {s:.1f}%" + ("（其他广告组合计是上升的）" if s > 100 else "")
        lines.append(f"下滑最多的广告组是 {ag}{share}。")
        ld = r.get("located_decomposition")
        if ld is None:
            lines.append(f"{ag} 自身的数据不足以单独分解，原因按店铺层面判断。")
        else:
            lines.append(f"{_roas_change(f'{ag} ', ld)}。")
            lines += _cause_lines(ld, ld["main_cause"], ld.get("secondary_causes", []), f"{ag} 的")
            if not ld["significant"]:
                lines.append(f"置信度提醒：{ag} 自身的变化没有达到显著标准，它的主因判断置信度较低。")
            if ld["main_cause"] != r["main_cause"]:
                lines.append(f"注意：店铺层面的主因（{CAUSE_NAMES[r['main_cause']]}）"
                             f"和 {ag} 的主因（{CAUSE_NAMES[ld['main_cause']]}）不同。")

    else:   # drilldown
        ag, dd, ld = r["located_ad_group"], r["drilldown"], r["located_decomposition"]
        lines.append(f"结论：{_roas_change(obj, d, '变为')}，整体没有达到显著下滑的标准；"
                     f"逐个检查 {dd['groups_scanned']} 个广告组后，发现 {ag} 显著下滑。")
        lines.append(f"{_roas_change(f'{ag} ', ld)}。")
        lines += _cause_lines(ld, ld["main_cause"], ld.get("secondary_causes", []), f"{ag} 的")
        others = [f["ad_group"] for f in dd["findings"] if f["ad_group"] != ag]
        if others:
            lines.append(f"另外，{', '.join(others)} 也显著下滑，本次只针对下滑最明显的 {ag} 给出建议。")

    rec = r.get("recommendation")
    if rec:
        lines.append(f"建议动作：{ACTION_TEXT[rec['action']].format(ag=rec['ad_group_id'])}理由：{rec['reason']}。")
    refund = _refund_line(r, today, settle_days)
    if refund:
        lines.append(refund)
    return "\n".join(lines)


def advice_context(r: dict) -> dict:
    """给模型写建议用的结构化摘要（不含数字：数字都在结论段里）。

    只放有值的字段。初版对广告组问题也传了 "shop_main_cause": null，模型把它读成"店铺层面没有定位到主因"，
    写进了建议段，而这次根本没分析店铺（2026-11-14 读重跑输出时发现）。"""
    ld = r.get("located_decomposition") or {}
    rec = r.get("recommendation") or {}
    ctx = {"analysis_object": _obj(r["scope"], r["target"]).strip(), "significant": r["significant"]}
    if r.get("found_by") == "drilldown":
        ctx["how_found"] = "店铺整体不显著，逐个检查广告组后发现"
    if r.get("found_by") == "shop" and r.get("main_cause"):
        ctx["shop_main_cause"] = CAUSE_NAMES[r["main_cause"]]
    if r.get("located_ad_group"):
        ctx |= {"located_ad_group": r["located_ad_group"],
                "located_main_cause": CAUSE_NAMES[r["located_main_cause"]],
                "recommended_action": rec.get("action")}
    if ld and not ld.get("significant", True):
        ctx["located_low_confidence"] = True
    return ctx


# ---------------- 模型段落里的数字检查 ----------------

_ID = re.compile(r"[A-Z]{2}-(?:AG|SKU)\d+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """抽出数字并规范化（"58.30" 和 "58.3" 视为同一个）。广告组 / SKU 编号和日期不算数字。"""
    text = _DATE.sub(" ", _ID.sub(" ", text))
    return {repr(float(x)) for x in _NUM.findall(text)}


def new_numbers(advice: str, facts: str) -> list[str]:
    """模型段落里出现、但结论段里没有的数字。不为空就回传模型重写（SPEC Q21）。
    局限：中文数字（"三成""一半"）不检查。"""
    return sorted(numbers_in(advice) - numbers_in(facts), key=float)
