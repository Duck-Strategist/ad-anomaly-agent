"""歧义反问：校验模型提出的反问、把用户的回答映射回候选指标、兜底文案。

原则和"LLM 不写 SQL"一样：要不要问由 LLM 判断（它能看懂上下文），
但能问哪个词、能给哪些选项、用户最终选了什么，都由代码对照 catalog.yaml 校验。
设计记录见 SPEC 第七节"反问的实现方式"。
"""

from __future__ import annotations

from .llm import LLM
from .semantic.catalog import Catalog

MAX_ASKS = 2   # 同一个词最多问 2 次（第二次明确列出选项），仍对不上就兜底


def validate_clarify(clarify: object, cat: Catalog, resolved: dict[str, str]) -> str | None:
    """校验模型输出的 {"term", "options"}。合法返回 None，否则返回回传给模型的错误说明。"""
    terms = cat.ambiguous_terms
    if not isinstance(clarify, dict):
        return 'clarify 必须是对象：{"term": "...", "options": ["指标名", ...]}'
    term, options = clarify.get("term"), clarify.get("options")
    if term not in terms:
        return f"clarify.term 只能是以下说法之一：{', '.join(terms)}；其他情况不要反问。"
    if term in resolved:
        return f'用户已经确认"{term}"指 {resolved[term]}，不要再反问，请直接按这个口径输出 MQL。'
    if (not isinstance(options, list) or len(set(options)) < 2
            or any(o not in terms[term] for o in options)):
        return f'"{term}"的 options 必须是 {terms[term]} 中的至少 2 个。'
    return None


def check_resolved_used(mql: dict, resolved: dict[str, str]) -> str | None:
    """用户选定的口径必须出现在最终 MQL 里，不靠 prompt "希望"模型遵守。"""
    metrics = mql.get("metrics") if isinstance(mql, dict) else None
    missing = [f'"{t}"→{m}' for t, m in resolved.items() if not isinstance(metrics, list) or m not in metrics]
    if missing:
        return f"用户已确认口径：{'，'.join(missing)}，MQL 的 metrics 里必须使用这些指标。"
    return None


def options_with_desc(options: list[str], cat: Catalog) -> list[dict]:
    return [{"metric": m, "desc": cat.metrics[m]["desc"]} for m in options]


MAP_PROMPT = """系统刚才问用户："{term}"指的是哪个口径。候选项：
{options}

请判断用户的回答选的是哪一个。只输出 JSON：{{"choice": "指标名"}}。
如果用户的回答无法对应到上面任何一个候选项（含糊、答非所问、或说的是别的口径），输出 {{"choice": "none"}}。不要猜。"""


def map_reply(reply: str, term: str, options: list[str], cat: Catalog, llm: LLM) -> str | None:
    """把用户回答映射到候选指标。按钮（回答正好是指标名）直接采用；自由文本交给 LLM 多选一。"""
    reply = (reply or "").strip()
    if reply in options:
        return reply
    if not reply:
        return None
    lines = "\n".join(f"- {o['metric']}：{o['desc']}" for o in options_with_desc(options, cat))
    call = llm.chat_json([{"role": "system", "content": MAP_PROMPT.format(term=term, options=lines)},
                          {"role": "user", "content": reply}])
    choice = (call.parsed or {}).get("choice") if not call.error else None
    return choice if choice in options else None   # 模型给出候选项以外的值，一律当作没对上


def ask_payload(term: str, options: list[str], asks: int, cat: Catalog) -> dict:
    opts = options_with_desc(options, cat)
    if asks == 0:
        question = f'您说的"{term}"有几种口径，请问指的是哪一个？'
    else:
        question = f'没能确定您的意思。"{term}"请从下面选一个：' + "；".join(f"{o['desc']}（{o['metric']}）" for o in opts)
    return {"term": term, "question": question, "options": opts, "retry": asks > 0}


def unresolved_message(term: str, options: list[str], cat: Catalog) -> str:
    defs = "；".join(f"{o['metric']} = {o['desc']}" for o in options_with_desc(options, cat))
    return (f'没能确定"{term}"指哪个口径，为避免给出错误数字，这次不返回结果。'
            f"各口径定义：{defs}。请换个更明确的说法重新提问。")
