"""生成 m3_compare_holdout.yaml 的 gold SQL。手写的 SQL 片段拼接，不经过编译器。"""
import sys
from pathlib import Path

import yaml

ORDER_CTE = """WITH ol AS (
  SELECT o.shop_id, o.order_id, oi.sku_id, (o.created_at AT TIME ZONE sh.timezone)::date AS d,
         oi.qty * oi.unit_price * fx.rate_to_usd AS amt,
         (SELECT COALESCE(SUM(r.amount), 0) FROM refunds r WHERE r.item_id = oi.item_id) * fx.rate_to_usd AS ref
  FROM orders o
  JOIN shops sh ON sh.shop_id = o.shop_id
  JOIN order_items oi ON oi.order_id = o.order_id
  JOIN fx_rates fx ON fx.currency = sh.currency AND fx.rate_date = (o.created_at AT TIME ZONE sh.timezone)::date
  WHERE o.deleted_at IS NULL AND {where})"""

AD_CTE = """WITH ad AS (
  SELECT c.shop_id, g.campaign_id, s.ad_group_id, g.sku_id, s.stat_date AS d,
         s.spend * fx.rate_to_usd AS spend, s.ad_sales * fx.rate_to_usd AS sales,
         s.impressions, s.clicks, s.ad_orders
  FROM ad_daily_stats s
  JOIN ad_groups g ON g.ad_group_id = s.ad_group_id
  JOIN ad_campaigns c ON c.campaign_id = g.campaign_id
  JOIN shops sh ON sh.shop_id = c.shop_id
  JOIN fx_rates fx ON fx.currency = sh.currency AND fx.rate_date = s.stat_date
  WHERE {where})"""


def f(expr, p):
    return f"{expr} FILTER (WHERE d BETWEEN '{p[0]}' AND '{p[1]}')"


METRIC = {
    "gross_sales": lambda p: f("SUM(amt)", p),
    "refund_amount": lambda p: f("SUM(ref)", p),
    "net_sales": lambda p: f("SUM(amt - ref)", p),
    "refund_rate": lambda p: f"{f('SUM(ref)', p)} / {f('SUM(amt)', p)}",
    "order_count": lambda p: f("COUNT(DISTINCT order_id)", p),
    "aov": lambda p: f"{f('SUM(amt)', p)} / {f('COUNT(DISTINCT order_id)', p)}",
    "ad_spend": lambda p: f("SUM(spend)", p),
    "ad_sales": lambda p: f("SUM(sales)", p),
    "impressions": lambda p: f("SUM(impressions)", p),
    "clicks": lambda p: f("SUM(clicks)", p),
    "ad_orders": lambda p: f("SUM(ad_orders)", p),
    "acos": lambda p: f"{f('SUM(spend)', p)} / {f('SUM(sales)', p)}",
    "roas": lambda p: f"{f('SUM(sales)', p)} / {f('SUM(spend)', p)}",
    "ctr": lambda p: f"{f('SUM(clicks)', p)}::numeric / {f('SUM(impressions)', p)}",
    "cpc": lambda p: f"{f('SUM(spend)', p)} / {f('SUM(clicks)', p)}",
    "cvr": lambda p: f"{f('SUM(ad_orders)', p)}::numeric / {f('SUM(clicks)', p)}",
}
ORDER_SIDE = {"gross_sales", "refund_amount", "net_sales", "refund_rate", "order_count", "aov"}


def gold(metrics, cur, prev, where, key=None):
    side = "order" if metrics[0] in ORDER_SIDE else "ad"
    assert all((m in ORDER_SIDE) == (side == "order") for m in metrics)
    cte = (ORDER_CTE if side == "order" else AD_CTE).format(where=where)
    lo, hi = min(cur[0], prev[0]), max(cur[1], prev[1])
    cols = []
    for m in metrics:
        cols.append(f"  {METRIC[m](cur)} AS {m}_cur")
        cols.append(f"  {METRIC[m](prev)} AS {m}_prev")
    sel = (f"  {key},\n" if key else "") + ",\n".join(cols)
    tail = f"\nFROM {'ol' if side == 'order' else 'ad'}\nWHERE d BETWEEN '{lo}' AND '{hi}'"
    if key:
        tail += f"\nGROUP BY {key}"
    return f"{cte}\nSELECT\n{sel}{tail}\n"


