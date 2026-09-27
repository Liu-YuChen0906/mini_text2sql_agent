"""离线验证主 Agent、成功查询存档及子图人工审核的暂停恢复。"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_graph import build_agent_graph, create_text2sql_tool, new_agent_turn
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from query_history import QueryHistory
from schema_linking import QuestionInfo
from sql_quality import QualityScore
from test_text2sql_graph import _error, _make_graph, _success


def _tool_call(turn_id, history_ids=None):
    return AIMessage(
        content="",
        tool_calls=[{"name": "run_text2sql", "args": {"history_turn_ids": history_ids or []}, "id": f"call-{turn_id}"}],
    )


class FakeRoutingLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def bind_tools(self, tools, **kwargs):
        assert tools[0].name == "run_text2sql"
        assert set(tools[0].tool_call_schema.model_json_schema()["properties"]) == {"history_turn_ids"}
        return self

    def invoke(self, messages):
        self.prompts.append(messages)
        return self.responses.pop(0)


def _parent(tmp_path, monkeypatch, routing, *, checkpoint_name="checkpoints.sqlite", **child_options):
    # 复用子图测试中的离线替身，再构建不带独立检查点的子图；
    # 这样人工暂停点才能经由主图检查点保存并恢复。
    unused, child_connection, calls = _make_graph(tmp_path, monkeypatch, **child_options)
    del unused
    child_connection.close()
    path = tmp_path / checkpoint_name
    connection = sqlite3.connect(path, check_same_thread=False)
    resources = SimpleNamespace(
        catalog=object(), indexes=SimpleNamespace(text2sql=object()),
        llm=routing, database_path=Path("unused"),
    )
    history = QueryHistory(path)
    graph = build_agent_graph(routing, SqliteSaver(connection), history, lambda: resources)
    return graph, connection, history, calls


def test_text2sql_tool_is_callable_and_hides_trusted_arguments(tmp_path):
    history = QueryHistory(tmp_path / "archive.sqlite")
    seen = []

    class FakeChild:
        def invoke(self, inputs, config):
            seen.append((inputs, config))
            return {
                "status": "success", "rewrite_question": "完整问题", "sql": "SELECT 1",
                "result": {"columns": ["id"], "rows": [[1]], "truncated": False},
            }

    query_tool = create_text2sql_tool(lambda: FakeChild(), history)
    assert set(query_tool.tool_call_schema.model_json_schema()["properties"]) == {"history_turn_ids"}
    result = query_tool.invoke(
        {"history_turn_ids": [], "question": "原始问题", "turn_id": 3},
        config={"configurable": {"thread_id": "session"}},
    )
    assert result["sql"] == "SELECT 1"
    assert seen[0][0]["question"] == "原始问题"
    assert seen[0][1]["configurable"]["thread_id"] == "session"
    assert history.get("session", 3)["rows"] == [[1]]


def test_direct_chat_and_query_have_distinct_outputs(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([AIMessage(content="你好"), _tool_call(2)])
    graph, connection, history, calls = _parent(tmp_path, monkeypatch, routing)
    config = {"configurable": {"thread_id": "session"}}
    try:
        chat = graph.invoke(new_agent_turn("你好", 1), config=config)
        assert chat["reply"] == "你好" and chat["query_outcome"] is None
        assert history.list("session") == []
        queried = graph.invoke(new_agent_turn("Count orders", 2), config=config)
        assert queried["query_outcome"]["status"] == "success"
        assert queried["query_outcome"]["record_turn_id"] == 2
        assert "rows" not in repr(queried["query_outcome"])
        assert calls["executed"] == ["SELECT 1 AS id"]
        assert history.get("session", 2)["rows"] == [[1]]
    finally:
        connection.close()


def test_query_resources_load_only_for_data_question(tmp_path):
    path = tmp_path / "checkpoints.sqlite"
    connection = sqlite3.connect(path, check_same_thread=False)
    routing = FakeRoutingLLM([AIMessage(content="你好"), _tool_call(2)])
    calls = []

    def unavailable_resources():
        calls.append("load")
        raise RuntimeError("Chroma 索引版本不兼容")

    graph = build_agent_graph(routing, SqliteSaver(connection), QueryHistory(path), unavailable_resources)
    config = {"configurable": {"thread_id": "lazy"}}
    try:
        assert graph.invoke(new_agent_turn("你好", 1), config=config)["reply"] == "你好"
        assert calls == []
        with pytest.raises(RuntimeError, match="Chroma 索引版本不兼容"):
            graph.invoke(new_agent_turn("查订单", 2), config=config)
        assert calls == ["load"]
    finally:
        connection.close()


def test_sixth_turn_can_load_first_sql_and_returned_rows(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([_tool_call(number, [1] if number == 6 else []) for number in range(1, 7)])
    results = [{**_success(), "rows": [[number]], "truncated": number == 1} for number in range(1, 7)]
    graph, connection, history, calls = _parent(
        tmp_path, monkeypatch, routing,
        extracts=[(QuestionInfo(f"query {number}", [], [], []), None) for number in range(1, 7)],
        sqls=[f"SELECT {number}" for number in range(1, 7)],
        results=results,
        scores=[QualityScore(0.9, [])] * 6,
    )
    config = {"configurable": {"thread_id": "six"}}
    try:
        for number in range(1, 7):
            state = graph.invoke(new_agent_turn(f"Question {number}", number), config=config)
            assert state["query_outcome"]["status"] == "success"
        assert len(history.list("six")) == 6
        assert history.get("six", 1)["sql"] == "SELECT 1"
        assert history.get("six", 1)["truncated"] is True
        assert history.get("six", 1)["rows"] == [[1]]
        assert "第 1 轮已保存查询" in calls["extract"][5]
        assert "结果行=[[1]]；截断=True" in calls["extract"][5]
    finally:
        connection.close()


def test_review_interrupt_resumes_through_parent(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([_tool_call(1)])
    graph, connection, history, _ = _parent(
        tmp_path, monkeypatch, routing, scores=[QualityScore(0.2, ["uncertain"])],
    )
    config = {"configurable": {"thread_id": "review"}}
    try:
        first = graph.invoke(new_agent_turn("Count orders", 1), config=config)
        assert first["__interrupt__"][0].value["kind"] == "sql_review"
        assert graph.get_state(config).tasks[0].interrupts
        assert history.list("review") == []
        resumed = graph.invoke(Command(resume={"decision": "approve"}), config=config)
        assert resumed["query_outcome"]["status"] == "success"
        assert len(history.list("review")) == 1
        assert len(routing.prompts) == 1
    finally:
        connection.close()


def test_reopen_checkpoint_and_resume_nested_review(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([_tool_call(1)])
    graph, connection, history, calls = _parent(
        tmp_path, monkeypatch, routing, scores=[QualityScore(0.2, ["uncertain"])],
    )
    config = {"configurable": {"thread_id": "reopened"}}
    first = graph.invoke(new_agent_turn("Count orders", 1), config=config)
    assert first["__interrupt__"][0].value["kind"] == "sql_review"
    connection.close()

    reopened, reopened_connection, reopened_history, resumed_calls = _parent(
        tmp_path, monkeypatch, FakeRoutingLLM([]),
    )
    try:
        assert reopened.get_state(config).tasks[0].interrupts
        state = reopened.invoke(Command(resume={"decision": "approve"}), config=config)
        assert state["query_outcome"]["status"] == "success"
        assert reopened_history.get("reopened", 1)["sql"] == "SELECT 1 AS id"
        assert calls["executed"] == ["SELECT 1 AS id"]
        assert resumed_calls["executed"] == []
    finally:
        reopened_connection.close()


def test_reopen_checkpoint_between_turns_uses_saved_history(tmp_path, monkeypatch):
    first_graph, first_connection, _, first_calls = _parent(
        tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(1)]),
    )
    config = {"configurable": {"thread_id": "between"}}
    first = first_graph.invoke(new_agent_turn("Count orders", 1), config=config)
    assert first["query_outcome"]["record_turn_id"] == 1
    first_connection.close()

    reopened, reopened_connection, history, calls = _parent(
        tmp_path, monkeypatch, FakeRoutingLLM([_tool_call(2, [1])]),
        extracts=[(QuestionInfo("monthly count", [], [], []), None)],
        sqls=["SELECT 2"],
    )
    try:
        assert reopened.get_state(config).values["turn_id"] == 1
        second = reopened.invoke(new_agent_turn("按月呢？", 2), config=config)
        assert second["query_outcome"]["record_turn_id"] == 2
        assert history.get("between", 1)["rows"] == [[1]]
        assert history.get("between", 2)["sql"] == "SELECT 2"
        assert "第 1 轮已保存查询" in calls["extract"][0]
        assert first_calls["executed"] == ["SELECT 1 AS id"]
    finally:
        reopened_connection.close()


def test_failed_turn_not_archived_and_next_turn_resets_query_outcome(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([_tool_call(1), _tool_call(2)])
    graph, connection, history, calls = _parent(
        tmp_path, monkeypatch, routing,
        extracts=[(QuestionInfo("first", [], [], []), None), (QuestionInfo("second", [], [], []), None)],
        sqls=["SELECT 1", "SELECT 2"],
        results=[_error("database is locked"), _success()],
        scores=[QualityScore(0.9, [])],
    )
    config = {"configurable": {"thread_id": "failed"}}
    try:
        first = graph.invoke(new_agent_turn("first", 1), config=config)
        assert first["query_outcome"]["status"] == "error"
        assert history.list("failed") == []
        second = graph.invoke(new_agent_turn("second", 2), config=config)
        assert second["query_outcome"]["status"] == "success"
        assert second["query_outcome"]["retry_count"] == 0
        assert [record["turn_id"] for record in history.list("failed")] == [2]
        assert "第 1 轮" not in calls["extract"][1]
    finally:
        connection.close()


def test_review_exit_closes_parent_session(tmp_path, monkeypatch):
    routing = FakeRoutingLLM([_tool_call(1)])
    graph, connection, history, _ = _parent(
        tmp_path, monkeypatch, routing, scores=[QualityScore(0.2, ["uncertain"])],
    )
    config = {"configurable": {"thread_id": "exit"}}
    try:
        graph.invoke(new_agent_turn("Count orders", 1), config=config)
        state = graph.invoke(Command(resume={"decision": "exit"}), config=config)
        assert state["session_closed"] is True
        assert state["query_outcome"]["status"] == "cancelled"
        assert history.list("exit") == []
        assert graph.invoke(new_agent_turn("another", 2), config=config)["session_closed"] is True
        assert len(routing.prompts) == 1
    finally:
        connection.close()
