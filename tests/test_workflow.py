"""工作流 + 审批执行测试。用脚本化的假 LLM，只测图和幂等逻辑，不测模型。

覆盖：批准执行、重复审批、拒绝（必须填理由）、非审批人、过期、状态已变（stale）、
并发执行只成功一次、create_proposal 重跑不产生第二条记录、越权归因被拒。
"""

import json
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from adagent import actions
from adagent.llm import LLMCall
from adagent.workflow import build_graph, open_checkpointer, resume, run
from datagen.load import admin_dsn

E1_PLAN = {"intent": "attribution", "scope": "shop", "target": "US", "time": {"range": "last_7_days"}}


class FakeLLM:
    """按 system prompt 区分：路由返回给定的 plan，解释返回固定文本。"""

    def __init__(self, plan: dict):
        self.plan, self.calls = plan, []

    def chat_json(self, messages):
        sys_prompt = messages[0]["content"]
        out = self.plan if "路由器" in sys_prompt else {"explanation": "fake explanation"}
        call = LLMCall("fake", messages, json.dumps(out, ensure_ascii=False), 0, 0, 0.0, parsed=out)
        self.calls.append(call)
        return call


def admin(sql: str, params=()):
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture(autouse=True)
def clean_state():
    """每个测试前后恢复广告组状态、清空审计表。"""
    reset = lambda: (admin("UPDATE ad_groups SET status = 'enabled'"), admin("DELETE FROM action_audit_log"))
    reset()
    yield
    reset()


@pytest.fixture(scope="module")
def saver():
    return open_checkpointer()


def graph_for(plan, saver):
    return build_graph(FakeLLM(plan), saver)


def ad_group_status(ag):
    return admin("SELECT status FROM ad_groups WHERE ad_group_id = %s", (ag,))[0][0]


def audit_rows():
    return admin("SELECT status, decided_by, reason FROM action_audit_log")


def start_e1(saver):
    g = graph_for(E1_PLAN, saver)
    r = run("美国店最近 ROAS 为什么掉了？", "u_ops_us", g)
    assert r["state"]["status"] == "awaiting_approval"
    assert r["pending_approval"]["proposal"]["ad_group_id"] == "US-AG07"
    assert audit_rows() == [("pending", None, None)]     # 提议生成时就写入 pending
    return g, r["thread_id"]


def test_approve_executes_once(saver):
    g, tid = start_e1(saver)
    r = resume(tid, "u_ops_us", True, g)
    assert r["state"]["execution"]["status"] == "executed"
    assert ad_group_status("US-AG07") == "paused"
    # 重复点击"批准"：不再恢复图、不再执行
    again = resume(tid, "u_ops_us", True, g)
    assert again["already_processed"]
    assert audit_rows() == [("executed", "u_ops_us", None)]


def test_reject_requires_reason_and_does_not_execute(saver):
    g, tid = start_e1(saver)
    with pytest.raises(actions.ApprovalError):
        resume(tid, "u_ops_us", False, g, reason="  ")
    assert audit_rows() == [("pending", None, None)]     # 不合法的请求不改变状态
    r = resume(tid, "u_ops_us", False, g, reason="正在大促，先观察两天")
    assert r["state"]["execution"]["status"] == "rejected"
    assert ad_group_status("US-AG07") == "enabled"
    assert audit_rows() == [("rejected", "u_ops_us", "正在大促，先观察两天")]


def test_viewer_cannot_approve(saver):
    g, tid = start_e1(saver)
    with pytest.raises(actions.ApprovalError, match="not an approver"):
        resume(tid, "u_viewer_us", True, g)
    with pytest.raises(actions.ApprovalError):
        resume(tid, "u_ops_uk", True, g)                 # 别家店的审批人也不行
    assert ad_group_status("US-AG07") == "enabled"
    assert "approve" in g.get_state({"configurable": {"thread_id": tid}}).next   # 仍在等待审批


