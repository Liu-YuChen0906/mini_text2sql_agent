# 代码架构

## 模块依赖

- `mini.cli.main` 与 `mini.web.service` 都创建 `mini.agents.main_graph`，因此 CLI 和网页复用同一套 Agent 流程。
- `mini.agents.main_graph` 只在数据问题时懒加载 `mini.agents.text2sql_graph`；普通交流无需打开 Chroma 索引。
- `mini.agents.text2sql_graph` 调用 `mini.query` 中的提取、选表、SQL 生成、数据库适配器和评分函数。业务数据库通过 `DatabaseAdapter` 接口接入；内置 SQLite 和 PostgreSQL 实现。
- `mini.runtime.resources` 创建聊天模型、Catalog、三个 Chroma 集合和按配置选择的业务数据库适配器；`mini.runtime.query_history` 保存成功查询结果。
- `mini.web.api` 只负责 HTTP 与静态文件，`mini.web.service` 负责幂等请求、会话状态、检查点恢复和页面消息。

## 状态与数据

主图 `AgentState` 保存会话消息、轮次与本轮结果；子图 `SQLState` 保存一次查询的中间状态。LangGraph 检查点支持暂停恢复，`successful_queries` 保存成功查询的 SQL 与已返回结果；网页另用 `web_sessions`、`web_messages`、`web_requests` 表维护页面状态。CLI 默认使用 `checkpoints.sqlite`，网页默认使用 `web_checkpoints.sqlite`。

## 入口

- `python main.py`：委托给 `mini.cli.main.main()`。
- `python demo_output.py`：委托给 `mini.cli.demo_output.main()`。
- `python -m uvicorn web_api:app`：加载 `mini.web.api.app`。

测试统一放在 `tests/`。新增代码应使用 `mini.*` 的绝对导入；新增类说明用途，函数说明输入、输出和异常或状态副作用，沿用现有中文文档字符串风格。
