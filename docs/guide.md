# Mini OpenChatBI Agent

这是一个独立的命令行项目。持续运行的主 Agent 保存会话消息，判断用户是在普通交流，还是需要调用 Text2SQL 查询子图。子图使用 LangGraph 编排问题澄清、Schema Linking、SQL 生成、执行错误修复与低置信度人工审核。业务数据库通过统一适配层接入，当前提供 SQLite 和 PostgreSQL 实现。它有自己的 Git 仓库，不依赖外层 `openchatbi` 项目的 Python 包或运行配置。

## 目前项目的整体流程

```text
用户输入消息或追问
  → 主 Agent 读取会话消息与成功查询历史摘要
  → 普通交流：直接回复
  → 数据问题：调用 Text2SQL 子图，按需读取指定旧轮次的已保存结果
  → 子图提取问题信息；必要时暂停询问用户，恢复后重新提取
  → Schema Linking：匹配字段、筛选候选表、选择表和字段
  → 拼接表结构、业务规则及相似 SQL 示例
  → 按业务数据库方言生成 SQL
  → 只读执行 SQL；可修复错误最多重试 3 次
  → 执行成功后评分；低于 0.7 或评分失败时暂停人工审核
  → 人工可通过、用自然语言反馈重写 SQL 或重新选表、拒绝或退出
  → 成功轮保存原始问题、完整改写、最终 SQL 与实际返回结果
  → 格式化并打印本轮 SQL 和结果表；继续等待下一轮消息
```

入口是 [`main.py`](../mini/cli/main.py)，持续会话的主图定义在 [`agent_graph.py`](../mini/agents/main_graph.py)，单轮 Text2SQL 子图由 [`text2sql_graph.py`](../mini/agents/text2sql_graph.py) 中的 `build_text2sql_graph()` 构建。主图中的 `run_text2sql` 是可执行工具：模型只填写历史轮次 ID，主图注入当前问题和任务配置；工具运行子图并在成功后保存记录。[`query_history.py`](../mini/runtime/query_history.py) 在检查点 SQLite 文件中另建 `successful_queries` 表，以会话 ID 和轮次 ID 保存成功查询。主图检查点仅保留成功记录 ID 和本轮摘要，展示时从历史表读取完整 SQL 与结果；子图检查点仍可保存审核恢复所需的执行状态。同一任务 ID 对应一个持续会话；旧版单图任务暂不迁移，其检查点文件保留。

主 Agent 的模型只有 `run_text2sql` 一个数据工具。需要数据库事实时必须调用该工具；普通交流可直接回复。主图每轮读取有长度上限的历史摘要和结果预览，按轮次 ID 取被引用旧表的已保存行交给子图。每次数据问题都重新生成并执行本轮 SQL，不把旧表写入业务数据库。成功记录包含原始问题、完整改写、最终 SQL、列名、实际返回的行及截断标记；失败轮不入库。历史保存的行最多为执行器实际返回的 100 行，并非数据库的全部匹配行。

### 1. 准备资源

[`resources.py`](../mini/runtime/resources.py) 从本地 `config.yaml` 读取配置。启动时只创建主 Agent 的 DeepSeek 聊天模型；第一次数据查询才加载 Catalog、三个 Chroma 索引和业务数据库适配器，后续查询复用这些资源：

| 资源 | 来源 | 用途 |
| --- | --- | --- |
| `FileCatalog` | `catalog_data/` 中的 CSV、YAML | 提供表、字段、业务规则、选表示例和 SQL 示例；不保存业务数据行 |
| `columns` 索引 | 本地 `chroma_db/` | 按相似度寻找可能相关的字段 |
| `table_selection_example` 索引 | 本地 `chroma_db/` | 寻找相似的选表示例 |
| `text2sql` 索引 | 本地 `chroma_db/` | 寻找相似的 SQL 示例 |
| 业务数据库 | 默认 `data/tracking_orders.sqlite`，可配置 PostgreSQL | 保存实际客户、订单等数据，供最终查询 |

三个 Chroma 集合的文档会与 Catalog 核对；正常查询默认只打开已有索引，不自动建索引。若 Chroma 数据库使用了比当前安装版本更新的迁移，普通聊天仍可运行，首次数据查询会给出明确错误。`config.yaml`、`chroma_db/` 都不提交到 Git。

### 2. 提取问题信息

[`schema_linking.py`](../mini/query/schema_linking.py) 的图专用提取函数第一次调用聊天模型，先判断是否必须澄清；明确的问题整理成 `QuestionInfo`：

