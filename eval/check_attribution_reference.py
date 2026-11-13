"""不调用 LLM，核对归因题：按题目的 reference_plan（正确的分析对象和时间段）直接调 decompose_roas，
看算法能否得到 expect_conclusion。相当于查询题的 check_reference。

这里不一致说明算法本身在这道题上会错（例如干扰题被误报），和模型无关；评测时模型只负责把问题理解成计划。
结论的取法与工作流 attribute 节点一致。

用法：uv run python -m eval.check_attribution_reference [题目文件]
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from adagent.attribution.decompose import Window, decompose_roas
from adagent.db import reader_conn
from eval.check_reference import load_questions
from eval.workflow_eval import score_conclusion

ROOT = Path(__file__).resolve().parent


def conclusion_for(plan: dict, user_id: str) -> dict:
    scope, target = plan["scope"], plan["target"]
    if scope == "sku":   # 1 组 1 SKU：按外键换成广告组（与工作流一致）
        with reader_conn() as conn:
            target = conn.execute("SELECT ad_group_id FROM ad_groups WHERE sku_id = %s", (target,)).fetchone()[0]
        scope = "ad_group"
    r = decompose_roas(scope, target, Window(date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])), user_id)
    rec = r.get("recommendation") or {}
    return {"significant": r["significant"], "main_cause": r.get("located_main_cause"),
            "ad_group_id": r.get("located_ad_group"), "suggest_pause": bool(rec.get("requires_approval")),
            "z": r.get("z"), "found_by": r.get("found_by")}


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "questions" / "m2_attribution.yaml"
    failed = 0
    for q in load_questions(path):
        got = conclusion_for(q["reference_plan"], q["user_id"])
        ok, why = score_conclusion(q["expect_conclusion"], got)
        failed += not ok
        z = f"z={got['z']:+.2f}" if got["z"] is not None else "z=n/a"
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']} {q['kind']:10s} {z} {got['found_by'] or '':9s} {why}")
    n = len(load_questions(path))
    print(f"\n{n - failed}/{n} reference plans reach the expected conclusion")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
