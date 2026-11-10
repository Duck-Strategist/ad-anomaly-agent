"""结果集比对（执行准确率）：比较的是查询结果，不是 SQL 写法。

规则：
  - 行数必须相同。
  - 标准答案里的非数值列（店铺、广告组、日期……）是"键"：候选结果里必须有一列取值完全相同，用它对齐行。
  - 标准答案里的每个数值列，都要在候选结果里找到一列逐行匹配（相对误差 ≤ 0.5%）。
    比率允许以百分数形式给出（候选值 = 标准值 × 100）。
  - 候选结果可以多出列（比如附带了上期值、变化率），不扣分。
  - 列名不参与比较：基线 SQL 和本方案的列名不同是正常的。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

REL_TOL = 0.005


def _norm(v):
    if v is None:
        return None
    if isinstance(v, (Decimal, int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v).strip()


def _is_num(col: list) -> bool:
    return all(isinstance(x, float) or x is None for x in col) and any(isinstance(x, float) for x in col)


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if abs(a - b) <= REL_TOL * max(abs(a), abs(b)) or abs(a - b) < 1e-9:
        return True
    return False


def _col_matches(gold: list, cand: list) -> bool:
    return (all(_close(g, c) for g, c in zip(gold, cand))
            or all(_close(g * 100 if g is not None else None, c) for g, c in zip(gold, cand)))


def compare_results(gold_rows: list[dict], cand_rows: list[dict]) -> tuple[bool, str]:
    """返回 (是否正确, 原因)。"""
    if len(gold_rows) != len(cand_rows):
        return False, f"row count {len(cand_rows)} != expected {len(gold_rows)}"
    if not gold_rows:
        return True, "both empty"

    g_cols = list(gold_rows[0].keys())
    c_cols = list(cand_rows[0].keys())
    G = {k: [_norm(r[k]) for r in gold_rows] for k in g_cols}
    C = {k: [_norm(r[k]) for r in cand_rows] for k in c_cols}

    key_cols = [k for k in g_cols if not _is_num(G[k])]
    num_cols = [k for k in g_cols if _is_num(G[k])]

    # 1. 找到候选结果中与每个键列取值集合相同的列，并据此对齐行
    key_map = {}
    for k in key_cols:
        match = next((c for c in c_cols if c not in key_map.values() and sorted(map(str, C[c])) == sorted(map(str, G[k]))), None)
        if match is None:
            return False, f"no column matching key '{k}' (values {sorted(map(str, G[k]))[:5]}...)"
        key_map[k] = match

    g_order = sorted(range(len(gold_rows)), key=lambda i: tuple(str(G[k][i]) for k in key_cols))
    c_order = sorted(range(len(cand_rows)), key=lambda i: tuple(str(C[key_map[k]][i]) for k in key_cols))
    G = {k: [v[i] for i in g_order] for k, v in G.items()}
    C = {k: [v[i] for i in c_order] for k, v in C.items()}

    # 2. 每个数值列都要找到一个匹配的候选列（每个候选列只能用一次）
    used = set(key_map.values())
    for k in num_cols:
        match = next((c for c in c_cols if c not in used and _is_num(C[c]) and _col_matches(G[k], C[c])), None)
        if match is None:
            return False, f"no column matching value '{k}' (expected {G[k][:3]}...)"
        used.add(match)
    return True, "match"
