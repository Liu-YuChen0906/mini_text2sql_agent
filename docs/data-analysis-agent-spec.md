# 数据分析子 Agent 开发任务说明

状态：实施规格（2026-11-03 已交付核心实现并完成本地验收）。本文保留原始验收要求；实际完成情况、运行证据与未完成项见 [验收记录](data-analysis-agent-acceptance.md)，不能仅据本文推断功能已完成。
编写日期：2026-11-03。目标项目：BetterChatBI（Python `mini/` + Vue `frontend/`）。

## 1. 任务与交付边界

在现有项目中增加基于 **DeepAgents** 的 `data_analysis` 子 Agent。让它自主拆解复杂分析、查询数据、调用常见分析工具、编写并实际执行 Python，最终给出可追溯的结论。

核心验收：用户看到的每条业务事实和数值，都能展开查看对应数据、SQL、计算方法或 Python 源码及真实执行结果。只有提示词要求“不要编造”、只有工具日志、只有 SQL 链接，均不算完成。

本期必须交付：

- 保留现有主图，以工具方式委派 DeepAgents 分析子图；简单查询继续走现有 Text2SQL。
- 统一的数据集、执行证据、结论协议，以及持久化和会话隔离。
- 可生成、执行、修正 Python 的隔离执行器；常见分析工具。
- 报告提交前的确定性校验；CLI 和网页中的结论及证据展示。
- 离线数值测试、真实 Python 执行集成测试、主子图暂停恢复测试，以及可复现演示。

本期不需要重写 Text2SQL、迁移主 Agent 框架、引入微服务/队列、训练预测模型或实现因果推断。不得把尚未跑通的能力写成已支持。

## 2. 已核对的项目现状与参考

### 2.1 当前代码接入点

| 文件 | 当前行为 | 实施要求 |
| --- | --- | --- |
| `mini/agents/main_graph.py` | 主图只允许一次 `run_text2sql`；查询后结束本轮 | 增加分析路由和 `analysis_outcome`；更新工具白名单、提示词和新轮次清空逻辑 |
| `mini/agents/text2sql_graph.py` | 澄清、选表、SQL 生成/执行/修复、人工审核；使用 `interrupt` | 复用子图；分析内部的每个查询须有独立调用身份，保持暂停恢复 |
| `mini/query/database.py` | `DatabaseAdapter.execute_readonly`；`QueryResult` 含 `columns/rows/truncated` | 继续走只读适配器，明确数据截断与完整性 |
| `mini/runtime/query_history.py` | 主键为 `(session_id, turn_id)`，同轮写入会覆盖；只保存成功查询 | 不能用它直接存分析的多次查询；新增证据存储，不破坏旧记录 |
| `mini/runtime/resources.py` | 模型、Catalog、检索及数据库资源 | 分析资源懒加载，支持独立分析模型配置，默认复用现有模型 |
| `mini/web/service.py`、`mini/web/api.py` | 会话、请求幂等、恢复、结果展示 | 新增分析消息和按会话读取证据/产物的接口 |
| `mini/cli/main.py`、`mini/cli/presentation.py` | CLI 驱动与呈现 | 支持分析状态、报告、证据编号及查询入口 |
| `frontend/src/App.vue` | 现有网页入口 | 增加报告和证据展开组件，保持原查询表格 |
| `requirements/runtime.txt` | 已锁定部分 LangChain/LangGraph 版本，没有 DeepAgents | 验证兼容版本后固定依赖，记录实际版本，不盲目升级整个生态 |

开始开发前重新读取相关文件和 Git diff；本文记录的是编写时状态。保留他人未提交的改动。

### 2.2 上游借鉴与明确改造

