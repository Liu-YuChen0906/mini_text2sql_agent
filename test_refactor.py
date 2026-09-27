"""Mini 主链路的离线回归测试，不连接真实模型或在线向量服务。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from catalog import FileCatalog
from execute_sql import execute_readonly_sql
from generate_sql import generate_sql, revise_sql
from presentation import render_result_table
from resources import IndexStores, _check_chroma_migrations, initialize_chroma
from schema_linking import SchemaLinker
from sql_context import build_sql_context

PROJECT_DIR = Path(__file__).resolve().parent


class FakeLLM:
    """类用途：按顺序返回预设的模型 JSON 响应。

    支持功能：记录传入的系统和用户提示词，供测试核对提示内容及重试次数。
    """

    def __init__(self, responses):
        """用途：保存后续调用需要依次返回的响应。

        参数输入：responses（list[dict]）是按调用顺序排列的 JSON 对象列表。
        输出：None；初始化响应队列和模型调用记录。
        """
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        """用途：模拟一次聊天模型调用并记录实际提示词。

        参数输入：messages（list）含系统消息与用户消息，各自有 content 属性。
        输出：SimpleNamespace，其 content（str）为下一份响应的 JSON 文本。
        """
        self.calls.append((messages[0].content, messages[1].content))
        return SimpleNamespace(content=json.dumps(self.responses.pop(0)))


class FakeStore:
    """类用途：模拟字段检索与示例检索所需的最小接口。

    支持功能：按配置返回字段或示例文档，记录检索参数，也可模拟检索异常。
    """

    def __init__(self, docs=(), *, fail=False):
        """用途：设置预期检索结果或失败模式。

        参数输入：docs（tuple[str, ...] | list[str]）是示例问题文本集合；
            fail（bool）为 True 时检索函数抛 RuntimeError。
        输出：None；保存测试用状态。
        """
        self.docs = docs
        self.fail = fail
        self.calls = []

    def similarity_search_with_score(self, query, k):
        """用途：模拟字段索引的带距离相似度检索。

        参数输入：query（str）是检索文本；k（int）是最大返回数。
        输出：list[tuple]，两份含 column_name 元数据的文档及其距离。
        """
        self.calls.append(("similarity", query, k))
        if self.fail:
            raise RuntimeError("离线模拟检索失败")
        return [
            (SimpleNamespace(metadata={"column_name": "customer_name"}), 0.2),
            (SimpleNamespace(metadata={"column_name": "order_status"}), 0.3),
        ]

    def max_marginal_relevance_search(self, query, k, fetch_k):
        """用途：模拟示例索引的多样性检索。

        参数输入：query（str）是检索文本；k、fetch_k（int）是检索数量参数。
        输出：list[SimpleNamespace]，每项以 page_content 保存示例问题文本。
        """
        self.calls.append(("mmr", query, k, fetch_k))
        if self.fail:
            raise RuntimeError("离线模拟检索失败")
        return [SimpleNamespace(page_content=doc) for doc in self.docs]


def make_linker(responses, *, fail=False):
    """用途：构造使用真实目录和数据库、假模型与假索引的选表器。

    参数输入：responses（list[dict]）是模型响应序列；fail（bool）控制检索异常。
    输出：tuple[SchemaLinker, FakeLLM, IndexStores]，供测试调用及检查记录。
    """
    catalog = FileCatalog(PROJECT_DIR / "catalog_data")
    llm = FakeLLM(responses)
    indexes = IndexStores(
        FakeStore(fail=fail),
        FakeStore(["Show order status and customer information"], fail=fail),
        FakeStore(["Find all pending orders with customer information"], fail=fail),
    )
    linker = SchemaLinker(catalog, indexes, llm, PROJECT_DIR / "data" / "tracking_orders.sqlite")
    return linker, llm, indexes


def question_response():
    """用途：提供固定的正常问题提取响应。

    参数输入：无。
    输出：dict[str, object]，含改写问题与两个关键词及空维度、空指标。
    """
    return {
        "rewrite_question": "Show customer name and order status",
        "keywords": ["customer_name", "order_status"],
        "dimensions": [],
        "metrics": [],
    }


def selection_response():
    """用途：提供固定的有效选表响应。

    参数输入：无。
    输出：dict[str, object]，含 Customers 和 Orders 的表名与字段列表。
    """
    return {
        "tables": [
            {"table": "Customers", "columns": ["customer_name", "customer_id"]},
            {"table": "Orders", "columns": ["order_status", "customer_id"]},
        ]
    }


def test_link_and_context_match_before_refactor():
    """用途：核对正常路径与重构前的固定输出逐字一致。

    参数输入：无；使用固定模型响应、真实目录及假检索结果。
    输出：None；断言选表、模型提示、上下文哈希和检索参数。
    """
    linker, llm, indexes = make_linker([question_response(), selection_response()])
    linked = linker.link("Show customer name and order status")
    context = build_sql_context(linked, linker.catalog, indexes.text2sql)

    def digest(value):
        """用途：计算用于逐字对照的文本哈希。

        参数输入：value（str）是模型提示或 SQL 上下文文本。
        输出：str，文本 UTF-8 编码后的 SHA-256 十六进制摘要。
        """
        return hashlib.sha256(value.encode()).hexdigest()

    assert [(item.table, item.columns) for item in linked.selected] == [
        ("Customers", ["customer_name", "customer_id"]),
        ("Orders", ["order_status", "customer_id"]),
    ]
    assert [digest(system + "\x00" + user) for system, user in llm.calls] == [
        "33997bb922a5d7c88430e90705d0e913501a4b4e4d659767ae6c659022557891",
        "2ddd587c20e4bdf6f8d2c28733aa7153031d014607b68c241f54d83253aca71a",
    ]
    assert digest(context) == "7c71957113719e4c4af4045ce412eb92ac5ada9d8bb3995c47abcd5b94b28749"
    assert indexes.columns.calls == [("similarity", "customer_name order_status", 12)]
    assert indexes.table_selection_example.calls == [("mmr", "customer_name order_status", 5, 20)]
    assert indexes.text2sql.calls == [("mmr", "Show customer name and order status", 5, 20)]


def test_no_candidate_table(monkeypatch):
    """用途：确认没有候选表时仍在模型选表之前报错。

    参数输入：monkeypatch（pytest fixture）替换字段匹配结果为空间外字段。
    输出：None；断言原有异常文本及仅发生一次模型调用。
    """
    import schema_linking

    linker, llm, _ = make_linker([question_response()])
    monkeypatch.setattr(schema_linking, "_related_columns", lambda *_: {"not_a_real_column"})
    with pytest.raises(ValueError, match="Catalog 中没有与数据库一致的候选表"):
        linker.link("Show customer name and order status")
    assert len(llm.calls) == 1


def test_invalid_selection_retries_once():
    """用途：确认模型选到候选范围外的表后按原规则重试一次。

    参数输入：无；先返回无效表，再返回有效表。
    输出：None；断言调用次数、纠错提示和最后选中的表。
    """
    linker, llm, _ = make_linker(
        [
            question_response(),
            {"tables": [{"table": "Unknown", "columns": ["id"]}]},
            selection_response(),
        ]
    )
    linked = linker.link("Show customer name and order status")
    assert len(llm.calls) == 3
    assert "上次输出无效：模型选了候选表之外的表" in llm.calls[2][1]
    assert [item.table for item in linked.selected] == ["Customers", "Orders"]


def test_link_from_info_does_not_extract_again(monkeypatch):
    """用途：确认图的选表节点复用已提取信息，不再次调用问题提取。

    参数输入：monkeypatch（pytest fixture）把重复提取替换为测试失败。
    输出：None；断言选表成功且模型只调用一次。
    """
    import schema_linking

    linker, llm, _ = make_linker([selection_response()])
    monkeypatch.setattr(schema_linking, "extract_question", lambda *_: pytest.fail("duplicate extraction"))
    info = schema_linking.QuestionInfo(**question_response())
    linked = linker.link_from_info(info)
    assert [item.table for item in linked.selected] == ["Customers", "Orders"]
    assert len(llm.calls) == 1


def test_retrieval_failure_keeps_text_match_and_table_context(capsys):
    """用途：确认三个索引失败时沿用原有文本匹配和空示例回退。

    参数输入：capsys（pytest fixture）用于读取错误提示。
    输出：None；断言仍能选表、生成表说明并打印检索失败提示。
    """
    linker, _, indexes = make_linker([question_response(), selection_response()], fail=True)
    linked = linker.link("Show customer name and order status")
    context = build_sql_context(linked, linker.catalog, indexes.text2sql)
    assert [item.table for item in linked.selected] == ["Customers", "Orders"]
    assert "Table: Customers" in context
    assert context.endswith("Relevant SQL examples:\n")
    errors = capsys.readouterr().err
    assert "字段向量检索不可用" in errors
    assert "选表示例检索不可用" in errors
    assert "SQL 示例检索不可用" in errors


def test_sqlite_executor_and_terminal_rendering(tmp_path):
    """用途：验证只读查询、行数截断、拒绝写入和终端表格格式。

    参数输入：tmp_path（pytest fixture）是隔离的临时目录。
    输出：None；通过断言校验 QueryResult 字段及展示文本。
    """
    database = tmp_path / "example.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        connection.executemany("INSERT INTO items VALUES (?, ?)", [(1, "甲"), (2, "乙")])
        connection.commit()

    result = execute_readonly_sql("SELECT id, name FROM items ORDER BY id", database, row_limit=1)
    assert result == {
        "status": "success",
        "columns": ["id", "name"],
        "rows": [[1, "甲"]],
        "truncated": True,
        "error": None,
    }
    rendered = render_result_table(result)
    assert all(value in rendered for value in ("id", "name", "甲", "仅显示前 1 行，还有更多数据。"))
    assert "\x1b[" not in rendered
    assert execute_readonly_sql("DELETE FROM items", database)["status"] == "rejected"
    assert execute_readonly_sql("SELECT * FROM items WHERE id = 9", database)["rows"] == []


def test_sqlite_timeout(tmp_path):
    """用途：验证进度回调仍把超时查询标记为 timeout。

    参数输入：tmp_path（pytest fixture）用于创建临时 SQLite 数据库。
    输出：None；断言耗时递归查询在零秒截止时间下被中断。
    """
    database = tmp_path / "timeout.sqlite"
    with closing(sqlite3.connect(database)):
        pass
    sql = "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT SUM(x) FROM n"
    assert execute_readonly_sql(sql, database, timeout_seconds=0.0)["status"] == "timeout"


def test_generate_sql_uses_injected_model():
    """用途：确认 SQL 生成只调用显式传入的模型，仍移除代码围栏。

    参数输入：无；使用 LangChain 本地 Runnable 模拟模型响应。
    输出：None；断言生成结果和模型收到的 Schema 与问题。
    """
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    seen = []

    def respond(messages):
        """用途：记录模型输入并返回固定 SQL 文本。

        参数输入：messages（ChatPromptValue）含系统和用户提示消息。
        输出：AIMessage，content 为带 Markdown 围栏的 SQL。
        """
        seen.append(messages.to_messages())
        return AIMessage(content="```sql\nSELECT 1\n```")

    assert generate_sql("问题", "Table: Orders", RunnableLambda(respond)) == "SELECT 1"
    assert "Schema:\nTable: Orders" in seen[0][0].content
    assert seen[0][1].content == "问题"


def test_revise_sql_passes_human_feedback_to_model():
    """人工修改走模型生成链，提示词包含旧 SQL 与自然语言反馈。"""
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    seen = []

    def respond(messages):
        seen.append(messages.to_messages())
        return AIMessage(content="SELECT COUNT(DISTINCT order_id) FROM Orders")

    sql = revise_sql("统计订单", "Table: Orders", "SELECT COUNT(*) FROM Orders", "按订单去重", RunnableLambda(respond))
    assert sql == "SELECT COUNT(DISTINCT order_id) FROM Orders"
    assert "Table: Orders" in seen[0][0].content
    assert "SELECT COUNT(*) FROM Orders" in seen[0][1].content
    assert "按订单去重" in seen[0][1].content


def test_main_displays_graph_result(monkeypatch, capsys, tmp_path):
    """用途：确认命令行入口打印任务 ID、最终 SQL 和查询表格。

    参数输入：pytest 的替换工具、输出捕获工具和临时目录。
    输出：None；断言图收到初始问题及完整的终端文本。
    """
    import main
    from query_history import QueryHistory

    result = {"status": "success", "columns": ["id"], "rows": [[1]], "truncated": False, "error": None}

    class FakeGraph:
        """类用途：返回固定成功状态，隔离入口测试与真实图和模型。"""

        def stream(self, state, config, stream_mode, subgraphs):
            """用途：核对入口传入的问题与任务 ID，并返回固定结果。

            参数输入：state 为初始状态，config 为图运行配置；输出：成功状态字典。
            """
            assert state["current_question"] == "原始问题"
            assert config["configurable"]["thread_id"] == "fixed-id"
            assert stream_mode == "tasks" and subgraphs
            yield (), {"id": "execute-1", "name": "execute", "input": state}
            yield (), {"id": "execute-1", "name": "execute", "result": {"status": "success", "result": result}, "interrupts": []}

        def get_state(self, config):
            return SimpleNamespace(values={"messages": [], "turn_id": 1, "query_outcome":
                {"status": "success", "record_turn_id": 1}}, tasks=[])

    checkpoint = tmp_path / "checkpoints.sqlite"
    QueryHistory(checkpoint).save("fixed-id", {
        "turn_id": 1, "question": "原始问题", "rewrite_question": "完整问题",
        "sql": "SELECT 1 AS id", "columns": result["columns"],
        "rows": result["rows"], "truncated": False,
    })
    monkeypatch.setattr("sys.argv", ["main.py", "--checkpoint", str(checkpoint)])
    answers = iter(["原始问题"])
    def read(_):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr(main, "create_llm", lambda: object())
    monkeypatch.setattr(main, "load_resources", lambda **kwargs: object())
    monkeypatch.setattr(main, "build_agent_graph", lambda *args: FakeGraph())
    monkeypatch.setattr(main, "uuid4", lambda: SimpleNamespace(hex="fixed-id"))
    main.main()
    output = capsys.readouterr().out
    assert all(value in output for value in ("任务 ID：fixed-id", "执行过程", "执行 SQL", "SELECT 1 AS id", "查询结果", "返回 1 行"))
    assert "\x1b[" not in output


def test_main_resumes_pending_clarification(monkeypatch, capsys, tmp_path):
    """用途：确认入口用原任务 ID 读取澄清暂停，并把回答传回图。

    参数输入：pytest 的替换工具、输出捕获工具和临时目录。
    输出：None；断言恢复值和终端澄清提示。
    """
    import main

    class FakeGraph:
        """类用途：模拟带未处理澄清中断的检查点图。"""

        resumed = False

        def get_state(self, config):
            """用途：返回指定任务 ID 的待处理澄清快照。

            参数输入：config 为图运行配置；输出：带 interrupts 的模拟快照。
            """
            assert config["configurable"]["thread_id"] == "saved-id"
            if self.resumed:
                return SimpleNamespace(values={"messages": [], "query_outcome": {"status": "cancelled"}, "session_closed": True}, tasks=[])
            return SimpleNamespace(values={"messages": [], "current_question": "订单数量"}, tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"kind": "clarification", "question": "哪个月份？"})])])

        def stream(self, command, config, stream_mode, subgraphs):
            """用途：核对恢复命令和原任务 ID，并结束模拟查询。

            参数输入：command 为 Command.resume，config 为图配置；输出：结束状态。
            """
            assert command.resume == "上个月"
            assert config["configurable"]["thread_id"] == "saved-id"
            assert stream_mode == "tasks" and subgraphs
            self.resumed = True
            return iter(())

    monkeypatch.setattr("sys.argv", ["main.py", "--resume", "saved-id", "--checkpoint", str(tmp_path / "checkpoints.sqlite")])
    monkeypatch.setattr("builtins.input", lambda _: "上个月")
    monkeypatch.setattr(main, "create_llm", lambda: object())
    monkeypatch.setattr(main, "load_resources", lambda **kwargs: object())
    monkeypatch.setattr(main, "build_agent_graph", lambda *args: FakeGraph())
    main.main()
    output = capsys.readouterr().out
    assert "正在恢复任务：saved-id" in output
    assert "需要澄清：哪个月份？" in output


@pytest.mark.parametrize(
    ("letter", "feedback", "expected"),
    [
        ("a", None, {"decision": "approve"}),
        ("B", "按客户去重", {"decision": "edit_sql", "feedback": "按客户去重"}),
        ("c", "改用发票表", {"decision": "edit_schema", "feedback": "改用发票表"}),
        ("D", None, {"decision": "reject"}),
        ("e", None, {"decision": "exit"}),
    ],
)
def test_review_menu_uses_letters_and_natural_language(monkeypatch, capsys, letter, feedback, expected):
    """审核菜单接受大小写 A～E，修改选项只返回自然语言反馈。"""
    import main

    answers = iter([letter] + ([feedback] if feedback is not None else []))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    result = main._request_feedback({"kind": "sql_review", "sql": "SELECT 1", "options": [
        "approve", "edit_sql", "edit_schema", "reject", "exit",
    ]})

    assert result == expected
    output = capsys.readouterr().out
    assert "A. 通过" in output
    assert "B. 重写 SQL" in output
    assert "C. 重新选表" in output
    assert "D. 拒绝" in output
    assert "E. 退出" in output
    assert "请输入修改后的完整 SQL" not in output


def test_review_menu_hides_edits_at_limit_and_reprompts_empty_feedback(monkeypatch, capsys):
    """菜单遵守图的可选项，并要求修改说明非空。"""
    import main

    answers = iter(["b", "E"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main._request_feedback({"kind": "sql_review", "options": ["approve", "reject", "exit"]}) == {
        "decision": "exit"
    }
    output = capsys.readouterr().out
    assert "B. 重写 SQL" not in output
    assert "请输入菜单中显示的选项字母。" in output

    answers = iter(["B", "  ", "按订单去重"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main._request_feedback({"kind": "sql_review"}) == {
        "decision": "edit_sql", "feedback": "按订单去重"
    }
    assert "修改说明不能为空。" in capsys.readouterr().out


def test_chroma_collections_keep_names_and_existing_index_rule(monkeypatch, tmp_path):
    """用途：确认资源模块沿用三个集合名称及原有的索引一致性规则。

    参数输入：monkeypatch（pytest fixture）替换 Chroma；tmp_path 提供索引路径。
    输出：None；断言带名称的集合可访问，文档不一致时仍抛 RuntimeError。
    """
    import resources

    catalog = FileCatalog(PROJECT_DIR / "catalog_data")
    expected = {
        "columns": [f"{column['column_name']}: {column['display_name']}" for column in catalog.get_column_list()],
        "table_selection_example": [question for question, _ in catalog.get_table_selection_examples()],
        "text2sql": [question for question, _, _ in catalog.get_sql_examples()],
    }

    class FakeChroma:
        """类用途：模拟已有 Chroma 集合中的文档文本。

        支持功能：记录集合名，并通过 get 返回预设文本，不访问文件或网络。
        """

        def __init__(self, collection_name, **kwargs):
            """用途：保存当前打开的集合名。

            参数输入：collection_name（str）是集合名称；kwargs（dict）是
                生产构造函数使用、在本测试中无需处理的配置。
            输出：None；初始化集合名属性。
            """
            self.collection_name = collection_name

        def get(self, include):
            """用途：返回已有集合的文档文本。

            参数输入：include（list[str]）指明要读取 documents。
            输出：dict[str, list[str]]，documents 键对应预设文本列表。
            """
            assert include == ["documents"]
            return {"documents": expected[self.collection_name]}

    monkeypatch.setattr(resources, "Chroma", FakeChroma)
    stores = initialize_chroma(catalog, object(), tmp_path)
    assert stores.columns.collection_name == "columns"
    assert stores.table_selection_example.collection_name == "table_selection_example"
    assert stores.text2sql.collection_name == "text2sql"

    expected["columns"] = []
    with pytest.raises(RuntimeError, match="Chroma 集合 columns 缺失或与 Catalog 不一致"):
        initialize_chroma(catalog, object(), tmp_path)


def test_load_resources_separates_catalog_and_chroma_and_reads_config_once(monkeypatch, tmp_path):
    """组装资源时复用同一份配置和 Catalog，并正确解析相对路径。"""
    import resources

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "catalog_store:\n  store_type: file_system\n  data_path: catalog_data\n"
        "vector_db_path: chroma_db\n"
        "embedding_model:\n  class: langchain_openai.OpenAIEmbeddings\n"
        "  params:\n    model: test-embedding\n"
        "default_llm:\n  class: langchain_deepseek.ChatDeepSeek\n"
        "  params:\n    api_key: test-key\n    model: test-chat\n",
        encoding="utf-8",
    )
    read_config = resources._read_config
    reads = []

    def tracked_read(path):
        reads.append(path)
        return read_config(path)

    catalog = object()
    indexes = object()
    calls = {}
    monkeypatch.setattr(resources, "_read_config", tracked_read)

    def fake_catalog(path):
        calls["catalog_path"] = path
        return catalog

    monkeypatch.setattr(resources, "FileCatalog", fake_catalog)
    monkeypatch.setattr(resources, "OpenAIEmbeddings", lambda **params: params)
    monkeypatch.setattr(resources, "ChatDeepSeek", lambda **params: params)

    def fake_initialize(given_catalog, embedding, path):
        calls["chroma"] = (given_catalog, embedding, path)
        return indexes

    monkeypatch.setattr(resources, "initialize_chroma", fake_initialize)
    result = resources.load_resources(config_path, tmp_path / "orders.sqlite")

    assert reads == [config_path]
    assert calls["catalog_path"] == tmp_path / "catalog_data"
    assert result.catalog is catalog
    assert calls["chroma"] == (catalog, {"model": "test-embedding"}, tmp_path / "chroma_db")
    assert result.indexes is indexes
    assert result.llm == {"api_key": "test-key", "model": "test-chat"}
    assert result.database_path == tmp_path / "orders.sqlite"


def test_create_llm_still_accepts_config_path(monkeypatch, tmp_path):
    """独立创建模型时仍能从传入路径读取并校验配置。"""
    import resources

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "default_llm:\n  class: langchain_deepseek.ChatDeepSeek\n"
        "  params:\n    api_key: test-key\n    model: test-chat\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(resources, "ChatDeepSeek", lambda **params: params)

    assert resources.create_llm(config_path) == {"api_key": "test-key", "model": "test-chat"}


def test_chroma_version_mismatch_reports_clear_error(tmp_path):
    """用途：验证预检认可当前运行库迁移，并拒绝未来版本的迁移。

    参数输入：tmp_path（pytest fixture）用于创建独立的迁移元数据数据库。
    输出：None；断言错误指出具体不受支持的迁移版本。
    """
    import chromadb
    import resources
    from chromadb.config import Settings

    current = tmp_path / "current"
    chromadb.PersistentClient(path=str(current), settings=Settings(anonymized_telemetry=False))
    _check_chroma_migrations(current)

    available = max(version for directory, version in resources._runtime_chroma_migrations() if directory == "sysdb")
    future = tmp_path / "future"
    future.mkdir()
    database = future / "chroma.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE migrations (dir TEXT, version INTEGER, hash TEXT)")
        connection.execute("INSERT INTO migrations VALUES ('sysdb', ?, 'future')", (available + 1,))
        connection.commit()

    with pytest.raises(RuntimeError, match=f"sysdb 存在不受支持的第 {available + 1} 次迁移"):
        _check_chroma_migrations(future)
