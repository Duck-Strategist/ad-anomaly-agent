"""归因与处置工作流（LangGraph）。

    route ─┬─ query ─┬───────────────────────────────────────────────→ END
           │         └─(多口径)→ ask_clarify ⏸ ─→ apply_clarify ─┬─(确定)→ query
           │                        ↑                           ├─(没对上，再问)┘
           │                        └───────────────────────────┘└─(问满 2 次)→ END
           ├─ attribute ─→ explain ─┬─(不需审批)──────────────────────→ END
           │                        └─ create_proposal ─→ approve ⏸ ─→ execute ─→ END
           └─ cannot_answer ───────────────────────────────────────────→ END

要点：
  - create_proposal 和 approve 分成两个节点。interrupt 恢复时所在节点会从头重跑，
    approve 里只有 interrupt，没有副作用；pending 记录在前一个节点写，checkpoint 之后不会再执行。
  - 反问同理：query 里调 LLM（生成 MQL + 判断歧义），ask_clarify 里只有 interrupt。
    否则恢复时会重新调用模型，结果可能变（甚至换了一组选项），用户的回答就对不上了。
  - 审批人权限、拒绝理由在 resume() 里、恢复图之前检查；不合法的审批请求不会改变任何状态。
  - 暂停广告组只执行一次靠 actions.py 的幂等键 + 原子条件更新，不靠 LangGraph。
  - 计算交给 decompose_roas，LLM 只负责理解问题（route）和写解释（explain）。
"""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypedDict

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row

from . import actions, clarify
from .attribution.decompose import Window, decompose_roas
from .db import reader_conn
from .llm import LLM
from .nl2mql import answer_question
from .semantic.catalog import load_catalog
from .semantic.mql import TimeSpec, resolve_window

CAUSE_NAMES = {"cvr": "转化率下降", "ad_aov": "广告客单价下降", "cpc": "点击成本上升", "refund": "退款增加"}


class State(TypedDict, total=False):
    question: str
    user_id: str
    thread_id: str
    plan: dict                 # route 的输出
    status: str                # answered | refused | cannot_answer | failed | unresolved
                               # | awaiting_clarification | awaiting_approval | done
    answer: dict               # 问数结果
    resolved_terms: dict       # 反问中用户确认的口径，如 {"销售额": "net_sales"}
    clarify: dict              # 待反问：{"term", "options", "asks"}，asks = 已经问过几次
    clarify_reply: str         # 用户对反问的回答（按钮值或自由文本）
    attribution: dict          # decompose_roas 的结果
    conclusion: dict           # 结构化结论（评测用）：main_cause / ad_group_id / suggest_pause / significant
    explanation: str           # basis + 模型写的解释
    basis: str                 # 代码生成的口径和对比窗口说明
    proposal: dict
    decision: dict
    execution: dict
    message: str


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


# ---------------- prompts ----------------

ROUTE_PROMPT = """你是跨境电商经营助手的路由器。判断用户问题属于哪一类，只输出一个 JSON 对象。

1. 归因：问某个店铺 / 广告组 / SKU 的 ROAS、ROI、广告效果为什么下降、出了什么问题、该怎么办、表现怎么样。
   {{"intent": "attribution", "scope": "shop" | "ad_group" | "sku", "target": "US" | "US-AG07" | "US-SKU07",
     "time": {{"range": "last_7_days"}}}}
   - 店铺代码 US / UK / JP；广告组形如 US-AG07；SKU 形如 UK-SKU05。
   - 时间规则同下；没说时间就用 last_7_days（和前 7 天比较由系统自动完成）。
2. 问数：查询具体指标的数值或对比，如"上周美国店花了多少广告费""英国店本周 ACOS 比上周变化多少"。
   {{"intent": "query"}}
   - 只要是问具体数值（"是多少""各是多少""拉一下"），不管有没有提到 ROI、ROAS、广告效果，都属于问数；
     只有问原因、问怎么办、问表现好不好时才是归因。
3. 其他：与经营数据无关。{{"intent": "cannot_answer", "reason": "..."}}

时间：今天是 {today}（周日），一周从周一开始。"本周"→this_week，"上周"→last_week，
"最近 7/14/30 天"→last_7_days/last_14_days/last_30_days，具体日期→{{"range": "custom", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}。
"""

