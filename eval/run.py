"""评测运行器。

用法：
  uv run python -m eval.run --system ours     --model deepseek
  uv run python -m eval.run --system baseline --model qwen --ids S01,T04
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
from eval.check_reference import load_questions, run_gold
from eval.scoring import compare_results

ROOT = Path(__file__).resolve().parent
SYSTEMS = {"ours": ours, "baseline": baseline}


def score(q: dict, a: Answer, gold: list[dict] | None) -> tuple[bool, str]:
    refused = a.status in ("refused", "cannot_answer")
    if q["expect"] == "refuse":
        return (True, f"refused: {a.message[:80]}") if refused else (False, f"expected refusal, got {a.status}")
    if q["expect"] == "allowed_or_refuse" and refused:
        return True, "refused"
    if a.status != "rows":
        return False, f"{a.status}: {a.message[:120]}"
    return compare_results(gold, a.rows)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def run_one(q: dict, system: str, model: str) -> dict:
    llm = LLM(model)
    gold = run_gold(q["gold_sql"]) if q.get("gold_sql") else None
    try:
        a = SYSTEMS[system](q["question"], q["user_id"], llm)
    except Exception as e:  # 网络等异常也记录下来，不中断整轮评测
        a = Answer("failed", message=f"exception: {type(e).__name__}: {e}")
    ok, why = score(q, a, gold)
    return {
        "id": q["id"], "category": q["category"], "trap": q.get("trap"), "user_id": q["user_id"],
        "question": q["question"], "expect": q["expect"],
        "pass": ok, "reason": why,
        "first_try": ok and len(a.attempts) == 1,
        "status": a.status, "message": a.message, "mql": a.mql, "sql": a.sql,
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
    total = sum(r["pass"] for r in results)
    print(f"{'TOTAL':12s} {total:>4d}/{len(results):<4d} {sum(r['first_try'] for r in results):>5d}/{len(results):<4d}")
    print(f"tokens: {sum(r['prompt_tokens'] for r in results):,} in / {sum(r['completion_tokens'] for r in results):,} out; "
          f"avg latency {sum(r['latency_s'] for r in results) / len(results):.1f}s")
    for r in results:
        if not r["pass"]:
            print(f"  FAIL {r['id']}: {r['reason'][:150]}")


if __name__ == "__main__":
    main()