def two_sides(order_metrics, ad_metrics, cur, prev, order_where, ad_where):
    """订单侧和广告侧分别聚合（不 JOIN 明细，避免扇出）。"""
    o = gold(order_metrics, cur, prev, order_where).replace("SELECT\n", "SELECT\n", 1)
    a = gold(ad_metrics, cur, prev, ad_where)
    return f"SELECT o.*, a.*\nFROM (\n{o}) o\nCROSS JOIN (\n{a}) a\n"


OCT, SEP = ("2026-10-01", "2026-10-31"), ("2026-09-01", "2026-09-30")
NOV8, OCT8 = ("2026-11-01", "2026-11-08"), ("2026-10-01", "2026-10-08")
THIS_W, LAST_W, W2 = ("2026-11-02", "2026-11-08"), ("2026-10-26", "2026-11-01"), ("2026-10-19", "2026-10-25")
US_SKUS = ", ".join(f"'US-SKU{i:02d}'" for i in range(1, 11))

Q = [
    # id, source, user, question, rewrite_note, metrics, cur, prev, where, key, reference_time, reference_extra
    ("CP01", "gemini A1", "u_admin", "9月和10月我们三家店合在一起的订单金额、扣除退款后的订单金额各是多少美元？10月比9月涨了多少？", None,
     ["gross_sales", "net_sales"], OCT, SEP, "TRUE", None,
     {"range": "custom", "start": "2026-10-01", "end": "2026-10-31", "compare": "previous_period"}, {}),
    ("CP02", "gemini A2", "u_ops_us", "帮我拉一下美国店上个月和上上个月的广告花费、广告带来的销售额还有ACOS分别是多少，看看上个月ACOS是变好了还是变差了。", None,
     ["ad_spend", "ad_sales", "acos"], OCT, SEP, "c.shop_id = 'US'", None,
     {"range": "calendar", "unit": "month", "offset": -1, "compare": "previous_period"}, {"filters": {"shop": ["US"]}}),
    ("CP03", "gemini A3", "u_ops_uk", "那个，我是新来对账的助理，想问下英国店这个月到今天为止，跟上个月同期前8天（10月1号到8号）比，订单数和退款金额各是多少呀？",
     "去掉“英镑”（系统只按美元报告，同 TH08）",
     ["order_count", "refund_amount"], NOV8, OCT8, "o.shop_id = 'UK'", None,
     {"range": "calendar", "unit": "month", "offset": 0, "compare": "previous_period"}, {"filters": {"shop": ["UK"]}}),
    ("CP04", "gemini A4", "u_admin", "日本店11月到现在累计的订单金额是多少？跟上个月整月（10月全月）的订单金额比还差多少？两段时间的数都发我看看。",
     "去掉“日元”（同上）",
     ["gross_sales"], NOV8, OCT, "o.shop_id = 'JP'", None,
     {"range": "calendar", "unit": "month", "offset": 0, "compare": {"range": "calendar", "unit": "month", "offset": -1}}, {"filters": {"shop": ["JP"]}}),
    ("CP05", "gemini A5", "u_ops_us", "查下US-SKU05这个品，上周和这周的点击率、转化率、单次点击花费各是多少？这周比上周变好了吗？", None,
     ["ctr", "cvr", "cpc"], THIS_W, LAST_W, "g.sku_id = 'US-SKU05'", None,
     {"range": "this_week", "compare": "previous_period"}, {"filters": {"sku": ["US-SKU05"]}}),
    ("CP06", "gemini A6", "u_ops_uk", "英国店UK-C2这个广告活动，上周跟上上周比，曝光、点击和广告带来的订单数分别是多少？上周比上上周掉了多少？", None,
     ["impressions", "clicks", "ad_orders"], LAST_W, W2, "g.campaign_id = 'UK-C2'", None,
     {"range": "last_week", "compare": "previous_period"}, {"filters": {"campaign": ["UK-C2"]}}),
    ("CP07", "gemini A7", "u_admin", "把这周（11月2号到今天）和上周三家店总的广告花费、广告带来的销售额（都按美元算）以及整体ROAS调出来对比下，看下两周各是多少。", None,
     ["ad_spend", "ad_sales", "roas"], THIS_W, LAST_W, "TRUE", None,
     {"range": "this_week", "compare": "previous_period"}, {}),
    ("CP08", "gemini A8", "u_ops_us", "请问一下，美国店这个月1号到今天这8天，和10月整月比的话，全店的客单价和退款率在两段时间里分别是多少呀？", None,
     ["aov", "refund_rate"], NOV8, OCT, "o.shop_id = 'US'", None,
     {"range": "calendar", "unit": "month", "offset": 0, "compare": {"range": "custom", "start": "2026-10-01", "end": "2026-10-31"}}, {"filters": {"shop": ["US"]}}),
    ("CP09", "gemini A9", "u_admin", "11月到今天为止这8天，跟上个月的同样前8天（10月1日到8日）比，英国店和日本店加起来折合美元后的扣除退款后的订单金额各是多少？涨了还是跌了？", None,
     ["net_sales"], NOV8, OCT8, "o.shop_id IN ('UK', 'JP')", None,
     {"range": "calendar", "unit": "month", "offset": 0, "compare": "previous_period"}, {"filters": {"shop": ["UK", "JP"]}}),
    ("CP10", "gemini A10", "u_ops_uk", "帮我看下UK-AG03广告组，9月第一个完整自然周（9月7号到13号）和10月第一个完整自然周（10月5号到11号）的ACOS、ROAS各是多少？", None,
     ["acos", "roas"], ("2026-10-05", "2026-10-11"), ("2026-09-07", "2026-09-13"), "s.ad_group_id = 'UK-AG03'", None,
     {"range": "custom", "start": "2026-10-05", "end": "2026-10-11", "compare": {"range": "custom", "start": "2026-09-07", "end": "2026-09-13"}}, {"filters": {"ad_group": ["UK-AG03"]}}),
    ("CP11", "gemini A11", "u_ops_us", "美国店US-SKU12在9月整月和10月整月的订单数、订单金额、退款金额各是多少美元？10月比9月涨了多少？", None,
     ["order_count", "gross_sales", "refund_amount"], OCT, SEP, "oi.sku_id = 'US-SKU12'", None,
     {"range": "custom", "start": "2026-10-01", "end": "2026-10-31", "compare": "previous_period"}, {"filters": {"sku": ["US-SKU12"]}}),
    ("CP12", "gemini A12", "u_admin", "上上周（10月19号到25号）和这周（11月2号到8号），日本店JP-C1广告活动的广告花费和广告带来的销售额分别是多少？这周比上上周多了多少？",
     "去掉“日元”（同上）",
     ["ad_spend", "ad_sales"], THIS_W, W2, "g.campaign_id = 'JP-C1'", None,
     {"range": "this_week", "compare": {"range": "calendar", "unit": "week", "offset": -2}}, {"filters": {"campaign": ["JP-C1"]}}),
    ("CP13", "gemini B1", "u_admin", "10月上半月（10月1号到15号）和9月上半月（9月1号到15号），三家店汇总折合美元的订单金额、退款率各是多少？10月上半月表现变好还是变差了？", None,
     ["gross_sales", "refund_rate"], ("2026-10-01", "2026-10-15"), ("2026-09-01", "2026-09-15"), "TRUE", None,
     {"range": "custom", "start": "2026-10-01", "end": "2026-10-15", "compare": {"range": "custom", "start": "2026-09-01", "end": "2026-09-15"}}, {}),
    ("CP14", "gemini B2", "u_ops_us", "国庆黄金周那7天（10月1号到7号）跟它前面紧挨着的7天（9月24号到30号）比，美国店全店的广告花费、广告带来的订单数分别是多少？", None,
     ["ad_spend", "ad_orders"], ("2026-10-01", "2026-10-07"), ("2026-09-24", "2026-09-30"), "c.shop_id = 'US'", None,
     {"range": "custom", "start": "2026-10-01", "end": "2026-10-07", "compare": "previous_period"}, {"filters": {"shop": ["US"]}}),
    ("CP15", "gemini B3", "u_ops_uk", "麻烦查一下英国店8月15号到8月31号这17天，和10月下半月（10月16号到10月31号）这16天，扣除退款后的订单金额和客单价各是多少？差了多少？",
     "去掉“英镑”（同上）",
     ["net_sales", "aov"], ("2026-10-16", "2026-10-31"), ("2026-08-15", "2026-08-31"), "o.shop_id = 'UK'", None,
     {"range": "custom", "start": "2026-10-16", "end": "2026-10-31", "compare": {"range": "custom", "start": "2026-08-15", "end": "2026-08-31"}}, {"filters": {"shop": ["UK"]}}),
    ("CP16", "gemini B4", "u_ops_us", "10月13号到14号那两天会员日大促，跟大促前5天（10月8号到12号）比，美国店US-C1广告活动的曝光、点击率和单次点击花费各是多少？", None,
     ["impressions", "ctr", "cpc"], ("2026-10-13", "2026-10-14"), ("2026-10-08", "2026-10-12"), "g.campaign_id = 'US-C1'", None,
     {"range": "custom", "start": "2026-10-13", "end": "2026-10-14", "compare": {"range": "custom", "start": "2026-10-08", "end": "2026-10-12"}}, {"filters": {"campaign": ["US-C1"]}}),
    ("CP17", "gemini B5", "u_admin", "帮我算下日本店在8月11号到8月20号这10天，还有10月21号到10月30号这10天，全店的订单数和广告带来的销售额各是多少，涨了多少？",
     "去掉“日元”（同上）",
     None, ("2026-10-21", "2026-10-30"), ("2026-08-11", "2026-08-20"), None, None,
     {"range": "custom", "start": "2026-10-21", "end": "2026-10-30", "compare": {"range": "custom", "start": "2026-08-11", "end": "2026-08-20"}}, {"filters": {"shop": ["JP"]}}),
    ("CP18", "gemini B6", "u_ops_uk", "刚接手表格不太熟，想问下UK-SKU08在10月下旬（10月21号到31号）和9月下旬（9月21号到30号）的转化率、ACOS分别是多少呀？变好了还是变差了？", None,
     ["cvr", "acos"], ("2026-10-21", "2026-10-31"), ("2026-09-21", "2026-09-30"), "g.sku_id = 'UK-SKU08'", None,
     {"range": "custom", "start": "2026-10-21", "end": "2026-10-31", "compare": {"range": "custom", "start": "2026-09-21", "end": "2026-09-30"}}, {"filters": {"sku": ["UK-SKU08"]}}),
    ("CP19", "gemini B7", "u_ops_us", "帮我看下US-AG01这个广告组，最近5天（11月4号到11月8号）和再往前推5天（10月30号到11月3号）的点击、广告花费和ROAS各是多少，ROAS变好了没？", None,
     ["clicks", "ad_spend", "roas"], ("2026-11-04", "2026-11-08"), ("2026-10-30", "2026-11-03"), "s.ad_group_id = 'US-AG01'", None,
     {"range": "custom", "start": "2026-11-04", "end": "2026-11-08", "compare": "previous_period"}, {"filters": {"ad_group": ["US-AG01"]}}),
    ("CP20", "gemini B8", "u_admin", "9月10号到9月25号这16天，跟10月10号到10月22号这13天比，美国店和英国店合起来折成美元的退款金额、退款率各是多少？", None,
     ["refund_amount", "refund_rate"], ("2026-10-10", "2026-10-22"), ("2026-09-10", "2026-09-25"), "o.shop_id IN ('US', 'UK')", None,
     {"range": "custom", "start": "2026-10-10", "end": "2026-10-22", "compare": {"range": "custom", "start": "2026-09-10", "end": "2026-09-25"}}, {"filters": {"shop": ["US", "UK"]}}),
    ("CP21", "gemini C1", "u_admin", "9月和10月比，美国、英国、日本三家店各自的订单金额、扣除退款后的订单金额（统一折算成美元）分别是多少？每家店10月比9月涨了多少？", None,
     ["gross_sales", "net_sales"], OCT, SEP, "TRUE", "shop_id",
     {"range": "custom", "start": "2026-10-01", "end": "2026-10-31", "compare": "previous_period"}, {"dimensions": ["shop"]}),
    ("CP22", "gemini C2", "u_ops_us", "上周和上上周比，美国店每个广告组（US-AG01到US-AG10）各自的广告花费、ACOS和ROAS分别是多少？哪些广告组的ACOS变差了？", None,
     ["ad_spend", "acos", "roas"], LAST_W, W2, "c.shop_id = 'US'", "ad_group_id",
     {"range": "last_week", "compare": "previous_period"}, {"dimensions": ["ad_group"], "filters": {"shop": ["US"]}}),
    ("CP23", "gemini C3", "u_ops_uk", "帮我拉一下英国店这周（11月2号到8号）跟上周（10月26号到11月1号）比，三个广告活动（UK-C1到UK-C3）各自的曝光、点击率、广告带来的销售额分别是多少？",
     "去掉“英镑”（同上）",
     ["impressions", "ctr", "ad_sales"], THIS_W, LAST_W, "c.shop_id = 'UK'", "campaign_id",
     {"range": "this_week", "compare": "previous_period"}, {"dimensions": ["campaign"], "filters": {"shop": ["UK"]}}),
    ("CP24", "gemini C4", "u_admin", "11月1号到今天这8天，跟10月前8天（10月1号到8号）比，日本店两个广告活动JP-C1和JP-C2各自的广告带来的订单数、单次点击花费、转化率各是多少？", None,
     ["ad_orders", "cpc", "cvr"], NOV8, OCT8, "c.shop_id = 'JP'", "campaign_id",
     {"range": "calendar", "unit": "month", "offset": 0, "compare": "previous_period"}, {"dimensions": ["campaign"], "filters": {"shop": ["JP"]}}),
    ("CP25", "gemini C5", "u_ops_us", "请教下，主管让我填周报，美国店投了广告的那10个商品（US-SKU01到US-SKU10），在10月上半月（10月1号到15号）和10月下半月（10月16号到31号）各自的订单数、客单价和退款率分别是多少，能按每个商品拆开列一下吗？", None,
     ["order_count", "aov", "refund_rate"], ("2026-10-16", "2026-10-31"), ("2026-10-01", "2026-10-15"), f"oi.sku_id IN ({US_SKUS})", "sku_id",
     {"range": "custom", "start": "2026-10-16", "end": "2026-10-31", "compare": {"range": "custom", "start": "2026-10-01", "end": "2026-10-15"}},
     {"dimensions": ["sku"], "filters": {"sku": [f"US-SKU{i:02d}" for i in range(1, 11)]}}),
]

