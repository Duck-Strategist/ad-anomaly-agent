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
from eval.scoring import compare_any

ROOT = Path(__file__).resolve().parent


def load_questions(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run_gold(sql: str) -> list[dict]:
    with reader_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def load_golds(q: dict) -> list[tuple[list[dict], dict | None]] | None:
    """主 gold + 可接受的其他写法（gold_sql_alt），供 compare_any 使用。没有 gold 的题（如必须拒绝的越权题）返回 None。"""
    if not q.get("gold_sql"):
        return None
    return [(run_gold(q["gold_sql"]), None)] + [(run_gold(a["sql"]), a.get("key_aliases"))
                                               for a in q.get("gold_sql_alt", [])]


def score_system_result(q: dict, result: dict, golds: list | None) -> tuple[bool, str]:
    """按题目的 expect 判分。result 是 query_metrics 的返回值。"""
    refused = not result["ok"] and result.get("error_type") == "permission"
    if q["expect"] == "refuse":
        return (True, "refused") if refused else (False, f"expected refusal, got ok={result['ok']}")
    if q["expect"] == "allowed_disclosed_or_refuse":
        if refused:
            return True, "refused"
        if result["ok"] and not result["scope_limited"]:
            return False, "returned partial scope without telling the user"
    if q["expect"] == "no_data":
        if not result["ok"] and result["error_type"] == "out_of_range":
            return True, "out of range"
        return False, f"expected out_of_range, got ok={result['ok']} {result.get('error_type')}"
    if not result["ok"]:
        return False, f"{result['error_type']}: {result['errors']}"
    return compare_any(golds, result["rows"])


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "questions" / "m1.yaml"
    questions = load_questions(path)
    failed = skipped = 0
    for q in questions:
        golds = load_golds(q)
        if q.get("reference_mql") is None:
            # MQL 表达不了的题（如自然月对比）：只确认 gold 能执行，没有可对照的编译结果
            skipped += 1
            print(f"SKIP  {q['id']:4s} {q['category']:10s} no reference MQL; gold returned "
                  f"{' / '.join(str(len(g[0])) for g in golds or [])} row(s)")
            continue
        res = query_metrics(q["reference_mql"], q["user_id"])
        ok, why = score_system_result(q, res, golds)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']:4s} {q['category']:10s} {why}")
    checked = len(questions) - skipped
    print(f"\n{checked - failed}/{checked} reference MQLs match gold" + (f" ({skipped} skipped)" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
