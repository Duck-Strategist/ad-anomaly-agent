"""唯一的写动作"暂停广告组"：提议 → 审批 → 执行，全部幂等。

状态机（action_audit_log.status）：
    pending ──批准──→ approved ──执行成功──→ executed
       │                  └──广告组状态已变──→ stale（未执行）
       ├──拒绝（必须填理由）──→ rejected
       └──超过 24h 未审批──→ expired

防重复的核心：用数据库的**原子条件更新**抢占执行权。
    UPDATE ... SET status='approved' WHERE idempotency_key=? AND status='pending'
并发的两个请求里只有一个能把 pending 改掉（影响 1 行），另一个影响 0 行，直接返回已有结果。
不用"先查再写"：查和写之间有时间窗口，两个请求可能同时查到 pending。

全部使用 agent_app 账号：只能读写审计表、只能改 ad_groups.status。
"""

from __future__ import annotations

import json
import uuid

from .db import app_conn

ACTION = "pause_ad_group"
EXPIRE_HOURS = 24


class ApprovalError(Exception):
    """审批请求本身不合法（无审批权限、拒绝不填理由等）。不改变任何状态。"""


def new_proposal_id() -> str:
    return uuid.uuid4().hex


def idempotency_key(ad_group_id: str, proposal_id: str) -> str:
    return f"{ACTION}:{ad_group_id}:{proposal_id}"


def shop_of_ad_group(ad_group_id: str) -> str:
    with app_conn() as conn:
        row = conn.execute("""SELECT c.shop_id FROM ad_groups g JOIN ad_campaigns c ON c.campaign_id = g.campaign_id
                              WHERE g.ad_group_id = %s""", (ad_group_id,)).fetchone()
    if not row:
        raise ApprovalError(f"unknown ad group {ad_group_id}")
    return row[0]


def create_proposal(proposal_id: str, ad_group_id: str, requested_by: str, params: dict,
                    trace_id: str | None = None) -> dict:
    """写一条 pending 记录。重复调用（例如节点重跑）不会产生第二条：ON CONFLICT DO NOTHING。"""
    key = idempotency_key(ad_group_id, proposal_id)
    with app_conn() as conn:
        conn.execute(
            """INSERT INTO action_audit_log (proposal_id, idempotency_key, action, ad_group_id, params,
                                             requested_by, status, trace_id)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
               ON CONFLICT (idempotency_key) DO NOTHING""",
            (proposal_id, key, ACTION, ad_group_id, json.dumps(params, ensure_ascii=False), requested_by, trace_id))
        conn.commit()
    return {"proposal_id": proposal_id, "idempotency_key": key}


def check_approver(user_id: str, ad_group_id: str) -> None:
    shop = shop_of_ad_group(ad_group_id)
    with app_conn() as conn:
        row = conn.execute("SELECT role FROM user_shop_permissions WHERE user_id = %s AND shop_id = %s",
                           (user_id, shop)).fetchone()
    if not row or row[0] != "approver":
        raise ApprovalError(f"user {user_id} is not an approver for shop {shop}")


def _current(conn, key: str) -> dict:
    status, result = conn.execute("SELECT status, result FROM action_audit_log WHERE idempotency_key = %s",
                                  (key,)).fetchone()
    return {"status": status, "result": result, "already_processed": True}


def execute_pause(key: str, ad_group_id: str, decided_by: str, reason: str | None = None) -> dict:
    """批准后执行。可以安全地重复调用：只有第一次真正执行。"""
    with app_conn() as conn, conn.transaction():
        # 1. 抢占：只有仍是 pending 且未过期的提议才能被批准
        won = conn.execute(
            f"""UPDATE action_audit_log SET status = 'approved', decided_by = %s, decided_at = now(), reason = %s
                WHERE idempotency_key = %s AND status = 'pending'
                  AND created_at > now() - interval '{EXPIRE_HOURS} hours'""",
            (decided_by, reason, key)).rowcount
        if not won:
            expired = conn.execute(
                """UPDATE action_audit_log SET status = 'expired', decided_at = now()
                   WHERE idempotency_key = %s AND status = 'pending'""", (key,)).rowcount
            if expired:
                return {"status": "expired", "result": None, "already_processed": False}
            return _current(conn, key)

        # 2. 条件更新：广告组必须仍是 enabled 且未删除，否则说明状态已被别人改过
        paused = conn.execute(
            """UPDATE ad_groups SET status = 'paused'
               WHERE ad_group_id = %s AND status = 'enabled' AND deleted_at IS NULL""",
            (ad_group_id,)).rowcount
        status = "executed" if paused else "stale"
        result = {"ad_group_id": ad_group_id, "new_status": "paused"} if paused else {
            "ad_group_id": ad_group_id, "note": "广告组状态已变更（已暂停或已删除），未执行"}
        conn.execute("UPDATE action_audit_log SET status = %s, result = %s WHERE idempotency_key = %s",
                     (status, json.dumps(result, ensure_ascii=False), key))
    return {"status": status, "result": result, "already_processed": False}


def reject(key: str, decided_by: str, reason: str) -> dict:
    if not reason or not reason.strip():
        raise ApprovalError("rejection requires a reason")
    with app_conn() as conn, conn.transaction():
        n = conn.execute(
            """UPDATE action_audit_log SET status = 'rejected', decided_by = %s, decided_at = now(), reason = %s
               WHERE idempotency_key = %s AND status = 'pending'""", (decided_by, reason, key)).rowcount
        if not n:
            return _current(conn, key)
    return {"status": "rejected", "result": None, "already_processed": False}
