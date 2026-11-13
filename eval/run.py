"""评测运行器。

用法：
  uv run python -m eval.run --system ours     --model deepseek
  uv run python -m eval.run --system baseline --model qwen --ids S01,T04
  uv run python -m eval.run --system ours --model deepseek --questions eval/questions/m2_ambiguity.yaml
查询题直接调 answer_question；歧义题、归因题走完整工作流（见 eval/workflow_eval.py）。
输出：
  eval/runs/<时间>_<system>_<model>.jsonl   每题一行：问题、回答、每轮模型输出、SQL、判分、token、耗时（供项目二使用）
  终端打印按类别汇总的结果
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from adagent.llm import LLM
from adagent.nl2mql import Answer
from adagent.nl2mql import answer_question as ours
from eval.baseline_text2sql import answer_question as baseline
from adagent.workflow import build_graph, open_checkpointer
from eval.check_reference import load_golds, load_questions
from eval.scoring import compare_any
from eval.workflow_eval import run_ambiguity, run_attribution

ROOT = Path(__file__).resolve().parent
SYSTEMS = {"ours": ours, "baseline": baseline}
WORKFLOW_CATEGORIES = ("ambiguity", "attribution")


def score(q: dict, a: Answer, golds: list | None) -> tuple[bool, str]:
    refused = a.status in ("refused", "cannot_answer")
    if q["expect"] == "refuse":
        return (True, f"refused: {a.message[:80]}") if refused else (False, f"expected refusal, got {a.status}")
    if q["expect"] == "allowed_disclosed_or_refuse":
        if refused:
            return True, "refused"
        if a.status == "rows" and not a.notes:
            return False, "returned partial scope without telling the user"
    if a.status != "rows":
        return False, f"{a.status}: {a.message[:120]}"
    return compare_any(golds, a.rows)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _usage(llm: LLM) -> dict:
    return {"prompt_tokens": sum(c.prompt_tokens for c in llm.calls),
            "completion_tokens": sum(c.completion_tokens for c in llm.calls),
            "latency_s": round(sum(c.latency_s for c in llm.calls), 2)}


def run_workflow_one(q: dict, system: str, model: str) -> dict:
    llm = LLM(model)
    golds = load_golds(q)
    base = {"id": q["id"], "category": q["category"], "user_id": q["user_id"],
            "question": q["question"], "expect": q["expect"]}

    if system == "baseline":
        if q["category"] == "attribution":
            raise NotImplementedError("ReAct 归因基线尚未实现")
        # Text-to-SQL 基线不会反问。该问题上 pass 的含义是"静默猜中了卖家真正要的口径"（只作参照，SPEC 第九节）
        try:
            a = baseline(q["question"], q["user_id"], llm)
        except Exception as e:
            a = Answer("failed", message=f"exception: {type(e).__name__}: {e}")
        ok, why = compare_any(golds, a.rows) if a.status == "rows" else (False, f"{a.status}: {a.message[:120]}")
        return {**base, "pass": ok, "reason": why, "first_try": ok and len(a.attempts) == 1, "status": a.status,
                "sql": a.sql, "rows": [{k: _jsonable(v) for k, v in r.items()} for r in a.rows[:50]],
                "attempts": a.attempts, **_usage(llm)}

    savers = []

    def graph_factory():
        s = open_checkpointer()   # 每次一个连接：PostgresSaver 的单个连接不能跨线程共享
        savers.append(s)
        return build_graph(llm, s)

    try:
        res = (run_ambiguity(q, graph_factory(), golds) if q["category"] == "ambiguity"
               else run_attribution(q, graph_factory))
    except Exception as e:   # 网络等异常也记录下来，不中断整轮评测
        res = {"pass": False, "reason": f"exception: {type(e).__name__}: {e}"}
    finally:
        for s in savers:
            s.conn.close()
    return {**base, **res, "first_try": res["pass"], **_usage(llm)}


def run_one(q: dict, system: str, model: str) -> dict:
    if q["category"] in WORKFLOW_CATEGORIES:
        return run_workflow_one(q, system, model)
    llm = LLM(model)
    golds = load_golds(q)
    try:
        a = SYSTEMS[system](q["question"], q["user_id"], llm)
    except Exception as e:  # 网络等异常也记录下来，不中断整轮评测
        a = Answer("failed", message=f"exception: {type(e).__name__}: {e}")
    ok, why = score(q, a, golds)
    return {
        "id": q["id"], "category": q["category"], "trap": q.get("trap"), "user_id": q["user_id"],
        "question": q["question"], "expect": q["expect"],
        "pass": ok, "reason": why,
        "first_try": ok and len(a.attempts) == 1,
        "status": a.status, "message": a.message, "notes": a.notes, "mql": a.mql, "sql": a.sql,
        "rows": [{k: _jsonable(v) for k, v in r.items()} for r in a.rows[:50]],
        "attempts": a.attempts,
        "prompt_tokens": sum(c.prompt_tokens for c in llm.calls),
        "completion_tokens": sum(c.completion_tokens for c in llm.calls),
        "latency_s": round(sum(c.latency_s for c in llm.calls), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=list(SYSTEMS), required=True)
    ap.add_argument("--model", default="qwen-small")
    ap.add_argument("--questions", default=str(ROOT / "questions" / "m1.yaml"))
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    qs = load_questions(Path(args.questions))
    if args.ids:
        wanted = set(args.ids.split(","))
        qs = [q for q in qs if q["id"] in wanted]

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda q: run_one(q, args.system, args.model), qs))

    out_dir = ROOT / "runs"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{args.system}_{args.model}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=_jsonable) + "\n")

    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    print(f"\n{args.system} / {args.model}  ({len(results)} questions)  → {out.relative_to(ROOT.parent)}")
    print(f"{'category':12s} {'pass':>9s} {'first-try':>10s}")
    for cat in ["single", "compare", "trap", "permission"]:
        rs = by_cat.get(cat, [])
        if rs:
            print(f"{cat:12s} {sum(r['pass'] for r in rs):>4d}/{len(rs):<4d} {sum(r['first_try'] for r in rs):>5d}/{len(rs):<4d}")
    amb = by_cat.get("ambiguity", [])
    for exp, label in (("clarify", "该问"), ("no_clarify", "不该问")):
        rs = [r for r in amb if r["expect"] == exp]
        if not rs:
            continue
        if args.system == "baseline" and exp == "clarify":
            print(f"ambiguity {label}：基线不会反问，静默猜中卖家要的口径 {sum(r['pass'] for r in rs)}/{len(rs)}（参照）")
        elif exp == "clarify":
            s1, s2 = sum(bool(r.get("step1")) for r in rs), sum(bool(r.get("step2")) for r in rs)
            print(f"ambiguity {label}：两步全对 {sum(r['pass'] for r in rs)}/{len(rs)}"
                  f"（① 问对 {s1}/{len(rs)}，② 回答后查对 {s2}/{len(rs)}）")
        else:
            print(f"ambiguity {label}：{sum(r['pass'] for r in rs)}/{len(rs)}")
    att = by_cat.get("attribution", [])
    if att:
        runs = sum(len(r.get("runs", [])) for r in att)
        print(f"attribution：pass^{len(att[0].get('runs', [])) or '?'} {sum(r['pass'] for r in att)}/{len(att)}，"
              f"单次准确率 {sum(r.get('runs_passed', 0) for r in att)}/{runs}")
    total = sum(r["pass"] for r in results)
    print(f"{'TOTAL':12s} {total:>4d}/{len(results):<4d} {sum(r['first_try'] for r in results):>5d}/{len(results):<4d}")
    print(f"tokens: {sum(r['prompt_tokens'] for r in results):,} in / {sum(r['completion_tokens'] for r in results):,} out; "
          f"avg latency {sum(r['latency_s'] for r in results) / len(results):.1f}s")
    for r in results:
        if not r["pass"]:
            print(f"  FAIL {r['id']}: {r['reason'][:150]}")


if __name__ == "__main__":
    main()
