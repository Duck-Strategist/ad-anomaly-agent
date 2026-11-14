"""eval/run.py 的判分规则：no_data 题，以及"统计区间"说明不能冒充统计范围说明（2026-11-14，SPEC Q19）。不需要数据库。"""

from adagent.nl2mql import Answer
from eval.run import score

NO_DATA = {"expect": "no_data"}
SCOPE = {"expect": "allowed_disclosed_or_refuse"}


def test_no_data_passes_when_system_says_so():
    assert score(NO_DATA, Answer("cannot_answer", message="没有这个时间段的数据"), None)[0]


def test_no_data_accepts_empty_or_null_rows():
    assert score(NO_DATA, Answer("rows", rows=[]), None)[0]
    assert score(NO_DATA, Answer("rows", rows=[{"refund_rate": None, "aov": None}]), None)[0]


def test_no_data_rejects_zero():
    # COUNT 在没有数据时返回 0，看起来像"一单都没有"，这是误导
    assert not score(NO_DATA, Answer("rows", rows=[{"order_count": 0, "gross_sales": None}]), None)[0]


def test_no_data_rejects_numbers_from_another_period():
    assert not score(NO_DATA, Answer("rows", rows=[{"gross_sales": 12345.6}]), None)[0]


def test_coverage_note_alone_is_not_scope_disclosure():
    only_coverage = Answer("rows", rows=[{"ad_spend": 1.0}], notes=["统计区间：2026-11-02 ~ 2026-11-08"])
    ok, why = score(SCOPE, only_coverage, [([{"ad_spend": 1.0}], None)])
    assert not ok and "without telling" in why
    disclosed = Answer("rows", rows=[{"ad_spend": 1.0}],
                       notes=["统计区间：2026-11-02 ~ 2026-11-08", "仅统计了您有权限的店铺：US"])
    assert score(SCOPE, disclosed, [([{"ad_spend": 1.0}], None)])[0]
