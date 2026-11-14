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
RangeName = Literal["last_7_days", "last_14_days", "last_30_days", "this_week", "last_week", "calendar", "custom"]
CalendarUnit = Literal["day", "week", "month"]


class MQLError(ValueError):
    """校验 / 编译阶段的错误。errors 会原样回传给模型做自修正。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class OutOfRange(MQLError):
    """时间段整段落在数据范围之外（SPEC Q18 / Q19）。重试后仍如此，应如实回答"没有这个时间段的数据"。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(_Strict):
    """一段时间：本期（TimeSpec）和指定的对比期（SPEC Q20）共用这套写法。"""
    range: RangeName
    start: date | None = None   # 仅 range=custom 时使用（店铺本地日期，含首尾）
    end: date | None = None
    # 仅 range=calendar 时使用（SPEC Q19）：自然日 / 周 / 月。offset 0 = 本期到今天为止，-1 = 上一个，-2 = 上上个
    unit: CalendarUnit | None = None
    offset: int | None = Field(default=None, ge=-12, le=0)

    @model_validator(mode="after")
    def _fields_match_range(self):
        if self.range == "custom":
            if self.start is None or self.end is None:
                raise ValueError("range=custom requires both start and end")
            if self.start > self.end:
                raise ValueError("start must not be after end")
        elif self.start is not None or self.end is not None:
            raise ValueError("start/end are only allowed when range=custom")
        if self.range == "calendar":
            if self.unit is None or self.offset is None:
                raise ValueError("range=calendar requires both unit and offset")
        elif self.unit is not None or self.offset is not None:
            raise ValueError("unit/offset are only allowed when range=calendar")
        return self


class TimeSpec(Period):
    # previous_period：和上一期比。本期是自然周 / 月时取上一个周 / 月的同期，其他取紧挨着的等长前一段（SPEC Q20）
    # Period：和指定的一段时间比，必须整段在本期之前
    compare: Literal["previous_period"] | Period | None = None


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
    if mql.time.compare:
        ps, pe = compare_window(mql.time, cat.today)
        # 对比期必须整段在本期之前：写反时两期数字都在，但变化方向会说反（SPEC Q20）
        if pe >= start:
            errors.append(f"the comparison period {ps}~{pe} must end before the main period {start}~{end} starts. "
                          f"Put the more recent period in time and the earlier one in compare")
    if errors:
        raise MQLError(errors)


def check_data_range(mql: MQL, cat: Catalog) -> None:
    """整段在数据范围之外：多半是模型把年份或区间写错了，回传重试（SPEC Q18 / Q19）。
    只有一部分在范围内的照常计算，由 coverage_notes 告诉用户实际覆盖的日期。
    在权限检查之后调用：越权的问题必须先被拒绝，不能被"没有数据"盖过去。"""
    start, end = resolve_window(mql.time, cat.today)
    out = []
    if end < cat.data_start:
        out.append(f"time window {start}~{end} is entirely before the first day of data ({cat.data_start})")
    elif mql.time.compare and compare_window(mql.time, cat.today)[1] < cat.data_start:
        ps, pe = compare_window(mql.time, cat.today)
        out.append(f"the comparison period {ps}~{pe} is entirely before the first day of data ({cat.data_start})")
    if out:
        raise OutOfRange(out + [f"data is available from {cat.data_start} to {cat.today}. "
                                f"Today is {cat.today}; if the user did not give a year, use the current year. "
                                f"If you misread the time, fix it; if the user really asked about this period, "
                                f"do not change it to another period: output cannot_answer saying there is no data for it."])


def resolve_window(t: TimeSpec, today: date) -> tuple[date, date]:
    """把时间描述换算成具体的店铺本地日期区间（含首尾）。"""
    if t.range == "custom":
        return t.start, t.end
    if t.range == "calendar":
        return calendar_window(t.unit, t.offset, today)
    if t.range.startswith("last_") and t.range.endswith("_days"):
        n = int(t.range.split("_")[1])
        return today - timedelta(n - 1), today
    monday = today - timedelta(today.weekday())
    if t.range == "this_week":
        return monday, today
    if t.range == "last_week":
        return monday - timedelta(7), monday - timedelta(1)
    raise AssertionError(t.range)


