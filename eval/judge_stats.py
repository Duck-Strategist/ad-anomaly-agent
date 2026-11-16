"""judge 校准的统计：Cohen's κ、召回率、误报率（SPEC 第九节 / Q21）。

"否"是要抓的那一类，所以召回率和误报率都以"否"为正类：
  召回率 = 人工标"否"的样本里，judge 也判"否"的比例
  误报率 = 人工标"是"的样本里，judge 判成"否"的比例
上线门槛：R1、R2 的 κ ≥ 0.6；R2 召回率 ≥ 0.8、误报率 ≤ 0.1（R1 的召回率 / 误报率只报告）。
"""

from __future__ import annotations

KAPPA_MIN = 0.6
R2_RECALL_MIN = 0.8
R2_FPR_MAX = 0.1


def cohen_kappa(human: list[bool], judge: list[bool]) -> float | None:
    """两个标注者在是 / 否上的 κ = (实际一致率 − 碰巧一致率) / (1 − 碰巧一致率)。碰巧一致率为 1 时无定义。"""
    n = len(human)
    if n == 0 or n != len(judge):
        raise ValueError("need two equal-length, non-empty label lists")
    po = sum(h == j for h, j in zip(human, judge)) / n
    ph, pj = sum(human) / n, sum(judge) / n
    pe = ph * pj + (1 - ph) * (1 - pj)
    return None if pe == 1 else (po - pe) / (1 - pe)


def rates(human: list[bool], judge: list[bool]) -> dict:
    neg = [j for h, j in zip(human, judge) if not h]     # 人工"否"
    pos = [j for h, j in zip(human, judge) if h]         # 人工"是"
    return {"n": len(human), "human_no": len(neg), "human_yes": len(pos),
            "agree": sum(h == j for h, j in zip(human, judge)),
            "kappa": cohen_kappa(human, judge),
            "recall": (sum(not j for j in neg) / len(neg)) if neg else None,
            "fpr": (sum(not j for j in pos) / len(pos)) if pos else None}


def passes(r1: dict, r2: dict) -> dict:
    ok = lambda x, f: x is not None and f(x)
    checks = {"r1_kappa": ok(r1["kappa"], lambda k: k >= KAPPA_MIN),
              "r2_kappa": ok(r2["kappa"], lambda k: k >= KAPPA_MIN),
              "r2_recall": ok(r2["recall"], lambda r: r >= R2_RECALL_MIN),
              "r2_fpr": ok(r2["fpr"], lambda r: r <= R2_FPR_MAX)}
    return {**checks, "all": all(checks.values())}
