"""归因解释模板（SPEC Q21）：每个结论分支一条测试；不认识的字段 / 形态必须报错；模型段落的数字检查。

前半部分不需要数据库：分解用 log_decompose / judge 算出来，再按 decompose_roas 的结构拼成结果。
最后一组测试需要数据库：对归因题的每个 reference_plan 跑真实的 decompose_roas，确认模板都认识、方向都说对。
"""

import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from adagent.attribution.decompose import ACTIONS, Window, decompose_roas, judge, log_decompose
from adagent.attribution.explain import (CAUSE_NAMES, FACTOR_NAMES, TemplateError, new_numbers, numbers_in,
                                         render_facts)

TODAY = date(2026, 11, 8)
SETTLE = 14
OLD_W = {"window": ["2026-09-28", "2026-10-04"], "previous_window": ["2026-09-21", "2026-09-27"]}


def m(cvr, aov, cpc, refund):
    roas = cvr * aov / cpc
    return {"cvr": cvr, "ad_aov": aov, "cpc": cpc, "roas": roas, "net_roas": roas * refund}


BASE = m(0.10, 30, 0.8, 0.97)
CVR_DROP = m(0.04, 30.5, 0.8, 0.97)           # 主因转化率，客单价略升
AOV_DROP = m(0.10, 21, 0.8, 0.97)
REFUND_UP = m(0.10, 30, 0.8, 0.70)
MIXED = m(0.06, 30, 0.95, 0.97)               # 转化率主因 + 点击成本次要因
OFFSET = m(0.05, 38, 0.8, 0.97)               # 转化率大跌，客单价明显上升（抵消）
FLAT = m(0.098, 30.2, 0.8, 0.97)


def dec(prev, cur, orders=(400, 400), **kw):
    d = log_decompose(prev, cur)
    return {**d, **judge(d, *orders, **kw)}


def ad_group_result(cur, target="JP-AG04", window=OLD_W, orders=(400, 400)):
    d = dec(BASE, cur, orders)
    r = {"scope": "ad_group", "target": target, **window, "decomposition": {k: d[k] for k in d if k not in ("z", "significant", "main_cause", "secondary_causes")},
         "z": d["z"], "significant": d["significant"], "main_cause": d["main_cause"], "secondary_causes": d["secondary_causes"]}
    if r["significant"]:
        _recommend(r, target, d["main_cause"])
    return r


def _recommend(r, ag, cause):
    action, reason = ACTIONS[cause]
    r |= {"located_ad_group": ag, "located_main_cause": cause,
          "recommendation": {"action": action, "ad_group_id": ag, "reason": reason,
                             "requires_approval": action == "pause_ad_group"}}


def shop_result(shop_cur, group_cur, found_by, ag="US-AG07", group_orders=(400, 400), window=OLD_W, others=()):
    sd = dec(BASE, shop_cur, (3000, 3000))
    r = {"scope": "shop", "target": ag[:2], **window,
         "decomposition": {k: sd[k] for k in ("total_log_change", "total_change_pct", "net_roas", "factors", "residual")},
         "z": sd["z"], "significant": sd["significant"], "main_cause": sd["main_cause"],
         "secondary_causes": sd["secondary_causes"]}
    if found_by is None:
        r["drilldown"] = {"groups_scanned": 10, "z_threshold": 3.64, "findings": []}
        return r
    gd = dec(BASE, group_cur, group_orders) if group_cur else None
    if found_by == "shop":
        delta = sd["net_roas"]["cur"] - sd["net_roas"]["prev"]
        r["ad_group_contributions"] = [{"ad_group": ag, "contribution": delta * 0.8}, {"ad_group": "X", "contribution": delta * 0.2}]
        if gd is not None:
            if not gd["significant"]:
                gd["main_cause"] = min(gd["factors"], key=lambda k: gd["factors"][k]["log_contrib"])
                gd["note"] = "定位到的广告组自身变化未达显著，主因判断置信度较低"
            r["located_decomposition"] = gd
        r["found_by"] = "shop"
        cause = (gd or sd)["main_cause"]
    else:
        r["drilldown"] = {"groups_scanned": 10, "z_threshold": 3.64,
                          "findings": [{"ad_group": a, "z": -5.0, "main_cause": "cvr"} for a in (ag, *others)]}
        r |= {"shop_significant": False, "significant": True, "found_by": "drilldown", "located_decomposition": gd,
              "note": "店铺整体变化不显著……"}
        cause = gd["main_cause"]
    _recommend(r, ag, cause)
    return r


