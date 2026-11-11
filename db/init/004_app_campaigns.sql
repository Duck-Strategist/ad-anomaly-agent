-- 004：审批时要按外键查广告组属于哪家店（ad_groups → ad_campaigns → shop），不用 ID 前缀推断
GRANT SELECT (campaign_id, shop_id) ON ad_campaigns TO agent_app;
