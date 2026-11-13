"""走完整工作流的评测：歧义题（反问 → 卖家回答 → 接着查）和归因题（结构化结论，跑 3 次算 pass^3）。

为什么不直接调 answer_question：用户用的是完整工作流（路由 → 问数 / 归因 → 反问 interrupt → 恢复）。
走捷径会漏掉路由分错（"ROI 是多少"被分到归因、不反问）、卖家原话映射错、从 checkpoint 恢复出错这几类真实失败。
代价：查询题为了和 Text-to-SQL 基线公平比较不经过路由，歧义题经过路由，两者口径不完全一致（写入 RESULTS 局限）。

评测产生的 thread_id 一律以 "eval-" 开头。归因建议暂停时工作流会写一条 pending 提议并停在审批节点，
评测不审批、不删除、不拒绝（拒绝会在审计日志里留下一个没人做过的决定），提议没人审批就不会执行，24 小时后失效；
统计审计日志时按 trace_id 前缀排除。

函数都接收 llm 和 graph 工厂，测试可以注入假 LLM。
"""

from __future__ import annotations

import uuid
from typing import Callable

from adagent.workflow import answer_clarification, run
from eval.scoring import compare_any

ATTRIBUTION_RUNS = 3
CONCLUSION_KEYS = ("significant", "main_cause", "ad_group_id", "suggest_pause")


def eval_thread_id() -> str:
    return f"eval-{uuid.uuid4().hex}"


def _rows(state: dict) -> list[dict] | None:
    return (state.get("answer") or {}).get("rows") if state.get("status") == "answered" else None


def _describe(state: dict) -> str:
    if state.get("conclusion") is not None:
        return "routed to attribution"
    return f"{state.get('status')}: {str(state.get('message', ''))[:120]}"


def run_ambiguity(q: dict, graph, golds: list) -> dict:
    """歧义题跑 1 次。该问：① 问对了词、选项包含 required_options；② 用卖家原话作答后结果正确。不该问：没反问且结果正确。"""
    tid = eval_thread_id()
    r = run(q["question"], q["user_id"], graph, thread_id=tid)
    ask = r["pending_clarification"]
    out = {"thread_id": tid, "clarification": ask, "step1": None, "step2": None}

    if q["expect"] == "no_clarify":
        if ask:
            return {**out, "pass": False, "reason": f"asked about '{ask['term']}' but should not"}
        rows = _rows(r["state"])
        if rows is None:
            return {**out, "pass": False, "reason": _describe(r["state"])}
        ok, why = compare_any(golds, rows)
        return {**out, "pass": ok, "reason": why, "rows": rows}

    # expect == "clarify"
    if not ask:
        return {**out, "step1": False, "step2": False, "pass": False,
                "reason": f"did not ask ({_describe(r['state'])})"}
    offered = {o["metric"] for o in ask["options"]}
    missing = set(q["required_options"]) - offered
    step1 = ask["term"] == q["term"] and not missing
    why1 = ("ok" if step1 else
            f"asked about '{ask['term']}' (expected '{q['term']}')" if ask["term"] != q["term"]
            else f"options missing {sorted(missing)}")

    r2 = answer_clarification(tid, q["reply"], graph)
    if r2["pending_clarification"]:
        step2, why2, rows = False, "reply not mapped; asked again", None
    else:
        rows = _rows(r2["state"])
        if rows is None:
            step2, why2 = False, _describe(r2["state"])
        else:
            step2, why2 = compare_any(golds, rows)
    resolved = r2["state"].get("resolved_terms", {})
    return {**out, "step1": step1, "step2": step2, "pass": step1 and step2,
            "reason": f"① {why1}; ② {why2}", "resolved_terms": resolved, "rows": rows}


def score_conclusion(expected: dict, got: dict | None) -> tuple[bool, str]:
    """结论分（规则判分，SPEC 第九节）：只比题目里给出的字段。"无显著下滑"干扰题只给 significant: false。"""
    if got is None:
        return False, "no conclusion (not routed to attribution, or failed before deciding)"
    wrong = [f"{k}={got.get(k)!r} (expected {v!r})" for k, v in expected.items() if got.get(k) != v]
    return (not wrong), ("match" if not wrong else "; ".join(wrong))


def run_attribution_baseline(q: dict, analyze: Callable[[str, str], dict], runs: int = ATTRIBUTION_RUNS) -> dict:
    """ReAct 基线跑 runs 次，判分与本方案相同（score_conclusion）。analyze(question, user_id) → {status, conclusion, ...}"""
    results = []
    for _ in range(runs):
        a = analyze(q["question"], q["user_id"])
        ok, why = score_conclusion(q["expect_conclusion"], a["conclusion"])
        results.append({"pass": ok, "reason": why, "status": a["status"], "conclusion": a["conclusion"],
                        "explanation": a["explanation"], "queries": a["queries"], "steps": a["steps"]})
    n_pass = sum(x["pass"] for x in results)
    return {"pass": n_pass == runs, "runs_passed": n_pass, "runs": results,
            "reason": f"{n_pass}/{runs} runs correct" + ("" if n_pass == runs else
                                                         f"; first failure: {next(x['reason'] for x in results if not x['pass'])}")}


def run_attribution(q: dict, graph_factory: Callable[[], object], runs: int = ATTRIBUTION_RUNS) -> dict:
    """同一题独立跑 runs 次（每次新的 thread）。pass^k：k 次全对；另报单次平均准确率。"""
    results = []
    for _ in range(runs):
        tid = eval_thread_id()
        r = run(q["question"], q["user_id"], graph_factory(), thread_id=tid)
        st = r["state"]
        ok, why = score_conclusion(q["expect_conclusion"], st.get("conclusion"))
        results.append({"thread_id": tid, "pass": ok, "reason": why, "status": st.get("status"),
                        "plan": st.get("plan"), "conclusion": st.get("conclusion"),
                        "explanation": st.get("explanation"), "pending_approval": bool(r["pending_approval"])})
    n_pass = sum(x["pass"] for x in results)
    return {"pass": n_pass == runs, "runs_passed": n_pass, "runs": results,
            "reason": f"{n_pass}/{runs} runs correct" + ("" if n_pass == runs else
                                                         f"; first failure: {next(x['reason'] for x in results if not x['pass'])}")}
