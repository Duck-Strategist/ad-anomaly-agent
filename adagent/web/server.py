"""极简前端的服务端（SPEC Q22）。

工作流的三个对外函数各对应一个接口：run → /api/ask，answer_clarification → /api/clarify，resume → /api/approve。
所有判断都在服务端：
- "我是谁"只从签名 cookie 的会话里取，请求体里没有 user_id / approver_id。
- 会话归属：只能查看、回答、审批自己发起的 thread（thread_id 不是访问凭证）。
- 谁能审批、拒绝必须填理由、重复点击只执行一次，仍由 workflow.resume / actions 保证。
演示登录不是认证：下拉框里谁都能选。生产环境换成真实认证时只改 /api/login。

启动：uv run uvicorn adagent.web.server:app --port 8000
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .. import actions, tracing, workflow
from ..db import app_conn
from ..llm import LLM
from ..semantic.catalog import load_catalog
from ..semantic.mql import MQL, resolve_window

HERE = Path(__file__).resolve().parent
MODEL = os.getenv("MAIN_PROVIDER", "deepseek")
_saver: PostgresSaver | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _saver
    # 连接池：多个请求并发时，checkpoint 不共用同一个连接
    pool = ConnectionPool(
        conninfo="", max_size=8, open=True,
        kwargs=dict(host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "54329"),
                    dbname=os.getenv("PG_DB", "adagent"), user=os.environ["PG_GRAPH_USER"],
                    password=os.environ["PG_GRAPH_PASSWORD"], autocommit=True, prepare_threshold=0,
                    row_factory=dict_row, connect_timeout=5))
    _saver = PostgresSaver(pool)
    _saver.setup()
    yield
    tracing.shutdown()   # 把还没发出去的 trace 发完
    pool.close()


app = FastAPI(title="经营异常归因 Agent（演示）", lifespan=lifespan)
# 没配置 SESSION_SECRET 时每次启动随机生成：重启后需要重新"登录"，演示足够
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET") or secrets.token_hex(32),
                   same_site="lax", max_age=8 * 3600)


def graph():
    # 每个请求一个新的 LLM 实例（LLM 会累积调用记录）；状态都在 checkpoint 里，图本身无状态
    return workflow.build_graph(LLM(MODEL), _saver)


# ---------------- 身份 ----------------

def demo_users() -> dict[str, list[dict]]:
    with app_conn() as conn:
        rows = conn.execute("SELECT user_id, shop_id, role FROM user_shop_permissions ORDER BY user_id, shop_id").fetchall()
    out: dict[str, list[dict]] = {}
    for user_id, shop_id, role in rows:
        out.setdefault(user_id, []).append({"shop": shop_id, "role": role})
    return out


def current_user(request: Request) -> str:
    user = request.session.get("user_id")
    if not user:
        raise HTTPException(401, "请先在页面顶部选择演示用户")
    return user


def is_approver_for(values: dict, user: str) -> bool:
    """这个会话带有暂停提议，并且当前用户是该广告组所在店铺的审批人。"""
    proposal = values.get("proposal")
    if not proposal:
        return False
    try:
        actions.check_approver(user, proposal["ad_group_id"])
        return True
    except actions.ApprovalError:
        return False


def owned_thread(thread_id: str, user: str, g, approver_ok: bool = False) -> None:
    """访问会话的规则：发起人可以；approver_ok 时，提议所在店铺的审批人也可以（查看和审批别人发起的提议）。
    其他人一律当作不存在（不泄露它是否存在）。回答反问只允许发起人。"""
    values = g.get_state({"configurable": {"thread_id": thread_id}}).values
    if values and (values.get("user_id") == user or (approver_ok and is_approver_for(values, user))):
        return
    raise HTTPException(404, "找不到这个会话")


class Login(BaseModel):
    user_id: str


@app.get("/api/users")
def users():
    return demo_users()


@app.post("/api/login")
def login(body: Login, request: Request):
    if body.user_id not in demo_users():
        raise HTTPException(400, "没有这个演示用户")
    request.session["user_id"] = body.user_id
    return {"user_id": body.user_id}


@app.get("/api/me")
def me(request: Request):
    user = request.session.get("user_id")
    return {"user_id": user, "permissions": demo_users().get(user, []) if user else []}


# ---------------- 工作流 ----------------

def week_ranges(answer: dict, today: date) -> dict[str, str]:
    """按周拆分时，每个"周一日期"实际覆盖的起止日期（截到查询范围内）。SPEC 第十节展示待办。"""
    try:
        mql = MQL.model_validate(answer["mql"])
    except Exception:
        return {}
    if "week" not in mql.dimensions:
        return {}
    start, end = resolve_window(mql.time, today)
    out = {}
    for row in answer.get("rows", []):
        wk = row.get("week")
        if not wk:
            continue
        monday = date.fromisoformat(str(wk)[:10])
        lo, hi = max(monday, start), min(monday + timedelta(days=6), end)
        out[str(wk)] = f"{lo:%m-%d}" if lo == hi else f"{lo:%m-%d}~{hi:%m-%d}"
    return out


def view(res: dict, user: str) -> dict:
    """工作流结果 → 前端要显示的字段。"""
    s = res["state"]
    out = {k: s.get(k) for k in ("question", "status", "message", "facts", "advice", "advice_check", "basis",
                                 "conclusion", "proposal", "decision", "execution")}
    out["thread_id"] = res["thread_id"]
    out["requested_by"] = s.get("user_id")
    out["trace_url"] = tracing.trace_url(res["thread_id"])
    out["intent"] = (s.get("plan") or {}).get("intent")
    out["already_processed"] = res.get("already_processed", False)
    if s.get("answer"):
        out["answer"] = {**s["answer"], "week_ranges": week_ranges(s["answer"], load_catalog().today)}
    out["clarification"] = res.get("pending_clarification")
    if res.get("pending_approval") is not None:
        out["status"] = "awaiting_approval"
        try:   # 只决定按钮是否可用；真正的校验在 resume 里
            actions.check_approver(user, s["proposal"]["ad_group_id"])
            out["can_approve"] = True
        except actions.ApprovalError:
            out["can_approve"] = False
    return jsonable_encoder(out)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class Clarify(BaseModel):
    thread_id: str
    reply: str = Field(min_length=1, max_length=200)


class Approve(BaseModel):
    thread_id: str
    approved: bool
    reason: str | None = Field(default=None, max_length=500)


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    user = current_user(request)
    return view(workflow.run(body.question, user, graph()), user)


@app.post("/api/clarify")
def clarify(body: Clarify, request: Request):
    user, g = current_user(request), graph()
    owned_thread(body.thread_id, user, g)
    return view(workflow.answer_clarification(body.thread_id, body.reply, g), user)


@app.post("/api/approve")
def approve(body: Approve, request: Request):
    user, g = current_user(request), graph()
    owned_thread(body.thread_id, user, g, approver_ok=True)
    try:
        # approver_id 来自会话，不来自请求体
        res = workflow.resume(body.thread_id, user, body.approved, g, reason=body.reason)
    except actions.ApprovalError as e:
        raise HTTPException(403 if "not an approver" in str(e) else 400, str(e))
    return view(res, user)


@app.get("/api/thread/{thread_id}")
def thread(thread_id: str, request: Request):
    """请求中断（超时、刷新页面）后按 thread_id 取回结果。"""
    user, g = current_user(request), graph()
    owned_thread(thread_id, user, g, approver_ok=True)
    config = {"configurable": {"thread_id": thread_id}}
    return view(workflow._result(g, config, {}), user)


@app.get("/api/pending")
def pending(request: Request):
    """待我审批：当前用户是审批人的店铺下、24 小时内、还没处理的提议，不管是谁发起的。
    这里只是列出来；打开和审批时仍走 owned_thread + resume 的校验。"""
    user = current_user(request)
    shops = [p["shop"] for p in demo_users().get(user, []) if p["role"] == "approver"]
    if not shops:
        return []
    with app_conn() as conn:
        rows = conn.execute(
            """SELECT a.trace_id, a.ad_group_id, a.requested_by, a.created_at
                 FROM action_audit_log a
                 JOIN ad_groups g ON g.ad_group_id = a.ad_group_id
                 JOIN ad_campaigns c ON c.campaign_id = g.campaign_id
                WHERE c.shop_id = ANY(%s) AND a.status = 'pending' AND a.trace_id NOT LIKE 'eval-%%'
                  AND a.created_at > now() - interval '24 hours'
                ORDER BY a.id DESC LIMIT 20""", (shops,)).fetchall()
    g = graph()
    out = []
    for trace_id, ag, by, created in rows:
        values = g.get_state({"configurable": {"thread_id": trace_id}}).values
        out.append({"thread_id": trace_id, "ad_group_id": ag, "requested_by": by, "created_at": created,
                    "question": values.get("question")})
    return jsonable_encoder(out)


@app.get("/api/audit")
def audit(request: Request):
    """当前用户有权限的店铺下最近的审批记录，以及广告组现在的状态。评测产生的记录（trace_id 以 eval- 开头）不显示。"""
    user = current_user(request)
    shops = [p["shop"] for p in demo_users().get(user, [])]
    with app_conn() as conn:
        rows = conn.execute(
            """SELECT a.id, a.ad_group_id, g.status AS ad_group_status, a.requested_by, a.decided_by, a.status,
                      a.reason, a.trace_id, a.created_at, a.decided_at
                 FROM action_audit_log a
                 JOIN ad_groups g ON g.ad_group_id = a.ad_group_id
                 JOIN ad_campaigns c ON c.campaign_id = g.campaign_id
                WHERE c.shop_id = ANY(%s) AND a.trace_id NOT LIKE 'eval-%%'
                ORDER BY a.id DESC LIMIT 20""", (shops,)).fetchall()
        cols = ["id", "ad_group_id", "ad_group_status", "requested_by", "decided_by", "status", "reason",
                "trace_id", "created_at", "decided_at"]
    return jsonable_encoder([dict(zip(cols, r)) for r in rows])


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")