EXPLAIN_PROMPT = """你是跨境电商广告分析师。下面是系统用确定性算法算出的归因结果（JSON）。
请用中文写一段不超过 200 字的解释给卖家看：说明对比的两个时间窗口、ROAS 变化了多少、主因是什么、定位到哪个广告组、建议怎么做。
要求：
- 所有数字必须来自给定的 JSON，不得编造或自行计算新数字；百分比保留一位小数。
- 如果 significant 为 false，说明没有显著下滑、不需要处理，不要强行找原因。
- factors 里每个因子的 effect_on_net_roas_pct 表示"只看这一个因子，净 ROAS 会变化多少"，direction 表示它是拉低还是抬高；照此描述，不要自己推断方向。
- 如果 notes 不为空，必须在解释里提到；为空就不要提。
- 面向卖家写，不要出现 JSON 字段名（如 significant、notes）。
只输出 JSON：{"explanation": "..."}"""


# ---------------- 节点 ----------------

def build_graph(llm: LLM, checkpointer) -> Any:
    cat = load_catalog()

    def window_error(plan: dict) -> str | None:
        """归因计划的时间段由代码检查（2026-11-13，SPEC Q18）：本期和自动取的上一期都要落在数据范围内。
        首轮评测里路由偶尔把没写年份的"9月28日"解析成 2025 年，而 2025 年没有数据。"""
        if plan.get("intent") != "attribution":
            return None
        try:
            start, end = resolve_window(TimeSpec.model_validate(plan.get("time") or {"range": "last_7_days"}), cat.today)
        except Exception as e:
            return f"time 格式错误：{e}"
        prev_start = Window(start, end).previous.start
        if prev_start < cat.data_start or end > cat.today:
            return (f"时间段 {start}~{end}（对比的上一期从 {prev_start} 开始）超出了数据范围 "
                    f"{cat.data_start}~{cat.today}。今天是 {cat.today}；用户没写年份时按今年理解。")
        return None

    def route(state: State) -> State:
        messages = [{"role": "system", "content": ROUTE_PROMPT.format(today=cat.today)},
                    {"role": "user", "content": state["question"]}]
        call = llm.chat_json(messages)
        plan = call.parsed if not call.error else {"intent": "query"}
        err = window_error(plan)
        if err:   # 把错误回传给模型重试一次（同 nl2mql 的自修正）
            messages += [{"role": "assistant", "content": call.output},
                         {"role": "user", "content": f"{err} 请重新输出完整的 JSON。"}]
            retry = llm.chat_json(messages)
            plan = retry.parsed if not retry.error else plan
            err = window_error(plan)
            plan = {**plan, "route_retried": True} | ({"time_error": err} if err else {})
        return {"plan": plan}

    def after_route(state: State) -> str:
        return {"attribution": "attribute", "cannot_answer": "cannot_answer"}.get(state["plan"].get("intent"), "query")

    def query(state: State) -> State:
        a = answer_question(state["question"], state["user_id"], llm, state.get("resolved_terms"))
        if a.status == "clarify":
            return {"status": "awaiting_clarification", "message": a.message,
                    "clarify": {**a.clarify, "asks": 0}}
        status = {"rows": "answered"}.get(a.status, a.status)
        return {"status": status, "message": a.message,
                "answer": _jsonable({"rows": a.rows, "mql": a.mql, "notes": a.notes})}

    def after_query(state: State) -> str:
        return "ask_clarify" if state["status"] == "awaiting_clarification" else END

    def ask_clarify(state: State) -> State:
        # 只做 interrupt，没有任何副作用：恢复时本节点会从头重跑
        c = state["clarify"]
        reply = interrupt(clarify.ask_payload(c["term"], c["options"], c["asks"], cat))
        return {"clarify_reply": reply}

    def apply_clarify(state: State) -> State:
        c = state["clarify"]
        metric = clarify.map_reply(state.get("clarify_reply", ""), c["term"], c["options"], cat, llm)
        if metric:
            return {"resolved_terms": {**state.get("resolved_terms", {}), c["term"]: metric},
                    "clarify": {}, "status": "clarified"}
        asks = c["asks"] + 1
        if asks < clarify.MAX_ASKS:
            return {"clarify": {**c, "asks": asks}}
        return {"clarify": {**c, "asks": asks}, "status": "unresolved",
                "message": clarify.unresolved_message(c["term"], c["options"], cat)}

    def after_apply_clarify(state: State) -> str:
        if state["status"] == "clarified":
            return "query"
        return END if state["status"] == "unresolved" else "ask_clarify"

    def cannot_answer(state: State) -> State:
        return {"status": "cannot_answer", "message": state["plan"].get("reason", "")}

    def attribute(state: State) -> State:
        p = state["plan"]
        if p.get("time_error"):   # 重试后时间段仍超出数据范围：如实说没有数据，不做分析
            return {"status": "cannot_answer", "message": f"没有这个时间段的数据：{p['time_error']}",
                    "conclusion": {"significant": None, "insufficient_data": True}}
        scope, target = p.get("scope", "shop"), p.get("target", "")
        if scope == "sku":   # 1 组 1 SKU：SKU 换成对应的广告组
            with reader_conn() as conn:
                row = conn.execute("SELECT ad_group_id FROM ad_groups WHERE sku_id = %s", (target,)).fetchone()
            if not row:
                return {"status": "cannot_answer", "message": f"SKU {target} 没有投放广告，无法做广告归因"}
            scope, target = "ad_group", row[0]
        try:
            start, end = resolve_window(TimeSpec.model_validate(p.get("time") or {"range": "last_7_days"}), cat.today)
            result = decompose_roas(scope, target, Window(start, end), state["user_id"])
        except PermissionError as e:
            return {"status": "refused", "message": "; ".join(e.args[0])}
        except Exception as e:  # 时间格式错误、目标不存在等：降级返回原因
            return {"status": "failed", "message": f"{type(e).__name__}: {e}"}
        if result.get("insufficient_data"):
            # "没有数据"不等于"没有下滑"：原来记成 significant=false，卖家看起来像"无显著下滑"，
            # 评测里干扰题也会因此被误判为通过（2026-11-13）
            return {"status": "cannot_answer", "attribution": _jsonable(result),
                    "message": f"{result['target']} 在 {result['window'][0]}~{result['window'][1]} 或上一期没有足够数据，无法分析",
                    "conclusion": {"significant": None, "insufficient_data": True}}
        rec = result.get("recommendation") or {}
        conclusion = {"significant": result["significant"], "main_cause": result.get("located_main_cause"),
                      "ad_group_id": result.get("located_ad_group"),
                      "suggest_pause": bool(rec.get("requires_approval"))}
        return {"attribution": _jsonable(result), "conclusion": conclusion}

    def after_attribute(state: State) -> str:
        if "attribution" not in state or (state.get("conclusion") or {}).get("insufficient_data"):
            return END
        return "explain"

    def explain(state: State) -> State:
        r = state["attribution"]
        drilldown = r.get("found_by") == "drilldown"
        # 下钻发现时，店铺整体的分解没有意义（不显著），给模型看的是定位到的广告组自己的分解
        d = r.get("located_decomposition", {}) if drilldown else r.get("decomposition", {})
        summary = {
            "scope": r["scope"], "target": r["target"], "window": r["window"], "previous_window": r["previous_window"],
            "significant": r["significant"],
            "analysis_level": f"广告组 {r.get('located_ad_group')}（店铺整体不显著，下钻发现）" if drilldown else r["scope"],
            "net_roas": {k: round(v, 3) for k, v in d.get("net_roas", {}).items()},
            "net_roas_change_pct": round(d["total_change_pct"], 1) if d else None,
            # 不给"占比"（下滑时占比的正负号容易被读反），给"该因子单独使净 ROAS 变化了多少"，并标明方向
            "factors": {CAUSE_NAMES[k].replace("下降", "").replace("上升", "").replace("增加", ""): {
                            "prev": round(v["prev"], 4), "cur": round(v["cur"], 4),
                            "effect_on_net_roas_pct": round((math.exp(v["log_contrib"]) - 1) * 100, 1),
                            "direction": "拉低" if v["log_contrib"] < 0 else "抬高"}
                        for k, v in d.get("factors", {}).items()},
            "main_cause": CAUSE_NAMES.get(r.get("located_main_cause")),
            "located_ad_group": r.get("located_ad_group"),
            "recommendation": (r.get("recommendation") or {}).get("reason"),
            "notes": [x for x in [r.get("note"), (r.get("located_decomposition") or {}).get("note")] if x],
        }
        call = llm.chat_json([{"role": "system", "content": EXPLAIN_PROMPT},
                              {"role": "user", "content": json.dumps(summary, ensure_ascii=False)}])
        text = (call.parsed or {}).get("explanation", "") if not call.error else ""
        # 口径和窗口由代码写明，不依赖模型记得说（SPEC Q15：用户说"ROI"时归因固定按净 ROAS 分析，不反问）
        w, pw = r["window"], r["previous_window"]
        basis = (f"分析口径：净 ROAS（扣除退款后的广告投入产出比）；"
                 f"对比窗口：{w[0]}~{w[1]} vs {pw[0]}~{pw[1]}。")
        status = "awaiting_approval" if state["conclusion"]["suggest_pause"] else "answered"
        return {"explanation": f"{basis}\n{text}", "basis": basis, "status": status}

    def after_explain(state: State) -> str:
        return "create_proposal" if state["conclusion"]["suggest_pause"] else END

    def create_proposal(state: State) -> State:
        ag = state["conclusion"]["ad_group_id"]
        pid = actions.new_proposal_id()
        p = actions.create_proposal(pid, ag, state["user_id"],
                                    params={"reason": state["attribution"]["recommendation"]["reason"]},
                                    trace_id=state["thread_id"])
        return {"proposal": {**p, "action": actions.ACTION, "ad_group_id": ag}}

    def approve(state: State) -> State:
            "analysis_level": f"广告组 {r.get('located_ad_group')}（店铺整体不显著，下钻发现）" if drilldown else r["scope"],
        # 只做 interrupt，没有任何副作用：恢复时本节点会从头重跑
        decision = interrupt({"proposal": state["proposal"], "explanation": state.get("explanation", "")})
        return {"decision": decision}

    def execute(state: State) -> State:
        p, d = state["proposal"], state["decision"]
        if d["approved"]:
            res = actions.execute_pause(p["idempotency_key"], p["ad_group_id"], d["by"], d.get("reason"))
        else:
            res = actions.reject(p["idempotency_key"], d["by"], d["reason"])
        return {"execution": res, "status": "done"}

    g = StateGraph(State)
    for name, fn in [("route", route), ("query", query), ("ask_clarify", ask_clarify), ("apply_clarify", apply_clarify),
                     ("cannot_answer", cannot_answer), ("attribute", attribute),
                     ("explain", explain), ("create_proposal", create_proposal), ("approve", approve),
                     ("execute", execute)]:
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, ["query", "attribute", "cannot_answer"])
    g.add_conditional_edges("query", after_query, ["ask_clarify", END])
    g.add_edge("ask_clarify", "apply_clarify")
    g.add_conditional_edges("apply_clarify", after_apply_clarify, ["query", "ask_clarify", END])
    g.add_conditional_edges("attribute", after_attribute, ["explain", END])
    g.add_conditional_edges("explain", after_explain, ["create_proposal", END])
    g.add_edge("create_proposal", "approve")
    g.add_edge("approve", "execute")
    for n in ("cannot_answer", "execute"):
        g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)


