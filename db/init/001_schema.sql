-- 001_schema.sql：业务表 + 权限表 + 审计表
-- 由 PostgreSQL 容器在第一次初始化时自动执行。改了这个文件后，要 `docker compose down -v` 清空数据卷才会重新执行。

-- ========== 维度表 ==========

CREATE TABLE shops (
    shop_id     text PRIMARY KEY,              -- 'US' / 'UK' / 'JP'
    name        text NOT NULL,
    marketplace text NOT NULL,
    currency    char(3) NOT NULL,              -- 陷阱：多币种，金额按当地币种存储
    timezone    text NOT NULL                  -- 陷阱：时区，用 IANA 名称（处理夏令时），不用固定偏移
);

CREATE TABLE products (
    sku_id      text PRIMARY KEY,
    shop_id     text NOT NULL REFERENCES shops(shop_id),
    title       text NOT NULL,
    list_price  numeric(12,2) NOT NULL         -- 当地币种
);

-- ========== 订单侧 ==========

CREATE TABLE orders (
    order_id    bigint PRIMARY KEY,
    shop_id     text NOT NULL REFERENCES shops(shop_id),
    created_at  timestamptz NOT NULL,          -- 陷阱：存 UTC，按天统计时要换算到店铺本地日期
    status      text NOT NULL DEFAULT 'completed',
    deleted_at  timestamptz                    -- 陷阱：软删除（重复导入 / 测试订单），统计时必须排除
);
CREATE INDEX ON orders (shop_id, created_at);

CREATE TABLE order_items (
    item_id     bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(order_id),
    sku_id      text NOT NULL REFERENCES products(sku_id),
    qty         int NOT NULL CHECK (qty > 0),
    unit_price  numeric(12,2) NOT NULL         -- 当地币种；促销期间低于 list_price
);
CREATE INDEX ON order_items (order_id);
CREATE INDEX ON order_items (sku_id);

-- 陷阱：退款在单独的表里；一个订单行可以有多笔部分退款（直接 JOIN 会扇出）；退款比下单晚 3–14 天
CREATE TABLE refunds (
    refund_id   bigint PRIMARY KEY,
    item_id     bigint NOT NULL REFERENCES order_items(item_id),
    amount      numeric(12,2) NOT NULL CHECK (amount > 0),   -- 当地币种
    refunded_at timestamptz NOT NULL                          -- UTC
);
CREATE INDEX ON refunds (item_id);

-- ========== 广告侧 ==========

CREATE TABLE ad_campaigns (
    campaign_id text PRIMARY KEY,
    shop_id     text NOT NULL REFERENCES shops(shop_id),
    name        text NOT NULL
);

CREATE TABLE ad_groups (
    ad_group_id  text PRIMARY KEY,
    campaign_id  text NOT NULL REFERENCES ad_campaigns(campaign_id),
    sku_id       text NOT NULL UNIQUE REFERENCES products(sku_id),  -- 1 个广告组只推 1 个 SKU
    status       text NOT NULL DEFAULT 'enabled' CHECK (status IN ('enabled', 'paused')),
    daily_budget numeric(12,2) NOT NULL,                             -- 当地币种
    deleted_at   timestamptz
);

-- 广告日报：粒度 = 广告组 × 店铺本地日期。ad_orders / ad_sales 是平台归因口径，和订单表对不上是正常的
CREATE TABLE ad_daily_stats (
    ad_group_id text NOT NULL REFERENCES ad_groups(ad_group_id),
    stat_date   date NOT NULL,                 -- 店铺本地日期（不是 UTC）
    impressions int NOT NULL,
    clicks      int NOT NULL,
    spend       numeric(12,2) NOT NULL,        -- 当地币种
    ad_orders   int NOT NULL,
    ad_sales    numeric(12,2) NOT NULL,        -- 当地币种
    PRIMARY KEY (ad_group_id, stat_date)
);

CREATE TABLE fx_rates (
    rate_date   date NOT NULL,
    currency    char(3) NOT NULL,
    rate_to_usd numeric(14,8) NOT NULL,        -- 1 单位该币种 = 多少美元
    PRIMARY KEY (rate_date, currency)
);

-- ========== 应用元数据（Agent 生成的查询不能访问） ==========

CREATE TABLE user_shop_permissions (
    user_id text NOT NULL,
    shop_id text NOT NULL REFERENCES shops(shop_id),
    role    text NOT NULL CHECK (role IN ('viewer', 'approver')),
    PRIMARY KEY (user_id, shop_id)
);

CREATE TABLE action_audit_log (
    id              bigserial PRIMARY KEY,
    proposal_id     text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,      -- 防重复执行的核心：数据库唯一约束
    action          text NOT NULL,
    ad_group_id     text NOT NULL REFERENCES ad_groups(ad_group_id),
    params          jsonb NOT NULL DEFAULT '{}',
    requested_by    text NOT NULL,
    decided_by      text,
    status          text NOT NULL CHECK (status IN ('pending', 'approved', 'executed', 'rejected', 'expired', 'stale')),
    reason          text,
    result          jsonb,
    trace_id        text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz,
    CHECK (status <> 'rejected' OR reason IS NOT NULL)   -- 拒绝必须填理由
);
