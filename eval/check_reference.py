"""在接入任何模型之前，核对"标准答案 SQL"和"编译器（用 reference_mql）"是否一致。

用法：uv run python -m eval.check_reference [题目文件]
两者是独立写出来的，不一致时说明标准答案或编译器之一有错，需要人工排查（记录到 eval/CHANGELOG.md）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from adagent.db import reader_conn
from adagent.semantic.executor import query_metrics
from eval.scoring import compare_results

ROOT = Path(__file__).resolve().parent


def load_questions(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run_gold(sql: str) -> list[dict]:
    with reader_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def score_system_result(q: dict, result: dict, gold_rows: list[dict] | None) -> tuple[bool, str]:
    """按题目的 expect 判分。result 是 query_metrics 的返回值。"""
    refused = not result["ok"] and result.get("error_type") == "permission"
    if q["expect"] == "refuse":
        return (True, "refused") if refused else (False, f"expected refusal, got ok={result['ok']}")
    if q["expect"] == "allowed_or_refuse" and refused:
        return True, "refused"
    if not result["ok"]:
        return False, f"{result['error_type']}: {result['errors']}"
    return compare_results(gold_rows, result["rows"])


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "questions" / "m1.yaml"
    questions = load_questions(path)
    failed = 0
    for q in questions:
        gold = run_gold(q["gold_sql"]) if q.get("gold_sql") else None
        res = query_metrics(q["reference_mql"], q["user_id"])
        ok, why = score_system_result(q, res, gold)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']:4s} {q['category']:10s} {why}")
    print(f"\n{len(questions) - failed}/{len(questions)} reference MQLs match gold")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
