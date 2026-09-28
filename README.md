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
