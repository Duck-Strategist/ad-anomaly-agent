"""工作流类评测的判分测试。用脚本化的假 LLM，测判分逻辑本身，不测模型。

覆盖：歧义题两步判分（全对 / ① 选项缺失 / 没反问 / 不该问却问了）、评测 thread_id 前缀、
归因 pass^3（3 次里错 1 次就不算过）、评测产生的提议带 eval- 标记且不被执行。
"""

import json
from pathlib import Path

import psycopg
import pytest

from adagent.llm import LLMCall
from adagent.workflow import build_graph, open_checkpointer
from datagen.load import admin_dsn
from eval.check_reference import load_golds, load_questions
from eval.workflow_eval import run_ambiguity, run_attribution, score_conclusion

AMB = {q["id"]: q for q in load_questions(Path(__file__).resolve().parent.parent / "eval" / "questions" / "m2_ambiguity.yaml")}


class ScriptLLM:
    """路由按 plans 依次返回；nl2mql、回答映射按脚本依次返回；解释返回固定文本。"""

    def __init__(self, plans=({"intent": "query"},), nl2mql=(), mapping=()):
        self.plans, self.nl2mql, self.mapping = list(plans), list(nl2mql), list(mapping)
        self.calls = []

    def chat_json(self, messages):
        sp = messages[0]["content"]
        if "路由器" in sp:
            out = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        elif "系统刚才问用户" in sp:
            out = self.mapping.pop(0)
        elif "广告分析师" in sp:
            out = {"explanation": "fake"}
        else:
            out = self.nl2mql.pop(0)
        call = LLMCall("fake", messages, json.dumps(out, ensure_ascii=False), 0, 0, 0.0, parsed=out)
        self.calls.append(call)
        return call


def admin(sql, params=()):
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture(autouse=True)
def clean_state():
    reset = lambda: (admin("UPDATE ad_groups SET status = 'enabled'"), admin("DELETE FROM action_audit_log"))
    reset()
    yield
    reset()


@pytest.fixture(scope="module")
def saver():
    return open_checkpointer()


def mql_of(qid):
    return {"mql": AMB[qid]["reference_mql"]}


def clarify(term, options):
    return {"clarify": {"term": term, "options": options}}


# ---------------- 歧义题 ----------------

def test_clarify_both_steps_pass(saver):
    llm = ScriptLLM(nl2mql=[clarify("销售额", ["gross_sales", "net_sales", "ad_sales"]), mql_of("AM01")],
                    mapping=[{"choice": "net_sales"}])
    r = run_ambiguity(AMB["AM01"], build_graph(llm, saver), load_golds(AMB["AM01"]))
    assert r["pass"] and r["step1"] and r["step2"], r["reason"]
    assert r["thread_id"].startswith("eval-")
    assert r["resolved_terms"] == {"销售额": "net_sales"}


def test_clarify_wrong_term_fails_step1_only(saver):
    # 问错了词（AM01 歧义在"销售额"，却问了"ROI"）：① 判错；卖家回答后仍查对，② 判对；整题不过
    # 确认 ROI→roas 后，代码要求 MQL 里必须用到 roas，所以第二次输出带上 roas（多出的列不影响比对）
    llm = ScriptLLM(nl2mql=[clarify("ROI", []),
                            {"mql": {**AMB["AM01"]["reference_mql"], "metrics": ["net_sales", "roas"]}}],
                    mapping=[{"choice": "roas"}])
    r = run_ambiguity(AMB["AM01"], build_graph(llm, saver), load_golds(AMB["AM01"]))
    assert not r["pass"] and r["step1"] is False and r["step2"] is True
    assert "expected '销售额'" in r["reason"]


def test_clarify_question_answered_without_asking_fails(saver):
    llm = ScriptLLM(nl2mql=[{"mql": {**AMB["AM01"]["reference_mql"], "metrics": ["gross_sales"]}}])
    r = run_ambiguity(AMB["AM01"], build_graph(llm, saver), load_golds(AMB["AM01"]))
    assert not r["pass"] and r["step1"] is False and "did not ask" in r["reason"]


