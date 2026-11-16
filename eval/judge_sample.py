"""judge 校准样本：抽样 → （人工构造错误样本）→ 组批。SPEC Q21 / 第九节。

每批 40 条：本方案真实 13 + 基线真实 15 + 构造 12（R2 否 9：明显 2、"可能"式 2、像常识 2、过度延伸 3；R1 否 3）。
构造样本以本方案真实的建议段为底稿，底稿不和本方案真实样本重复。共三批，顺序切片，加批次不影响前面的批。

用法：
  uv run python -m eval.judge_sample pick --ours 运行记录.jsonl --baseline 运行记录.jsonl
      → eval/judge/picks.json（抽到哪些运行；固定种子）和 eval/judge/construct_bases.md（构造样本的底稿）
  （按 construct_bases.md 写 eval/judge/constructed.yaml）
  uv run python -m eval.judge_sample build
      → eval/judge/batch1.json、batch2.json（打乱、不含来源，给标注页面和 judge 用）
        eval/judge/batch1_key.json、batch2_key.json（来源和构造意图；标注完成前不要打开）
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import yaml

DIR = Path(__file__).resolve().parent / "judge"
SEED = 20261114
PER_BATCH = {"ours": 13, "baseline": 15, "construct": 12}
CONSTRUCT_PLAN = {"r2_obvious": 2, "r2_maybe": 2, "r2_common_sense": 2, "r2_overreach": 3, "r1_contradiction": 3}
BATCHES = 3   # 第三批：第二批之后补了 rubric，用新数据验证（CHANGELOG 2026-11-15）


def _load(path: str) -> dict[str, dict]:
    """运行记录 → {"AT05#2": {question, run}}。"""
    out = {}
    for line in open(path, encoding="utf-8"):
        q = json.loads(line)
        if q.get("category") != "attribution":
            continue
        for i, r in enumerate(q.get("runs", []), 1):
            out[f"{q['id']}#{i}"] = {"question": q["question"], "run": r}
    return out


def _conclusion_text(c: dict | None) -> str:
    if not c:
        return "（没有结论）"
    if c.get("significant") is False:
        return "无显著下滑"
    return (f"显著下滑；主因 {c.get('main_cause')}；广告组 {c.get('ad_group_id')}；"
            f"{'建议暂停（需审批）' if c.get('suggest_pause') else '不建议暂停'}")


def ours_item(key: str, rec: dict, text: str | None = None) -> dict:
    r = rec["run"]
    return {"question": rec["question"], "evidence": r["facts"], "conclusion": _conclusion_text(r["conclusion"]),
            "text": r["advice"] if text is None else text}


def baseline_item(key: str, rec: dict) -> dict:
    r = rec["run"]
    results = [s["result"] for s in r.get("steps", []) if "result" in s]
    evidence = "\n\n".join(f"查询 {i} 的结果：{x}" for i, x in enumerate(results, 1)) or "（没有执行任何查询）"
    return {"question": rec["question"], "evidence": evidence, "conclusion": _conclusion_text(r["conclusion"]),
            "text": r["explanation"]}


def pick(ours_path: str, baseline_path: str) -> None:
    ours, base = _load(ours_path), _load(baseline_path)
    ours_keys = sorted(k for k, v in ours.items() if v["run"].get("advice"))
    base_keys = sorted(k for k, v in base.items() if v["run"].get("explanation") and v["run"].get("steps") is not None)
    if any("result" not in s for v in base.values() for s in v["run"]["steps"] if s.get("result_ok") is not None):
        raise SystemExit("baseline run has no recorded tool results; rerun after eval/baseline_react.py records them")
    rng = random.Random(SEED)
    rng.shuffle(ours_keys)
    rng.shuffle(base_keys)
    need_ours = BATCHES * (PER_BATCH["ours"] + PER_BATCH["construct"])
    if len(ours_keys) < need_ours or len(base_keys) < BATCHES * PER_BATCH["baseline"]:
        raise SystemExit(f"not enough runs: ours {len(ours_keys)}/{need_ours}, baseline {len(base_keys)}")
    picks = {"seed": SEED, "ours_file": ours_path, "baseline_file": baseline_path, "batches": []}
    for b in range(BATCHES):
        o = ours_keys[b * 25:(b + 1) * 25]
        picks["batches"].append({"ours": o[:PER_BATCH["ours"]], "construct_bases": o[PER_BATCH["ours"]:],
                                 "baseline": base_keys[b * 15:(b + 1) * 15]})
    DIR.mkdir(exist_ok=True)
    (DIR / "picks.json").write_text(json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    # 构造样本的底稿：每个底稿分配一种构造方式（顺序固定）
    kinds = [k for k, n in CONSTRUCT_PLAN.items() for _ in range(n)]
    lines = ["# 构造样本底稿（eval/judge_sample.py pick 生成）", ""]
    for b, batch in enumerate(picks["batches"], 1):
        for key, kind in zip(batch["construct_bases"], kinds):
            rec = ours[key]
            lines += [f"## batch{b} {key} → {kind}", "", f"问题：{rec['question']}", "", "结论段：", "",
                      rec["run"]["facts"], "", f"原建议段：{rec['run']['advice']}", ""]
    (DIR / "construct_bases.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"picked; write {DIR / 'constructed.yaml'} from {DIR / 'construct_bases.md'}")


def build() -> None:
    picks = json.loads((DIR / "picks.json").read_text(encoding="utf-8"))
    ours, base = _load(picks["ours_file"]), _load(picks["baseline_file"])
    constructed = {(c["batch"], c["base"]): c for c in
                   yaml.safe_load((DIR / "constructed.yaml").read_text(encoding="utf-8"))}
    rng = random.Random(SEED + 1)
    for b, batch in enumerate(picks["batches"], 1):
        items = [({**ours_item(k, ours[k])}, {"source": "ours", "run": k}) for k in batch["ours"]]
        items += [(baseline_item(k, base[k]), {"source": "baseline", "run": k}) for k in batch["baseline"]]
        for k in batch["construct_bases"]:
            c = constructed[(b, k)]
            items.append((ours_item(k, ours[k], c["text"]),
                          {"source": "constructed", "run": k, "kind": c["kind"], "intended": c["intended"]}))
        rng.shuffle(items)
        public = [{"id": f"B{b}-{i:02d}", **item} for i, (item, _) in enumerate(items, 1)]
        key = {f"B{b}-{i:02d}": meta for i, (_, meta) in enumerate(items, 1)}
        (DIR / f"batch{b}.json").write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
        (DIR / f"batch{b}_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"batch{b}: {len(public)} items")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--ours", required=True)
    p.add_argument("--baseline", required=True)
    sub.add_parser("build")
    a = ap.parse_args()
    pick(a.ours, a.baseline) if a.cmd == "pick" else build()
