"""Offline branch and persistence tests for the LangGraph Text2SQL workflow."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import workflow
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from schema_linking import LinkResult, QuestionInfo, SelectedTable, extract_question_for_graph
from sql_quality import QualityScore, score_sql


def _success():
    return {"status": "success", "columns": ["id"], "rows": [[1]], "truncated": False, "error": None}


def _error(message):
    return {"status": "error", "columns": [], "rows": [], "truncated": False, "error": message}


def _make_graph(tmp_path, monkeypatch, *, extracts=None, sqls=None, results=None, scores=None):
    extracts = list(extracts or [(QuestionInfo("count orders", ["order"], [], ["count"]), None)])
    sqls = list(sqls or ["SELECT 1 AS id"])
    results = list(results or [_success()])
    scores = list(scores or [QualityScore(0.9, ["matches intent"])])
    calls = {"extract": [], "link": [], "generated": [], "executed": []}

    def extract(llm, question):
        calls["extract"].append(question)
        return extracts.pop(0)

    class FakeLinker:
        def __init__(self, *args):
            pass

        def link_from_info(self, info):
            calls["link"].append(info)
            return LinkResult(info.rewrite_question, [SelectedTable("Orders", ["order_id"])], {"Orders": {"order_id"}})

    def generate(question, schema, llm):
        calls["generated"].append(question)
        return sqls.pop(0)

    def execute(sql, path):
        calls["executed"].append(sql)
        return results.pop(0)

    def score(*args):
        value = scores.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(workflow, "extract_question_for_graph", extract)
    monkeypatch.setattr(workflow, "SchemaLinker", FakeLinker)
    monkeypatch.setattr(workflow, "build_sql_context", lambda *args: "Table: Orders")
    monkeypatch.setattr(workflow, "generate_sql", generate)
    monkeypatch.setattr(workflow, "regenerate_sql", lambda question, schema, old, error, llm: generate(question, schema, llm))
    monkeypatch.setattr(workflow, "execute_readonly_sql", execute)
    monkeypatch.setattr(workflow, "score_sql", score)
    resources = SimpleNamespace(catalog=object(), indexes=SimpleNamespace(text2sql=object()), llm=object(), database_path=Path("unused"))
    connection = sqlite3.connect(tmp_path / "checkpoints.sqlite", check_same_thread=False)
    return workflow.build_workflow(resources, SqliteSaver(connection)), connection, calls


def _start(graph, thread="test"):
    return graph.invoke(
        {"question": "How many orders?", "clarification_history": [], "errors": [], "retry_count": 0},
        config={"configurable": {"thread_id": thread}},
    )


def _resume(graph, value, thread="test"):
    return graph.invoke(Command(resume=value), config={"configurable": {"thread_id": thread}})


def test_normal_flow_extracts_only_once(tmp_path, monkeypatch):
    graph, connection, calls = _make_graph(tmp_path, monkeypatch)
    try:
        state = _start(graph)
        assert state["status"] == "success"
        assert state["sql"] == "SELECT 1 AS id"
        assert len(calls["extract"]) == len(calls["link"]) == 1
    finally:
        connection.close()


def test_clarification_persists_and_resumes_after_reopen(tmp_path, monkeypatch):
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
    graph, connection, _ = _make_graph(tmp_path, monkeypatch, scores=[QualityScore(0.3, ["uncertain"])])
    try:
        state = _start(graph, "approve")
        assert state["__interrupt__"][0].value["kind"] == "sql_review"
        assert _resume(graph, {"decision": "approve"}, "approve")["status"] == "success"
    finally:
        connection.close()

    graph, connection, calls = _make_graph(
        tmp_path, monkeypatch, results=[_success(), _success()],
        scores=[QualityScore(0.3, []), QualityScore(0.9, [])],
    )
    try:
        _start(graph, "edit")
        state = _resume(graph, {"decision": "edit", "sql": "SELECT 2 AS id"}, "edit")
        assert state["status"] == "success"
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
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


def test_score_failure_requires_review(tmp_path, monkeypatch):
    graph, connection, _ = _make_graph(tmp_path, monkeypatch, scores=[ValueError("bad score")])
    try:
        state = _start(graph)
        assert state["__interrupt__"][0].value["confidence"] is None
        assert "评分不可用" in state["__interrupt__"][0].value["reasons"][0]
    finally:
        connection.close()


def test_graph_extraction_and_score_validate_model_json():
    class FakeLLM:
        def __init__(self, responses):
            self.responses = list(responses)

        def invoke(self, messages):
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
