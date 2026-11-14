"""同一套题跑 k 次之后，算 pass^k（k 次全对的题数）和单次准确率（所有回答里对的比例）。

报告方式在跑之前定好（SPEC Q20）：两段时间对比留出集每题跑 3 次，报这两个数，不挑其中某一次。
用法：uv run python -m eval.pass_k 运行记录1.jsonl 运行记录2.jsonl 运行记录3.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict


def pass_k(runs: list[list[dict]]) -> dict:
    ids = [{r["id"] for r in run} for run in runs]
    if any(s != ids[0] for s in ids):
        raise ValueError("every run must contain the same questions")
    by_id: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        for r in run:
            by_id[r["id"]].append(bool(r["pass"]))
    k = len(runs)
    return {
        "k": k,
        "questions": len(by_id),
        "pass_k": sum(all(v) for v in by_id.values()),
        "single": sum(sum(v) for v in by_id.values()),
        "answers": k * len(by_id),
        "unstable": sorted(q for q, v in by_id.items() if any(v) and not all(v)),
        "never": sorted(q for q, v in by_id.items() if not any(v)),
    }


def main(paths: list[str]) -> None:
    runs = [[json.loads(line) for line in open(p, encoding="utf-8")] for p in paths]
    s = pass_k(runs)
    print(f"pass^{s['k']}: {s['pass_k']}/{s['questions']}   单次准确率: {s['single']}/{s['answers']}")
    print(f"有时对有时错: {', '.join(s['unstable']) or '-'}")
    print(f"每次都错: {', '.join(s['never']) or '-'}")


if __name__ == "__main__":
    main(sys.argv[1:])
