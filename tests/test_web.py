"""极简前端的服务端（SPEC Q22）：身份只来自会话、会话归属、审批仍由服务端校验。用假 LLM，不调模型。"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from adagent.web import server
from tests.test_workflow import E1_PLAN, FakeLLM, ad_group_status, clean_state  # noqa: F401（clean_state 是 autouse fixture）


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "graph", lambda: server.workflow.build_graph(FakeLLM(E1_PLAN), server._saver))
    with TestClient(server.app) as c:
        yield c


def login(c, user):
    assert c.post("/api/login", json={"user_id": user}).status_code == 200


def test_requires_login(client):
    assert client.post("/api/ask", json={"question": "美国店上周表现"}).status_code == 401
    assert client.post("/api/login", json={"user_id": "u_nobody"}).status_code == 400


def test_user_id_in_body_is_ignored(client):
    """请求体里塞 user_id 也没用：身份只看会话。u_ops_uk 问美国店 → 无权限。"""
    login(client, "u_ops_uk")
    r = client.post("/api/ask", json={"question": "美国店上周表现", "user_id": "u_admin"}).json()
    assert r["status"] == "refused"


def test_approve_flow_and_double_click(client):
    login(client, "u_admin")
    r = client.post("/api/ask", json={"question": "美国店上周表现"}).json()
    assert r["status"] == "awaiting_approval" and r["can_approve"] and r["proposal"]["ad_group_id"] == "US-AG07"
    tid = r["thread_id"]
    assert client.post("/api/approve", json={"thread_id": tid, "approved": False}).status_code == 400   # 拒绝要理由
    a = client.post("/api/approve", json={"thread_id": tid, "approved": True, "approver_id": "u_viewer_us"}).json()
    assert a["execution"]["status"] == "executed" and a["decision"]["by"] == "u_admin"   # approver 来自会话
    b = client.post("/api/approve", json={"thread_id": tid, "approved": True}).json()
    assert b["already_processed"] and ad_group_status("US-AG07") == "paused"
    audit = client.get("/api/audit").json()
    assert [x["status"] for x in audit if x["trace_id"] == tid] == ["executed"]


def test_other_users_thread_is_not_found(client):
    login(client, "u_admin")
    tid = client.post("/api/ask", json={"question": "美国店上周表现"}).json()["thread_id"]
    login(client, "u_ops_uk")   # 不是这个店铺的审批人：当作不存在
    assert client.get(f"/api/thread/{tid}").status_code == 404
    assert client.post("/api/approve", json={"thread_id": tid, "approved": True}).status_code == 404
    assert ad_group_status("US-AG07") == "enabled"


def test_approver_sees_and_approves_proposal_raised_by_viewer(client):
    """只读用户发起的提议：同店铺的审批人在"待我审批"里看到并审批；别的店铺的审批人看不到、打不开。"""
    login(client, "u_viewer_us")
    tid = client.post("/api/ask", json={"question": "美国店上周表现"}).json()["thread_id"]
    assert client.get("/api/pending").json() == []          # 只读用户没有待审批列表

    login(client, "u_ops_uk")
    assert client.get("/api/pending").json() == []
    assert client.get(f"/api/thread/{tid}").status_code == 404

    login(client, "u_ops_us")
    pend = client.get("/api/pending").json()
    assert [(x["thread_id"], x["requested_by"]) for x in pend] == [(tid, "u_viewer_us")]
    t = client.get(f"/api/thread/{tid}").json()
    assert t["can_approve"] and t["requested_by"] == "u_viewer_us"
    assert client.post("/api/clarify", json={"thread_id": tid, "reply": "net_sales"}).status_code == 404   # 反问只给发起人
    a = client.post("/api/approve", json={"thread_id": tid, "approved": True}).json()
    assert a["execution"]["status"] == "executed" and a["decision"]["by"] == "u_ops_us"
    assert client.get("/api/pending").json() == []


def test_viewer_cannot_approve_own_proposal(client):
    login(client, "u_viewer_us")
    r = client.post("/api/ask", json={"question": "美国店上周表现"}).json()
    assert r["status"] == "awaiting_approval" and r["can_approve"] is False
    assert client.post("/api/approve", json={"thread_id": r["thread_id"], "approved": True}).status_code == 403
    assert ad_group_status("US-AG07") == "enabled"


def test_week_ranges_are_clipped_to_window():
    answer = {"mql": {"metrics": ["net_roas"], "dimensions": ["week"],
                      "time": {"range": "custom", "start": "2026-10-01", "end": "2026-10-31"}},
              "rows": [{"week": "2026-09-28"}, {"week": "2026-10-05"}, {"week": "2026-10-26"}]}
    assert server.week_ranges(answer, date(2026, 11, 8)) == {
        "2026-09-28": "10-01~10-04", "2026-10-05": "10-05~10-11", "2026-10-26": "10-26~10-31"}


def test_degrade_returns_last_sql_and_attempts(monkeypatch):
    """SPEC 第五节第 6 条：超过重试上限后降级，返回最后一次的 MQL、SQL 和每一轮的过程。"""
    from adagent import nl2mql
    from adagent.llm import LLMCall
    from adagent.workflow import _attempts_view

    mql = {"metrics": ["ad_spend"], "filters": {"shop": ["US"]}, "time": {"range": "last_7_days"}}

    class AlwaysSameMQL:
        def chat_json(self, messages):
            out = {"mql": mql}
            return LLMCall("fake", messages, '{"mql": "..."}', 0, 0, 0.0, parsed=out)

    monkeypatch.setattr(nl2mql, "query_metrics", lambda m, u: {
        "ok": False, "error_type": "execution", "errors": ["canceling statement due to statement timeout"],
        "mql": m, "sql": ["SELECT 1 /* last */"]})
    a = nl2mql.answer_question("美国店上周花费", "u_admin", AlwaysSameMQL())
    assert a.status == "failed" and a.mql == mql and a.sql == ["SELECT 1 /* last */"]
    assert "statement timeout" in a.message and len(a.attempts) == nl2mql.MAX_RETRIES + 1
    view = _attempts_view(a.attempts)
    assert [v["error_type"] for v in view] == ["execution"] * 3 and all(v["feedback"] for v in view)
