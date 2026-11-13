"""用 Python 直接从生成器的 CSV 重算部分题目的答案，和 gold SQL 的结果比对（SPEC 第九节"数据生成器交叉验证"）。

重点覆盖"没有参考 MQL"的题（自然月对比、不相邻时间段对比）：这些题没有编译器结果可以对照，
gold SQL 是唯一的一条路径，所以用一条完全不经过 SQL 的路径再算一遍。
另外检查陷阱题是否真的"考得出"陷阱：用错误口径算一遍，看结果是否和正确答案有明显差异。

用法：uv run python -m eval.crosscheck_csv
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from eval.check_reference import load_questions, run_gold
from eval.scoring import compare_results

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "datagen" / "out"


def rd(name: str) -> list[list[str]]:
    with open(OUT / f"{name}.csv", encoding="utf-8") as f:
        return list(csv.reader(f))


D = date.fromisoformat
SEP, OCT = (D("2026-09-01"), D("2026-09-30")), (D("2026-10-01"), D("2026-10-31"))
inside = lambda d, w: w[0] <= d <= w[1]


class Data:
    def __init__(self) -> None:
        shops = rd("shops")
        self.tz = {r[0]: ZoneInfo(r[4]) for r in shops}
        self.cur = {r[0]: r[3] for r in shops}
        self.fx = {(D(r[0]), r[1]): Decimal(r[2]) for r in rd("fx_rates")}
        camp_shop = {r[0]: r[1] for r in rd("ad_campaigns")}
        self.ag = {r[0]: {"campaign": r[1], "sku": r[2], "shop": camp_shop[r[1]]} for r in rd("ad_groups")}
        # 广告日报：一行 = 广告组 × 店铺本地日期
        self.ad = [{"ag": r[0], "d": D(r[1]), "imp": int(r[2]), "clk": int(r[3]), "spend": Decimal(r[4]),
                    "orders": int(r[5]), "sales": Decimal(r[6]), **self.ag[r[0]]} for r in rd("ad_daily_stats")]
        # 订单行：剔除软删除，按店铺本地日期切日；退款按订单行汇总（不扇出），归属下单日
        orders = {r[0]: (r[1], datetime.fromisoformat(r[2]), r[4]) for r in rd("orders")}
        refund = defaultdict(Decimal)
        for _, item, amt, _ in rd("refunds"):
            refund[item] += Decimal(amt)
        self.lines, self.lines_with_deleted = [], []
        for iid, oid, sku, qty, price in rd("order_items"):
            shop, created, deleted = orders[oid]
            line = {"order": oid, "shop": shop, "sku": sku, "amt": int(qty) * Decimal(price), "ref": refund[iid],
                    "d": created.astimezone(self.tz[shop]).date(), "d_utc": created.date()}
            self.lines_with_deleted.append(line)
            if not deleted:
                self.lines.append(line)

    def rate(self, shop: str, d: date) -> Decimal:
        c = self.cur[shop]
        return Decimal(1) if c == "USD" else self.fx[(d, c)]

    def ad_sum(self, field: str, w, **flt) -> Decimal:
        return sum((r[field] * (self.rate(r["shop"], r["d"]) if field in ("spend", "sales") else 1)
                    for r in self.ad if inside(r["d"], w) and all(r[k] in v for k, v in flt.items())), Decimal(0))

    def ord_sum(self, field: str, w, **flt) -> Decimal:
        return sum((r[field] * self.rate(r["shop"], r["d"])
                    for r in self.lines if inside(r["d"], w) and all(r[k] in v for k, v in flt.items())), Decimal(0))

    def order_count(self, w, **flt) -> int:
        return len({r["order"] for r in self.lines if inside(r["d"], w) and all(r[k] in v for k, v in flt.items())})


def expected(db: Data) -> dict[str, list[dict]]:
    """每道题一条与 gold 无关的计算路径。列名不参与比对，只看数值。"""
    out = {}
    sku = {"sku": {"UK-SKU04"}}
    out["C12"] = [{
        "oc_oct": db.order_count(OCT, **sku), "oc_sep": db.order_count(SEP, **sku),
        "rr_oct": db.ord_sum("ref", OCT, **sku) / db.ord_sum("amt", OCT, **sku),
        "rr_sep": db.ord_sum("ref", SEP, **sku) / db.ord_sum("amt", SEP, **sku),
        "ns_oct": db.ord_sum("amt", OCT, **sku) - db.ord_sum("ref", OCT, **sku),
        "ns_sep": db.ord_sum("amt", SEP, **sku) - db.ord_sum("ref", SEP, **sku)}]
    out["C16"] = [{"campaign": c,
                   "acos_oct": db.ad_sum("spend", OCT, campaign={c}) / db.ad_sum("sales", OCT, campaign={c}),
                   "acos_sep": db.ad_sum("spend", SEP, campaign={c}) / db.ad_sum("sales", SEP, campaign={c})}
                  for c in ("UK-C1", "UK-C2", "UK-C3")]
    cur, prev = (D("2026-09-01"), D("2026-09-21")), (D("2026-08-11"), D("2026-08-31"))
    out["C17"] = [{"sp_c": db.ad_sum("spend", cur), "sp_p": db.ad_sum("spend", prev),
                   "sa_c": db.ad_sum("sales", cur), "sa_p": db.ad_sum("sales", prev),
                   "r_c": db.ad_sum("sales", cur) / db.ad_sum("spend", cur),
                   "r_p": db.ad_sum("sales", prev) / db.ad_sum("spend", prev)}]
    nov7, oct7, us = (D("2026-11-01"), D("2026-11-07")), (D("2026-10-01"), D("2026-10-07")), {"shop": {"US"}}
    out["C18"] = [{"aov_n": db.ord_sum("amt", nov7, **us) / db.order_count(nov7, **us),
                   "aov_o": db.ord_sum("amt", oct7, **us) / db.order_count(oct7, **us),
                   "rr_n": db.ord_sum("ref", nov7, **us) / db.ord_sum("amt", nov7, **us),
                   "rr_o": db.ord_sum("ref", oct7, **us) / db.ord_sum("amt", oct7, **us)}]
    jp = {"shop": {"JP"}}
    out["C20"] = [{"acos_o": db.ad_sum("spend", OCT, **jp) / db.ad_sum("sales", OCT, **jp),
                   "acos_s": db.ad_sum("spend", SEP, **jp) / db.ad_sum("sales", SEP, **jp),
                   "cvr_o": db.ad_sum("orders", OCT, **jp) / db.ad_sum("clk", OCT, **jp),
                   "cvr_s": db.ad_sum("orders", SEP, **jp) / db.ad_sum("clk", SEP, **jp),
                   "rr_o": db.ord_sum("ref", OCT, **jp) / db.ord_sum("amt", OCT, **jp),
                   "rr_s": db.ord_sum("ref", SEP, **jp) / db.ord_sum("amt", SEP, **jp)}]
    return out


def trap_bite(db: Data) -> list[str]:
    """陷阱题用错误口径算一遍：差异太小的题考不出它要考的陷阱（参考 CHANGELOG 2026-11-09 T10 的修订）。"""
    def day_orders(shop, d, key):
        return len({r["order"] for r in db.lines if r["shop"] == shop and r[key] == d})

    def pct(right, wrong):
        return f"{(wrong / right - 1) * 100:+.1f}%" if right else "n/a"

    rows = []
    for qid, shop, d in [("T17", "UK", D("2026-09-26")), ("T17 原题", "UK", D("2026-10-31"))]:
        right, wrong = day_orders(shop, d, "d"), day_orders(shop, d, "d_utc")
        rows.append(f"{qid:10s} 时区 {shop} {d} 订单数：本地日期 {right}，按 UTC 日期 {wrong}（{pct(right, wrong)}）")
    for shop in ("US", "UK", "JP"):
        right = day_orders(shop, D("2026-11-01"), "d")
        wrong = day_orders(shop, D("2026-11-01"), "d_utc")
        rows.append(f"T18        时区 {shop} 2026-11-01 订单数：本地日期 {right}，按 UTC 日期 {wrong}（{pct(right, wrong)}）")
    us_day = [r for r in db.lines_with_deleted if r["shop"] == "US" and r["d"] == D("2026-10-10")]
    right = len({r["order"] for r in db.lines if r["shop"] == "US" and r["d"] == D("2026-10-10")})
    rows.append(f"T13        软删除 US 2026-10-10 订单数：剔除后 {right}，不剔除 {len({r['order'] for r in us_day})}"
                f"（{pct(right, len({r['order'] for r in us_day}))}）")
    return rows


def main() -> int:
    db = Data()
    qs = {q["id"]: q for q in load_questions(ROOT / "questions" / "m2_query.yaml")}
    failed = 0
    for qid, rows in expected(db).items():
        ok, why = compare_results(run_gold(qs[qid]["gold_sql"]), rows)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {qid}  gold vs CSV: {why}")
    print("\n陷阱是否考得出：")
    for line in trap_bite(db):
        print("  " + line)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
