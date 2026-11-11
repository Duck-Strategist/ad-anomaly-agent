"""基线：同一个模型直接写 SQL（Text-to-SQL）。

公平性（SPEC 第九节）：
  - 同一模型、temperature=0、同样的重试上限、同样的只读账号和超时
  - 信息量等价：业务表 DDL（含陷阱注释）+ 从同一份指标目录生成的口径文档 + 同样的日期约定
  - 唯一差别：规则写在 prompt 里让模型遵守（权限也只能靠 prompt），而不是由编译器强制执行
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from adagent.db import allowed_shops, reader_conn
from adagent.llm import LLM
from adagent.nl2mql import MAX_RETRIES, Answer
from adagent.semantic.catalog import Catalog, load_catalog

ROOT = Path(__file__).resolve().parent.parent


def business_ddl() -> str:
    sql = (ROOT / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8")
    return sql.split("-- ========== 应用元数据")[0].strip()


def metric_doc(cat: Catalog) -> str:
    lines = []
    for name, spec in cat.metrics.items():
        parts = [f"{m}（{cat.measures[m].get('expr', 'Σ_SKU 广告销售额 × (1 − 该 SKU 同期退款率)')}）"
                 for m in cat.measures_of(name)]
        lines.append(f"- {name}：{spec['desc']}。公式：{spec['formula']}，其中 " + "；".join(parts))
    return "\n".join(lines)


def build_system_prompt(cat: Catalog, shops: list[str]) -> str:
    return f"""你是跨境电商经营数据分析师，使用 PostgreSQL 16。把用户问题写成一条 SQL 查询。

## 表结构
{business_ddl()}

## 口径规则（必须遵守）
1. 软删除：orders.deleted_at 非空的是重复 / 测试订单，统计时必须排除。
2. 时区：orders.created_at 存的是 UTC。按天统计订单要换算成店铺本地日期：(o.created_at AT TIME ZONE shops.timezone)::date。
   ad_daily_stats.stat_date 已经是店铺本地日期。所有日期范围都按店铺本地日期理解。
3. 币种：金额按店铺当地币种存储（USD / GBP / JPY）。所有金额都要折合美元：按"金额所属日期（订单用下单本地日期，广告用 stat_date）+ 店铺币种"
   JOIN fx_rates，乘以 rate_to_usd 后再求和。
4. 退款：在 refunds 表，一个订单行可能有多笔部分退款。退款归属到原订单的下单日（不是退款日）。
   先把退款按 item_id 汇总再关联订单行，不要直接 JOIN 后对订单行金额求和（会重复计算）。
5. 比率指标一律"先求和再相除"，不要对每天的比率取平均。比率用小数表示。
6. 1 个广告组只推 1 个 SKU（ad_groups.sku_id）。广告指标来自 ad_daily_stats（平台归因口径），不要用订单表推算广告销售额。

## 指标定义
{metric_doc(cat)}

## 时间
今天是 {cat.today}（周日），一周从周一开始。"本周" = 本周一到今天；"上周" = 上周一到上周日；"最近 N 天" = 含今天在内的 N 天。
要求和上一个周期对比时，在同一行里分别给出本期和上期的值。

## 权限（必须遵守）
当前用户只能访问店铺：{', '.join(shops)}。SQL 只能查询这些店铺的数据。
如果问题要求查看其他店铺，不要写 SQL，直接拒绝。
问"所有店铺"时，只统计有权限的店铺，并在输出里用 note 字段告诉用户实际统计了哪些店铺。

## 输出
只输出一个 JSON 对象：
- {{"sql": "SELECT ..."}}，需要向用户说明统计范围时加 "note": "说明"
- 需要拒绝时：{{"refuse": "原因"}}
"""


def answer_question(question: str, user_id: str, llm: LLM) -> Answer:
    cat = load_catalog()
    messages = [{"role": "system", "content": build_system_prompt(cat, allowed_shops(user_id))},
                {"role": "user", "content": question}]
    attempts: list[dict] = []
    last_sql = None

    for attempt in range(MAX_RETRIES + 1):
        call = llm.chat_json(messages)
        record = {"attempt": attempt, "output": call.output, "tokens": [call.prompt_tokens, call.completion_tokens],
                  "latency_s": call.latency_s}
        attempts.append(record)

        if call.error:
            feedback = f"你的输出不是合法 JSON 对象：{call.error}。请只输出一个 JSON 对象。"
        elif "refuse" in call.parsed:
            return Answer("refused", message=str(call.parsed["refuse"]), attempts=attempts)
        elif "sql" not in call.parsed:
            feedback = '输出必须是 {"sql": "..."} 或 {"refuse": "..."}。'
        else:
            last_sql = call.parsed["sql"]
            try:
                with reader_conn() as conn, conn.cursor() as cur:
                    cur.execute(last_sql)
                    names = [d.name for d in cur.description]
                    rows = [dict(zip(names, r)) for r in cur.fetchmany(1000)]
                note = call.parsed.get("note")
                return Answer("rows", rows=rows, sql=[last_sql], notes=[str(note)] if note else [], attempts=attempts)
            except psycopg.Error as e:
                record["result"] = {"ok": False, "error_type": "execution", "errors": [str(e).strip()]}
                feedback = f"SQL 执行失败，错误如下，请修正后重新输出完整的 JSON：\n{str(e).strip()}"

        record["feedback"] = feedback
        messages += [{"role": "assistant", "content": call.output}, {"role": "user", "content": feedback}]

    return Answer("failed", message=attempts[-1].get("feedback", ""), sql=[last_sql] if last_sql else None, attempts=attempts)
