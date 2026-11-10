"""MQL：LLM 输出的结构化查询意图。

两层校验：
  1. Pydantic：结构和取值类型（多余字段直接拒绝，防止模型"发明"字段）
  2. validate_against_catalog：指标 / 维度是否存在、维度是否适用于指标、时间是否合法
校验失败抛 MQLError，错误信息写给模型看，要具体到能据此改正。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import Catalog

ShopId = Literal["US", "UK", "JP"]
RangeName = Literal["last_7_days", "last_14_days", "last_30_days", "this_week", "last_week", "custom"]


class MQLError(ValueError):
    """校验 / 编译阶段的错误。errors 会原样回传给模型做自修正。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeSpec(_Strict):
    range: RangeName
    start: date | None = None   # 仅 range=custom 时使用（店铺本地日期，含首尾）
    end: date | None = None
    compare: Literal["previous_period"] | None = None   # 和紧挨着的上一个等长周期对比

    @model_validator(mode="after")
    def _custom_needs_dates(self):
        if self.range == "custom":
            if self.start is None or self.end is None:
                raise ValueError("range=custom requires both start and end")
            if self.start > self.end:
                raise ValueError("start must not be after end")
        elif self.start is not None or self.end is not None:
            raise ValueError("start/end are only allowed when range=custom")
        return self


class Filters(_Strict):
    shop: list[ShopId] | None = None
    sku: list[str] | None = None
    ad_group: list[str] | None = None
    campaign: list[str] | None = None


class OrderBy(_Strict):
    metric: str
    desc: bool = True


class MQL(_Strict):
    metrics: list[str] = Field(min_length=1, max_length=6)
    dimensions: list[str] = Field(default_factory=list, max_length=3)
    filters: Filters = Field(default_factory=Filters)
    time: TimeSpec
    order_by: OrderBy | None = None
    limit: int = Field(default=200, ge=1, le=1000)


def validate_against_catalog(mql: MQL, cat: Catalog) -> None:
    errors: list[str] = []
    for m in mql.metrics:
        if m not in cat.metrics:
            errors.append(f"unknown metric '{m}'. Available metrics: {', '.join(cat.metrics)}")
    for d in mql.dimensions:
        if d not in cat.dimensions:
            errors.append(f"unknown dimension '{d}'. Available dimensions: {', '.join(cat.dimensions)}")
    if errors:
        raise MQLError(errors)

    # 维度（和按维度过滤）必须适用于每个请求的指标
    used_dims = set(mql.dimensions) | {k for k, v in mql.filters.model_dump().items() if v}
    for m in mql.metrics:
        allowed = cat.allowed_dimensions(m)
        for d in sorted(used_dims - set(allowed)):
            errors.append(f"metric '{m}' cannot be split or filtered by '{d}'. "
                          f"Allowed dimensions for '{m}': {', '.join(allowed)}")
    if "date" in mql.dimensions and "week" in mql.dimensions:
        errors.append("use either 'date' or 'week' as a dimension, not both")

    if mql.order_by and mql.order_by.metric not in mql.metrics:
        errors.append(f"order_by metric '{mql.order_by.metric}' must be one of the requested metrics")

    start, end = resolve_window(mql.time, cat.today)
    if end > cat.today:
        errors.append(f"time window ends after today ({cat.today}); data is only available up to today")
    if errors:
        raise MQLError(errors)


def resolve_window(t: TimeSpec, today: date) -> tuple[date, date]:
    """把时间描述换算成具体的店铺本地日期区间（含首尾）。"""
    if t.range == "custom":
        return t.start, t.end
    if t.range.startswith("last_") and t.range.endswith("_days"):
        n = int(t.range.split("_")[1])
        return today - timedelta(n - 1), today
    monday = today - timedelta(today.weekday())
    if t.range == "this_week":
        return monday, today
    if t.range == "last_week":
        return monday - timedelta(7), monday - timedelta(1)
    raise AssertionError(t.range)


def previous_window(start: date, end: date) -> tuple[date, date]:
    """紧挨着的上一个等长周期。"""
    n = (end - start).days + 1
    return start - timedelta(n), start - timedelta(1)
