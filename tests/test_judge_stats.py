"""judge 校准统计（κ、召回率、误报率、上线门槛）和 prompt 生成。不需要数据库、不调模型。"""

import pytest

from eval.judge_run import build_prompt, rubric_sections
from eval.judge_stats import cohen_kappa, passes, rates


def test_always_yes_judge_has_zero_kappa():
    """SPEC 第九节的例子：36 是 4 否，judge 永远答是 → 一致率 90%，κ = 0，召回率 0。"""
    human = [True] * 36 + [False] * 4
    judge = [True] * 40
    assert cohen_kappa(human, judge) == pytest.approx(0.0)
    r = rates(human, judge)
    assert r["agree"] == 36 and r["recall"] == 0 and r["fpr"] == 0


def test_kappa_known_value():
    # 一致 8/10；人工是 5、judge 是 5 → pe = 0.5，κ = (0.8 − 0.5) / 0.5 = 0.6
    human = [True] * 5 + [False] * 5
    judge = [True, True, True, True, False, True, False, False, False, False]
    assert cohen_kappa(human, judge) == pytest.approx(0.6)


def test_kappa_undefined_when_both_constant():
    assert cohen_kappa([True] * 5, [True] * 5) is None


def test_recall_and_false_positive_rate_use_no_as_positive():
    human = [False] * 10 + [True] * 30
    judge = [False] * 8 + [True] * 2 + [False] * 3 + [True] * 27
    r = rates(human, judge)
    assert r["recall"] == pytest.approx(0.8) and r["fpr"] == pytest.approx(0.1)
    assert passes({"kappa": 0.7}, {"kappa": 0.65, "recall": r["recall"], "fpr": r["fpr"]})["all"]
    assert not passes({"kappa": 0.7}, {"kappa": 0.65, "recall": 0.7, "fpr": 0.05})["all"]


def test_prompt_uses_rubric_r1_r2_sections_only():
    s = rubric_sections()
    assert s.startswith("## R1") and "## R2" in s and "## 怎么记" not in s
    assert s in build_prompt()