```python
QuestionInfo(
    rewrite_question="列出每位客户的姓名和订单数量",
    keywords=["客户", "订单"],
    dimensions=["客户姓名"],
    metrics=["订单数量"],
)
```

上面仅是格式示例，具体内容由模型返回。需要澄清时，子图用 `interrupt` 暂停；用户补充信息后用相同任务 ID 通过主图恢复，再次提取。每轮最多澄清两次。主图为提取节点提供成功查询历史，当前问题明确提出的条件优先，独立新问题不继承历史条件。每轮重置的是子图内的 SQL、执行结果、错误、审核及修复次数；主图的消息和结构化成功历史会继续保留。`rewrite_question` 是可独立执行的完整改写；后三项都是 `list[str]`。当前 Mini 版把三组词合并用作字段检索线索，**没有**像外层原项目那样按维度和指标的字段类别分别过滤向量搜索。`keywords` 还会单独用于检索选表示例。

### 3. Schema Linking：找字段、筛表、选表

1. 通过业务数据库适配器读取真实存在的表及字段，之后只允许使用 Catalog 和数据库中都存在的字段。
2. 把 `keywords`、`dimensions`、`metrics` 中的非空词合成一次字段检索文本；全为空时使用 `rewrite_question`。对 `columns` 索引取前 12 条，只接收距离小于 `0.5` 且字段名存在于 Catalog 的结果；同时用 Catalog 中的字段名、显示名、别名、标签和描述做文本包含及相似度匹配，相似度门槛为 `0.8`。向量检索失败时仍继续文本匹配。
3. 从 Catalog 中筛出真实数据库存在、且至少包含一个相关字段的候选表。如果一个字段也没命中，则不按相关性过滤候选表；若仍没有候选表就报错。
4. 在 `table_selection_example` 中检索相似问题（`k=5, fetch_k=20`），只保留其示例表全部位于候选表集合中的示例。
5. 把候选表、字段说明、选表规则、相似示例和改写问题交给模型。模型返回所选表及字段；程序核对表和字段必须在候选范围内。无效结果会携带错误原因重试一次。图调用 `link_from_info`，不会再次提取问题。

这一步输出 `LinkResult`：`rewrite_question`、通过校验的 `selected` 表及字段、业务数据库中真实存在的 `real` 字段集合。**它还没有生成 SQL。**

### 4. 构造 SQL 上下文并生成 SQL

[`sql_context.py`](../mini/query/sql_context.py) 为每张已选表整理描述、相关字段、全部可用且真实存在的字段、派生指标说明和 SQL 规则；再用改写后的问题从 `text2sql` 检索相似 SQL 示例（`k=5, fetch_k=20`）。示例涉及的表必须都在已选表中。

[`generate_sql.py`](../mini/query/generate_sql.py) 把改写问题、数据库方言和上下文交给同一个聊天模型，要求只返回一条 `SELECT` 查询。代码会移除可能出现的 Markdown 代码围栏；生成阶段仍不验证 SQL 的业务正确性。

### 5. 执行与展示

业务数据库适配器只接受单条 `SELECT` 或 `WITH` 查询。SQLite 使用只读文件连接；PostgreSQL 使用只读事务。默认超时为 2 秒，最多返回 100 行；为了判断是否截断，会额外读取第 101 行。返回字典含 `status`、`columns`、`rows`、`truncated` 和 `error`。

图只对可修复的语法、字段、表、函数及聚合错误尝试重新生成 SQL，每轮最多重试 3 次；超时和被拒绝的查询结束当前轮。成功执行后，[`sql_quality.py`](../mini/query/sql_quality.py) 请模型按问题、字段含义、SQL 和结果预览评分。低于 `0.7` 或评分失败时，图暂停显示 A 通过、B 重写 SQL、C 重新选表、D 拒绝、E 退出。B、C 接收自然语言修改说明，不接收用户提供的 SQL：B 让模型在当前表结构上下文中重写 SQL，C 重新提取问题并选表，再生成 SQL。人工修改不限次数，每次修改后重新执行和评分；D 沿用本轮自动修复次数限制，E 结束整个会话。评分是审核线索，不保证业务答案正确。

[`presentation.py`](../mini/cli/presentation.py) 将结果排成终端表格，显示失败、无数据或后续还有数据的提示。当前入口打印最终 SQL 和查询结果，**不生成自然语言说明**。

## 待完善的流程

