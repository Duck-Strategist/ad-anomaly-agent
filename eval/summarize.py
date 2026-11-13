"""把多次运行记录合并成一张表（如 M1 + M2 查询题），按类别汇总。

用法：uv run python -m eval.summarize 标签=运行记录1.jsonl,运行记录2.jsonl [标签=...]
例：  uv run python -m eval.summarize ours/deepseek=eval/runs/a.jsonl,eval/runs/b.jsonl baseline/deepseek=...
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

CATS = ["single", "compare", "trap", "permission"]


def load(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            rows += [json.loads(line) for line in f]
    return rows


def summarize(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    out = {c: (sum(r["pass"] for r in by[c]), sum(r["first_try"] for r in by[c]), len(by[c])) for c in CATS if by[c]}
    query = [r for c in ("single", "compare", "trap") for r in by[c]]
    out["query"] = (sum(r["pass"] for r in query), sum(r["first_try"] for r in query), len(query))
    out["all"] = (sum(r["pass"] for r in rows), sum(r["first_try"] for r in rows), len(rows))
    out["tokens_in"] = sum(r["prompt_tokens"] for r in rows)
    out["tokens_out"] = sum(r["completion_tokens"] for r in rows)
    out["latency"] = sum(r["latency_s"] for r in rows) / len(rows)
    return out


def pct(a: int, n: int) -> str:
    return f"{a}/{n} ({a / n * 100:.1f}%)" if n else "-"


def main() -> None:
    results = {}
    for arg in sys.argv[1:]:
        label, paths = arg.split("=", 1)
        results[label] = summarize(load(paths.split(",")))
    labels = list(results)
    print("| 类别 | " + " | ".join(labels) + " |")
    print("|---|" + "---|" * len(labels))
    for key, name in [("single", "单指标"), ("compare", "对比"), ("trap", "陷阱"), ("permission", "越权"),
                      ("query", "**查询类（单指标+对比+陷阱）**"), ("all", "**合计**")]:
        print(f"| {name} | " + " | ".join(pct(*results[l][key][::2]) if key in results[l] else "-" for l in labels) + " |")
    for key, name in [("query", "查询类首次即对"), ("all", "合计首次即对")]:
        print(f"| {name} | " + " | ".join(pct(results[l][key][1], results[l][key][2]) for l in labels) + " |")
    print("| token 入 / 出 | " + " | ".join(f"{results[l]['tokens_in'] / 1000:.0f}k / {results[l]['tokens_out'] / 1000:.0f}k"
                                         for l in labels) + " |")
    print("| 平均耗时 / 题 | " + " | ".join(f"{results[l]['latency']:.1f}s" for l in labels) + " |")


if __name__ == "__main__":
    main()
