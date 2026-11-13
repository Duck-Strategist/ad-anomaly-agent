"""query_metrics：校验 → 编译 → 注入权限 → 只读执行。Agent 调用的就是这个函数。

返回统一的 dict，无论成功失败都带上 MQL / SQL，便于核对和写进 trace：
  ok=True  : rows, columns, window, previous_window, refund_incomplete, sql
  ok=False : error_type（validation / permission / execution）, errors（写给模型看的具体原因）
"""

from __future__ import annotations

from decimal import Decimal
from typing import get_args

import psycopg
from pydantic import ValidationError

from ..db import allowed_shops as lookup_allowed_shops
from ..db import reader_conn
from .catalog import load_catalog
from .compiler import ID_FILTERS, CompiledQuery, IdIndex, PermissionDenied, compile_mql
from .mql import MQL, MQLError, ShopId

# 编号 → 所属店铺的查法（以外键为准）。只有这几段固定 SQL，编号本身走参数绑定
_ID_SOURCES = {
    "sku":      ("products x", "x.sku_id", "x.shop_id"),
    "ad_group": ("ad_groups x JOIN ad_campaigns c ON c.campaign_id = x.campaign_id", "x.ad_group_id", "c.shop_id"),
    "campaign": ("ad_campaigns x", "x.campaign_id", "x.shop_id"),
}


def _id_index(conn: psycopg.Connection, parsed: MQL, allowed: list[str]) -> IdIndex | None:
    requested = {k: getattr(parsed.filters, k) or [] for k in ID_FILTERS}
    if not any(requested.values()):
        return None
    idx = IdIndex()
    for kind, values in requested.items():
        if not values:
            continue
        frm, id_col, shop_col = _ID_SOURCES[kind]
        rows = conn.execute(f"SELECT {id_col}, {shop_col} FROM {frm} WHERE {id_col} = ANY(%s)", (values,)).fetchall()
        idx.owners[kind] = dict(rows)
        if set(values) - idx.owners[kind].keys():   # 有查不到的编号：取有权限店铺下的全集，用来给候选
            idx.accessible[kind] = [r[0] for r in conn.execute(
                f"SELECT {id_col} FROM {frm} WHERE {shop_col} = ANY(%s) ORDER BY 1", (allowed,)).fetchall()]
    return idx


def _run(conn: psycopg.Connection, q: CompiledQuery) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(q.sql, q.params)
        names = [d.name for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _merge_compare(cur_rows: list[dict], prev_rows: list[dict], dims: list[str], metrics: list[str]) -> list[dict]:
    """本期 / 上期按维度对齐，补上 *_prev 和 *_change_pct。"""
    key = lambda r: tuple(r[d] for d in dims)
    prev = {key(r): r for r in prev_rows}
    out = []
    for r in cur_rows:
        p = prev.get(key(r), {})
        row = dict(r)
        for m in metrics:
            a, b = r.get(m), p.get(m)
            row[f"{m}_prev"] = b
            row[f"{m}_change_pct"] = (round((Decimal(a) / Decimal(b) - 1) * 100, 1)
                                      if a is not None and b not in (None, 0) else None)
        out.append(row)
    return out


def query_metrics(mql: dict, user_id: str) -> dict:
    cat = load_catalog()
    try:
        parsed = MQL.model_validate(mql)
    except ValidationError as e:
        errors = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        return {"ok": False, "error_type": "validation", "errors": errors, "mql": mql}

    try:
        allowed = lookup_allowed_shops(user_id)
        with reader_conn() as conn:
            ids = _id_index(conn, parsed, allowed)
        compiled = compile_mql(parsed, cat, allowed, ids)
    except PermissionDenied as e:
        return {"ok": False, "error_type": "permission", "errors": e.errors, "mql": mql}
    except MQLError as e:
        return {"ok": False, "error_type": "validation", "errors": e.errors, "mql": mql}

    queries = [q for q in (compiled.current, compiled.previous) if q]
    try:
        with reader_conn() as conn:
            results = [_run(conn, q) for q in queries]
    except psycopg.Error as e:
        return {"ok": False, "error_type": "execution", "errors": [str(e).strip()], "mql": mql,
                "sql": [q.sql for q in queries]}

    rows = results[0]
    if compiled.previous:
        rows = _merge_compare(results[0], results[1], compiled.dimensions, compiled.metrics)
    return {
        "ok": True,
        "mql": mql,
        "rows": rows,
        "columns": list(rows[0].keys()) if rows else compiled.current.columns,
        "window": [d.isoformat() for d in compiled.current.window],
        "previous_window": [d.isoformat() for d in compiled.previous.window] if compiled.previous else None,
        "refund_incomplete": any(q.refund_incomplete for q in queries),
        # 没指定店铺、且用户只能看部分店铺时，结果只是"有权限店铺"的数据，必须告诉用户，
        # 否则用户会把部分当成全部。这个标记由系统根据权限生成，不依赖模型自觉说明。
        "scope_limited": not parsed.filters.shop and set(allowed) != set(get_args(ShopId)),
        "shops_in_scope": sorted(parsed.filters.shop or allowed),
        "currency": cat.raw["reporting_currency"],
        "sql": [q.sql for q in queries],
        "params": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in q.params.items()} for q in queries],
    }
