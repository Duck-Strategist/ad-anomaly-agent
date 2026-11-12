"""歧义反问测试。用脚本化的假 LLM，只测校验、图的流转和兜底，不测模型判断得准不准（那是评测的事）。

覆盖：候选项校验、按钮回答不调 LLM、自由文本映射、两次没对上就兜底且不给数字、
恢复时不重复调用 nl2mql、对已确认的词再次反问会被拒、最终 MQL 必须用所选指标。
"""

import json

import pytest

from adagent.clarify import check_resolved_used, map_reply, validate_clarify
from adagent.llm import LLMCall
from adagent.semantic.catalog import load_catalog
from adagent.workflow import answer_clarification, build_graph, open_checkpointer, run

CAT = load_catalog()
Q = "美国店上周销售额多少？"
ASK = {"clarify": {"term": "销售额", "options": ["gross_sales", "net_sales", "ad_sales"]}}


def mql(metric):
    return {"mql": {"metrics": [metric], "filters": {"shop": ["US"]}, "time": {"range": "last_week"}}}


class ScriptLLM:
    """按 system prompt 区分调用方：路由固定返回问数；nl2mql 和回答映射按脚本依次返回。"""

    def __init__(self, nl2mql: list[dict], mapping: list[dict] = ()):
        self.nl2mql, self.mapping = list(nl2mql), list(mapping)
        self.calls = {"route": 0, "nl2mql": 0, "map": 0}
        self.messages: list[list[dict]] = []

    def chat_json(self, messages):
        sys_prompt = messages[0]["content"]
        if "路由器" in sys_prompt:
            kind, out = "route", {"intent": "query"}
        elif "系统刚才问用户" in sys_prompt:
            kind, out = "map", self.mapping.pop(0)
        else:
            kind, out = "nl2mql", self.nl2mql.pop(0)
            self.messages.append(messages)
        self.calls[kind] += 1
        return LLMCall("fake", messages, json.dumps(out, ensure_ascii=False), 0, 0, 0.0, parsed=out)


@pytest.fixture(scope="module")
def saver():
    return open_checkpointer()


# ---------------- 校验（不需要数据库） ----------------

@pytest.mark.parametrize("c, ok", [
    ({"term": "销售额", "options": ["gross_sales", "net_sales"]}, True),
    ({"term": "销售额", "options": ["gross_sales", "profit"]}, False),      # 目录外的指标
    ({"term": "销售额", "options": ["gross_sales"]}, False),                # 少于 2 个
    ({"term": "销售额", "options": ["gross_sales", "gross_sales"]}, False), # 重复不算 2 个
    ({"term": "销售额", "options": ["roas", "net_roas"]}, False),           # 别的词的候选
    ({"term": "利润", "options": ["gross_sales", "net_sales"]}, False),     # 不在 ambiguous_terms
    ("销售额", False),
])
def test_validate_clarify(c, ok):
    assert (validate_clarify(c, CAT, {}) is None) == ok


def test_cannot_reask_resolved_term():
    err = validate_clarify({"term": "销售额", "options": ["gross_sales", "net_sales"]}, CAT, {"销售额": "net_sales"})
    assert err and "net_sales" in err


def test_resolved_metric_must_be_used():
    assert check_resolved_used(mql("net_sales")["mql"], {"销售额": "net_sales"}) is None
    assert check_resolved_used(mql("gross_sales")["mql"], {"销售额": "net_sales"})


def test_map_reply_rejects_out_of_options():
    llm = ScriptLLM([], mapping=[{"choice": "roas"}])     # 模型给了候选项以外的值
    assert map_reply("广告那个", "销售额", ["gross_sales", "net_sales"], CAT, llm) is None


# ---------------- 工作流（需要数据库） ----------------

def test_button_reply_resolves_without_llm(saver):
    llm = ScriptLLM([ASK, mql("net_sales")])
    g = build_graph(llm, saver)
    r = run(Q, "u_admin", g)
    assert r["state"]["status"] == "awaiting_clarification"
    p = r["pending_clarification"]
    assert [o["metric"] for o in p["options"]] == ["gross_sales", "net_sales", "ad_sales"] and not p["retry"]

    r = answer_clarification(r["thread_id"], "net_sales", g)
    assert r["state"]["status"] == "answered"
    assert r["state"]["answer"]["mql"]["metrics"] == ["net_sales"]
    assert r["state"]["answer"]["rows"]
    assert llm.calls["map"] == 0                          # 按钮值是结构化的，不需要模型理解
    assert llm.calls["nl2mql"] == 2                       # 第一次 + 确认后一次；恢复时没有重复调用
    assert "net_sales" in llm.messages[1][-1]["content"]  # 确认的口径传回给了模型
    again = answer_clarification(r["thread_id"], "net_sales", g)
    assert again["already_processed"]


def test_free_text_reply_mapped(saver):
    llm = ScriptLLM([ASK, mql("net_sales")], mapping=[{"choice": "net_sales"}])
    g = build_graph(llm, saver)
    r = answer_clarification(run(Q, "u_admin", g)["thread_id"], "就是扣了退款的那个", g)
    assert r["state"]["status"] == "answered"
    assert r["state"]["resolved_terms"] == {"销售额": "net_sales"}
    assert llm.calls["map"] == 1


def test_two_misses_give_no_number(saver):
    llm = ScriptLLM([ASK], mapping=[{"choice": "none"}, {"choice": "none"}])
    g = build_graph(llm, saver)
    tid = run(Q, "u_admin", g)["thread_id"]
    r = answer_clarification(tid, "随便", g)
    assert r["pending_clarification"]["retry"]            # 第二次明确列出选项
    assert "gross_sales" in r["pending_clarification"]["question"]
    r = answer_clarification(tid, "不知道", g)
    assert r["state"]["status"] == "unresolved"
    assert "answer" not in r["state"]                     # 不给数字
    assert "net_sales" in r["state"]["message"]           # 列出各口径定义
    assert r["pending_clarification"] is None
    assert llm.calls["nl2mql"] == 1


def test_model_ignoring_choice_is_corrected(saver):
    # 用户选了 net_sales，模型先输出 gross_sales、再对同一个词反问，两次都被代码拦下并回传，第三次改对
    llm = ScriptLLM([ASK, mql("gross_sales"), ASK, mql("net_sales")])
    g = build_graph(llm, saver)
    r = answer_clarification(run(Q, "u_admin", g)["thread_id"], "net_sales", g)
    assert r["state"]["status"] == "answered"
    assert r["state"]["answer"]["mql"]["metrics"] == ["net_sales"]
    assert llm.calls["nl2mql"] == 4


def test_unambiguous_question_does_not_ask(saver):
    llm = ScriptLLM([mql("ad_sales")])
    r = run("美国店上周广告销售额多少？", "u_admin", build_graph(llm, saver))
    assert r["state"]["status"] == "answered" and r["pending_clarification"] is None