def test_expired_proposal_is_not_executed(saver):
    g, tid = start_e1(saver)
    admin("UPDATE action_audit_log SET created_at = now() - interval '25 hours'")
    r = resume(tid, "u_ops_us", True, g)
    assert r["state"]["execution"]["status"] == "expired"
    assert ad_group_status("US-AG07") == "enabled"


def test_stale_when_ad_group_already_paused(saver):
    g, tid = start_e1(saver)
    admin("UPDATE ad_groups SET status = 'paused' WHERE ad_group_id = 'US-AG07'")   # 有人手动暂停了
    r = resume(tid, "u_ops_us", True, g)
    assert r["state"]["execution"]["status"] == "stale"


def test_concurrent_execution_only_once():
    p = actions.create_proposal(actions.new_proposal_id(), "US-AG03", "u_ops_us", {})
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: actions.execute_pause(p["idempotency_key"], "US-AG03", "u_ops_us"), range(8)))
    assert sum(not r["already_processed"] for r in results) == 1
    assert all(r["status"] == "executed" for r in results)
    assert ad_group_status("US-AG03") == "paused"


def test_create_proposal_rerun_is_harmless():
    pid = actions.new_proposal_id()
    for _ in range(3):   # 模拟节点在写入后、checkpoint 前崩溃，恢复时重跑
        actions.create_proposal(pid, "US-AG07", "u_ops_us", {})
    assert len(audit_rows()) == 1


def test_attribution_permission(saver):
    g = graph_for({**E1_PLAN}, saver)
    r = run("美国店最近 ROAS 为什么掉了？", "u_ops_uk", g)
    assert r["state"]["status"] == "refused"
    assert audit_rows() == []


def test_no_proposal_when_cause_is_promotion(saver):
    g = graph_for({"intent": "attribution", "scope": "ad_group", "target": "JP-AG08",
                   "time": {"range": "custom", "start": "2026-10-19", "end": "2026-10-25"}}, saver)
    r = run("JP-AG08 那周为什么 ROI 变差了", "u_admin", g)
    assert r["state"]["conclusion"]["main_cause"] == "ad_aov"
    assert r["pending_approval"] is None and audit_rows() == []
    # 口径和窗口由代码写明（SPEC Q15），不依赖模型
    assert r["state"]["explanation"].startswith("分析口径：净 ROAS")
    assert "2026-10-19~2026-10-25 vs 2026-10-12~2026-10-18" in r["state"]["basis"]


class AdviceLLM(FakeLLM):
    """建议段依次返回给定的文本，用来测数字检查（SPEC Q21）。"""

    def __init__(self, plan: dict, advices: list[str]):
        super().__init__(plan)
        self.advices = list(advices)

    def chat_json(self, messages):
        if "路由器" in messages[0]["content"]:
            return super().chat_json(messages)
        out = {"advice": self.advices.pop(0)}
        call = LLMCall("fake", messages, json.dumps(out, ensure_ascii=False), 0, 0, 0.0, parsed=out)
        self.calls.append(call)
        return call


JP_AG08 = {"intent": "attribution", "scope": "ad_group", "target": "JP-AG08",
           "time": {"range": "custom", "start": "2026-10-19", "end": "2026-10-25"}}


@pytest.mark.parametrize("advices, kept, retried, dropped", [
    (["先确认 JP-AG08 的商品是否在降价促销。"], True, False, False),
    (["预计两周后能恢复到 7.5。", "先确认 JP-AG08 是否在促销。"], True, True, False),
    (["预计两周后能恢复到 7.5。", "大约 3 天后再看。"], False, True, True),
])
def test_advice_with_new_numbers_is_rewritten_or_dropped(saver, advices, kept, retried, dropped):
    llm = AdviceLLM(JP_AG08, advices)
    r = run("JP-AG08 那周为什么 ROI 变差了", "u_admin", build_graph(llm, saver))
    st = r["state"]
    assert st["advice_check"] == {"retried": retried, "dropped": dropped}
    assert ("建议和说明：" in st["explanation"]) == kept
    assert st["explanation"].startswith(st["facts"]) and "广告客单价下降" in st["facts"]
