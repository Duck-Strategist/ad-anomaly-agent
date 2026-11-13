"""加载指标目录，并提供"指标 → 用到哪些基础度量 / 能按哪些维度拆"的查询。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

import yaml

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")
_IDENT = re.compile(r"[a-z_]+")


@dataclass(frozen=True)
class Catalog:
    raw: dict

    @property
    def today(self) -> date:
        return self.raw["today"]

    @property
    def data_start(self) -> date:
        return self.raw["data_start"]

    @property
    def refund_settle_days(self) -> int:
        return self.raw["refund_settle_days"]

    @property
    def metrics(self) -> dict:
        return self.raw["metrics"]

    @property
    def measures(self) -> dict:
        return self.raw["measures"]

    @property
    def dimensions(self) -> dict:
        return self.raw["dimensions"]

    @property
    def ambiguous_terms(self) -> dict[str, list[str]]:
        """多口径说法 → 候选指标，例如 "销售额" → [gross_sales, net_sales, ad_sales]。"""
        return self.raw["ambiguous_terms"]

    def measures_of(self, metric: str) -> list[str]:
        """公式里引用到的基础度量，例如 roas → [ad_sales, ad_spend]。"""
        return [t for t in _IDENT.findall(self.metrics[metric]["formula"]) if t in self.measures]

    def sources_of(self, metric: str) -> set[str]:
        out = set()
        for m in self.measures_of(metric):
            src = self.measures[m]["source"]
            # net_ad_sales 的子查询以广告日报为主体，通过"1 组 1 SKU"把订单侧的退款率挂上来，
            # 所以按维度来看它属于广告侧（可以按广告组 / 广告活动拆）
            out |= {"ad"} if src == "special" else {src}
        return out

    def allowed_dimensions(self, metric: str) -> list[str]:
        """一个指标能按哪些维度拆：它用到的每一侧数据都必须有这个维度。"""
        srcs = self.sources_of(metric)
        return [d for d, spec in self.dimensions.items() if srcs <= set(spec["sources"])]


@cache
def load_catalog() -> Catalog:
    return Catalog(yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")))
