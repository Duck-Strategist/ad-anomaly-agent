"""数据库角色的安全边界测试（需要 docker compose 里的数据库在运行）。

对应 SPEC 验收标准第 3 条：只读账号无法写；超时的查询会被中止；
以及"最小权限"：Agent 查询读不到权限表 / 审计表，应用账号只能改 ad_groups.status。
"""

import os

import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv()


def connect(role: str) -> psycopg.Connection:
    prefix = {"reader": "PG_READER", "app": "PG_APP"}[role]
    return psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "54329"),
        dbname=os.getenv("PG_DB", "adagent"),
        user=os.getenv(f"{prefix}_USER"), password=os.getenv(f"{prefix}_PASSWORD"),
        autocommit=True,
    )


# ---------- agent_reader ----------

def test_reader_can_read_business_tables():
    with connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ad_daily_stats").fetchone()[0] > 0


@pytest.mark.parametrize("sql", [
    "UPDATE ad_groups SET status = 'paused'",
    "DELETE FROM orders",
    "INSERT INTO shops VALUES ('XX', 'x', 'x', 'USD', 'UTC')",
    "CREATE TABLE evil (id int)",
])
def test_reader_cannot_write(sql):
    with connect("reader") as conn:
        with pytest.raises(psycopg.errors.Error):
            conn.execute(sql)


@pytest.mark.parametrize("table", ["user_shop_permissions", "action_audit_log"])
def test_reader_cannot_see_app_metadata(table):
    with connect("reader") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {table}")


def test_reader_statement_timeout():
    with connect("reader") as conn:
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(6)")


# ---------- agent_app ----------

def test_app_can_only_update_status_column():
    with connect("app") as conn:
        with conn.transaction(force_rollback=True):  # 测试完回滚，不改动数据
            conn.execute("UPDATE ad_groups SET status = 'paused' WHERE ad_group_id = 'US-AG01'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE ad_groups SET daily_budget = 0 WHERE ad_group_id = 'US-AG01'")


def test_app_cannot_read_orders():
    with connect("app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM orders LIMIT 1")


def test_audit_log_rejection_requires_reason():
    with connect("app") as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction(force_rollback=True):
                conn.execute(
                    "INSERT INTO action_audit_log (proposal_id, idempotency_key, action, ad_group_id, requested_by, status)"
                    " VALUES ('p1', 'k1', 'pause_ad_group', 'US-AG01', 'u_ops_us', 'rejected')"
                )


def test_audit_log_idempotency_key_unique():
    with connect("app") as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction(force_rollback=True):
                for _ in range(2):
                    conn.execute(
                        "INSERT INTO action_audit_log (proposal_id, idempotency_key, action, ad_group_id, requested_by, status)"
                        " VALUES ('p1', 'k-dup', 'pause_ad_group', 'US-AG01', 'u_ops_us', 'pending')"
                    )