# ---------------- 对外接口 ----------------

def open_checkpointer() -> PostgresSaver:
    conn = psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "54329"), dbname=os.getenv("PG_DB", "adagent"),
        user=os.environ["PG_GRAPH_USER"], password=os.environ["PG_GRAPH_PASSWORD"],
        autocommit=True, prepare_threshold=0, row_factory=dict_row, connect_timeout=5)
    saver = PostgresSaver(conn)
    saver.setup()   # 建表（幂等）
    return saver


def _result(graph, config: dict, out: dict) -> dict:
    snapshot = graph.get_state(config)
    pending = {t.name: i.value for t in snapshot.tasks for i in t.interrupts}
    return {"thread_id": config["configurable"]["thread_id"], "state": dict(snapshot.values),
            "pending_approval": pending.get("approve"), "pending_clarification": pending.get("ask_clarify")}


def run(question: str, user_id: str, graph, thread_id: str | None = None) -> dict:
    """thread_id 同时用作审计日志的 trace_id。评测传入 "eval-" 前缀的 id，产生的提议保留不删，按前缀排除。"""
    thread_id = thread_id or uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke({"question": question, "user_id": user_id, "thread_id": thread_id}, config)
    return _result(graph, config, out)


def answer_clarification(thread_id: str, reply: str, graph) -> dict:
    """反问的回答接口。reply 是按钮值（指标名）或用户输入的自由文本。"""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if not snapshot.next or "ask_clarify" not in snapshot.next:
        return {**_result(graph, config, {}), "already_processed": True}
    out = graph.invoke(Command(resume=reply), config)
    return _result(graph, config, out)


def resume(thread_id: str, approver_id: str, approved: bool, graph, reason: str | None = None) -> dict:
    """审批接口。先校验，再恢复图；校验失败抛 ApprovalError，图保持原样等待。"""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if not snapshot.next or "approve" not in snapshot.next:
        # 已经处理过（重复点击 / 重试）：不再恢复，直接返回当前结果
        return {**_result(graph, config, {}), "already_processed": True}
    proposal = snapshot.values["proposal"]
    actions.check_approver(approver_id, proposal["ad_group_id"])
    if not approved and not (reason and reason.strip()):
        raise actions.ApprovalError("rejection requires a reason")
    out = graph.invoke(Command(resume={"approved": approved, "by": approver_id, "reason": reason}), config)
    return _result(graph, config, out)
