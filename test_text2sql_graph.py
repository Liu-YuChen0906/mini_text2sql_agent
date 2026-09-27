"""LangGraph Text2SQL 分支与检查点恢复的离线测试。"""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import text2sql_graph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from schema_linking import LinkResult, QuestionInfo, SelectedTable, extract_question_for_graph
from sql_quality import QualityScore, score_sql


def _success():
    """用途：构造固定的一行成功查询结果。

    参数输入：无。输出：符合执行器格式的成功结果字典。
    """
    return {"status": "success", "columns": ["id"], "rows": [[1]], "truncated": False, "error": None}


def _error(message):
    """用途：构造带指定错误文本的失败查询结果。

    参数输入：message 为模拟的 SQLite 错误；输出：失败结果字典。
    """
    return {"status": "error", "columns": [], "rows": [], "truncated": False, "error": message}


def _make_graph(tmp_path, monkeypatch, *, extracts=None, sqls=None, results=None, scores=None):
    """用途：用预设响应替换外部依赖，构建可离线测试的状态图。

    参数输入：tmp_path 放检查点；monkeypatch 替换模型和数据库调用；
        extracts、sqls、results、scores 分别提供各阶段依次消费的响应。
    输出：(已编译图、SQLite 连接、调用记录)；调用方负责关闭连接。
    """
    extracts = list(extracts or [(QuestionInfo("count orders", ["order"], [], ["count"]), None)])
    sqls = list(sqls or ["SELECT 1 AS id"])
    results = list(results or [_success()])
    scores = list(scores or [QualityScore(0.9, ["matches intent"])])
    calls = {"extract": [], "link": [], "context": [], "generated": [], "revised": [], "executed": []}

    def extract(llm, question):
        """用途：模拟问题提取并记录输入。

        参数输入：llm 为占位模型，question 为问题文本；输出：下一条预设提取结果。
        """
        calls["extract"].append(question)
        return extracts.pop(0)

    class FakeLinker:
        """类用途：替代真实 SchemaLinker，固定返回 Orders 表的选表结果。"""

        def __init__(self, *args):
            """用途：接受生产构造参数但不初始化外部资源。

            参数输入：args 为目录、索引、模型和数据库路径；输出：None。
            """
            pass

        def link_from_info(self, info):
            """用途：记录已提取的问题信息并返回固定选表结果。

            参数输入：info 为 QuestionInfo；输出：仅含 Orders 的 LinkResult。
            """
            calls["link"].append(info)
            return LinkResult(info.rewrite_question, [SelectedTable("Orders", ["order_id"])], {"Orders": {"order_id"}})

    def generate(question, schema, llm):
        """用途：模拟 SQL 生成并记录问题。

        参数输入：question 为问题，schema 和 llm 为占位依赖；输出：下一条预设 SQL。
        """
        calls["generated"].append(question)
        return sqls.pop(0)

    def context(link, catalog, store):
        """记录 SQL 上下文是否按新的选表结果重建。"""
        calls["context"].append(link)
        return "Table: Orders"

    def revise(question, schema, old, feedback, llm):
        """记录人工反馈，并返回下一条模拟模型生成的 SQL。"""
        calls["revised"].append((question, schema, old, feedback))
        return generate(question, schema, llm)

    def execute(sql, path):
        """用途：模拟 SQL 执行并记录被执行的语句。

        参数输入：sql 为查询，path 为占位数据库路径；输出：下一条预设结果。
        """
        calls["executed"].append(sql)
        return results.pop(0)

    def score(*args):
        """用途：模拟质量评分或评分异常。

        参数输入：args 为评分调用参数；输出：下一条 QualityScore，或抛出预设异常。
        """
        value = scores.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(text2sql_graph, "extract_question_for_graph", extract)
    monkeypatch.setattr(text2sql_graph, "SchemaLinker", FakeLinker)
    monkeypatch.setattr(text2sql_graph, "build_sql_context", context)
    monkeypatch.setattr(text2sql_graph, "generate_sql", generate)
    monkeypatch.setattr(text2sql_graph, "regenerate_sql", lambda question, schema, old, error, llm: generate(question, schema, llm))
    monkeypatch.setattr(text2sql_graph, "revise_sql", revise)
    monkeypatch.setattr(text2sql_graph, "execute_readonly_sql", execute)
    monkeypatch.setattr(text2sql_graph, "score_sql", score)
    resources = SimpleNamespace(catalog=object(), indexes=SimpleNamespace(text2sql=object()), llm=object(), database_path=Path("unused"))
    connection = sqlite3.connect(tmp_path / "checkpoints.sqlite", check_same_thread=False)
    return text2sql_graph.build_text2sql_graph(resources, SqliteSaver(connection)), connection, calls


