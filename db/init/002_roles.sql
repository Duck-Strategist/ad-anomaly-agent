-- 002_roles.sql：最小权限的数据库角色
-- 密码是本地开发用的测试值，和 .env.example 保持一致。

-- ---------- agent_reader：执行 Agent / 基线生成的查询 ----------
-- 只能读业务表；读不到权限表和审计表；会话默认只读；单条查询最多 5 秒
CREATE ROLE agent_reader LOGIN PASSWORD 'reader_dev';
GRANT CONNECT ON DATABASE adagent TO agent_reader;
GRANT USAGE ON SCHEMA public TO agent_reader;
GRANT SELECT ON shops, products, orders, order_items, refunds,
                ad_campaigns, ad_groups, ad_daily_stats, fx_rates
      TO agent_reader;
ALTER ROLE agent_reader SET statement_timeout = '5s';
ALTER ROLE agent_reader SET default_transaction_read_only = on;

-- ---------- agent_app：应用自身的元数据和唯一的写动作 ----------
-- 查权限表；读写审计表；对 ad_groups 只能改 status 这一列
CREATE ROLE agent_app LOGIN PASSWORD 'app_dev';
GRANT CONNECT ON DATABASE adagent TO agent_app;
GRANT USAGE ON SCHEMA public TO agent_app;
GRANT SELECT ON user_shop_permissions TO agent_app;
GRANT SELECT, INSERT, UPDATE ON action_audit_log TO agent_app;
GRANT USAGE ON SEQUENCE action_audit_log_id_seq TO agent_app;
GRANT SELECT ON ad_groups TO agent_app;
GRANT UPDATE (status) ON ad_groups TO agent_app;   -- 列级授权：改不了预算、SKU 等其他字段
ALTER ROLE agent_app SET statement_timeout = '5s';