HEADER = """# M3 两段时间对比留出集（25 道）：按自然月 / 周对比 12（CP01–CP12）、任意两段 8（CP13–CP20）、对比的同时拆分 5（CP21–CP25）
#
# 用途：两段时间对比的修复（SPEC Q20）是看了 M2 全集里 C12、C16、C17、C18、C20 之后设计的，在原题上复测会偏高。
#       本文件在修复代码提交之前写好 gold 并冻结；修复期间不在这些题上跑模型。修复后每题跑 3 次，报 pass^3 和单次准确率。
# 来源：问法由 Gemini 按 eval/question_brief_compare.md 生成（原始输出 eval/question_sources/gemini_2026-11-14_compare.txt），
#       用户挑选：25 道全部保留；6 道去掉"英镑 / 日元"（系统只按美元报告）；"涨了多少 / 差了多少"保留问法，只判两期数值。
# 判分：gold 是宽表（每个指标本期、对比期各一列）。periods: 2 的题，候选结果是长表（每期一行）时先转成宽表再比，
#       列名和期间标签都不参与比较（eval/scoring.py: long_to_wide）。
#
# 金额统一折合美元：广告侧按 stat_date 当天汇率，订单侧（含退款）按订单的店铺本地下单日汇率。
# "今天" = 2026-11-08（周日）。本周 11-02..11-08；上周 10-26..11-01；上上周 10-19..10-25。
# gold 由手写的 SQL 片段拼成（eval/gen_compare_holdout.py，不经编译器）；reference_mql 只给 check_reference 核对用，不给模型看。
#
# 审核状态：AI 起草，待人工审核

"""


