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

## 可选数据分析分支

`analysis.enabled=false` 时保留原路径，既不导入 DeepAgents，也不连接 Docker。启用时，主模型只选择本轮的一个工具：简单查询使用 `run_text2sql`；多步分析使用 `data_analysis(AnalysisTask)`。`prepare_analysis` 节点生成并检查点保存 run ID；`data_analysis` 节点调用 DeepAgents 0.6.7 子图，最终直接交接已存储报告，不再调用模型改写事实。

`mini.analysis.agent` 使用虚拟 `StateBackend`，通过模型和工具调用两层白名单屏蔽默认文件、shell 与递归委派工具。规划和查询/统计/Python/报告工具可用。模型调用（包括嵌套 Text2SQL）通过继承的回调计入预算；工具调用单独计数，数据库执行也计入。墙钟预算按实际执行片段累加，暂停等待用户的时间不计入；边界检查是协作式的，已经发出的模型请求不会被强制中断。

`query_dataset` 使用本步骤的问题构建新的 Text2SQL 状态，通过只读适配器包装保存每次真实 SQL 尝试及其全量实际返回快照。服务端将工具调用身份映射为独立 operation ID，父图/run 隔离分析状态；同一操作不同参数会被拒绝。已落盘的同一 SQL 可在检查点提交前崩溃后复用，避免重跑。子图中断穿透 DeepAgents 和主图；恢复由 LangGraph 检查点定位原调用。离线测试覆盖同轮双查询、第二次查询审核暂停、重启后恢复、不重跑已执行 SQL。分析查询不会写入按轮覆盖的旧 `successful_queries` 表。

`mini.analysis.store` 使用独立 SQLite 元数据与原子写入的不可变产物。每次读取快照/源码/日志核验 hash 和会话；原始数值、Decimal 字符串、日期与空值及列类型分别保存。Python SDK 对 Decimal/日期/字节恢复类型，派生表可通过 save_dataset 的 units 参数声明列单位；计算链包含任意 Python 派生时保持 supported。预览省略和源查询截断分别记录；主动 LIMIT 会使总体事实校验失败。SQLite/产物写入失败不发布成功；孤立文件不会成为可引用证据。

固定计算与工具包装分离。支持描述统计、两期对比、按单维加总贡献分解、每日趋势、历史滚动 z-score、单快照按唯一键对齐的 Pearson/Spearman 相关。窗口按实际观测点计算；缺失日期不补零，重复日期需显式选择 sum。贡献缺组默认拒绝，补零需显式参数；不支持非加总指标的贡献分解。

Python 仅由 `mini.analysis.execution.docker` 执行：固定镜像、非 root、禁网、只读根文件系统、只读输入、受限 tmpfs、CPU/内存/PID/单文件/总量/日志/超时限制。输出仅导入 JSON 数据集和 PNG；代码执行失败及修复分别留档，绝不回退宿主执行。生成程序使用 UID 65533；PID 1 哨兵使用另一非 root UID 65532。实际退出码由宿主等待 docker exec 获取，不能由生成程序写文件伪造；成功后先终止生成程序的后代再导入产物。哨兵在执行时限加 15 秒后退出，避免宿主退出后无限留下运行容器。PNG 导入前检查结构、CRC 和尺寸。执行 SDK 的源码限制不是安全边界，容器才是执行隔离边界。

提交报告时校验成功执行、会话归属、产物 hash、JSON Pointer、数值、单位和固定工具从快照重算的结果。事实用受控模板渲染；任意 Python 结果仅为 supported。解释必须引用同次报告的已接受事实，使用 `fact:0` 等零基索引；自由解释仍需人工判断。刷新报告会复查证据，缺失/篡改时撤下相应结论。该机制不证明 SQL 业务语义、原始数据真实性或因果关系。

Web 增加 analysis 消息，以及按会话读取报告、证据、分页数据集与安全下载产物的接口；支持请求取消分析与删除会话时联动清理。关闭新分析后仍可读取并清理已有存档。源码/日志以纯文本显示，PNG 用图像显示。CLI 打印同一报告并提供 `python -m mini.analysis.reporting` 查看/导出入口。当前仍是本地单人会话机制，不是公开部署的多租户认证系统。
