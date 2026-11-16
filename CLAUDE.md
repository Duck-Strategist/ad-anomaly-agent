# CLAUDE.md

跨境卖家经营异常归因与处置 Agent。求职作品：证明能把 Agent 接到真实业务数据和操作上，做到结果可校验、有权限边界、有失败处理，并用评测数据证明效果。

**设计以 [SPEC.md](SPEC.md) 为准**（每个决定都带日期和理由）。实测数字在 [eval/RESULTS.md](eval/RESULTS.md)，评测题的改动记录在 [eval/CHANGELOG.md](eval/CHANGELOG.md)。

## 和用户协作的方式

- 用户是应届生，第一次做 Agent 项目，设计决定由用户做，但用户需要先理解。**涉及新概念或薄弱环节（评测、幂等 / 重试 / 权限、LangGraph、RAG、复杂 SQL、Docker）时：先讲原理和取舍，出 2–3 道小测让用户用自己的话回答，批改并追问，再写代码。**
- 每完成一个模块，说明面试可能被问到什么、要答到什么程度。
- 给推荐时说清理由，让用户能复述；不要让用户只是点头同意。用户不同意的地方当开放问题处理，写回 SPEC。
- 用中文交流。提交前确认用户同意。
- **维护 [INTERVIEW_NOTES.md](INTERVIEW_NOTES.md)**：遇到值得在面试里讲的挑战（发现 bug、实测推翻假设、设计取舍、评测里发现的问题）时，当场补一条，写清情境、怎么发现、怎么解决、取舍、一句话总结和可能的追问，并附上实测数字。只记真实发生过的事。"设计中"的条目在实现后补充细节。

## 不能破坏的原则

- **LLM 不写 SQL**：只输出 MQL（JSON），由 `adagent/semantic/compiler.py` 确定性编译。
- **权限在编译层强制注入**：每个读表子查询都带 `shop_id = ANY(%(allowed_shops)s)`；`allowed_shops` 只来自服务端权限表，不来自模型。prompt 里不提权限。
- **来自 MQL 的值一律参数绑定**，不拼进 SQL 字符串。
- **口径只有一个来源**：`adagent/semantic/catalog.yaml`。改口径只改这里；LLM 的 prompt 和基线的口径文档都从它生成。
- 比率指标先求和再相除；两侧（广告 / 订单）分别聚合到相同粒度再 JOIN，避免扇出。
- 写操作只有"暂停广告组"，用 `agent_app` 账号（只能改 `ad_groups.status`）；查询用只读的 `agent_reader`。
- **评测诚实**：标准答案 SQL 独立手写（不经编译器）；**不针对某道评测题调 prompt**；简历和 RESULTS 只写实测数字，并写明局限。改题必须记入 CHANGELOG。
- 确定性的计算交给代码（编译器、`decompose_roas`），LLM 只做理解和解释。

## 常用命令

```bash
docker compose up -d --wait              # 先确认 Docker Desktop 在运行
uv run python -m datagen.generate        # 生成合成数据（固定种子）
uv run python -m datagen.load            # 导入数据库
uv run pytest -q                         # 全部测试（需要数据库）
uv run python -m eval.check_reference    # 标准答案 vs 编译器（不调用模型）
uv run python -m eval.run --system ours --model deepseek       # 评测；baseline / qwen / qwen-small 同理
```

- Windows 终端跑评测时加 `PYTHONIOENCODING=utf-8`，否则中文输出乱码。
- `db/init/*.sql` 只在数据卷为空时执行；改了建表脚本要 `docker compose down -v` 后重建并重新导入。已有数据库上加新脚本，可以用 `docker compose exec -T db psql -U app_admin -d adagent < db/init/00X_xxx.sql` 单独执行。
- `.env` 存 API key 和数据库密码，已被忽略，不要读取或打印 key 的值。模型名配置在 `.env`（主力 DeepSeek，judge 千问，调试 `qwen-flash`）。

## 代码地图

| 路径 | 作用 |
|---|---|
| `db/init/` | 建表（含四个数据陷阱）和最小权限角色 |
| `datagen/` | 合成数据生成（`config.yaml` 里有 6 个埋入事件）和导入 |
| `adagent/semantic/` | 指标目录、MQL 校验、编译器、执行器 `query_metrics` |
| `adagent/nl2mql.py` | 自然语言 → MQL → 查询，带自修正（最多重试 2 次） |
| `adagent/attribution/decompose.py` | 确定性归因：对数分解 + 广告组贡献度 + z 检验 |
| `adagent/attribution/explain.py` | 归因解释模板：结论段由代码生成，不认识的形态报错；检查模型建议段里的新数字（SPEC Q21） |
| `adagent/workflow.py` | LangGraph 工作流：路由 → 问数 / 归因 → 解释 → 提议 → 审批（interrupt）→ 执行；`run()` / `resume()` |
| `adagent/clarify.py` | 歧义反问：校验反问的词和选项、映射用户回答、兜底文案 |
| `adagent/actions.py` | 唯一的写动作：提议、幂等执行暂停、拒绝 |
| `adagent/llm.py` | DeepSeek / 千问调用（OpenAI 兼容接口） |
| `eval/judge/`、`eval/judge_sample.py` | judge 校准：标注说明 `rubric.md`、构造样本、批次文件、本地标注页 `label.html`（`*_key.json` 标注完成前不打开） |
| `eval/` | 评测题、评分器、基线（Text-to-SQL）、运行器；`eval/ground_truth/events.json` 是事件真值，只给评测用 |
| `tests/` | 权限、编译器、评分器、分解算法的测试 |

