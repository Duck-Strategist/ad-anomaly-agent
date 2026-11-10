"""自然语言 → MQL → 查询，带自修正。

流程：
  1. LLM 把问题翻译成 MQL（JSON）。prompt 从指标目录自动生成，口径只有一个来源。
  2. 调 query_metrics：校验 → 编译 → 注入权限 → 只读执行。
  3. 校验 / 执行报错 → 把错误原文回传给模型重试，最多 MAX_RETRIES 次；超限后降级（返回最后一次的 MQL 和错误供人查看）。
  4. 权限错误不重试：这不是模型能"修好"的问题，应如实告诉用户。

注意：模型不知道用户的权限。权限完全由编译器根据 user_id 判断，prompt 里刻意不提。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .llm import LLM
from .semantic.catalog import Catalog, load_catalog
from .semantic.executor import query_metrics

MAX_RETRIES = 2


def build_system_prompt(cat: Catalog) -> str:
    metric_lines = "\n".join(
        f"- {name}：{spec['desc']}（可按维度：{', '.join(cat.allowed_dimensions(name))}）"
        for name, spec in cat.metrics.items()
    )
    dim_lines = "\n".join(f"- {name}：{spec['desc']}" for name, spec in cat.dimensions.items())
    return f"""你是跨境电商经营数据助手。你的任务是把用户的问题翻译成 MQL（一种结构化的指标查询 JSON）。
你不写 SQL；系统会把 MQL 编译成 SQL 并负责汇率换算、时区、软删除、退款口径和权限。

## 可用指标
{metric_lines}

## 可用维度
{dim_lines}

店铺代码：US（美国）、UK（英国）、JP（日本）。广告组 ID 形如 US-AG07，SKU 形如 UK-SKU05，广告活动形如 JP-C1。
所有金额由系统统一折合美元，你不需要处理币种。

## 时间
今天是 {cat.today}（周日），一周从周一开始。
- "本周" → this_week；"上周" → last_week
- "最近 7 / 14 / 30 天" → last_7_days / last_14_days / last_30_days
- 具体日期、月份或其他天数 → custom，并给出 start、end（含首尾，格式 YYYY-MM-DD）
- 要求和前一个等长周期对比（"和上周比""和前 7 天比""环比"）→ 在当前周期上加 "compare": "previous_period"

## MQL 格式
{{
  "metrics": ["指标名", ...],                 // 1–6 个
  "dimensions": ["维度名", ...],              // 可选，最多 3 个；要"每个 / 分别 / 按…"时才用
  "filters": {{"shop": [...], "sku": [...], "ad_group": [...], "campaign": [...]}},   // 可选
  "time": {{"range": "...", "start": "...", "end": "...", "compare": "previous_period"}},
  "order_by": {{"metric": "指标名", "desc": true}},   // 可选；问"最高 / 最低 / 前几"时用，配合 limit
  "limit": 200                                // 可选
}}
不要输出格式之外的字段。"所有店铺 / 全部店铺"不要加 shop 过滤。

## 输出
只输出一个 JSON 对象：
- 能回答：{{"mql": {{...}}}}
- 问题与经营数据无关或指标不存在：{{"cannot_answer": "原因"}}

## 示例
问：最近 14 天英国店每个广告活动的点击量
答：{{"mql": {{"metrics": ["clicks"], "dimensions": ["campaign"], "filters": {{"shop": ["UK"]}}, "time": {{"range": "last_14_days"}}}}}}

问：日本店 JP-SKU01 在 9 月 1 日到 15 日的订单客单价
答：{{"mql": {{"metrics": ["aov"], "filters": {{"sku": ["JP-SKU01"]}}, "time": {{"range": "custom", "start": "2026-09-01", "end": "2026-09-15"}}}}}}

问：美国店本周的退款率和上周比怎么样
答：{{"mql": {{"metrics": ["refund_rate"], "filters": {{"shop": ["US"]}}, "time": {{"range": "this_week", "compare": "previous_period"}}}}}}
"""


@dataclass
class Answer:
    status: str                         # rows | refused | cannot_answer | failed
    rows: list[dict] = field(default_factory=list)
    message: str = ""
    mql: dict | None = None
    sql: list[str] | None = None
    refund_incomplete: bool = False
    attempts: list[dict] = field(default_factory=list)   # 每轮：模型输出、执行结果 / 错误


def answer_question(question: str, user_id: str, llm: LLM) -> Answer:
    cat = load_catalog()
    messages = [{"role": "system", "content": build_system_prompt(cat)},
                {"role": "user", "content": question}]
    attempts: list[dict] = []
    last_mql = None

    for attempt in range(MAX_RETRIES + 1):
        call = llm.chat_json(messages)
        record = {"attempt": attempt, "output": call.output, "tokens": [call.prompt_tokens, call.completion_tokens],
                  "latency_s": call.latency_s}
        attempts.append(record)

        if call.error:
            feedback = f"你的输出不是合法 JSON 对象：{call.error}。请只输出一个 JSON 对象。"
        elif "cannot_answer" in call.parsed:
            return Answer("cannot_answer", message=str(call.parsed["cannot_answer"]), attempts=attempts)
        elif "mql" not in call.parsed:
            feedback = '输出必须是 {"mql": {...}} 或 {"cannot_answer": "..."}。'
        else:
            last_mql = call.parsed["mql"]
            result = query_metrics(last_mql, user_id)
            record["result"] = {k: result.get(k) for k in ("ok", "error_type", "errors")}
            if result["ok"]:
                return Answer("rows", rows=result["rows"], mql=last_mql, sql=result["sql"],
                              refund_incomplete=result["refund_incomplete"], attempts=attempts)
            if result["error_type"] == "permission":
                return Answer("refused", message="; ".join(result["errors"]), mql=last_mql, attempts=attempts)
            feedback = "这个 MQL 执行失败，错误如下，请修正后重新输出完整的 JSON：\n" + "\n".join(result["errors"])

        record["feedback"] = feedback
        messages += [{"role": "assistant", "content": call.output}, {"role": "user", "content": feedback}]

    # 超过重试上限：降级，返回最后一次的 MQL 和错误供人查看
    return Answer("failed", message=attempts[-1].get("feedback", ""), mql=last_mql, attempts=attempts)


if __name__ == "__main__":
    import sys
    a = answer_question(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "u_admin", LLM(sys.argv[3] if len(sys.argv) > 3 else "qwen-small"))
    print(a.status, json.dumps(a.mql, ensure_ascii=False))
    for r in a.rows[:10]:
        print("  ", r)
    print(a.message)
