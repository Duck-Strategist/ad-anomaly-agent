"""按事先登记的规则抽"无显著下滑"干扰题（规则见 SPEC 第九节"归因题的组成"、CHANGELOG 2026-11-13）。

不人工挑：选题的人已经知道每个（对象, 周）系统会怎么判，人工挑无论避开还是专挑误报都会让误报率失真。
抽到什么用什么。这个脚本只负责抽样，不调用系统、不看结论。

用法：uv run python -m eval.sample_attribution_distractors
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = 20261113
FIRST_MONDAY, LAST_MONDAY = date(2026, 8, 24), date(2026, 11, 2)   # 08-17 周的上一周缺 08-10 的数据，不纳入
SHOPS = ["JP", "UK", "US"]
AD_GROUPS = sorted(f"{s}-AG{i:02d}" for s in SHOPS for i in range(1, 11))
ASKER = {"US": "u_ops_us", "UK": "u_ops_uk", "JP": "u_admin"}


def population() -> tuple[list, list]:
    events = json.loads((ROOT / "ground_truth" / "events.json").read_text(encoding="utf-8"))
    excluded = {(e["shop_id"], e["start"]) for e in events} | {(e["ad_group_id"], e["start"]) for e in events}
    mondays, d = [], FIRST_MONDAY
    while d <= LAST_MONDAY:
        mondays.append(d.isoformat())
        d += timedelta(7)
    ag = sorted((a, m) for a in AD_GROUPS for m in mondays if (a, m) not in excluded)
    shop = sorted((s, m) for s in SHOPS for m in mondays if (s, m) not in excluded)
    return ag, shop


def sample() -> list[dict]:
    ag_pop, shop_pop = population()
    rng = random.Random(SEED)
    picked = [("ad_group", t, m) for t, m in rng.sample(ag_pop, 4)] + [("shop", t, m) for t, m in rng.sample(shop_pop, 3)]
    out = []
    for scope, target, monday in picked:
        start = date.fromisoformat(monday)
        out.append({"scope": scope, "target": target, "start": monday, "end": (start + timedelta(6)).isoformat(),
                    "user_id": ASKER[target[:2]]})
    return out


if __name__ == "__main__":
    ag_pop, shop_pop = population()
    print(f"总体：广告组 {len(ag_pop)} 个（对象, 周），店铺 {len(shop_pop)} 个；seed = {SEED}")
    for s in sample():
        print(s)
