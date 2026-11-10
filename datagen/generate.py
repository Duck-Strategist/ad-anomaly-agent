"""合成跨境电商数据生成器。

用法：uv run python -m datagen.generate
输出：
  datagen/out/*.csv                 业务表数据（由 datagen.load 导入数据库）
  eval/ground_truth/events.json     埋入事件真值（只给评测用，不进业务库）

设计要点：
  - 固定随机种子，结果可复现。
  - 广告报表和订单是两套口径：报表按点击日归因，实际下单可能晚 0–2 天。
  - 四个陷阱：软删除（重复订单）、多币种、时区（created_at 存 UTC）、退款（单独表、部分退款、滞后）。
"""

from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "datagen" / "config.yaml"
OUT_DIR = ROOT / "datagen" / "out"
TRUTH_PATH = ROOT / "eval" / "ground_truth" / "events.json"

# 事件类型 → 归因真值
EXPECTED = {
    "cvr_drop": {"main_cause": "cvr", "suggest_pause": True},
    "refund_spike": {"main_cause": "refund", "suggest_pause": False},
    "aov_drop": {"main_cause": "ad_aov", "suggest_pause": False},
}


def money(x: float, currency: str) -> Decimal:
    """按币种取整：日元到个位，其他到分。"""
    q = Decimal("1") if currency == "JPY" else Decimal("0.01")
    return Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # 大均值用正态近似
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def binomial(rng: random.Random, n: int, p: float) -> int:
    p = min(max(p, 0.0), 1.0)
    return sum(1 for _ in range(n) if rng.random() < p)


@dataclass
class Sku:
    sku_id: str
    shop_id: str
    currency: str
    list_price: Decimal
    organic_base: float
    refund_base: float


@dataclass
class AdGroup:
    ad_group_id: str
    campaign_id: str
    sku: Sku
    impr_base: float
    ctr: float
    cpc: float
    cvr: float
    daily_budget: Decimal


@dataclass
class Tables:
    shops: list = field(default_factory=list)
    products: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    order_items: list = field(default_factory=list)
    refunds: list = field(default_factory=list)
    ad_campaigns: list = field(default_factory=list)
    ad_groups: list = field(default_factory=list)
    ad_daily_stats: list = field(default_factory=list)
    fx_rates: list = field(default_factory=list)
    user_shop_permissions: list = field(default_factory=list)