def facts(r):
    return render_facts(r, TODAY, SETTLE)


# ---------------- 各分支 ----------------

def test_ad_group_not_significant():
    t = facts(ad_group_result(FLAT))
    assert "没有达到显著下滑的标准" in t and "正常波动" in t and "不需要处理" in t
    assert "主因" not in t and "建议动作" not in t


def test_ad_group_significant_with_pause():
    t = facts(ad_group_result(CVR_DROP))
    assert "属于显著下滑" in t and "主因：转化率下降。转化率从 10.00% 降到 4.00%" in t
    assert "建议暂停广告组 JP-AG04（执行前需要你审批）" in t


@pytest.mark.parametrize("cur, cause", [(CVR_DROP, "cvr"), (AOV_DROP, "ad_aov"), (REFUND_UP, "refund"),
                                        (m(0.10, 30, 1.3, 0.97), "cpc")])
def test_every_cause_has_matching_action(cur, cause):
    r = ad_group_result(cur)
    assert r["main_cause"] == cause
    t = facts(r)
    assert f"主因：{CAUSE_NAMES[cause]}" in t and ACTIONS[cause][1] in t


def test_refund_direction_is_stated_as_seller_sees_it():
    """退款因子下降 = 退款扣减比例上升。初版模板按因子方向写成了"降到"（2026-11-14 用真实数据渲染时发现）。"""
    t = facts(ad_group_result(REFUND_UP))
    assert "退款扣减比例从 3.0% 升到 30.0%" in t


def test_secondary_and_offset():
    t = facts(ad_group_result(MIXED))
    assert "主因：转化率下降" in t and "次要因：点击成本上升。点击成本从 $0.800 升到 $0.950" in t
    t = facts(ad_group_result(OFFSET))
    assert "抵消因素：广告客单价从 $30.00 升到 $38.00" in t and "部分抵消了下滑" in t


def test_shop_not_significant_reports_drilldown_scan():
    t = facts(shop_result(FLAT, None, None))
    assert "店铺 US 的净 ROAS" in t and "已逐个检查该店铺的 10 个广告组" in t and "建议动作" not in t


def test_shop_significant_same_cause():
    t = facts(shop_result(m(0.08, 30, 0.8, 0.97), CVR_DROP, "shop"))
    assert "店铺层面主因：转化率下降" in t and "下滑最多的广告组是 US-AG07，占店铺净 ROAS 下降幅度的 80.0%" in t
    assert "US-AG07 的主因：转化率下降" in t and "注意" not in t and "置信度提醒" not in t


def test_shop_significant_group_cause_differs():
    t = facts(shop_result(m(0.08, 30, 0.8, 0.97), REFUND_UP, "shop"))
    assert "注意：店铺层面的主因（转化率下降）和 US-AG07 的主因（退款增加）不同" in t


def test_shop_significant_group_low_confidence():
    t = facts(shop_result(m(0.08, 30, 0.8, 0.97), m(0.093, 30, 0.8, 0.97), "shop", group_orders=(20, 20)))
    assert "置信度提醒：US-AG07 自身的变化没有达到显著标准" in t


def test_shop_significant_group_without_own_decomposition():
    t = facts(shop_result(m(0.08, 30, 0.8, 0.97), None, "shop"))
    assert "US-AG07 自身的数据不足以单独分解" in t


def test_drilldown_with_other_groups():
    t = facts(shop_result(FLAT, CVR_DROP, "drilldown", others=("US-AG02",)))
    assert "整体没有达到显著下滑的标准；逐个检查 10 个广告组后，发现 US-AG07 显著下滑" in t
    assert "US-AG02 也显著下滑" in t


def test_refund_settlement_note_only_for_recent_windows():
    recent = {"window": ["2026-11-02", "2026-11-08"], "previous_window": ["2026-10-26", "2026-11-01"]}
    assert "数据提醒：2026-10-26 及之后下单的订单" in facts(ad_group_result(CVR_DROP, window=recent))
    assert "数据提醒" not in facts(ad_group_result(CVR_DROP))


