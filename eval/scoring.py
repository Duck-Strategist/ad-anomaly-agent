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


def _alias_key(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)                          # 月份写成数字 9 / 9.0 时都按 "9" 查别名
    return str(v).strip().lower()


def long_to_wide(rows: list[dict]) -> list[list[dict]]:
    """两段时间对比的长表（每期一行，有一列标明是哪一期）→ 宽表（每个数值列按期拆成两列）。

    期间列：取值恰好 2 种、且每组其他键（店铺、广告组……）下两期各一行的非数值列。和期间列一一对应的其他列
    （如 period_start / period_end）一并当作期间描述。标签写法（"本期"、"2026-10"、日期……）不参与比较，
    因为宽表 gold 的列名本来就不参与比较；两期数字放反照样判不出来，这一点和宽表相同。
    返回所有可能的转换结果（通常 0 或 1 个）。"""
    if not rows or len(rows) % 2:
        return []
    cols = list(rows[0].keys())
    N = {c: [str(_norm(r[c])) for r in rows] for c in cols}
    nonnum = [c for c in cols if not _is_num([_norm(r[c]) for r in rows])]
    out = []
    for p in nonnum:
        labels = sorted(set(N[p]))
        if len(labels) != 2:
            continue
        desc = [c for c in nonnum if len(set(N[c])) == 2 and len(set(zip(N[p], N[c]))) == 2]
        keys = [c for c in nonnum if c not in desc]
        groups: dict[tuple, dict[str, dict]] = {}
        for i, r in enumerate(rows):
            groups.setdefault(tuple(r[k] for k in keys), {})[N[p][i]] = r
        if any(len(g) != 2 for g in groups.values()) or sum(len(g) for g in groups.values()) != len(rows):
            continue
        wide = []
        for k, g in groups.items():
            w = dict(zip(keys, k))
            for lab in labels:
                for c in cols:
                    if c not in nonnum:
                        w[f"{c}@{lab}"] = g[lab][c]
            wide.append(w)
        out.append(wide)
    return out


def compare_any(golds: list[tuple[list[dict], dict | None]], cand_rows: list[dict],
                allow_long: bool = False) -> tuple[bool, str]:
    """一道题可以有多个可接受的标准答案（如同一组数字的宽表 / 长表写法），和其中任意一个完全对上就算对。
    golds：[(gold_rows, key_aliases), ...]，第一个是主 gold。
    allow_long：两段时间对比题（periods: 2），候选结果是长表时先转成宽表再比（见 long_to_wide）。"""
    reasons = []
    for i, (rows, aliases) in enumerate(golds):
        ok, why = compare_results(rows, cand_rows, aliases)
        if ok:
            return True, why if i == 0 else f"{why} (alt gold {i})"
        if allow_long:
            for wide in long_to_wide(cand_rows):
                if compare_results(rows, wide, aliases)[0]:
                    return True, "match (long table)" if i == 0 else f"match (long table, alt gold {i})"
        reasons.append(why)
    return False, reasons[0] if len(reasons) == 1 else " | ".join(f"gold {i}: {r}" for i, r in enumerate(reasons))


def compare_results(gold_rows: list[dict], cand_rows: list[dict],
                    key_aliases: dict[str, list[str]] | None = None) -> tuple[bool, str]:
    """返回 (是否正确, 原因)。

    key_aliases：键列取值的别名表，{标准值: [可接受的写法, ...]}，例如 {"2026-09": ["2026-09-01", "9月", "sep"]}。
    用于长表 gold 里"时间段"这类没有统一写法的键：候选结果的键值先按别名换成标准值再对齐。
    键仍然参与对齐，所以把 9 月和 10 月的数字放反了照样判错——这正是不用"只比数值集合"的原因。"""
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
    alias = {}
    for canon, alts in (key_aliases or {}).items():
        for a in [canon, *alts]:
            alias[_alias_key(a)] = canon
    canon_of = lambda v: alias.get(_alias_key(v), v) if alias else v

    key_map = {}
    for k in key_cols:
        match = None
        for c in c_cols:
            if c in key_map.values():
                continue
            vals = [canon_of(x) for x in C[c]]
            if sorted(map(str, vals)) == sorted(map(str, G[k])):
                match, C[c] = c, vals          # 换成标准值，后面按它排序对齐
                break
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
