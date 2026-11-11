-- 003_graph.sql：LangGraph checkpoint 专用的角色和 schema
-- checkpointer 需要建表、读写自己的表，但不该碰业务数据。所以单独一个角色，只拥有 graph schema。
CREATE ROLE agent_graph LOGIN PASSWORD 'graph_dev';
GRANT CONNECT ON DATABASE adagent TO agent_graph;
CREATE SCHEMA graph AUTHORIZATION agent_graph;
ALTER ROLE agent_graph SET search_path = graph;
REVOKE ALL ON SCHEMA public FROM agent_graph;