def _start(graph, thread="test"):
    """用途：用固定问题和初始状态启动一次图运行。

    参数输入：graph 为已编译图，thread 为检查点任务 ID；输出：图返回的状态。
    """
    return graph.invoke(text2sql_graph.new_text2sql_turn("How many orders?"), config={"configurable": {"thread_id": thread}})


def _resume(graph, value, thread="test"):
    """用途：向指定任务的暂停节点提供恢复值。

    参数输入：graph 为图，value 为回答或审核决定，thread 为任务 ID；输出：新状态。
    """
    return graph.invoke(Command(resume=value), config={"configurable": {"thread_id": thread}})


def test_normal_flow_extracts_only_once(tmp_path, monkeypatch):
    """用途：验证正常路径只提取和选表各一次。

    参数输入：pytest 的临时目录及替换工具；输出：None，以状态和调用次数断言。
    """
    graph, connection, calls = _make_graph(tmp_path, monkeypatch)
    try:
        state = _start(graph)
        assert state["status"] == "success"
        assert state["sql"] == "SELECT 1 AS id"
        assert len(calls["extract"]) == len(calls["link"]) == 1
        assert "successful_turns" not in state and "last_successful_question" not in state
    finally:
        connection.close()


def test_each_text2sql_call_resets_turn_state_and_uses_supplied_context(tmp_path, monkeypatch):
    """子图只使用主图传入的上下文，各轮修复额度从零开始。"""
    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch,
        extracts=[
            (QuestionInfo("count orders", [], [], []), None),
            (QuestionInfo("monthly order count", [], [], []), None),
        ],
        sqls=["SELECT bad", "SELECT 1 AS count", "SELECT 2 AS monthly"],
        results=[_error("no such column: bad"), _success(), _success()],
        scores=[QualityScore(0.9, []), QualityScore(0.9, [])],
    )
    config = {"configurable": {"thread_id": "isolated"}}
    try:
        first = graph.invoke(text2sql_graph.new_text2sql_turn("How many orders?"), config=config)
        assert first["retry_count"] == 1
        second = graph.invoke(
            text2sql_graph.new_text2sql_turn("What about monthly?", context="第 1 轮完整问题：count orders"),
            config=config,
        )
        assert second["status"] == "success" and second["retry_count"] == 0
        assert second["errors"] == [] and second["sql"] == "SELECT 2 AS monthly"
        assert "第 1 轮完整问题：count orders" in calls["extract"][1]
        assert "successful_turns" not in second and "last_successful_question" not in second
    finally:
        connection.close()


def test_child_does_not_infer_history_from_earlier_query(tmp_path, monkeypatch):
    """未由主图传入的旧问题不会成为下一轮提取上下文。"""
    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch,
        extracts=[
            (QuestionInfo("count orders", [], [], []), None),
            (QuestionInfo("count customers", [], [], []), None),
        ],
        sqls=["SELECT 1", "SELECT 2"],
        results=[_success(), _success()],
        scores=[QualityScore(0.9, []), QualityScore(0.9, [])],
    )
    config = {"configurable": {"thread_id": "independent"}}
    try:
        graph.invoke(text2sql_graph.new_text2sql_turn("Count orders"), config=config)
        graph.invoke(text2sql_graph.new_text2sql_turn("Count customers"), config=config)
        assert calls["extract"][1] == "Count customers"
    finally:
        connection.close()


def test_clarification_persists_and_resumes_after_reopen(tmp_path, monkeypatch):
    """用途：验证澄清暂停可跨连接重开恢复，补充答案会重新参与提取。

    参数输入：pytest 的临时目录及替换工具；输出：None，以检查点和调用记录断言。
    """
    graph, connection, _ = _make_graph(tmp_path, monkeypatch, extracts=[(None, "Which time period?")])
    state = _start(graph)
    assert state["__interrupt__"][0].value["kind"] == "clarification"
    connection.close()

    graph, connection, calls = _make_graph(tmp_path, monkeypatch)
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": "test"}})
        assert snapshot.tasks[0].interrupts[0].value["question"] == "Which time period?"
        state = _resume(graph, "last month")
        assert state["status"] == "success"
        assert "last month" in calls["extract"][0]
    finally:
        connection.close()


def test_repairable_sql_error_retries_with_feedback(tmp_path, monkeypatch):
    """用途：验证可修复执行错误会带错误信息重新生成并再执行。

    参数输入：pytest 的临时目录及替换工具；输出：None，断言两次 SQL 和重试次数。
    """
    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch,
        sqls=["SELECT bad", "SELECT 1 AS id"],
        results=[_error("no such column: bad"), _success()],
    )
    try:
        state = _start(graph)
        assert state["status"] == "success"
        assert state["retry_count"] == 1
        assert calls["executed"] == ["SELECT bad", "SELECT 1 AS id"]
        assert state["errors"][0]["error"] == "no such column: bad"
    finally:
        connection.close()


