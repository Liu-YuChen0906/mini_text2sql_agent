# Mini OpenChatBI Agent

独立的本地数据问答项目：主 Agent 管理多轮会话，Text2SQL 子图负责数据库查询，提供命令行和网页入口。

## 目录

```text
mini/agents/     主 Agent 图和 Text2SQL 子图
mini/query/      Catalog、选表、SQL 上下文、生成、执行与评分
mini/runtime/    模型与检索资源、成功查询历史
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

交互与 SQL 正确性排查、原项目对照及验证边界见 [改进说明](docs/sql-quality-improvements.md)。
