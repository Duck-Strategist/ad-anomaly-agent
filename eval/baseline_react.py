"""归因基线：ReAct 自由循环（SPEC 第九节"基线"）。

同一个模型 + 同一个 query_metrics 工具（同一语义层，权限照常由编译器注入），自己多步查数、自己下结论。
它要回答的问题是"为什么不直接让 LLM 自己分析"。

公平性（2026-11-13 确认）：
  - 给：可查的指标和维度（从同一份 catalog 生成）、MQL 格式、业务规则的文字说明——分析口径是净 ROAS、
    四类原因各是什么意思、**只有转化率下降才建议暂停**、默认对比窗口。相当于 Text-to-SQL 基线拿到的口径文档。
    不告诉它暂停规则的话，它在"是否暂停"上的失分和分析能力无关，基线就成了稻草人。
  - 不给：decompose_roas、对数分解公式、显著性门槛（z ≤ −3）、下钻规则。这些正是要比较的东西。
  - 没有路由：分析对象和时间段由它自己从问题里理解（本方案由路由理解，对等）。
  - 同一判分（结构化结论），同样每题跑 3 次算 pass^3。
循环：每一步输出一个 JSON，要么查询，要么给最终结论；最多 MAX_STEPS 次查询，超过判失败。
不用厂商专用的函数调用格式，换模型也能用。
"""

from __future__ import annotations

import json

from adagent.llm import LLM
from adagent.semantic.catalog import Catalog, load_catalog
from adagent.semantic.executor import query_metrics

MAX_STEPS = 8
MAX_ROWS_SHOWN = 60
CAUSES = ("cvr", "ad_aov", "cpc", "refund")


def build_prompt(cat: Catalog) -> str:
    metric_lines = "\n".join(
        f"- {name}：{spec['desc']}（可按维度：{', '.join(cat.allowed_dimensions(name))}）"
        for name, spec in cat.metrics.items())
    dim_lines = "\n".join(f"- {name}：{spec['desc']}" for name, spec in cat.dimensions.items())
    return f"""你是跨境电商广告分析师。用户会问某个店铺、广告组或商品在某段时间的广告投产为什么变差、有没有问题、该怎么办。
你可以多次调用查询工具获取数据，自己分析，最后给出结构化结论。

## 业务规则
- 分析口径：净 ROAS（net_roas，扣除退款后的广告投入产出比）。
- 对比窗口：用户给的时间段 vs 紧邻的前一个等长时间段；用户没说时间时，用最近 7 天 vs 前 7 天。
- 净 ROAS 下滑的原因只有四类：
  cvr：广告转化率下降；ad_aov：广告客单价下降；cpc：单次点击花费上升；refund：退款增加（net_roas 相对 roas 变差）。
- 只在确实存在显著下滑时才归因；正常的周度波动不算下滑，不要硬找原因。
- 建议动作：主因是 cvr 时建议暂停对应的广告组；refund 建议排查商品质量；ad_aov 通常是促销降价，不建议暂停；cpc 提示竞价成本上升。只有 cvr 需要暂停。
- 1 个广告组只推 1 个商品（SKU），广告组 US-AG07 推的是商品 US-SKU07 这种对应关系以数据为准，可以按维度查出来。

## 查询工具（MQL）
可用指标：
{metric_lines}

可用维度：
{dim_lines}

店铺代码：US、UK、JP。所有金额已统一折合美元。
今天是 {cat.today}（周日），一周从周一开始。

MQL 格式：
{{"metrics": ["指标名", ...], "dimensions": ["维度名", ...], "filters": {{"shop": [...], "sku": [...], "ad_group": [...], "campaign": [...]}},
  "time": {{"range": "this_week|last_week|last_7_days|last_14_days|last_30_days|custom", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "compare": "previous_period"}},
  "order_by": {{"metric": "指标名", "desc": true}}, "limit": 200}}
加上 "compare": "previous_period" 时，每个指标会同时返回上一个等长时段的值（*_prev）和变化百分比（*_change_pct）。

## 每一步只输出一个 JSON 对象
- 查询：{{"action": "query", "thought": "为什么查这个", "mql": {{...}}}}
- 结论：{{"action": "final", "significant": true 或 false, "main_cause": "cvr|ad_aov|cpc|refund" 或 null,
         "ad_group_id": "问题出在哪个广告组（问的是商品就给对应的广告组）" 或 null,
         "suggest_pause": true 或 false, "explanation": "给卖家看的中文解释，不超过 200 字"}}
没有显著下滑时：significant 为 false，main_cause 和 ad_group_id 为 null，suggest_pause 为 false。
最多查询 {MAX_STEPS} 次，之后必须给出结论。"""


def _tool_result(r: dict) -> str:
    if not r["ok"]:
        return json.dumps({"ok": False, "error_type": r["error_type"], "errors": r["errors"]}, ensure_ascii=False)
    rows = r["rows"][:MAX_ROWS_SHOWN]
    out = {"ok": True, "window": r.get("window"), "previous_window": r.get("previous_window"),
           "rows": rows, "truncated": len(r["rows"]) > MAX_ROWS_SHOWN}
    return json.dumps(out, ensure_ascii=False, default=lambda v: float(v) if hasattr(v, "is_finite") else str(v))


def analyze(question: str, user_id: str, llm: LLM) -> dict:
    """跑一次 ReAct。返回 {status, conclusion, explanation, steps}，conclusion 字段与本方案一致。"""
    cat = load_catalog()
    messages = [{"role": "system", "content": build_prompt(cat)}, {"role": "user", "content": question}]
    steps: list[dict] = []
    queries = 0
    for _ in range(MAX_STEPS + 3):   # 留几次给格式错误的回传
        call = llm.chat_json(messages)
        messages.append({"role": "assistant", "content": call.output})
        step = {"output": call.output}
        steps.append(step)
        out = call.parsed if not call.error else None
        if out is None or out.get("action") not in ("query", "final"):
            feedback = '只输出一个 JSON 对象，action 必须是 "query" 或 "final"。'
        elif out["action"] == "final":
            conclusion = {"significant": out.get("significant"), "main_cause": out.get("main_cause"),
                          "ad_group_id": out.get("ad_group_id"), "suggest_pause": bool(out.get("suggest_pause"))}
            return {"status": "answered", "conclusion": conclusion, "explanation": out.get("explanation", ""),
                    "steps": steps, "queries": queries}
        elif queries >= MAX_STEPS:
            feedback = f"已经查询了 {MAX_STEPS} 次，不能再查了，请直接给出 final 结论。"
        else:
            queries += 1
            result = query_metrics(out.get("mql") or {}, user_id)
            step["result_ok"] = result["ok"]
            feedback = "查询结果：" + _tool_result(result)
        messages.append({"role": "user", "content": feedback})
    return {"status": "failed", "conclusion": None, "explanation": "", "steps": steps, "queries": queries}