def test_retry_limit_and_timeout_end(tmp_path, monkeypatch):
    """用途：验证语法错误最多修复三次，超时则直接结束。

    参数输入：pytest 的临时目录及替换工具；输出：None，断言最终状态和调用次数。
    """
    graph, connection, _ = _make_graph(
        tmp_path, monkeypatch,
        sqls=["SELECT bad"] * 4,
        results=[_error("syntax error near bad")] * 4,
    )
    try:
        state = _start(graph)
        assert state["status"] == "error"
        assert state["retry_count"] == 3
        assert len(state["errors"]) == 4
    finally:
        connection.close()

    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch,
        results=[{"status": "timeout", "columns": [], "rows": [], "truncated": False, "error": "timeout"}],
    )
    try:
        state = _start(graph, thread="timeout")
        assert state["status"] == "timeout"
        assert len(calls["executed"]) == 1
    finally:
        connection.close()


def test_low_confidence_approve_edit_and_reject(tmp_path, monkeypatch):
    """用途：覆盖低分审核的批准、修改和拒绝三条路径。

    参数输入：pytest 的临时目录及替换工具；输出：None，断言恢复后的状态和 SQL。
    """
    graph, connection, _ = _make_graph(tmp_path, monkeypatch, scores=[QualityScore(0.3, ["uncertain"])])
    try:
        state = _start(graph, "approve")
        assert state["__interrupt__"][0].value["kind"] == "sql_review"
        approved = _resume(graph, {"decision": "approve"}, "approve")
        assert approved["status"] == "success"
        assert "successful_turns" not in approved
    finally:
        connection.close()

    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch, sqls=["SELECT 1 AS id", "SELECT 2 AS id"], results=[_success(), _success()],
        scores=[QualityScore(0.3, []), QualityScore(0.9, [])],
    )
    try:
        _start(graph, "edit")
        state = _resume(graph, {"decision": "edit_sql", "feedback": "Count distinct orders"}, "edit")
        assert state["status"] == "success"
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
        assert calls["revised"] == [("count orders", "Table: Orders", "SELECT 1 AS id", "Count distinct orders")]
        assert "human_edit_count" not in state
        assert state["retry_count"] == 0
    finally:
        connection.close()

    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch,
        sqls=["SELECT 1 AS id", "SELECT 2 AS id"],
        results=[_success(), _success()],
        scores=[QualityScore(0.3, []), QualityScore(0.9, [])],
    )
    try:
        _start(graph, "reject")
        state = _resume(graph, {"decision": "reject"}, "reject")
        assert state["status"] == "success"
        assert state["retry_count"] == 1
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
    finally:
        connection.close()


def test_schema_feedback_reselects_tables_before_generating_sql(tmp_path, monkeypatch):
    """选表反馈重新经过提取、选表和上下文，原反馈不作为 SQL 执行。"""
    graph, connection, calls = _make_graph(
        tmp_path,
        monkeypatch,
        extracts=[
            (QuestionInfo("count orders", ["order"], [], ["count"]), None),
            (QuestionInfo("count invoices", ["invoice"], [], ["count"]), None),
        ],
        sqls=["SELECT 1 AS id", "SELECT 2 AS id"],
        results=[_success(), _success()],
        scores=[QualityScore(0.3, []), QualityScore(0.9, [])],
    )
    try:
        _start(graph)
        state = _resume(graph, {"decision": "edit_schema", "feedback": "Use invoices instead of orders"})
        assert state["status"] == "success"
        assert "Use invoices instead of orders" in calls["extract"][1]
        assert [info.rewrite_question for info in calls["link"]] == ["count orders", "count invoices"]
        assert len(calls["context"]) == 2
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
        assert "human_edit_count" not in state
        assert state["retry_count"] == 0
    finally:
        connection.close()


def test_sql_feedback_resumes_from_review_after_reopening_checkpoint(tmp_path, monkeypatch):
    """关闭并重开检查点后，人工自然语言反馈仍可触发模型重写。"""
    graph, connection, _ = _make_graph(tmp_path, monkeypatch, scores=[QualityScore(0.3, [])])
    state = _start(graph, "resume_edit")
    assert state["__interrupt__"][0].value["kind"] == "sql_review"
    connection.close()

    graph, connection, calls = _make_graph(
        tmp_path,
        monkeypatch,
        sqls=["SELECT 2 AS id"],
        results=[_success()],
        scores=[QualityScore(0.9, [])],
    )
    try:
        state = _resume(graph, {"decision": "edit_sql", "feedback": "Use a distinct count"}, "resume_edit")
        assert state["status"] == "success"
        assert calls["revised"][0][-1] == "Use a distinct count"
        assert calls["executed"] == ["SELECT 2 AS id"]
    finally:
        connection.close()


