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
  - 计算交给 decompose_roas，结论段由代码模板生成（explain.py），LLM 只负责理解问题（route）和写建议段（explain）。
"""

from __future__ import annotations

import json
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

from langgraph.errors import GraphInterrupt

from . import actions, clarify, tracing
from .attribution.decompose import Window, decompose_roas
from .attribution.explain import TemplateError, advice_context, basis_line, new_numbers, render_facts
from .db import reader_conn
from .llm import LLM
from .nl2mql import answer_question
from .semantic.catalog import load_catalog
from .semantic.mql import TimeSpec, resolve_window, time_rules



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
    explanation: str           # facts + 模型写的建议（SPEC Q21）
    facts: str                 # 代码按模板生成的结论段
    advice: str                # 模型写的"建议和说明"（数字检查不通过时为空）
    advice_check: dict         # {"retried", "dropped"}：数字检查是否触发重写 / 丢弃
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

时间：{time_rules}
"""

ADVICE_PROMPT = """你是跨境电商广告分析师。下面是系统已经写好的归因结论（由代码根据确定性算法生成，数字都已核对），以及一份结构化摘要。
请在结论后面补一段"建议和说明"给卖家，不超过 150 字：
- 结合主因和建议动作，说明卖家接下来具体可以检查或做什么。
- 只有摘要里同时有 shop_main_cause 和 located_main_cause、且两者不同时，才解释两者为什么可以同时成立；摘要里没有的字段不要提。
- 如果结论是没有显著下滑，简短说明无需处理即可，不要给处理建议。
要求：
- 不要写任何数字（百分比、金额、日期都不要），数字已经在结论里了。广告组编号可以写。
- 只根据结论里的信息写。结论里没有的原因不要当作事实写，也不要猜测。
- 不要和结论矛盾，不要重复结论原文，不要出现字段名。
只输出 JSON：{"advice": "..."}"""


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
        messages = [{"role": "system", "content": ROUTE_PROMPT.format(time_rules=time_rules(cat.today))},
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
                "answer": _jsonable({"rows": a.rows, "mql": a.mql, "sql": a.sql, "notes": a.notes,
                                     "attempts": _attempts_view(a.attempts)})}

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
        # 数字和结论由代码按模板生成，模型只写最后一段建议（SPEC Q21）。
        # 模板不认识的形态直接报错（TemplateError），不静默漏说。
        r = state["attribution"]
        try:
            facts = render_facts(r, cat.today, cat.refund_settle_days)
        except TemplateError as e:
            return {"status": "failed", "message": f"TemplateError: {e}"}
        messages = [{"role": "system", "content": ADVICE_PROMPT},
                    {"role": "user", "content": f"结论：\n{facts}\n\n摘要：{json.dumps(advice_context(r), ensure_ascii=False)}"}]
        call = llm.chat_json(messages)
        advice = (call.parsed or {}).get("advice", "") if not call.error else ""
        extra = new_numbers(advice, facts)
        retried = bool(extra)
        if extra:   # 段落里出现结论段没有的数字：回传重写一次
            messages += [{"role": "assistant", "content": call.output},
                         {"role": "user", "content": f"你的段落里出现了结论里没有的数字：{', '.join(extra)}。"
                                                     f"请去掉所有数字，重新输出完整的 JSON。"}]
            call = llm.chat_json(messages)
            advice = (call.parsed or {}).get("advice", "") if not call.error else ""
            extra = new_numbers(advice, facts)
        dropped = bool(extra)
        if dropped:   # 重写后仍有：丢掉这一段，只保留模板
            advice = ""
        explanation = facts + (f"\n建议和说明：{advice}" if advice else "")
        status = "awaiting_approval" if state["conclusion"]["suggest_pause"] else "answered"
        return {"explanation": explanation, "basis": basis_line(r), "facts": facts, "advice": advice,
                "advice_check": {"retried": retried, "dropped": dropped}, "status": status}

    def after_explain(state: State) -> str:
        if state["status"] == "failed":
            return END
        return "create_proposal" if state["conclusion"]["suggest_pause"] else END

    def create_proposal(state: State) -> State:
        ag = state["conclusion"]["ad_group_id"]
        pid = actions.new_proposal_id()
        p = actions.create_proposal(pid, ag, state["user_id"],
                                    params={"reason": state["attribution"]["recommendation"]["reason"]},
                                    trace_id=state["thread_id"])
        return {"proposal": {**p, "action": actions.ACTION, "ad_group_id": ag}}

    def approve(state: State) -> State:
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
        g.add_node(name, _traced(name, fn))
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