以下是尚未实现、值得按顺序推进的改进，不应误认为当前能力：

1. **真正利用问题分类。** 当前 `dimensions`、`metrics` 在字段检索时被合并。可参考原项目，分别检索维度字段和指标字段，再评估候选表召回是否改善；同时需要处理 Catalog 中并非 `dimension`／`metric` 的现有 `category` 值。
2. **提供正式的建索引命令。** 当前首次部署必须手动调用 `initialize_chroma(..., build_missing=True)`；增加独立命令、索引版本检查和清晰的重建步骤会降低部署难度。
3. **增强生成前校验。** 当前已能对部分执行错误反馈并重试，但生成前没有完整的 SQL 语法、字段引用与业务指标可计算性校验。
4. **支持分页或导出完整结果。** 默认只展示前 100 行；“所有客户”这类问题可能只显示一部分数据。
5. **设计可靠的结果说明。** 早期版本只把前 10 行交给模型，曾出现概括错误；当前入口已停用该步骤。若要恢复，应先做确定性的统计摘要，或明确限制模型只能描述预览中的事实。
6. **补足生产部署能力。** 当前网页入口面向本地单人使用；鉴权、日志治理和远端部署仍需另行设计。

## 历史版本与变更

以下依据本仓库真实 Git 提交记录整理；版本名称是为了阅读方便，不代表已经创建 Git tag。

| 阶段 | 提交 | 添加或修改的内容 |
| --- | --- | --- |
| 初始 Mini 版，2026-09-24 | `b13a848` | 建立独立项目；加入示例 SQLite、配置样例和依赖；直接读取数据库的建表语句交给模型生成 SQL；增加只读执行、100 行上限、2 秒超时、终端表格和可选中文说明。 |
| 许可补充，2026-09-24 | `ab0f99b` | 加入 `UPSTREAM_LICENSE`，保留来源项目的许可文本。 |
| Catalog 与 RAG 重构，2026-09-25 | `7f3b8b3` | 加入文件式 Catalog、表和字段说明、选表与 SQL 示例、三个 Chroma 集合及 Schema Linking；按“资源准备 → 选表 → SQL 上下文 → 生成 → 执行 → 展示”拆分模块；复用模型对象，增加离线回归测试及 Chroma 版本兼容性提示。 |
| 当前整理版 | 本次提交 | 将字段检索中的双重列表推导式拆成普通循环；停用容易误导的自然语言结果说明，并同步入口和测试；新增本 README，写明实际流程、待完善事项与从零部署步骤。 |

## 部署与运行说明

### 1. 克隆独立仓库并安装依赖

下面的命令在项目目录内运行。当前开发环境使用 Python 3.13；建议先使用同版本完成部署。

```bash
git clone https://github.com/Liu-YuChen0906/mini_text2sql_agent.git
cd mini_text2sql_agent
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. 配置两个模型服务

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

- `default_llm.params.api_key`：填写 DeepSeek 聊天模型密钥。
- `embedding_model.params.api_key`、`base_url`、`model`：填写与 `OpenAIEmbeddings` 接口兼容的嵌入服务配置。示例中的占位符不能直接用于检索和建索引。
- `catalog_store.data_path` 默认指向仓库内的 `catalog_data/`；`vector_db_path` 默认指向本地 `chroma_db/`。

聊天模型用于信息提取、选表和生成 SQL；嵌入服务用于首次建索引以及运行时向量查询。首次建索引与查询都需要能够连接相应服务。`config.yaml` 已被 Git 忽略，请勿提交密钥。

### 3. 首次创建 Chroma 索引

Git 仓库带有 Catalog 文件和示例 SQLite，**不带** `chroma_db/`。因此新克隆后必须在仓库根目录执行一次下列命令；它读取你刚填写的配置，将三组 Catalog 文本嵌入并写入本地 Chroma。建索引会调用嵌入服务。

```bash
python - <<'PY'
from pathlib import Path

import yaml
from langchain_openai import OpenAIEmbeddings

from mini.query.catalog import FileCatalog
from mini.runtime.resources import initialize_chroma

root = Path.cwd()
config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

def local_path(value):
    path = Path(value)
    return path if path.is_absolute() else root / path