def test_review_rejects_empty_or_sql_only_feedback(tmp_path, monkeypatch):
    """审核节点要求自然语言 feedback，不接受旧的 sql 载荷直接执行。"""
    for thread, payload in (
        ("empty", {"decision": "edit_sql", "feedback": "  "}),
        ("sql_only", {"decision": "edit_sql", "sql": "SELECT 2 AS id"}),
    ):
        graph, connection, calls = _make_graph(tmp_path, monkeypatch, scores=[QualityScore(0.3, [])])
        try:
            _start(graph, thread)
            state = _resume(graph, payload, thread)
            assert state["status"] == "error"
            assert calls["executed"] == ["SELECT 1 AS id"]
        finally:
            connection.close()


def test_unlimited_human_edits_and_exit(tmp_path, monkeypatch):
    """人工修改超过三次仍可继续；E 会让本轮返回会话结束标记。"""
    graph, connection, calls = _make_graph(
        tmp_path,
        monkeypatch,
        extracts=[
            (QuestionInfo("count orders", ["order"], [], ["count"]), None),
            (QuestionInfo("count orders with corrected tables", ["order"], [], ["count"]), None),
        ],
        sqls=[f"SELECT {number} AS id" for number in range(1, 6)],
        results=[_success()] * 5,
        scores=[QualityScore(0.3, [])] * 5,
    )
    try:
        state = _start(graph, "limit")
        for decision in ("edit_sql", "edit_schema", "edit_sql"):
            state = _resume(graph, {"decision": decision, "feedback": "Change the calculation"}, "limit")
        assert state["__interrupt__"][0].value["options"] == ["approve", "edit_sql", "edit_schema", "reject", "exit"]
        state = _resume(graph, {"decision": "edit_sql", "feedback": "Another change"}, "limit")
        assert state["__interrupt__"][0].value["kind"] == "sql_review"
        assert len(calls["executed"]) == 5
    finally:
        connection.close()

    graph, connection, calls = _make_graph(tmp_path, monkeypatch, scores=[QualityScore(0.3, [])])
    try:
        _start(graph, "exit")
        state = _resume(graph, {"decision": "exit"}, "exit")
        assert state["status"] == "cancelled"
        assert state["session_closed"] is True
        assert calls["executed"] == ["SELECT 1 AS id"]
        assert len(calls["generated"]) == 1
    finally:
        connection.close()


def test_score_failure_requires_review(tmp_path, monkeypatch):
    """用途：验证评分异常会保留原因，并进入人工审核。

    参数输入：pytest 的临时目录及替换工具；输出：None，以暂停内容断言。
    """
    graph, connection, _ = _make_graph(tmp_path, monkeypatch, scores=[ValueError("bad score")])
    try:
        state = _start(graph)
        assert state["__interrupt__"][0].value["confidence"] is None
        assert "评分不可用" in state["__interrupt__"][0].value["reasons"][0]
    finally:
        connection.close()


def test_graph_extraction_and_score_validate_model_json():
    """用途：验证提取和评分模块会校验模型 JSON 的必需字段及数值范围。

    参数输入：无，使用本地假模型；输出：None，以返回值和异常断言。
    """
    class FakeLLM:
        """类用途：按顺序返回预设 JSON，以模拟聊天模型。"""

        def __init__(self, responses):
            """用途：保存待返回的模型响应列表。

            参数输入：responses 为可迭代 JSON 对象；输出：None。
            """
            self.responses = list(responses)

        def invoke(self, messages):
            """用途：返回下一条序列化 JSON 模型响应。

            参数输入：messages 为占位提示消息；输出：含 content 的模拟响应。
            """
            return SimpleNamespace(content=json.dumps(self.responses.pop(0)))

    llm = FakeLLM([
        {"needs_clarification": True, "clarification_question": "Which period?"},
        {"needs_clarification": False, "rewrite_question": "Count orders", "keywords": [], "dimensions": [], "metrics": ["order count"]},
    ])
    assert extract_question_for_graph(llm, "orders?") == (None, "Which period?")
    info, question = extract_question_for_graph(llm, "count orders")
    assert question is None
    assert info.rewrite_question == "Count orders"

    llm = FakeLLM([{"score": 0.2, "reasons": ["wrong metric"]}, {"score": 2, "reasons": []}])
    assert score_sql(llm, "count orders", "SELECT 1", "Orders", _success()).score == 0.2
    with pytest.raises(ValueError, match="0 到 1"):
        score_sql(llm, "count orders", "SELECT 1", "Orders", _success())
