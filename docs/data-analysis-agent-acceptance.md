# 数据分析子 Agent 验收记录

日期：2026-11-03。项目：BetterChatBI。

核心开发已接入主图、CLI 和 Web，并完成离线、真实 Docker、真实模型及浏览器验证。真实模型报告为 **partial**：数值与引用校验通过，但仍有业务原因和时间口径问题待核实；没有人为将它改为 success。当前仅在本机运行，没有提交代码或发布部署。

## 实现与入口

| 阶段 | 新增/修改文件 | 已验证内容 |
| --- | --- | --- |
| 协议与存储 | `mini/analysis/models.py`、`store.py` | 版本化协议、SQLite、不可变快照/源码/日志/PNG、hash、会话权限、幂等、删除与保留期 |
| 查询证据 | `tools/runtime.py`、`delegation.py` | 本步骤 Text2SQL、多次 SQL 不覆盖、失败尝试留档、暂停/重启恢复、检查点前崩溃时复用已完成 SQL |
| Python | `execution/`、`Dockerfile.analysis`、`requirements/executor.txt` | 实际编写与执行、源码预先持久化、容器身份、真实退出码、输出导入、修复预算、取消、宿主中断后的明确失败 |
| 计算与报告 | `tools/statistics.py`、`validation.py`、`agent.py` | 七类固定工具、数值与单位、JSON Pointer、快照重算、强类型报告、受控事实文本、Python 支持级别 |
| 主图与展示 | `mini/agents/main_graph.py`、`mini/cli/`、`mini/web/`、`frontend/src/components/` | 分析路由、run 身份、报告交接、分页快照、源码与日志、父证据、PNG、刷新、取消与会话清理 |
| 演示与文档 | `mini/analysis/demo.py`、`reporting.py`、新增分析测试、README、architecture | 可复现合成演示、真实 LLM 模式、证据查看/导出、安装与能力边界 |

默认 `analysis.enabled=false`。本地实际配置已开启分析；新分析关闭后，已有存档仍可读取与清理。模型的默认文件、shell 与再委派工具通过模型/工具两层白名单禁用，只有规划和证据工具可用。

## 依赖与运行环境

- 全新 Python 3.12 环境安装并验证：DeepAgents **0.6.7**、LangChain **1.3.14**、langchain-core **1.5.0**、LangGraph **1.2.9**、SQLite checkpointer **3.1.0**。保留项目原 runtime 锁定版本，没有整体升级它们。
- 可重现的完整开发/验收环境：`requirements/analysis-dev.lock`。最小可选依赖：`requirements/analysis.txt`。
- Docker 基础镜像固定为 Python 3.12.10 的 digest；执行依赖完整固定在 `requirements/executor.txt`，包括 pandas 2.2.3、numpy 2.2.5、scipy 1.15.2、matplotlib 3.10.1。
- 本机 Docker 29.8.2。默认 Docker Hub 认证端点连接失败；使用官方 Python 公共镜像的同一 digest，以及可达的 PyPI 镜像完成构建。
- 本地 `config.yaml` 已配置，文件权限 600 且被 Git 忽略。密钥不进入本记录。LLM `deepseek-flash` 真实连通；embedding `text-embedding-v4` 真实返回 **1024** 维。
- 三组 Chroma 索引已建立：columns **21** 条、table_selection_example **10** 条、text2sql **8** 条。运行目录被忽略。

## 测试结果

最终全量命令（安装上述锁定环境并准备镜像后）：

```bash
RUN_ANALYSIS_DOCKER=1 python -m pytest -q -o addopts='' tests
```

结果：**136 passed，1 skipped**。唯一跳过项为原有 PostgreSQL 集成测试，原因是没有设置 `TEST_POSTGRES_DSN`。Docker 分组包含真实执行，也包含明确的宿主中断故障注入测试；不将模拟中断描述成实际拔电或重启 Docker daemon。

覆盖人工固定数值、零/负基期、缺组、空数据、ddof、相关常数列/样本不足/重复键、历史异常基线、缺失日期、时区跨日、截断与主动 LIMIT、单位与百分点、大金额不使用相对误差豁免、伪造/跨会话引用、错误指针、缺失/篡改产物、SQLite 写失败、并行会话、操作身份冲突、双查询暂停与重启恢复、完整 SQL 已落盘但外层中断后的复用、关闭分析后的存档访问和删除。

Python 验证包含真实计算/PNG/派生数据集单位、语法失败后修复、无 emit_result、非法 JSON/NaN、超时、超内存、日志/文件大小/文件数量、禁网、非 root、只读输入与根文件系统、宿主凭据不可见、SDK 路径穿越、符号链接、pickle/伪 PNG 拒绝、取消以及伪造成功退出标记。

生成程序使用 UID 65533；PID 1 哨兵为另一非 root UID 65532。成功状态读取真实 `docker exec` 退出码，不读取生成代码可写的退出文件。程序结束后先终止其后代再导入产物；宿主中断时哨兵最多保留到执行时限加 15 秒。运行记录保留容器身份，恢复时不重跑原操作。容器不可用时明确失败，绝不在宿主执行代码。

其他检查：Vue 类型检查及 Vite 生产构建通过；新增分析模块和测试的 Ruff F/I 检查及格式检查通过；`git diff --check` 通过。CLI 原测试替身已适配可选分析参数。