class Generator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rng = random.Random(cfg["seed"])
        self.start: date = cfg["start_date"]
        self.end: date = cfg["end_date"]
        self.days = [self.start + timedelta(d) for d in range((self.end - self.start).days + 1)]
        self.t = Tables()
        self.shop_cfg = {s["shop_id"]: s for s in cfg["shops"]}
        self.skus: dict[str, list[Sku]] = {}
        self.groups: dict[str, list[AdGroup]] = {}
        self.events = cfg["events"]
        self._order_id = 100000
        self._item_id = 500000
        self._refund_id = 900000

    # ---------- 事件效果 ----------
    def effect(self, shop_id: str, sku_id: str, local_day: date, key: str) -> float:
        mult = 1.0
        for e in self.events:
            sku_of_event = f"{e['shop']}-SKU{e['ad_group']:02d}"
            if e["shop"] == shop_id and sku_of_event == sku_id and e["start"] <= local_day <= e["end"]:
                mult *= e["effects"].get(key, 1.0)
        return mult

    def override(self, shop_id: str, sku_id: str, local_day: date, key: str) -> float | None:
        """事件期间直接指定的取值（如 refund_rate），没有则返回 None。"""
        for e in self.events:
            if (e["shop"] == shop_id and f"{e['shop']}-SKU{e['ad_group']:02d}" == sku_id
                    and e["start"] <= local_day <= e["end"] and key in e["effects"]):
                return e["effects"][key]
        return None

    # ---------- 维度 ----------
    def build_dimensions(self) -> None:
        rng, cfg = self.rng, self.cfg
        for s in cfg["shops"]:
            sid, cur = s["shop_id"], s["currency"]
            self.t.shops.append([sid, s["name"], s["marketplace"], cur, s["timezone"]])
            skus = []
            for i in range(1, s["n_skus"] + 1):
                lo, hi = s["price_range"]
                price = rng.uniform(lo, hi)
                price = round(price, -1) if cur == "JPY" else round(price, 0) - 0.01
                sku = Sku(
                    sku_id=f"{sid}-SKU{i:02d}", shop_id=sid, currency=cur,
                    list_price=money(price, cur),
                    organic_base=rng.uniform(*cfg["orders"]["organic_per_sku_day"]),
                    refund_base=rng.uniform(*cfg["refunds"]["base_rate_range"]),
                )
                skus.append(sku)
                self.t.products.append([sku.sku_id, sid, f"{sid} Product {i:02d}", sku.list_price])
            self.skus[sid] = skus

            for c in range(1, s["n_campaigns"] + 1):
                self.t.ad_campaigns.append([f"{sid}-C{c}", sid, f"{sid} Campaign {c}"])

            groups = []
            a = cfg["ads"]
            hero = {e["ad_group"] for e in self.events if e["shop"] == sid}
            for i in range(1, s["n_ad_groups"] + 1):
                sku = skus[i - 1]  # 1 组 1 SKU：AGnn 推 SKUnn
                cpc = rng.uniform(*s["cpc_range"])
                g = AdGroup(
                    ad_group_id=f"{sid}-AG{i:02d}",
                    campaign_id=f"{sid}-C{(i - 1) % s['n_campaigns'] + 1}",
                    sku=sku,
                    impr_base=rng.uniform(*a["impressions_range"]),
                    ctr=rng.uniform(*a["ctr_range"]),
                    cpc=cpc,
                    cvr=rng.uniform(*a["cvr_range"]),
                    daily_budget=money(cpc * 120, cur),
                )
                groups.append(g)

            # 主力广告组：把曝光调到"每个主力组占店铺点击的 hero_click_share"
            share = a.get("hero_click_share")
            if share and hero:
                others = sum(g.impr_base * g.ctr for i, g in enumerate(groups, 1) if i not in hero)
                hero_clicks = share / (1 - share * len(hero)) * others
                for i, g in enumerate(groups, 1):
                    if i in hero:
                        g.impr_base = hero_clicks / g.ctr
                        g.daily_budget = money(hero_clicks * g.cpc * 1.5, cur)

            for g in groups:
                self.t.ad_groups.append([g.ad_group_id, g.campaign_id, g.sku.sku_id, "enabled", g.daily_budget, None])
            self.groups[sid] = groups

        for u in cfg["users"]:
            for sid in u["shops"]:
                self.t.user_shop_permissions.append([u["user_id"], sid, u["role"]])

    def build_fx(self) -> None:
        rng = self.rng
        # 汇率表多给几天余量，覆盖时区换算后可能落在范围外的日期
        fx_days = [self.start - timedelta(1)] + self.days + [self.end + timedelta(1)]
        for cur, spec in self.cfg["fx"].items():
            rate = spec["start"]
            for d in fx_days:
                rate *= math.exp(rng.gauss(0, spec["daily_sigma"]))
                self.t.fx_rates.append([d, cur, f"{rate:.8f}"])
        for d in fx_days:
            self.t.fx_rates.append([d, "USD", "1.00000000"])

    # ---------- 订单 ----------
    def local_to_utc(self, local_day: date, tz: str) -> datetime:
        seconds = self.rng.randrange(0, 86400)
        local_dt = datetime.combine(local_day, time()) + timedelta(seconds=seconds)
        return local_dt.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)

    def unit_price(self, sku: Sku, local_day: date) -> Decimal:
        return money(float(sku.list_price) * self.effect(sku.shop_id, sku.sku_id, local_day, "price"), sku.currency)

    def add_order(self, shop_id: str, local_day: date, lines: list[tuple[Sku, int, Decimal]]) -> None:
        s = self.shop_cfg[shop_id]
        created = self.local_to_utc(local_day, s["timezone"])
        self._order_id += 1
        oid = self._order_id
        self.t.orders.append([oid, shop_id, created.isoformat(), "completed", None])
        item_rows = []
        for sku, qty, price in lines:
            self._item_id += 1
            row = [self._item_id, oid, sku.sku_id, qty, price]
            self.t.order_items.append(row)
            item_rows.append(row)
            self.maybe_refund(self._item_id, sku, qty, price, local_day, created)

        # 陷阱：重复导入的订单，随后被软删除
        if self.rng.random() < self.cfg["orders"]["duplicate_prob"]:
            self._order_id += 1
            dup = self._order_id
            dup_created = created + timedelta(seconds=self.rng.randrange(1, 120))
            self.t.orders.append([dup, shop_id, dup_created.isoformat(), "completed",
                                  (dup_created + timedelta(days=1)).isoformat()])
            for _, _, sku_id, qty, price in item_rows:
                self._item_id += 1
                self.t.order_items.append([self._item_id, dup, sku_id, qty, price])

    def maybe_refund(self, item_id: int, sku: Sku, qty: int, price: Decimal,
                     local_day: date, created: datetime) -> None:
        rcfg, rng = self.cfg["refunds"], self.rng
        fixed = self.override(sku.shop_id, sku.sku_id, local_day, "refund_rate")
        p = fixed if fixed is not None else min(0.6, sku.refund_base * self.effect(sku.shop_id, sku.sku_id, local_day, "refund"))
        if rng.random() >= p:
            return
        line_total = float(price) * qty
        r = rng.random()
        if r < rcfg["full_prob"]:
            parts = [line_total]
        elif r < rcfg["full_prob"] + rcfg["two_partials_prob"]:
            total = line_total * rng.uniform(0.3, 0.9)
            first = total * rng.uniform(0.3, 0.7)
            parts = [first, total - first]
        else:
            parts = [line_total * rng.uniform(0.2, 0.6)]

        refunded_at = created + timedelta(days=rng.uniform(*rcfg["lag_days"]))
        data_end_utc = datetime.combine(self.end + timedelta(1), time(), tzinfo=timezone.utc)
        for amt in parts:
            if refunded_at > data_end_utc:  # 还没发生的退款不生成
                break
            amount = money(amt, sku.currency)
            if amount > 0:
                self._refund_id += 1
                self.t.refunds.append([self._refund_id, item_id, amount, refunded_at.isoformat()])
            refunded_at += timedelta(days=rng.uniform(1, 4))

    # ---------- 主循环 ----------
    def run(self) -> None:
        self.build_dimensions()
        self.build_fx()
        rng, a, o = self.rng, self.cfg["ads"], self.cfg["orders"]

        for sid, groups in self.groups.items():
            cur = self.shop_cfg[sid]["currency"]
            # 广告归因订单：按点击日记入报表，实际下单日 = 点击日 + 延迟
            scheduled: dict[date, list[tuple[Sku, int, Decimal]]] = {}

            for d in self.days:
                weekend = a["weekend_lift"] if d.weekday() >= 5 else 1.0
                for g in groups:
                    sku = g.sku
                    impr = round(g.impr_base * weekend * math.exp(rng.gauss(0, a["daily_noise_sigma"])))
                    clicks = poisson(rng, impr * g.ctr)
                    cpc = g.cpc * math.exp(rng.gauss(0, a["cpc_noise_sigma"]))
                    spend = money(clicks * cpc, cur)
                    cvr = g.cvr * self.effect(sid, sku.sku_id, d, "cvr")
                    n_orders = binomial(rng, clicks, cvr)
                    price = self.unit_price(sku, d)
                    ad_sales = Decimal(0)
                    for _ in range(n_orders):
                        qty = 2 if rng.random() < o["qty2_prob"] else 1
                        ad_sales += price * qty
                        lag = rng.choices(range(len(a["order_lag_probs"])), a["order_lag_probs"])[0]
                        scheduled.setdefault(d + timedelta(lag), []).append((sku, qty, price))
                    self.t.ad_daily_stats.append([g.ad_group_id, d, impr, clicks, spend, n_orders, ad_sales])

                # 当天实际下单：之前点击、今天落地的广告订单
                for sku, qty, price in scheduled.pop(d, []):
                    self.add_order(sid, d, [(sku, qty, price)])

                # 自然订单
                for sku in self.skus[sid]:
                    lam = sku.organic_base * weekend * self.effect(sid, sku.sku_id, d, "organic")
                    for _ in range(poisson(rng, lam)):
                        qty = 2 if rng.random() < o["qty2_prob"] else 1
                        lines = [(sku, qty, self.unit_price(sku, d))]
                        if rng.random() < o["multi_item_prob"]:
                            other = rng.choice([x for x in self.skus[sid] if x is not sku])
                            lines.append((other, 1, self.unit_price(other, d)))
                        self.add_order(sid, d, lines)
            # 数据末尾之后才落地的订单不生成（和真实情况一致：报表有、订单还没到）

    # ---------- 输出 ----------
    def write(self) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, rows in vars(self.t).items():
            with open(OUT_DIR / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for r in rows:
                    w.writerow(["" if v is None else v for v in r])

        TRUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        truth = []
        for e in self.events:
            truth.append({
                "event_id": e["id"],
                "type": e["type"],
                "shop_id": e["shop"],
                "ad_group_id": f"{e['shop']}-AG{e['ad_group']:02d}",
                "sku_id": f"{e['shop']}-SKU{e['ad_group']:02d}",
                "start": e["start"].isoformat(),
                "end": e["end"].isoformat(),
                "effects": e["effects"],
                **EXPECTED[e["type"]],
            })
        TRUTH_PATH.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")

        print("rows:", {k: len(v) for k, v in vars(self.t).items()})


def main() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    g = Generator(cfg)
    g.run()
    g.write()


if __name__ == "__main__":
    main()