catalog = FileCatalog(local_path(config["catalog_store"]["data_path"]))
embedding = OpenAIEmbeddings(**config["embedding_model"]["params"])
index_path = local_path(config.get("vector_db_path", "chroma_db"))
initialize_chroma(catalog, embedding, index_path, build_missing=True)
print(f"索引已准备好：{index_path}")
PY
```

若已有索引报“需要比当前 chromadb 更新的版本”，不要修改旧索引的迁移记录。重建时先将上面命令中的 `index_path` 改为另一个空目录，确认三个集合建立成功后，再将 `config.yaml` 的 `vector_db_path` 指向新目录；建索引会把 Catalog 文本发送到配置的嵌入服务。正常运行 `main.py` 不会自动重建索引。

### 4. 启动并提问

```bash
python main.py
```

在提示符输入自然语言问题，例如“给我说出所有客户的名字和订单数量”。程序会打印任务 ID；每轮完成后打印 SQL 和结果表，并继续接受追问。输入 `exit` 或 `quit` 结束会话。若需要澄清或审核，程序会提示输入；在暂停时或两轮之间按 Ctrl-C、结束标准输入，稍后可使用 `python main.py --resume <任务 ID>` 从本地 `checkpoints.sqlite` 继续。明确结束的会话不能恢复追问。示例数据库已随仓库提交，无需另行导入。

终端使用 Rich 展示实时执行过程：理解问题、选表、生成、执行、评分等节点会依次报告开始、完成或暂停，以及实际耗时。重试会追加新的进度行，不覆盖历史。SQL 高亮、中文结果表和模型评分默认展示；评分是模型的判断，不等于正确率。人工审核使用带列名的前五行预览，低分结果被人工批准时会明确标注。

输出随终端宽度调整，宽表在窄终端中改为逐条“字段—值”展示；长文本换行，NULL 弱化显示。标题和关键状态使用少量 emoji，编码不支持时省略。重定向输出或设置 `NO_COLOR=1` 时禁用颜色。例如 `NO_COLOR=1 python main.py`。暂停后打印的恢复命令包含当前解释器、完整任务 ID 和实际检查点路径，可以直接复制使用。

只想查看界面效果，可运行 `python demo_output.py`；用 `python demo_output.py --width 48` 查看窄终端布局。演示使用固定的成功、审核和失败样例，不读取配置、不连接数据库、不调用模型。

### 5. 可选：运行离线回归测试

```bash
python -m pip install pytest
python -m pytest -q -o addopts='' tests/test_refactor.py tests/test_text2sql_graph.py tests/test_presentation.py tests/test_agent_graph.py
```

测试用假模型和假检索对象覆盖主要流程，不会调用真实聊天模型或在线嵌入服务。

## 许可

来源项目的许可文本见 [`UPSTREAM_LICENSE`](../UPSTREAM_LICENSE)。

## 本地网页入口

网页入口使用现有 Agent 与 Text2SQL 流程，命令行 `python main.py` 仍可独立使用。网页会话存储在独立的 `web_checkpoints.sqlite`，不会读取旧 CLI 会话；业务数据按 `database` 配置从 SQLite 或 PostgreSQL 查询。首次运行前完成上文的模型配置和 Chroma 索引准备。

安装 Python 和前端依赖（在本项目目录内）：

```bash
python -m pip install -r requirements.txt
cd frontend
npm install
cd ..
```

开发时分别打开两个终端：

```bash
python -m uvicorn web_api:app --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm run dev
```

浏览器访问 Vite 打印的本机地址。Vite 将 `/api` 代理到 FastAPI 的 8000 端口。构建并由 FastAPI 单独托管时：

```bash
cd frontend
npm run type-check
npm run build
cd ..
python -m uvicorn web_api:app --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000`。前端构建产物、`node_modules` 和网页运行数据库均被 Git 忽略。服务按单 worker 设计，不要用 `--workers` 启动多个进程；同一时刻仅运行一个 Agent 任务，其他会话的历史仍可读取。

网页发送消息后等待整轮结束。澄清或人工审核暂停时，可在会话中回答或决定；刷新页面会从后端恢复消息和待处理状态。执行过程意外中断时，页面显示“执行中断”，需要点击“重试执行”才会从已有检查点继续。断网后先刷新会话状态；不要盲目重发原消息。结果表最多显示前 100 行，BLOB 单元格以 `0x` 前缀十六进制文本展示。网页只在浏览器本地存储当前会话 ID。

离线验证命令：

```bash
python -m pip install pytest httpx
python -m pytest -q -o addopts='' tests/test_web_service.py tests/test_refactor.py tests/test_text2sql_graph.py tests/test_presentation.py tests/test_agent_graph.py
cd frontend && npm run type-check && npm run build
```