## 真实模型与实际执行记录

### 第一次真实模型运行

- 模型：`deepseek-flash`。
- run：`run_0b562119e2de41cfb3371a4b72ff8a66`。
- 实际固定计算、Python 与 PNG 已成功，但报告使用了错误字段/指针。校验器拒绝发布事实，最终触及 12 次模型预算，状态 failed。
- 根据实际失败修正了工具接口：报告使用 `list[ClaimInput]` 强类型 schema；SDK 返回语义和工具参数明确；工具返回服务端产物与数据集元数据。没有放宽数值、单位或引用校验。

### 第二次真实模型运行

- 模型：`deepseek-flash`；实际耗时约 **56.9 秒**，**6 次模型调用、6 次工具调用**。
- run：`run_7594a95bfc4540fc9356c8194f98b09c`。
- report：`report_f7d1758902e04dc3852d494d8ab401b6`。
- 固定贡献证据：`ev_454a2fc874d94110853c2dbdd691b5f3`。
- Python 证据：`ev_6c1a5df18ee447448a1b0546586e23af`。
- Python 源码：`art_c655ffa856634a4dac2b9954bbaeb418`。
- PNG：`art_bf3d6d0fb53448a5a39410f1eae58ae4`。
- Python 真实退出码 0，镜像 digest、源码 hash、输入 hash 和依赖版本见该证据 parameters。
- 共 **9 条结论**：5 条固定计算事实 verified、1 条 Python 事实 supported，另有解释/假设/建议各 1 条，均明确未自动验证语义。`validation_errors=[]`。
- 合成案例实得：1000 → 800，变化 -200、-20%；A 差值 -240、贡献 120%；B 差值 +40、贡献 -20%；贡献和为 100%，对账误差为 0。
- 人工检查生成源码确认使用 `load_dataset` 读取实际快照，执行分组、加总、差值与贡献计算并调用 `save_figure`；不是脚本预置模型回复。
- 状态保留 **partial**：模型列出了地区业务原因、期间归属与增长持续性等待核实问题。合成数据不能证明这些业务原因，不能声称因果分析已经完成。

本次模型模式使用脚本先查询并提供合成数据集，分析与 Python 代码由真实模型自主完成；它不是 Text2SQL 准确率评测。另行运行了真实主图 → Text2SQL → 项目自带数据库 → Web 查询消息，并与独立只读 COUNT(*) 对照一致。明细运行记录分别位于忽略路径：

- `var/analysis/real-llm-demo.json`：第一次失败。
- `var/analysis/real-llm-demo-v2.json`：第二次 partial 报告。
- `var/analysis/live-simple-query.json`：真实主图查询与独立对照。
- `var/analysis/connection-check.json`：不含密钥的连通性结果。

## 网页与 CLI 操作验证

浏览器实际操作通过：打开真实模型报告 → 展开固定计算 → 回查父 SQL → 读取/分页快照 → 展开 Python 源码、日志、结构化结果及 PNG → 刷新后再次展开。PNG 检查 naturalWidth > 0；无页面脚本错误。下载接口验证 Content-Type、attachment、nosniff 与跨会话 404。

本地截图（未提交 Git）：

- `var/analysis/web-evidence.png`
- `var/analysis/web-python-source.png`
- `var/analysis/web-python-chart.png`

本地应用为 `http://127.0.0.1:8000`；已保存真实模型报告的只读展示为 `http://127.0.0.1:8765`。这是本机验证，不是远端部署。

查看本次实际 Python 证据与源码：

```bash
python -m mini.analysis.reporting --store var/analysis/demo --session synthetic-demo --id ev_6c1a5df18ee447448a1b0546586e23af
python -m mini.analysis.reporting --store var/analysis/demo --session synthetic-demo --id art_c655ffa856634a4dac2b9954bbaeb418 --artifact
python -m mini.analysis.reporting --store var/analysis/demo --session synthetic-demo --id art_bf3d6d0fb53448a5a39410f1eae58ae4 --artifact --output /tmp/analysis-chart.png
```

## 复现与边界

```bash
python -m pip install -r requirements/analysis-dev.lock
docker build -f Dockerfile.analysis -t betterchatbi-analysis:1 .
python -m mini.analysis.demo --python
python -m mini.analysis.demo --llm --config config.yaml
RUN_ANALYSIS_DOCKER=1 python -m pytest -q -o addopts='' tests/test_analysis_docker.py
cd frontend
npm ci
npm run build
```

真实模型输出不保证每次相同；发生错误或预算耗尽时应保留 partial/failed，而不是伪造完成。调用次数和墙钟时间在边界检查；已发出的模型请求不能被强制中止，当前配置有请求超时。等待人工澄清不计入活动时间；Python 不恢复一半的进程，而是保留源码并明确中断，后续新操作需新身份。

固定工具当前支持每日趋势、历史滚动 z-score、单快照按唯一键对齐的相关及单维可加总贡献。超限明细不做分页拼接；不支持预测、因果推断或完整 Adtributor。当前本地会话隔离不等于公开部署的多租户认证。SQL 业务语义、业务输入真实性及自由解释仍须人工判断；未执行真实业务原因验证，也未运行未配置的 PostgreSQL 集成测试。