def _attempts_view(attempts: list[dict]) -> list[dict]:
    """自修正过程给页面看：每一轮模型输出了什么、哪里出错、回传给模型的提示。"""
    return [{"attempt": a["attempt"], "output": a["output"],
             "error_type": a.get("result", {}).get("error_type"), "errors": a.get("result", {}).get("errors"),
             "feedback": a.get("feedback")} for a in attempts]


# ---------------- 观测（SPEC Q24） ----------------

NODE_INPUT = {   # 每个节点的 span 记录哪些输入（其余从 trace 上一层能看到）
    "route": ("question",), "apply_clarify": ("clarify", "clarify_reply"), "approve": ("proposal",),
    "execute": ("proposal", "decision"),
}


def _traced(name: str, fn):
    """节点包一层 span，输出就是节点返回的字段。interrupt 是控制流：记成"等待用户"，不记成错误。"""
    def wrapper(state: State) -> State:
        waiting = None
        with tracing.observation(name, input={k: state.get(k) for k in NODE_INPUT.get(name, ())} or None) as obs:
            try:
                out = fn(state)
            except GraphInterrupt as e:
                waiting = e
                tracing.safe_update(obs, output={"interrupt": "waiting for user"})
            else:
                tracing.safe_update(obs, output=tracing.small(_jsonable(out)),
                                    **({"level": "ERROR", "status_message": out.get("message")}
                                       if out.get("status") == "failed" else {}))
        if waiting is not None:
            raise waiting
        return out
    return wrapper


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


def _trace_summary(res: dict) -> dict:
    s = res["state"]
    return {"status": s.get("status"), "conclusion": s.get("conclusion"), "execution": s.get("execution"),
            "waiting_for": "approval" if res["pending_approval"] is not None
            else "clarification" if res["pending_clarification"] is not None else None}


def run(question: str, user_id: str, graph, thread_id: str | None = None) -> dict:
    """thread_id 同时用作审计日志的 trace_id。评测传入 "eval-" 前缀的 id，产生的提议保留不删，按前缀排除。"""
    thread_id = thread_id or uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    with tracing.request("ask", thread_id, user_id, input={"question": question}) as root:
        out = graph.invoke({"question": question, "user_id": user_id, "thread_id": thread_id}, config)
        res = _result(graph, config, out)
        tracing.safe_update(root, output=_trace_summary(res))
    return res


def answer_clarification(thread_id: str, reply: str, graph) -> dict:
    """反问的回答接口。reply 是按钮值（指标名）或用户输入的自由文本。"""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if not snapshot.next or "ask_clarify" not in snapshot.next:
        return {**_result(graph, config, {}), "already_processed": True}
    with tracing.request("clarify", thread_id, snapshot.values.get("user_id", ""), input={"reply": reply}) as root:
        out = graph.invoke(Command(resume=reply), config)
        res = _result(graph, config, out)
        tracing.safe_update(root, output=_trace_summary(res))
    return res


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
    with tracing.request("approve", thread_id, approver_id,
                         input={"approved": approved, "reason": reason, "proposal": proposal}) as root:
        out = graph.invoke(Command(resume={"approved": approved, "by": approver_id, "reason": reason}), config)
        res = _result(graph, config, out)
        tracing.safe_update(root, output=_trace_summary(res))
    return res
