"""用 LLM judge 给一批样本打 R1 / R2，并和人工标注比对（SPEC 第九节 / Q21）。

judge 的判分标准直接取自 eval/judge/rubric.md 的 R1、R2 两节（和人工标注用同一份说明，只有一个来源）。
judge 和被测模型不同厂商：被测 DeepSeek，judge 千问。

用法：
  uv run python -m eval.judge_run --batch 1 --model qwen          # 打分 + 和人工标注比对
  uv run python -m eval.judge_run --batch 1 --model qwen --stats  # 只用已存的打分重算统计
输出：eval/judge/batch{N}_judge_{model}.json（每条的原始输出）
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adagent.llm import LLM

from .judge_stats import passes, rates

DIR = Path(__file__).resolve().parent / "judge"
EXCLUDE = {"B1-01"}   # 标注前由 Claude 示范过判法，人工标注不独立（CHANGELOG 2026-11-14）


def rubric_sections() -> str:
    """rubric.md 里从"## R1"到"## 怎么记"之前的部分。"""
    text = (DIR / "rubric.md").read_text(encoding="utf-8")
    m = re.search(r"(## R1.*?)(?=\n## 怎么记)", text, re.S)
    if not m:
        raise ValueError("rubric.md: cannot find the R1 / R2 sections")
    return m.group(1).strip()


def build_prompt() -> str:
    return f"""你是评测员，负责检查一段归因解释文字。下面是判分标准，和人工标注用的是同一份。

{rubric_sections()}

## 你会拿到
- 问题：卖家问的是什么。
- 依据：系统给出解释时能看到的全部信息（代码生成的结论段，或每次查询实际返回的结果原文）。"（没有执行任何查询）"表示依据为空。
- 结构化结论：系统的结论（显著与否、主因、广告组、是否建议暂停）。
- 待评文字：要你判分的那段文字。

## 怎么判
- R1：拿待评文字和结构化结论逐项比，只看有没有矛盾，不看写得全不全。
- R2：把待评文字拆成一条条关于"发生了什么、为什么"的说法（包括每个数字），逐条在依据里找出处。
  数字要核对字段和数值：四舍五入后相同算有依据；用依据里的数算出来的数，要自己算一遍，算对算有依据，算错算不符。
  排查建议、指标的定义不算事实说法。
- 先写理由，再给结论。

只输出 JSON：
{{"r1_reason": "一两句话", "r1": true 或 false,
  "r2_checks": ["说法 → 依据里的出处 / 找不到 / 不符", "..."],
  "r2_reason": "一两句话", "r2": true 或 false}}
true 表示"是"（一致 / 没有编造），false 表示"否"。"""


def user_message(item: dict) -> str:
    return (f"问题：{item['question']}\n\n依据：\n{item['evidence']}\n\n"
            f"结构化结论：{item['conclusion']}\n\n待评文字：{item['text']}")


def judge_one(llm: LLM, system: str, item: dict) -> dict:
    call = llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user_message(item)}])
    out = call.parsed or {}
    ok = not call.error and isinstance(out.get("r1"), bool) and isinstance(out.get("r2"), bool)
    return {"id": item["id"], "ok": ok, "r1": out.get("r1"), "r2": out.get("r2"), "output": out,
            "error": call.error, "model": call.model,
            "tokens": [call.prompt_tokens, call.completion_tokens], "latency_s": call.latency_s}


def run(batch: int, model: str, workers: int) -> Path:
    items = json.loads((DIR / f"batch{batch}.json").read_text(encoding="utf-8"))
    system = build_prompt()
    llm = LLM(model)
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda it: judge_one(llm, system, it), items))
    path = DIR / f"batch{batch}_judge_{model}.json"
    path.write_text(json.dumps({"model": model, "system_prompt": system, "results": results},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def report(batch: int, model: str) -> dict:
    human = json.loads((DIR / f"batch{batch}_human.json").read_text(encoding="utf-8"))["labels"]
    judged = {r["id"]: r for r in json.loads((DIR / f"batch{batch}_judge_{model}.json")
                                            .read_text(encoding="utf-8"))["results"]}
    key_path = DIR / f"batch{batch}_key.json"
    key = json.loads(key_path.read_text(encoding="utf-8")) if key_path.exists() else {}
    ids = sorted(i for i in human if i not in EXCLUDE)
    failed = [i for i in ids if not judged[i]["ok"]]
    ids = [i for i in ids if judged[i]["ok"]]
    out = {"batch": batch, "model": model, "excluded": sorted(EXCLUDE & set(human)), "judge_failed": failed}
    for r in ("r1", "r2"):
        out[r] = rates([human[i][r] for i in ids], [judged[i][r] for i in ids])
        out[r + "_disagree"] = [{"id": i, "human": human[i][r], "judge": judged[i][r],
                                 "source": key.get(i, {}).get("source"), "kind": key.get(i, {}).get("kind")}
                                for i in ids if human[i][r] != judged[i][r]]
    out["passes"] = passes(out["r1"], out["r2"])
    return out


def make_review(batch: int, model: str) -> Path:
    """人工和 judge 有分歧的样本 → batch{N}_review.json（标注页可直接打开）。
    只放样本本身：不放 judge 的理由、不放人工原来的标签、不放来源，复核时按 rubric 重新判断并写理由。"""
    s = report(batch, model)
    ids = sorted({d["id"] for r in ("r1", "r2") for d in s[r + "_disagree"]})
    items = {it["id"]: it for it in json.loads((DIR / f"batch{batch}.json").read_text(encoding="utf-8"))}
    path = DIR / f"batch{batch}_review.json"
    path.write_text(json.dumps([items[i] for i in ids], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def adjudicated(batch: int, model: str) -> dict:
    """复核后的人工标签：只有当初有分歧的那一条 rubric 用复核结果覆盖，其余保持原标注。"""
    human = json.loads((DIR / f"batch{batch}_human.json").read_text(encoding="utf-8"))["labels"]
    review = json.loads((DIR / f"batch{batch}_review_human.json").read_text(encoding="utf-8"))["labels"]
    s = report(batch, model)
    disputed = {(d["id"], r) for r in ("r1", "r2") for d in s[r + "_disagree"]}
    return {i: {**v, **{r: review[i][r] for r in ("r1", "r2") if (i, r) in disputed}} for i, v in human.items()}


def _fmt(x, pct=False):
    return "-" if x is None else (f"{x:.0%}" if pct else f"{x:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--model", default="qwen")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--stats", action="store_true", help="不调模型，只用已存的打分重算统计")
    a = ap.parse_args()
    if not a.stats:
        print("saved", run(a.batch, a.model, a.workers))
    s = report(a.batch, a.model)
    print(f"batch{a.batch} / judge {a.model}；排除 {s['excluded']}；judge 输出无效 {s['judge_failed'] or '-'}")
    for r in ("r1", "r2"):
        x = s[r]
        print(f"{r}: n={x['n']}（人工否 {x['human_no']}）一致 {x['agree']}/{x['n']}  κ={_fmt(x['kappa'])}  "
              f"召回率 {_fmt(x['recall'], True)}  误报率 {_fmt(x['fpr'], True)}")
        for d in s[r + "_disagree"]:
            print(f"   不一致 {d['id']}: 人工 {'是' if d['human'] else '否'} / judge {'是' if d['judge'] else '否'}"
                  f"  （{d['source']}{' · ' + d['kind'] if d['kind'] else ''}）")
    print("上线门槛：", s["passes"])


if __name__ == "__main__":
    main()