def main(out: Path):
    items = []
    for (qid, src, user, question, note, metrics, cur, prev, where, key, rtime, extra) in Q:
        if qid == "CP17":
            sql = two_sides(["order_count"], ["ad_sales"], cur, prev, "o.shop_id = 'JP'", "c.shop_id = 'JP'")
            ref_metrics = ["order_count", "ad_sales"]
        else:
            sql = gold(metrics, cur, prev, where, key)
            ref_metrics = metrics
        ref = {"metrics": ref_metrics, **{k: v for k, v in extra.items() if k == "dimensions"},
               **{k: v for k, v in extra.items() if k == "filters"}, "time": rtime}
        item = {"id": qid, "category": "compare_period", "source": src, "user_id": user, "question": question,
                "expect": "rows", "periods": 2, "gold_sql": sql, "reference_mql": ref}
        if note:
            item["rewrite"] = note
        items.append(item)
    class D(yaml.SafeDumper):
        pass

    D.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
    D.add_representer(dict, lambda d, m: d.represent_mapping("tag:yaml.org,2002:map", m.items(),
                                                             flow_style=("range" in m or "metrics" in m or "shop" in m or "sku" in m
                                                                         or "campaign" in m or "ad_group" in m)))
    text = HEADER + yaml.dump(items, Dumper=D, allow_unicode=True, sort_keys=False, width=400)
    text = text.replace("\n- id:", "\n\n- id:")
    out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
