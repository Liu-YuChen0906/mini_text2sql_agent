# Mini Text2SQL Agent

这是一个独立的命令行 Text2SQL 小项目：输入自然语言问题，先从文件式 Catalog 和向量索引中寻找相关数据库结构，再让模型生成 SQLite 查询，最后在示例数据库中只读执行并显示结果。它有自己的 Git 仓库，不依赖外层 `openchatbi` 项目的 Python 包或运行配置。

## 目前项目的整体流程

```text
用户输入问题
  → 加载配置、Catalog、三个 Chroma 索引和聊天模型
  → 提取问题信息
  → Schema Linking：匹配字段、筛选候选表、选择表和字段
  → 拼接表结构、业务规则及相似 SQL 示例
  → 生成 SQLite SQL
  → 只读执行 SQL
  → 格式化并打印结果
```

入口是 [`main.py`](main.py)，它按上述顺序调用各模块。运行一次问题的具体步骤如下。

### 1. 准备资源

[`resources.py`](resources.py) 从本地 `config.yaml` 读取配置，创建一次 DeepSeek 聊天模型，并准备以下资源：

| 资源 | 来源 | 用途 |
| --- | --- | --- |
| `FileCatalog` | `catalog_data/` 中的 CSV、YAML | 提供表、字段、业务规则、选表示例和 SQL 示例；不保存业务数据行 |
| `columns` 索引 | 本地 `chroma_db/` | 按相似度寻找可能相关的字段 |
| `table_selection_example` 索引 | 本地 `chroma_db/` | 寻找相似的选表示例 |
| `text2sql` 索引 | 本地 `chroma_db/` | 寻找相似的 SQL 示例 |
| SQLite 数据库 | `data/tracking_orders.sqlite` | 保存实际客户、订单等数据，供最终查询 |

三个 Chroma 集合的文档会与 Catalog 核对；正常查询默认只打开已有索引，不自动建索引。若 Chroma 数据库使用了比当前安装版本更新的迁移，启动前会给出明确错误。`config.yaml`、`chroma_db/` 都不提交到 Git。

### 2. 提取问题信息

[`schema_linking.py`](schema_linking.py) 第一次调用聊天模型，把原问题整理成 `QuestionInfo`：

```python
QuestionInfo(
    rewrite_question="列出每位客户的姓名和订单数量",
    keywords=["客户", "订单"],
    dimensions=["客户姓名"],
    metrics=["订单数量"],
)
```

上面仅是格式示例，具体内容由模型返回。`rewrite_question` 是完整、忠实的改写；后三项都是 `list[str]`。当前 Mini 版把三组词合并用作字段检索线索，**没有**像外层原项目那样，按维度和指标的字段类别分别过滤向量搜索。`keywords` 还会单独用于检索选表示例。

### 3. Schema Linking：找字段、筛表、选表

1. 用 SQLite 的 `sqlite_master` 和 `PRAGMA table_info` 读取真实存在的表及字段，之后只允许使用 Catalog 和数据库中都存在的字段。
2. 把 `keywords`、`dimensions`、`metrics` 中的非空词合成一次字段检索文本；全为空时使用 `rewrite_question`。对 `columns` 索引取前 12 条，只接收距离小于 `0.5` 且字段名存在于 Catalog 的结果；同时用 Catalog 中的字段名、显示名、别名、标签和描述做文本包含及相似度匹配，相似度门槛为 `0.8`。向量检索失败时仍继续文本匹配。
3. 从 Catalog 中筛出真实数据库存在、且至少包含一个相关字段的候选表。如果一个字段也没命中，则不按相关性过滤候选表；若仍没有候选表就报错。
4. 在 `table_selection_example` 中检索相似问题（`k=5, fetch_k=20`），只保留其示例表全部位于候选表集合中的示例。
5. 把候选表、字段说明、选表规则、相似示例和改写问题交给模型。模型返回所选表及字段；程序核对表和字段必须在候选范围内。无效结果会携带错误原因重试一次。

这一步输出 `LinkResult`：`rewrite_question`、通过校验的 `selected` 表及字段、SQLite 中真实存在的 `real` 字段集合。**它还没有生成 SQL。**

### 4. 构造 SQL 上下文并生成 SQL

[`sql_context.py`](sql_context.py) 为每张已选表整理描述、相关字段、全部可用且真实存在的字段、派生指标说明和 SQL 规则；再用改写后的问题从 `text2sql` 检索相似 SQL 示例（`k=5, fetch_k=20`）。示例涉及的表必须都在已选表中。

[`generate_sql.py`](generate_sql.py) 把改写问题和这段上下文交给同一个聊天模型，要求只返回一条 SQLite `SELECT` 查询。代码会移除可能出现的 Markdown 代码围栏，但**生成阶段不验证 SQL 一定正确**；执行错误会在下一步以结果状态返回。

### 5. 执行与展示

[`execute_sql.py`](execute_sql.py) 只接受首词为 `SELECT` 或 `WITH` 的一条语句，并以 SQLite `mode=ro` 打开数据库。默认超时为 2 秒，最多返回 100 行；为了判断是否截断，会额外读取第 101 行。返回字典含 `status`、`columns`、`rows`、`truncated` 和 `error`。

[`presentation.py`](presentation.py) 将结果排成终端表格，显示失败、无数据或后续还有数据的提示。当前入口只打印生成的 SQL 和查询结果，**不再生成自然语言说明**；查询结果以 SQLite 返回的数据为准。

## 待完善的流程

以下是尚未实现、值得按顺序推进的改进，不应误认为当前能力：

1. **真正利用问题分类。** 当前 `dimensions`、`metrics` 在字段检索时被合并。可参考原项目，分别检索维度字段和指标字段，再评估候选表召回是否改善；同时需要处理 Catalog 中并非 `dimension`／`metric` 的现有 `category` 值。
2. **提供正式的建索引命令。** 当前首次部署必须手动调用 `initialize_chroma(..., build_missing=True)`；增加独立命令、索引版本检查和清晰的重建步骤会降低部署难度。
3. **校验并修复生成的 SQL。** 目前模型生成的 SQL 不会按已选结构做完整语法和字段校验，执行失败后也不会反馈错误给模型重试。
4. **支持分页或导出完整结果。** 默认只展示前 100 行；“所有客户”这类问题可能只显示一部分数据。
5. **设计可靠的结果说明。** 早期版本只把前 10 行交给模型，曾出现概括错误；当前入口已停用该步骤。若要恢复，应先做确定性的统计摘要，或明确限制模型只能描述预览中的事实。
6. **补足服务化部署与故障处理。** 当前只有单次交互的命令行入口；若需要 API、Web 界面、鉴权、日志和远端部署，应在这个稳定流程之上另行设计。

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

from catalog import FileCatalog
from resources import initialize_chroma

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

若已有索引报“需要比当前 chromadb 更新的版本”，先备份该索引目录，再用与当前环境兼容的索引，或在空目录中执行上述命令重建。正常运行 `main.py` 不会自动重建索引。

### 4. 启动并提问

```bash
python main.py
```

在提示符输入自然语言问题，例如“给我说出所有客户的名字和订单数量”。程序会打印生成的 SQL 和结果表格。示例数据库已随仓库提交，无需另行导入。

### 5. 可选：运行离线回归测试

```bash
python -m pip install pytest
python -m pytest -q -o addopts='' test_refactor.py
```

测试用假模型和假检索对象覆盖主要流程，不会调用真实聊天模型或在线嵌入服务。

## 许可

来源项目的许可文本见 [`UPSTREAM_LICENSE`](UPSTREAM_LICENSE)。
