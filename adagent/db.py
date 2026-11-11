"""数据库连接。两个角色分开：reader 执行查询，app 读权限表 / 写审计。"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _connect(prefix: str, **kw) -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "54329"),
        dbname=os.getenv("PG_DB", "adagent"),
        user=os.environ[f"{prefix}_USER"], password=os.environ[f"{prefix}_PASSWORD"],
        connect_timeout=5,   # 数据库没起来时快速失败，而不是一直挂着
        **kw,
    )


def reader_conn() -> psycopg.Connection:
    """执行 Agent / 基线生成的查询：只读、5 秒超时（由数据库角色保证）。"""
    return _connect("PG_READER", autocommit=True)


def app_conn() -> psycopg.Connection:
    return _connect("PG_APP")


def allowed_shops(user_id: str) -> list[str]:
    """用户可访问的店铺。来源是服务端的权限表，不是模型输出。"""
    with app_conn() as conn:
        rows = conn.execute("SELECT shop_id FROM user_shop_permissions WHERE user_id = %s ORDER BY shop_id",
                            (user_id,)).fetchall()
    return [r[0] for r in rows]
