"""歧义题自检：把每道题换成同一说法的其他候选口径各算一遍，看和正确口径差多少。

差异落在评分容差（0.5%）以内的候选，说明"猜错口径"也会被判对，这道题考不出它想考的东西
（2026-11-13 发现 AM05、AM08 的时间窗口太近，退款尚未发生，扣不扣退款结果几乎一样）。
用编译器取数（各候选口径的 MQL 已经过 check_reference 核对），不调用模型。

用法：uv run python -m eval.check_ambiguity_options
"""

from __future__ import annotations

from pathlib import Path

from adagent.semantic.catalog import load_catalog
from adagent.semantic.executor import query_metrics
from eval.check_reference import load_questions
from eval.scoring import REL_TOL

ROOT = Path(__file__).resolve().parent


def main() -> int:
    cat = load_catalog()
    weak = 0
    for q in load_questions(ROOT / "questions" / "m2_ambiguity.yaml"):
        mql = q["reference_mql"]
        right = mql["metrics"][0]
        vals = {}
        for m in cat.ambiguous_terms[q["term"]]:
            r = query_metrics({**mql, "metrics": [m]}, q["user_id"])
            v = r["rows"][0][m] if r["ok"] and r["rows"] else None
            vals[m] = float(v) if v is not None else None   # None：这个口径在该粒度上算不出（如按广告组拆 TACOS）
        base = vals[right]
        parts = []
        for m, v in vals.items():
            if m == right:
                continue
            if v is None:
                parts.append(f"{m}=n/a")
                continue
            diff = v / base - 1
            flag = "  <-- 在容差内" if abs(diff) <= REL_TOL else ""
            weak += bool(flag)
            parts.append(f"{m}={v:.4f} ({diff * 100:+.1f}%){flag}")
        print(f"{q['id']} {q['expect']:10s} {right}={base:.4f} | " + ", ".join(parts))
    print(f"\n{weak} 个候选口径与正确口径的差异在容差内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