def test_unmapped_reply_fails_step2(saver):
    llm = ScriptLLM(nl2mql=[clarify("销售额", ["gross_sales", "net_sales"])], mapping=[{"choice": "none"}])
    r = run_ambiguity(AMB["AM01"], build_graph(llm, saver), load_golds(AMB["AM01"]))
    assert r["step1"] and not r["step2"] and "asked again" in r["reason"]


def test_no_clarify_question_that_asks_fails(saver):
    llm = ScriptLLM(nl2mql=[clarify("销售额", ["gross_sales", "ad_sales"])])
    r = run_ambiguity(AMB["AM06"], build_graph(llm, saver), load_golds(AMB["AM06"]))
    assert not r["pass"] and "should not" in r["reason"]


def test_no_clarify_question_answered_correctly_passes(saver):
    llm = ScriptLLM(nl2mql=[mql_of("AM06")])
    r = run_ambiguity(AMB["AM06"], build_graph(llm, saver), load_golds(AMB["AM06"]))
    assert r["pass"], r["reason"]


def test_misrouted_to_attribution_fails(saver):
    plan = {"intent": "attribution", "scope": "shop", "target": "UK", "time": {"range": "last_7_days"}}
    r = run_ambiguity(AMB["AM08"], build_graph(ScriptLLM(plans=[plan]), saver), load_golds(AMB["AM08"]))
    assert not r["pass"] and "routed to attribution" in r["reason"]


# ---------------- 归因题 ----------------

JP_AG08 = {"intent": "attribution", "scope": "ad_group", "target": "JP-AG08",
           "time": {"range": "custom", "start": "2026-10-19", "end": "2026-10-25"}}
Q_ATT = {"id": "X", "question": "JP-AG08 那周为什么 ROI 变差了", "user_id": "u_admin",
         "expect_conclusion": {"significant": True, "main_cause": "ad_aov", "ad_group_id": "JP-AG08", "suggest_pause": False}}


def test_pass_k_requires_every_run_correct(saver):
    # 第 3 次路由把时间理解错了：单次 2/3 正确，pass^3 不通过
    wrong = {**JP_AG08, "time": {"range": "custom", "start": "2026-09-07", "end": "2026-09-13"}}
    llm = ScriptLLM(plans=[JP_AG08, JP_AG08, wrong, wrong])
    r = run_attribution(Q_ATT, lambda: build_graph(llm, saver))
    assert r["runs_passed"] == 2 and not r["pass"]
    llm = ScriptLLM(plans=[JP_AG08])
    assert run_attribution(Q_ATT, lambda: build_graph(llm, saver))["pass"]


def test_eval_proposals_are_marked_and_not_executed(saver):
    e1 = {"intent": "attribution", "scope": "shop", "target": "US", "time": {"range": "last_7_days"}}
    q = {**Q_ATT, "question": "美国店最近 ROAS 为什么掉了？", "user_id": "u_ops_us",
         "expect_conclusion": {"main_cause": "cvr", "ad_group_id": "US-AG07", "suggest_pause": True}}
    r = run_attribution(q, lambda: build_graph(ScriptLLM(plans=[e1]), saver), runs=1)
    assert r["pass"] and r["runs"][0]["pending_approval"]
    rows = admin("SELECT status, trace_id FROM action_audit_log")
    assert rows == [("pending", r["runs"][0]["thread_id"])] and rows[0][1].startswith("eval-")
    assert admin("SELECT status FROM ad_groups WHERE ad_group_id = 'US-AG07'")[0][0] == "enabled"   # 没人审批，不执行


def test_score_conclusion_only_checks_given_fields():
    assert score_conclusion({"significant": False}, {"significant": False, "main_cause": None})[0]
    assert not score_conclusion({"significant": False}, {"significant": True, "main_cause": "cvr"})[0]
    assert not score_conclusion({"significant": False}, None)[0]