# ---------------- 不认识的形态必须报错 ----------------

@pytest.mark.parametrize("mutate", [
    lambda r: r.__setitem__("new_field", 1),
    lambda r: r["decomposition"].__setitem__("new_field", 1),
    lambda r: r["decomposition"]["factors"].__setitem__("tax", {"prev": 1, "cur": 1, "log_contrib": 0, "share": 0}),
    lambda r: r["recommendation"].__setitem__("action", "raise_budget"),
    lambda r: r.__setitem__("found_by", "magic"),
    lambda r: r.__setitem__("scope", "campaign"),
    lambda r: r.__setitem__("located_main_cause", "weather"),
    lambda r: r.__setitem__("insufficient_data", True),
])
def test_unknown_shape_raises(mutate):
    r = ad_group_result(CVR_DROP)
    mutate(r)
    with pytest.raises(TemplateError):
        facts(r)


def test_every_action_and_cause_has_text():
    from adagent.attribution.explain import ACTION_TEXT
    assert set(ACTION_TEXT) == {a for a, _ in ACTIONS.values()}
    assert set(CAUSE_NAMES) == set(FACTOR_NAMES) == set(ACTIONS)


# ---------------- 模型段落的数字检查 ----------------

def test_numbers_check():
    f = "结论：广告组 US-AG07 的净 ROAS 从 6.20 降到 2.73（-56.0%）。对比窗口：2026-11-02~2026-11-08"
    assert numbers_in("US-AG07 在 2026-11-02 之后") == set()
    assert new_numbers("建议先暂停 US-AG07，复查详情页。", f) == []
    assert new_numbers("跌幅 56%", f) == []                                   # 56 和 56.0 是同一个数
    assert new_numbers("净 ROAS 已经跌到 2.7", f) == ["2.7"]                  # 自己四舍五入也算新数字：prompt 要求不写数字
    assert new_numbers("预计恢复后能回到 6.5", f) == ["6.5"]


# ---------------- 真实数据（需要数据库） ----------------

QUESTIONS = Path(__file__).resolve().parent.parent / "eval" / "questions" / "m2_attribution.yaml"
SENTENCE = re.compile(r"(转化率|广告客单价|点击成本|退款扣减比例)(?:从 \S+ ([升降])到 \S+|基本不变)，单看这一项，净 ROAS 变化 (\S+?)%")
HURTS_WHEN = {"转化率": "降", "广告客单价": "降", "点击成本": "升", "退款扣减比例": "升"}


def _plans():
    for q in yaml.safe_load(QUESTIONS.read_text(encoding="utf-8")):
        p = q["reference_plan"]
        yield q["id"], p


@pytest.mark.parametrize("qid, plan", list(_plans()))
def test_real_results_render_and_directions_agree(qid, plan):
    scope, target = plan["scope"], plan["target"]
    if scope == "sku":
        scope, target = "ad_group", target.replace("SKU", "AG")
    r = decompose_roas(scope, target, Window(date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])), "u_admin")
    t = render_facts(r, TODAY, SETTLE)
    found = SENTENCE.findall(t)
    assert bool(found) == r["significant"], qid
    for name, move, effect in found:
        if move and effect != "0.0":
            # 文字里的"升 / 降"和"单看这一项使净 ROAS 变化"的正负号必须一致
            assert (move == HURTS_WHEN[name]) == effect.startswith("-"), (qid, name, move, effect)


def test_advice_context_only_has_fields_that_apply():
    """没分析店铺时不能出现店铺字段：初版传了 shop_main_cause: null，模型写成"店铺层面没有定位到主因"。"""
    from adagent.attribution.explain import advice_context
    ctx = advice_context(ad_group_result(CVR_DROP))
    assert "shop_main_cause" not in ctx and None not in ctx.values()
    assert advice_context(shop_result(m(0.08, 30, 0.8, 0.97), REFUND_UP, "shop"))["shop_main_cause"] == "转化率下降"
    assert "shop_main_cause" not in advice_context(shop_result(FLAT, CVR_DROP, "drilldown"))
    assert set(advice_context(ad_group_result(FLAT))) == {"analysis_object", "significant"}