## 当前进度（2026-11-14）

- **M1 完成**：数据层、语义层、编译器、NL→MQL、Text-to-SQL 基线、40 道评测题（陷阱题和越权题已人工审核）。
- **M2 完成（2026-11-13）**：全集正式评测数字见 RESULTS 顶部"M2 全集正式评测"一节（简历用这一节）。查询类本方案 / DeepSeek 93.9%（基线 98.5%，差在 4 道 MQL 表达不了的月度对比），首次即对 93.9% vs 69.7%；跨模型稳定性目标未达标（相差 7.5 个百分点）；归因 pass^3 25/25（ReAct 基线 14/25）。
- M2 内容：`decompose_roas`、LangGraph 归因工作流（`adagent/workflow.py`）和幂等审批执行（`adagent/actions.py`）。checkpoint 存在 PostgreSQL 的 `graph` schema（`agent_graph` 角色）。
- **歧义反问已完成**（2026-11-11，`adagent/clarify.py` + 工作流 `ask_clarify` / `apply_clarify` 节点，接口 `answer_clarification()`）。
- **M2 查询题 36 道已完成并人工审核**（`eval/questions/m2_query.yaml`）：问法由 Gemini 按 `eval/question_brief.md` 生成（避免出题人接触系统 prompt），用户审核。`eval/crosscheck_csv.py` 用生成器 CSV 独立重算并检查陷阱是否考得出；评分器支持多个可接受的 gold（`gold_sql_alt` + `key_aliases`）。
- **M2 评测题齐了（111 道）**：M1 40 + 查询 36 + 歧义 10（另有留出集 9）+ 归因 25。运行器 `eval/workflow_eval.py` 让歧义 / 归因题走完整工作流（歧义两步判分、归因每题 3 次算 pass^3），评测 thread_id 以 `eval-` 开头。实测数字见 RESULTS：歧义留出集 9/9，归因 pass^3 25/25（都在通用修复之后，修复前的数字也记录了）。
- **归因**：店铺不显著时下钻（Bonferroni 校正门槛）；路由时间段由代码检查数据范围；"没有数据"不等于"没有下滑"（SPEC Q18）。
- 评测工具：`eval/run.py`（查询 / 歧义 / 归因，含 ReAct 基线 `eval/baseline_react.py`）、`eval/summarize.py`（合并多次运行出表）、`eval/rescore.py`（修正 gold 后用已存结果重判，不重跑模型）。
- **M3 相对时间已完成（2026-11-14，SPEC Q19）**：MQL 增加 `calendar`，日期由代码换算；时间范围检查；回答写出统计区间。留出集 25 题（`eval/questions/m3_time_holdout.yaml`）本方案 DeepSeek 23/25、千问 24/25；原 76 题上跨模型仍相差 7.6 个百分点，未达标（见 RESULTS 顶部）。
- **M3 两段时间对比已完成（2026-11-14，SPEC Q20）**：`compare` 可写成一段时间且必须在本期之前；自然周 / 月的 `previous_period` 取同期。留出集 25 题 × 3 次（`eval/questions/m3_compare_holdout.yaml`），本方案两个模型 pass^3 都是 25/25；原 76 题上跨模型差距 3.0 个百分点（原题偏高，见 RESULTS 顶部）。
- **M3 归因解释改成模板（2026-11-14，SPEC Q21）**：结论段由代码生成，模型只写建议段，judge 只评 R1 / R2。校准样本两批，每批 40 条（`eval/judge/batch1.json`、`batch2.json`）。第一批已标注并跑完 judge（`eval/judge_run.py`）：人工独立标注 vs judge，R1 κ 0.84、R2 κ 0.51；分歧主要是人工漏标隐蔽编造，复核后 κ 1.0（偏高，不作依据，见 RESULTS）。
- **judge 第二批（2026-11-15）**：R1 κ 1.00，R2 κ 0.70、召回率 90%、误报率 13%（门槛 10%），未上线；3/4 误报来自 rubric 空白，已补规则（和依据矛盾的事实 R2 也判否），第三批 `eval/judge/batch3.json` 已生成待标注。
- **下一步（M3）**：第三批独立标注 → 跑一次 judge（prompt 不改、不复核）判断是否上线、极简前端、Docker Compose、Langfuse、README / 演示脚本。
- 开放问题见 SPEC 第十节（Q14：第 4 天检查点）。