参考仓库：[zhongyu09/openchatbi](https://github.com/zhongyu09/openchatbi)。本次查看时 `main` 为 `412a3e10d58263ef3bd4aca25b815447443fe79f`。

- [analysis/agent.py](https://github.com/zhongyu09/openchatbi/blob/412a3e10d58263ef3bd4aca25b815447443fe79f/openchatbi/analysis/agent.py)：用 `create_deep_agent` 组合 Text2SQL、Python、异常检测、归因等工具；主 Agent 通过工具委派自包含任务。借鉴这条路径。
- 上述文件的 `_extract_final_content` 只提取最后消息内容。因此本项目必须另外保存结构化报告和证据，不能依靠子 Agent 最后一段文字传递全部依据。
- [tool/run_python_code.py](https://github.com/zhongyu09/openchatbi/blob/412a3e10d58263ef3bd4aca25b815447443fe79f/openchatbi/tool/run_python_code.py)：借鉴执行器接口；不要照搬仅返回打印文本、Docker 不可用时回退本机执行的行为。
- [analysis/ 目录](https://github.com/zhongyu09/openchatbi/tree/412a3e10d58263ef3bd4aca25b815447443fe79f/openchatbi/analysis)：参考异常检测、维度下钻的职责划分，不要求完整移植算法或依赖远程预测服务。复用上游代码时保留 MIT 许可和归属。

DeepAgents 官方说明：[子 Agent](https://docs.langchain.com/oss/python/deepagents/subagents)、[沙箱](https://docs.langchain.com/oss/python/deepagents/sandboxes)。子 Agent 的上下文隔离不能替代证据传递；文件工具也不等于隔离执行。具体构造参数和默认工具行为以安装并锁定的版本为准，先做最小兼容性验证。

## 3. 总体流程与 DeepAgents 接入

```text
用户问题
  → 现有主 Agent
      → 简单查数：现有 run_text2sql
      → 多步分析：data_analysis(AnalysisTask)
          → DeepAgents 分析子图：规划 / 查数 / 分析工具 / Python / 修正
          → 数据集与执行记录持续写入 EvidenceStore
          → submit_analysis_report → 确定性校验 → 持久化 AnalysisReport
      → 主图读取已校验报告 → CLI / Web 展示结论和证据
```

`build_data_analysis_agent(...)` 使用 `deepagents.create_deep_agent`；将它包装为现有主图可调用的工具，不必把主图迁移到 DeepAgents。子图可以使用规划能力，但不需要再生成任意嵌套 Agent。检查安装版本默认附带的文件、shell、委派工具：禁止任何能绕过证据记录或沙箱的执行通道；虚拟文件区仅作草稿，不能作为权威存档。

主图负责解析当前时间和对话中已明确的口径，委派自包含任务；子 Agent 不应猜测父图历史。对指标、日期、维度、过滤条件仍有关键歧义时走现有澄清交互。

建议 `AnalysisTask` 字段：

- `question`、`objective`、`metrics`（名称、定义、单位）、`dimensions`、`filters`。
- `time_range`、`comparison_range`（起止时间，统一左闭右开）、`timezone`、`granularity`。
- `input_dataset_ids`（可空）、`assumptions`、`requested_outputs`。
- `session_id/turn_id/run_id/request_id`、权限上下文和预算由服务端注入，**不接受模型提供的身份或路径**。

每次分析创建 `run_id`，内部每次查询/执行有 `operation_id` 和 `attempt`。同一请求恢复沿用 run；新分析分配新 run。检查点按父会话、run、操作命名隔离，不能跨轮复用一个未清空的分析上下文。不要机械复制上游固定子线程命名。

当前 Text2SQL 工具注入的是整轮用户问题，不能原样用于分析中的所有查数。新增分析专用包装 `query_dataset(question, ...)`，使用子 Agent 提出的本步骤问题和明确口径调用 `new_text2sql_turn`，复用原 SQL 子图及只读适配器。

`GraphInterrupt` 必须穿透包装层，不得吞成普通工具错误。服务端保存待恢复子调用身份，再把用户回答送回正确子图；恢复不能从头重跑已完成的查询或 Python。先用双查询场景验证这一机制，再扩展工具集。

## 4. 核心数据协议与存储

所有协议采用有版本的 Pydantic 模型。工具返回 JSON 可序列化对象。ID 由服务端生成；模型不能注册自己声称的成功执行。

### 4.1 DatasetRecord：用于计算的实际数据

至少保存：

- `schema_version, dataset_id, session_id, run_id, created_at`。
- `source_evidence_id, parent_dataset_ids`，表明来自 SQL 还是 Python 派生。
- `columns`（名称/类型）、`row_count`、`preview`、`preview_truncated`。
- `data_artifact_id, content_sha256`；保存全部实际返回行，不仅前三行。
- `source_truncated`、`coverage`（时间/粒度/筛选范围/完整性说明）、`quality_warnings`。
- 指标单位、时区、空值与 Decimal/日期序列化规则。

必须区分“预览省略”和“查询本身截断”。已有适配器默认行限较小：分析时优先 SQL 聚合，再按可配置的行数/字节上限查询。达到上限应标记不完整，不能默默计算总体。`truncated=false` 也不能证明 SQL 没有主动 LIMIT、筛选遗漏或错误关联；记录查询范围和业务假设。需要明细分页时必须有稳定排序和一致性策略，否则本期明确拒绝超限任务。

使用规范化 JSON 数据快照作为最小实现，保留列类型与空值；需要 Parquet 时再增加适配。测试样本和用户业务数据分别存放。

### 4.2 ExecutionEvidence：可信执行记录

至少保存：

- `evidence_id, schema_version, session_id, run_id, operation_id, attempt`。
- `kind: sql | analysis_tool | python`、`tool_name`、工具/算法版本。
- `input_dataset_ids, parent_evidence_ids, parameters`。
- `started_at, finished_at, status: running | success | error | timeout | cancelled`。
- SQL 的实际执行文本、参数、数据源逻辑标识（无密码）、查询问题、范围、行限与截断标记。
- Python 的源码产物 ID/hash、输入 hash、执行镜像 digest/依赖版本、随机种子、退出码、stdout/stderr 产物及截断标记。
- 结构化 `result`、输出数据集/图表产物 ID、错误类型和数据质量警告。

只由服务端包装器根据真实执行写入。失败和修正尝试都保留；修正生成新 evidence 并关联旧记录，不能覆盖失败历史。hash 用于检查存档内容是否变化，不代表结论正确。

### 4.3 Claim 与 AnalysisReport：逐条绑定结论

`Claim` 至少含：

- `claim_id`、`kind: fact | interpretation | hypothesis | recommendation`。
- `text`、`evidence_refs`、`limitations`。
- 数值结论用结构化 `values`：`name, value, unit, evidence_id, result_pointer, display_precision`。`result_pointer` 是 JSON Pointer（如 `/result/change_rate`），不能只是泛泛指向某次执行。
- `verification_status` 由校验器写入：`verified | supported | unverified | rejected`。`verified` 仅表示规定的计算与引用检查通过，不表示 SQL 业务语义或因果关系得到证明。

`AnalysisReport` 至少含：`report_id, schema_version, session_id, run_id, status, task, claims, artifact_ids, limitations, unresolved_questions, validation_errors, created_at`。

状态：`success | partial | needs_clarification | failed | cancelled`。业务结论只允许发布经校验的项；未通过的事实记录在校验错误中，不能混进正式回答。解释须绑定支持它的事实；假设必须标记“待验证”；建议不得伪装成已观察到的结果。

### 4.4 落盘、权限和一致性

本期使用 SQLite 元数据 + `var/analysis/` 下的不可变数据/源码/图表产物，单独 `var/analysis/evidence.sqlite`，不要把大文件塞进 LangGraph 消息。

- 文件先写临时文件、校验、原子重命名，再提交元数据；只有元数据和产物都成功才能标记证据可引用。崩溃留下的孤立文件可清理，缺失文件使引用失效。
- 同一操作按 `(run_id, operation_id, attempt)` 唯一约束去重；恢复遇到完成记录直接复用，遇到 running 记录先判断执行器状态，不盲目启动第二个执行。
- 每次读取验证所属会话；未来有认证时同时验证 owner。当前本地会话机制不是多租户认证，不能声称已具备公开部署权限隔离。
- API 接收 ID，不接收任意文件路径。拒绝路径穿越、符号链接越界以及跨会话引用；无权访问的 ID 统一不可见。
- 不保存密钥、数据库密码和宿主环境变量；脱敏只面向展示，不能悄悄改变用于计算的数据并仍声称可复现。
- 保留策略可配置；删除会话时联动清理产物。已删除的证据显示“不可用”，不可继续显示为已验证。

## 5. 分析工具清单与语义

工具只接收数据集 ID 和显式参数，不让模型把表格手抄进参数。统一返回：`status, evidence_id, output_dataset_ids, result, preview, warnings, error`；大结果通过产物读取。纯计算实现和 LangChain 工具包装分离。

| 工具 | 主要输入 | 必须输出/约束 |
| --- | --- | --- |
| `query_dataset` | 本步骤问题、口径、历史数据集引用 | 复用 Text2SQL，存实际 SQL 与数据快照，返回 dataset/evidence ID |
| `inspect_dataset` | dataset ID、列、受限预览范围 | schema、行数、缺失值、重复数、范围、完整性；不把预览当全量 |
| `describe_dataset` | 数值列、可选分组 | count/缺失数、sum/mean/median/min/max/std/分位数；明确 std 的 ddof |
| `compare_periods` | 当前/基期数据集、连接维度、指标、聚合方式 | 两期值、绝对变化、相对变化、单位、缺失分组；基期为 0 返回 null 和原因 |
| `analyze_trend` | 时间列、指标、粒度、窗口 | 实际序列、移动均值、变化率；排序、缺失日期、重复日期处理显式化 |
| `contribution_analysis` | 两期数据、一个维度、可加总指标 | 各组差值、总差值、贡献度、对账误差；只能对可加总量做此分解 |
| `correlation_analysis` | 两个或多个数值列、对齐键、方法 | Pearson/Spearman 系数、有效样本量、缺失策略、常数列警告；不宣称因果 |
| `detect_anomalies` | 时间序列、历史窗口、方法/阈值 | 位置、观测值、基线、分数、阈值；本期实现历史滚动 z-score 或 IQR，不依赖预测服务 |
| `run_python_analysis` | Python 源码、输入 dataset IDs、目的 | 实际执行、结构化结果、源码/日志/输出 ID，详见下一节 |
| `get_evidence` | evidence ID、受限字段/分页 | 本会话证据，帮助子 Agent 核对与修正引用 |
| `submit_analysis_report` | 结构化 claims、限制、待解决问题 | 服务端校验并保存，返回 report ID 和错误；不接受模型自填验证状态 |

约定：

- 变化率为 `(current - baseline) / baseline`；同时保存原始比例和显示百分比。负基期给出解释警告，不用绝对值偷偷替换分母。比例指标区分百分比和百分点。
- 贡献度为 `group_delta / total_delta`，分组须互斥完备。允许负贡献和超过 100%；总变化为 0 时返回 null。缺组是 0 还是未知必须有明确策略，不能默认补 0。不同维度的贡献不能相加。
- 订单与订单明细关联可能放大销售额：SQL 口径中明确去重键/统计粒度，报告展示假设；数据质量检查不能替代业务口径确认。
- 异常检测基线不得包含待检测点或未来点；样本不足、标准差为 0 返回不可计算。阈值和窗口必须记录，异常标记不是业务原因。
- 相关分析先按键对齐，拒绝意外多对多连接；至少明确最小样本数、缺失处理和常数列行为。只做描述性关联，不自动输出显著性结论。
- 图表通过固定工具或 Python 产生，保存制图数据/参数/证据引用；图像本身不能替代数值证据。
- 预测、Adtributor 完整移植、多因素回归留作后续；本期不得输出未经执行的预测值。

## 6. Python 编写、执行与修正

必须支持子 Agent 直接生成 Python 源码并调用 `run_python_analysis`，不能只提供固定分析函数后宣称支持编程。

### 6.1 程序协议

执行器为代码提供受限 SDK：`load_dataset(id)`、`emit_result(dict)`、`save_dataset(df, name)`、`save_figure(fig, name)`。输入仅限本次授权数据集，输出名仅是逻辑名称。由宿主分配产物 ID、计算 hash 和关联输入。

示意（是待实现接口，不是现有 API）：

```python
from analysis_sdk import load_dataset, emit_result, save_figure
import matplotlib.pyplot as plt

df = load_dataset("ds_sales_daily")
sales = df.groupby("day", as_index=False)["sales"].sum().sort_values("day")
first = float(sales.iloc[0]["sales"])
last = float(sales.iloc[-1]["sales"])
emit_result({"first": first, "last": last,
             "change_rate": None if first == 0 else (last - first) / first})
fig, ax = plt.subplots()
ax.plot(sales["day"], sales["sales"])
save_figure(fig, "daily_sales.png")
```

只打印输出不等于有效结构化结果。代码退出成功但未提交合法结果、产生 NaN/Infinity 或缺失必要产物，返回协议错误。标准输出和标准错误仍保存以便修正。完整源码可查，不只保存最后一行输出。

### 6.2 执行隔离

本期默认 Docker 独立容器，镜像预装并固定 pandas/numpy/scipy/matplotlib 等版本；提供构建文件和构建命令。服务端通过参数数组启动固定命令，不拼接模型给出的 shell。不得在 Web 进程或宿主直接 `exec` 模型代码。

- 非 root、只读根文件系统、禁网、无宿主凭据、无 Docker socket、无特权、移除 capabilities、启用 no-new-privileges。
- 只挂载本次输入（只读）和受限输出目录；设置 CPU、内存、进程数、执行时限、输出字节数、文件数和文件大小上限。
- 默认建议：每次 30 秒、1 CPU、512 MiB、64 进程、总产物 20 MiB、日志各 64 KiB；最终值配置化并测试。
- 输出上限须在运行中监控/约束，不能等磁盘写满后才检查；杀死超限容器及子进程并收集失败记录。
- 容器不可用则返回 `executor_unavailable`，**绝不自动回退到本机执行**。仅依赖 AST 黑名单或受限 builtins 不能称为沙箱。
- 容器内仅有数据/源码/执行 SDK，不接触数据库；查询在服务端通过只读适配器完成。
- Python 产物只能通过验证的 JSON/表格/PNG 等白名单导入；不反序列化 pickle，不执行生成的 HTML/JS，拒绝符号链接和越界路径。

### 6.3 修复与预算

失败返回精简错误、失败 evidence ID、可用输入 ID；允许子 Agent 读取错误后修改代码，最多自动修复 2 次，保留每次源码和错误。超时、取消、资源不足有独立错误码。

整个 run 另设工具调用、模型调用、墙钟时间预算（建议 20 次工具、12 次模型调用、180 秒，不把待用户澄清时间计入）。嵌套 Text2SQL 的调用也计入总预算；原 SQL 重试上限继续生效。预算耗尽返回 partial/failed，不能编造完成结论。

## 7. “结论有依据”的校验与边界

最终回答必须来自 `submit_analysis_report` 已保存的报告。子 Agent 最后只需返回 report ID 与简短状态；包装器从本 run 的存储取报告，不能相信模型给出的任意 report ID 或最后文本。缺少报告则失败。

确定性校验至少包含：

1. 所有证据存在、属于本会话可访问范围、执行成功、产物完整、hash 匹配；历史复用注明原查询时间，不冒充本轮新数据。
2. 所有结果指针存在且类型正确；数字、单位和允许的显示舍入与实际结果一致，容差由代码配置，模型不能随意放大。
3. 针对增长率、占比、贡献度等固定工具，用输入快照独立重算关键公式和总分对账。不能只与模型提交的数字比较。
4. 截断数据不得支撑总体结论；局部样本结论须显式限定范围。空结果不能自动解释成业务值 0。
5. Python 的源码、输入、执行结果均存在才能作为计算证据；执行成功并不能证明代码计算逻辑正确。任意 Python 结果仅标记 supported；能由独立固定工具复核的公式才可提升对应检查级别。
6. 不允许有数值但无结构化 values 的事实项。MVP 中事实文本由受控模板及已绑定值渲染，包含时间范围和单位；不要依赖正则扫描自由文本来保证无编造。
7. 自由解释、假设、建议与事实分开展示。不得把“地区 A 贡献了下降额”写成“地区 A 的活动导致下降”；解释的语义正确性需要人工判断，不能声称已经自动证明。

主图和前端直接展示规范报告；如需要主 Agent 润色，只能调整连接语，不得新增事实/数值、删掉限制或改变引用。不要再走一轮无约束 LLM 总结覆盖校验结果。

证据保证的是来源和计算可核对，不保证自然语言问题已经被正确翻译成 SQL，也不保证输入数据真实无误。报告应展示统计口径和关键假设，让用户能够发现这一类错误。

## 8. Web、CLI 与配置

Web 消息新增 `analysis` 类型，包含报告 ID、状态、结论列表、限制和产物；旧的 SQL 结果消息结构保持兼容。证据面板至少展示：

- 引用结论、查询问题、时间范围、过滤条件、单位、完整性警告。
- SQL/参数、数据 schema、行数和分页预览、快照下载。
- 工具方法/参数，或 Python 源码、执行状态、日志、结构化结果。
- 父证据、输入数据集和图表，使用户可以沿计算链回查。

建议接口（均为待新增）：

- `GET /api/sessions/{session_id}/analysis/{run_id}`
- `GET /api/sessions/{session_id}/evidence/{evidence_id}`
- `GET /api/sessions/{session_id}/artifacts/{artifact_id}`

路由可按现有 API 风格调整。源码/日志按纯文本转义，下载设置安全 Content-Type 和 disposition；图表优先 PNG。CLI 打印结论编号和 evidence ID，并提供实际可用的查看命令，不只提示“查看日志”。分析进度可先复用现有请求机制，不强制引入 SSE。

配置放 `config.yaml.example` 的 `analysis` 节：`enabled`（默认关闭）、可选模型、证据目录、执行器/image、资源限额、最大查询行/字节数、调用预算、修复次数、保留期。实际密钥从现有配置/环境加载，不写进示例。关闭分析时原 CLI/Web 路径仍能运行，不应强制要求 Docker。

## 9. 建议文件布局与实施顺序

```text
mini/analysis/
  agent.py                 DeepAgents 构建、提示词与模型配置
  delegation.py            主图工具包装、输入注入和检查点衔接
  models.py                Task / Dataset / Evidence / Claim / Report
  store.py                 元数据、产物、权限和幂等
  validation.py            引用、数值、完整性校验
  reporting.py             受控事实模板与报告渲染
  tools/                   查询包装、统计、对比、趋势、贡献、相关、异常
  execution/               Docker 执行器、SDK、协议和限制
  prompts/                 子 Agent 指令
frontend/src/components/   AnalysisReport / EvidencePanel
Dockerfile.analysis
requirements/analysis.txt  按现有依赖组织方式接入
 tests/                    下述测试及小型人工构造 fixtures
```

按以下顺序交付，每步写明新增/修改文件、验证命令和已知限制；不要只实现工具定义而不接入主图。

1. **最小兼容性与协议**：固定 DeepAgents 兼容版本；确认默认工具面；定义模型、存储和合成数据 fixture，写协议测试。
2. **查询到证据**：完成分析专用 Text2SQL 包装、同轮多次查询存储、数据完整性和暂停恢复。
3. **隔离 Python**：真实执行、源码/输入/结果落盘、失败修正、资源限制；先打通一次完整计算链。
4. **固定分析工具与校验**：实现表中工具，完成数值与引用反例测试；建立报告提交与受控渲染。
5. **主图和页面**：分析路由、结构化报告交接、CLI/Web 证据查看、刷新恢复与产物下载。
6. **验收与交接**：完整演示、测试报告、安装和运行说明；更新 `docs/architecture.md` 与 README 的真实能力说明。

## 10. 验收用例与完成标准

### 10.1 固定数值案例（必须自动测试）

使用人工构造数据：基期 A=600、B=400，总额 1000；当前 A=360、B=440，总额 800。

应得到：总额下降 200，变化率 -20%；A 差值 -240、贡献 120%；B 差值 +40、贡献 -20%；贡献合计 100%。允许上述正负贡献，不把 B 写成下降因素。用户展开结论必须看到对应快照、查询与计算证据。

再让子 Agent 通过实际 Python 计算同样结果、生成 PNG 图，并与固定工具对照。自动测试验证执行链和数值；真实 LLM 演示另行运行并记录模型及成功/失败，不能用脚本预置回复冒充模型自主分析。

### 10.2 必须覆盖的失败与边界

| 类别 | 用例 / 预期 |
| --- | --- |
| 证据防伪 | 伪造 ID、跨会话 ID、错误 JSON Pointer、失败执行被引用、缺失/篡改产物 → 拒绝发布事实 |
| 数值一致性 | 工具返回 -20%，报告写 -30%；元/万元混用、百分点/百分比混用 → 拒绝 |
| 口径边界 | 截断行、主动 LIMIT、空数据、零基期、负基期、重复连接、时区跨日、缺失日期 → 明确警告或拒绝总体结论 |
| 算法边界 | 常数列相关、样本不足、异常检测未来泄漏、总差为零、维度不完备 → 返回受控结果或不可计算 |
| Python | 真实执行成功、语法错误后修正、无 emit_result、非法 JSON、超时、超内存、输出超限 → 状态与证据一致 |
| 隔离 | 访问宿主文件/环境密钥、联网、路径穿越、符号链接、pickle 输出 → 阻断或拒绝导入；Docker 不可用不降级 |
| 状态 | 同一轮多次 SQL 不覆盖；两会话并行无串数据；暂停后恢复原子调用；重复 request 不重复执行 |
| 取消/故障 | 中途取消杀死容器、SQLite 写失败不发布成功、重启后可恢复/明确中断状态 |
| 展示 | 网页刷新仍能展开证据；无证据文本不会被当成事实；日志中的 HTML 被转义 |
| 兼容 | 原有简单查询、历史引用、澄清/审核、CLI/Web 测试继续通过；analysis.enabled=false 无 Docker 依赖 |

无需 LLM 的测试必须离线可运行；Docker 集成测试明确标记，环境缺失只能报 skipped，不能写成通过。可执行命令按现有项目补齐，例如：

```bash
python -m pytest -q -o addopts='' tests
# 新增明确的 Docker 集成测试命令及样例演示命令，不留占位实现。
```

完成时提交一份简短验收记录：实现清单、依赖版本、测试结果与跳过原因、真实 Python 运行 evidence/report ID、可复现命令、网页证据展开截图或操作验证记录、未完成项。只有代码、没有实际执行证据和可查看入口，不算完成。

## 11. Git 与交接纪律

- 本文以外的功能均待后续 Agent 实现；不要从规格推断已交付状态。
- 不提交真实业务快照、生成的 Python 脚本/日志/图表、会话数据库、模型密钥、索引和执行器临时文件。
- 运行产物统一放 `var/analysis/`；合成测试样本放 `tests/fixtures/analysis/` 并提交。不要全局忽略 `*.json`、`*.csv`、`*.png`、`*.sql`、`*.sqlite`，项目已有样例数据需要保留。
- 依赖锁文件、Dockerfile、配置模板、测试 fixtures 应提交；`.gitignore` 不能移除已被 Git 跟踪的敏感文件，如发现需单独处理，不盲目删除。
- 当前环境系统 Git 可能不可用，可用项目已有 `sh scripts/with-runtime.sh git ...`。
- 不擅自覆盖工作区已有改动，不自动发布/部署；根据用户后续指令提交代码。
