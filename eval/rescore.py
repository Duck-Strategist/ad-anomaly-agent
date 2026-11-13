"""用已保存的运行结果重新判分（不重新调用模型）。

用于分歧排查后修正了 gold / 评分规则的情况：修正同时作用于所有系统和模型，只重判、不重跑，避免引入新的随机性。
只处理查询类题（结果行已存在运行记录里，最多 50 行）。输出写到 <原文件名>_rescored.jsonl，原文件保留不动。

用法：uv run python -m eval.rescore eval/runs/a.jsonl [eval/runs/b.jsonl ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from adagent.nl2mql import Answer
from eval.check_reference import load_golds, load_questions
from eval.run import score

ROOT = Path(__file__).resolve().parent
QUESTIONS = {q["id"]: q for f in ("m1", "m2_query") for q in load_questions(ROOT / "questions" / f"{f}.yaml")}


def rescore_file(path: Path) -> Path:
    out = path.with_name(path.stem + "_rescored.jsonl")
    changed = []
    golds_cache: dict[str, list | None] = {}
    with open(path, encoding="utf-8") as f, open(out, "w", encoding="utf-8") as g:
        for line in f:
            r = json.loads(line)
            q = QUESTIONS[r["id"]]
            if q["id"] not in golds_cache:
                golds_cache[q["id"]] = load_golds(q)
            a = Answer(r["status"], rows=r.get("rows") or [], message=r.get("message", ""),
                       notes=r.get("notes") or [], attempts=r.get("attempts") or [])
            ok, why = score(q, a, golds_cache[q["id"]])
            if ok != r["pass"]:
                changed.append(f"{r['id']}: {r['pass']} → {ok} ({why})")
            r.update({"pass": ok, "reason": why, "first_try": ok and len(a.attempts) == 1})
            g.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{path.name}: {len(changed)} changed" + "".join(f"\n  {c}" for c in changed))
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        rescore_file(Path(p))