def calendar_window(unit: str, offset: int, today: date) -> tuple[date, date]:
    """自然日 / 周（周一开始）/ 月。offset=0 是本期到今天为止，负数是往前第几个完整周期。"""
    if unit == "day":
        d = today + timedelta(offset)
        return d, d
    if unit == "week":
        monday = today - timedelta(today.weekday()) + timedelta(7 * offset)
        return monday, min(monday + timedelta(6), today)
    if unit == "month":
        y, m = divmod(today.year * 12 + today.month - 1 + offset, 12)   # m 从 0 开始
        first = date(y, m + 1, 1)
        ny, nm = divmod(y * 12 + m + 1, 12)
        return first, min(date(ny, nm + 1, 1) - timedelta(1), today)
    raise AssertionError(unit)


def coverage_notes(start: date, end: date, data_start: date, label: str = "统计区间") -> list[str]:
    """写给用户看的实际统计区间。时间段只有一部分有数据时，说明实际覆盖的日期（SPEC Q19 选 b）。"""
    if start >= data_start:
        return [f"{label}：{start} ~ {end}"]
    return [f"{label}：{start} ~ {end}，但数据从 {data_start} 开始，实际只覆盖 {data_start} ~ {end}"]


def time_rules(today: date) -> str:
    """查询和归因路由共用的时间说明，只有这一个来源。"""
    weekday = "一二三四五六日"[today.weekday()]
    return f"""今天是 {today}（周{weekday}），一周从周一开始。
- "本周" → {{"range": "this_week"}}；"上周" → {{"range": "last_week"}}
- "最近 7 / 14 / 30 天" → last_7_days / last_14_days / last_30_days
- 自然日 / 周 / 月的相对说法 → {{"range": "calendar", "unit": "day" | "week" | "month", "offset": N}}。
  offset 为 0 表示本期（到今天为止），-1 表示上一个，-2 表示再往前一个，依此类推。日期由系统换算，你不需要计算
- 具体日期、具体月份或其他天数 → {{"range": "custom", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}（含首尾）"""


def previous_window(start: date, end: date) -> tuple[date, date]:
    """紧挨着的上一个等长周期。"""
    n = (end - start).days + 1
    return start - timedelta(n), start - timedelta(1)


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(4)).replace(day=1) - timedelta(1)


def _prev_month(d: date) -> date:
    """上个月的同一天；对不上的日子截到上个月最后一天（3-31 → 2-28，用户 2026-11-14 决定）。"""
    last = d.replace(day=1) - timedelta(1)
    return last.replace(day=min(d.day, last.day))


def _is_month_unit(t: Period, start: date, end: date) -> bool:
    """本期按月看：calendar 按月，或 custom 恰好是一个整月（"9 月"）。
    custom 的半个月（如 10-01~10-07）不算：用户说"和前 7 天比"时应取紧挨着的 7 天，要比上月同期就显式写对比期。"""
    if t.range == "calendar":
        return t.unit == "month"
    return t.range == "custom" and start.day == 1 and end == _month_end(start)


def compare_window(t: TimeSpec, today: date) -> tuple[date, date]:
    """对比期（SPEC Q20）：
    - 指定的时间段：按它的写法换算；
    - previous_period，本期按月：上个月同期，整月对整月（10 月 → 9 月；11-01~11-08 → 10-01~10-08）；
    - previous_period，本期是自然周（this_week / last_week / calendar 按周）：上一周同期；
    - 其余（滚动窗口、任意日期、按天）：紧挨着的等长前一段。"""
    if isinstance(t.compare, Period):
        return resolve_window(t.compare, today)
    start, end = resolve_window(t, today)
    if _is_month_unit(t, start, end):
        prev_end = _month_end(_prev_month(end)) if end == _month_end(end) else _prev_month(end)
        return _prev_month(start), prev_end
    if t.range in ("this_week", "last_week") or (t.range == "calendar" and t.unit == "week"):
        return start - timedelta(7), end - timedelta(7)
    return previous_window(start, end)
