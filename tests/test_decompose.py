"""decompose_roas 测试：数学性质（单元）+ 埋入事件真值 + 误报率（集成）。"""

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from adagent.attribution.decompose import (Window, decompose_roas, group_contributions, judge, scan_threshold,
                                           log_decompose)

TRUTH = json.loads((Path(__file__).resolve().parent.parent / "eval" / "ground_truth" / "events.json")
                   .read_text(encoding="utf-8"))


# ---------- 数学性质 ----------

def m(cvr, aov, cpc, refund):
    roas = cvr * aov / cpc
    return {"cvr": cvr, "ad_aov": aov, "cpc": cpc, "roas": roas, "net_roas": roas * refund}


def test_log_decomposition_is_exact():
    d = log_decompose(m(0.10, 30, 0.8, 0.96), m(0.06, 27, 0.9, 0.90))
    assert abs(d["residual"]) < 1e-12
    assert abs(sum(f["share"] for f in d["factors"].values()) - 1) < 1e-12


def test_cpc_increase_counts_as_negative():
    d = log_decompose(m(0.10, 30, 0.8, 0.96), m(0.10, 30, 1.0, 0.96))
    assert d["factors"]["cpc"]["log_contrib"] < 0
    assert judge(d, 1000, 1000)["main_cause"] == "cpc"


def test_zero_data_cannot_be_decomposed():
    assert log_decompose(m(0.1, 30, 0.8, 0.96), {"cvr": 0, "ad_aov": None, "cpc": 1, "roas": 0, "net_roas": 0}) is None


def test_significance_depends_on_volume():
    d = log_decompose(m(0.10, 30, 0.8, 0.96), m(0.08, 30, 0.8, 0.96))   # −22%
    assert judge(d, 1000, 800)["significant"]          # 大组：z ≈ −6.6
    assert not judge(d, 30, 24)["significant"]         # 小组：同样幅度只是噪声（z ≈ −1.1）


def test_improvement_is_never_a_decline():
    d = log_decompose(m(0.08, 30, 0.8, 0.96), m(0.10, 30, 0.8, 0.96))
    assert not judge(d, 1000, 1000)["significant"]


def test_group_contributions_sum_to_shop_change():
    groups = [  # (spend_prev, net_roas_prev, spend_cur, net_roas_cur)
        ("A", 100, 4.0, 120, 2.0), ("B", 300, 3.0, 280, 3.1), ("C", 50, 6.0, 60, 5.5)]
    rows = [{"ad_group": g, "ad_spend_prev": sp, "net_roas_prev": rp, "ad_spend": sc, "net_roas": rc}
            for g, sp, rp, sc, rc in groups]
    R_prev = sum(sp * rp for _, sp, rp, _, _ in groups) / sum(sp for _, sp, *_ in groups)
    R_cur = sum(sc * rc for *_, sc, rc in groups) / sum(sc for *_, sc, _ in groups)
    contribs = group_contributions(rows, R_prev)
    assert abs(sum(c["contribution"] for c in contribs) - (R_cur - R_prev)) < 1e-12
    assert contribs[0]["ad_group"] == "A"


# ---------- 埋入事件：广告组层面全部要对 ----------

def window_of(e):
    return Window(date.fromisoformat(e["start"]), date.fromisoformat(e["end"]))


@pytest.mark.parametrize("e", TRUTH, ids=[e["event_id"] for e in TRUTH])
def test_event_at_ad_group_level(e):
    r = decompose_roas("ad_group", e["ad_group_id"], window_of(e), "u_admin")
    assert r["significant"], r.get("z")
    assert r["main_cause"] == e["main_cause"]
    assert r["recommendation"]["requires_approval"] == e["suggest_pause"]
    assert abs(r["decomposition"]["residual"]) < 1e-4   # 只剩 net_roas 6 位小数和金额合计 2 位小数的舍入


# ---------- 店铺层面：记录哪些事件能从大盘发现 ----------

SHOP_LEVEL_DETECTABLE = {"E1", "E6"}   # 店铺整体显著；其余事件在店铺层面接近噪声，靠下钻发现（SPEC 第七节）


@pytest.mark.parametrize("e", TRUTH, ids=lambda e: e["event_id"])
def test_event_at_shop_level_locates_ad_group(e):
    r = decompose_roas("shop", e["shop_id"], window_of(e), "u_admin")
    assert r["significant"], r.get("z")
    assert r["found_by"] == ("shop" if e["event_id"] in SHOP_LEVEL_DETECTABLE else "drilldown")
    assert r["located_ad_group"] == e["ad_group_id"]
    assert r["located_main_cause"] == e["main_cause"]
    assert r["recommendation"]["requires_approval"] == e["suggest_pause"]


def test_scan_threshold_is_bonferroni():
    assert scan_threshold(1) == 3.0
    assert abs(scan_threshold(10) - 3.64) < 0.01      # 单组误报率 Φ(−3)/10
    assert scan_threshold(30) > scan_threshold(10)    # 扫得越多，单组门槛越严


# ---------- 误报：没有事件的周不应报显著下滑 ----------

def non_event_weeks(shop):
    ev = [(date.fromisoformat(e["start"]), date.fromisoformat(e["end"])) for e in TRUTH if e["shop_id"] == shop]
    mon = date(2026, 8, 24)
    while mon + timedelta(6) <= date(2026, 11, 8):
        if not any(s <= mon + timedelta(6) and en >= mon - timedelta(7) for s, en in ev):
            yield Window(mon, mon + timedelta(6))
        mon += timedelta(7)


@pytest.mark.parametrize("shop", ["US", "UK", "JP"])
def test_no_false_alarm_at_shop_level(shop):
    """包括下钻：没有事件的周，店铺整体和下钻都不应报显著下滑。
    US 08-24 周里 US-AG06 有一个 z = −3.46 的随机下滑，单组门槛（−3）会误报，校正后的扫描门槛（−3.64）不会。"""
    hits = [w for w in non_event_weeks(shop) if decompose_roas("shop", shop, w, "u_admin")["significant"]]
    assert hits == []


# ---------- 权限照常生效 ----------

def test_permission_enforced():
    with pytest.raises(PermissionError):
        decompose_roas("shop", "UK", Window(date(2026, 11, 2), date(2026, 11, 8)), "u_ops_us")
