# Mini OpenChatBI Agent

独立的本地数据问答项目：主 Agent 管理多轮会话，Text2SQL 子图负责数据库查询，提供命令行和网页入口。

## 目录

```text
mini/agents/     主 Agent 图和 Text2SQL 子图
mini/query/      Catalog、选表、SQL 上下文、生成、执行与评分
mini/runtime/    模型与检索资源、成功查询历史
mini/analysis/   DeepAgents 分析、执行证据、Docker Python 与报告校验
mini/cli/        命令行流程与 Rich 展示
mini/web/        FastAPI 接口与网页会话服务
tests/           离线回归测试
frontend/        Vue + Vite 页面
docs/           详细设计与部署说明
requirements/   Python 依赖分组
```

根目录的 `main.py`、`web_api.py`、`demo_output.py` 保留原启动命令。`config.yaml.example`、`catalog_data/` 和 `data/` 是部署配置与样例数据；运行时的检查点和 Chroma 索引不提交到 Git。

## 业务数据库

默认查询 `data/tracking_orders.sqlite`。`config.yaml` 的 `database` 配置可选择业务数据源：

```yaml
database:
  type: sqlite
  path: data/tracking_orders.sqlite
```

PostgreSQL 使用相同的订单表时可改为 `type: postgres`，并在启动前设置 `MINI_DATABASE_URL`，例如 `postgresql://只读用户:密码@主机:5432/数据库名`。连接串不要提交到 Git。PostgreSQL 表建议使用未加引号的小写名称；适配器会与 Catalog 中的 `Customers` 等名称匹配。账号应仅有目标表的 `SELECT` 权限。程序还会在每次查询使用只读事务。

添加其他数据库时，实现 `DatabaseAdapter` 的表字段读取和只读查询接口，再用 `register_adapter` 注册类型；若该数据库需要专属 SQL 示例，可在 `catalog_data/` 增加 `sql_example_<dialect>.yaml`。同一次运行只选择一个业务数据源。会话检查点与网页状态使用的 SQLite 不受此配置影响。

CI 使用临时 PostgreSQL 服务运行集成测试；本地未配置 `TEST_POSTGRES_DSN` 时该测试会跳过。

## 启动与验证

如果应用内终端找不到 Node.js，可使用项目运行脚本调用已有的随附运行时：

```bash
sh scripts/with-runtime.sh node --version
sh scripts/with-runtime.sh pnpm --version
```

脚本只为本次命令补充 PATH，不修改 shell 配置；也会使用随附的独立 Git，避免调用依赖 Xcode 的系统 Git 启动器。默认运行时位于 `~/.cache/codex-runtimes/codex-primary-runtime/dependencies`，可通过 `CODEX_RUNTIME_ROOT` 指定其他目录。

在项目根目录运行：

```bash
python -m pip install -r requirements.txt
cp config.yaml.example config.yaml
python main.py
```

网页服务使用 `python -m uvicorn web_api:app --host 127.0.0.1 --port 8000`，前端开发服务在 `frontend/` 中运行 `npm install && npm run dev`。首次数据查询前还需要填写模型配置并建立 Chroma 索引，步骤见[完整指南](docs/guide.md)。

```bash
python -m pip install -r requirements/dev.txt
python -m pytest -q -o addopts='' tests
```

架构与导入规则见[代码架构](docs/architecture.md)。上游许可证见 [UPSTREAM_LICENSE](UPSTREAM_LICENSE)。

数据分析的实施规格见 [DeepAgents 数据分析子 Agent 开发说明](docs/data-analysis-agent-spec.md)；当前交付和未验收项见下文及验收记录。

## 数据分析（可选）

已接入 DeepAgents 分析分支、执行证据存储、固定分析工具、Docker Python 执行器、受控报告校验及 CLI/Web 证据查看。默认关闭；真实模型自主分析的验收状态与已运行测试分别记录在[分析验收记录](docs/data-analysis-agent-acceptance.md)，完整协议见[开发规格](docs/data-analysis-agent-spec.md)。

```bash
python -m pip install -r requirements/analysis.txt -r requirements/dev.txt
# 或复现本次完整测试环境：python -m pip install -r requirements/analysis-dev.lock
docker build -f Dockerfile.analysis -t betterchatbi-analysis:1 .
# 网络受限时，可为构建选择可达的 PyPI 镜像：
# docker build --build-arg ANALYSIS_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -f Dockerfile.analysis -t betterchatbi-analysis:1 .
```

在已有 `config.yaml` 中添加示例的 `analysis` 节并设置 `enabled: true`。分析模型默认复用主模型，可通过 `analysis.model` 单独配置（与 `default_llm` 同格式）。关闭分析时不需要安装 DeepAgents 或启动 Docker；原查询仍需原有模型/Catalog/索引配置。

无需模型的合成 SQL → 固定计算 → 真实 Python/PNG → 报告演示：

```bash
python -m mini.analysis.demo --python
# 本地只读网页演示：http://127.0.0.1:8765
python -m mini.analysis.demo --python --serve
# 单独运行真实模型自主分析：需要实际 config.yaml，不使用脚本预置模型回复
python -m mini.analysis.demo --llm --config config.yaml
```

演示使用合成数据，默认写入 `var/analysis/demo/`，输出实际 run/evidence/report ID；它不代表真实模型演示已运行。默认分析服务写入 `var/analysis/`，两者互不混用。查看演示证据时指定存储目录：

```bash
python -m mini.analysis.reporting --store var/analysis/demo --session synthetic-demo --id <evidence_id>
python -m mini.analysis.reporting --store var/analysis/demo --session synthetic-demo --id <artifact_id> --artifact --output /tmp/analysis.png
python -m pytest -q -o addopts='' tests
RUN_ANALYSIS_DOCKER=1 python -m pytest -q -o addopts='' tests/test_analysis_docker.py
cd frontend
npm ci
npm run build
```

Docker 集成测试默认跳过；显式启用后也会在 daemon/镜像缺失时说明跳过原因。固定工具覆盖描述统计、两期对比、每日趋势、单维贡献、相关与历史 z-score，不支持预测或因果推断。超限明细任务应改用 SQL 聚合，当前不做分页明细拼接。保留期按会话最新分析时间判断，在分析服务启动时清理；删除会话也会清理对应产物。
