"""Offline web service and HTTP checks with fake models and temporary SQLite."""

from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from tests.test_agent_graph import FakeRoutingLLM, _tool_call
from tests.test_text2sql_graph import _make_graph, _success
from mini.query.schema_linking import QuestionInfo
from mini.query.sql_quality import QualityScore
from mini.web.api import create_app
from mini.web.service import ServiceError, WebSessionService


def service_for(tmp_path, monkeypatch, responses, **child_options):
    unused, connection, calls = _make_graph(tmp_path, monkeypatch, **child_options)
    del unused
    connection.close()
    routing = FakeRoutingLLM(responses)
    resources = SimpleNamespace(
        catalog=object(), indexes=SimpleNamespace(text2sql=object()),
        llm=routing, database_path=Path("unused"),
    )
    service = WebSessionService(tmp_path / "web.sqlite", llm=routing,
                                query_resources_factory=lambda: resources)
    return service, calls


def test_chat_sessions_and_idempotency(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, [AIMessage(content="你好"), AIMessage(content="另一个会话")])
    try:
        first = service.create_session()["id"]
        second = service.create_session()["id"]
        response = service.send_message(first, "你好", "req-1")
        assert response["title"] == "你好"
        assert [(item["kind"], item["payload"]["content"]) for item in response["messages"]] == [
            ("text", "你好"), ("text", "你好")]
        assert len(service.send_message(first, "你好", "req-1")["messages"]) == 2
        with pytest.raises(ServiceError, match="已用于另一项请求"):
            service.send_message(first, "不一样", "req-1")
        service.send_message(second, "第二个", "req-2")
        assert service.get_session(first)["turn_id"] == 1
        assert service.get_session(second)["messages"][1]["payload"]["content"] == "另一个会话"
        assert service.list_sessions()[0]["id"] == second
    finally:
        service.close()


def test_query_result_json_and_restart(tmp_path, monkeypatch):
    service, _ = service_for(
        tmp_path, monkeypatch, [_tool_call(1)],
        results=[{**_success(), "columns": ["nullable", "blob"],
                  "rows": [[None, b"\x00\xff"]] + [[number, None] for number in range(99)],
                  "truncated": True}],
        scores=[QualityScore(0.9, [])],
    )
    session_id = service.create_session()["id"]
    try:
        state = service.send_message(session_id, "查订单", "req")
        result = state["messages"][-1]
        assert result["kind"] == "query"
        assert result["payload"]["rows"][0] == [None, "0x00ff"]
        assert len(result["payload"]["rows"]) == 100
        assert result["payload"]["truncated"] is True
    finally:
        service.close()
    reopened, _ = service_for(tmp_path, monkeypatch, [])
    try:
        assert reopened.get_session(session_id)["messages"][-1] == result
    finally:
        reopened.close()


def test_empty_query_result(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, [_tool_call(1)],
                             results=[{**_success(), "columns": ["id"], "rows": [], "truncated": False}],
                             scores=[QualityScore(0.9, [])])
    try:
        session_id = service.create_session()["id"]
        result = service.send_message(session_id, "没有匹配的订单", "empty")["messages"][-1]
        assert result["kind"] == "query"
        assert result["payload"]["columns"] == ["id"]
        assert result["payload"]["rows"] == []
        assert result["payload"]["truncated"] is False
    finally:
        service.close()


def test_review_resume_and_stale_action(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, [_tool_call(1)], scores=[QualityScore(0.2, ["待确认"])])
    session_id = service.create_session()["id"]
    try:
        paused = service.send_message(session_id, "查订单", "request")
        assert paused["status"] == "pending"
        pending_id = paused["pending"]["id"]
        with pytest.raises(ServiceError, match="自然语言"):
            service.resume(session_id, "bad", pending_id, decision="edit_sql")
        with pytest.raises(ServiceError, match="不匹配"):
            service.resume(session_id, "bad2", "old", decision="approve")
        assert len(service.get_session(session_id)["messages"]) == 2
    finally:
        service.close()
    reopened, _ = service_for(tmp_path, monkeypatch, [], scores=[QualityScore(0.2, ["待确认"])])
    try:
        assert reopened.get_session(session_id)["pending"]["id"] == pending_id
        completed = reopened.resume(session_id, "approve", pending_id, decision="approve")
        assert completed["status"] == "idle"
        assert completed["messages"][-1]["payload"]["human_decision"] == "approve"
        assert len(reopened.resume(session_id, "approve", pending_id, decision="approve")["messages"]) == 4
        with pytest.raises(ServiceError, match="失效"):
            reopened.resume(session_id, "new", pending_id, decision="approve")
    finally:
        reopened.close()


def test_clarification_and_closed_session(tmp_path, monkeypatch):
    service, _ = service_for(
        tmp_path, monkeypatch, [_tool_call(1)],
        extracts=[
            (QuestionInfo("", [], [], []), "哪个月份？"),
            (QuestionInfo("九月订单", [], [], []), None),
        ],
        scores=[QualityScore(0.2, ["待审核"])],
    )
    session_id = service.create_session()["id"]
    try:
        state = service.send_message(session_id, "查订单", "a")
        assert state["pending"]["kind"] == "clarification"
        with pytest.raises(ServiceError, match="不能为空"):
            service.resume(session_id, "empty", state["pending"]["id"], answer=" ")
        state = service.resume(session_id, "b", state["pending"]["id"], answer="九月")
        assert state["pending"]["kind"] == "sql_review"
        state = service.resume(session_id, "c", state["pending"]["id"], decision="exit")
        assert state["status"] == "closed"
        assert state["messages"][-1]["kind"] == "error"
        with pytest.raises(ServiceError, match="会话已结束"):
            service.send_message(session_id, "追问", "d")
    finally:
        service.close()


@pytest.mark.parametrize("decision", ["edit_sql", "edit_schema", "reject"])
def test_review_revision_actions(tmp_path, monkeypatch, decision):
    service, calls = service_for(
        tmp_path, monkeypatch, [_tool_call(1)],
        extracts=[
            (QuestionInfo("count orders", [], [], []), None),
            (QuestionInfo("count invoices", [], [], []), None),
        ],
        sqls=["SELECT 1 AS id", "SELECT 2 AS id"],
        results=[_success(), _success()],
        scores=[QualityScore(0.2, []), QualityScore(0.9, [])],
    )
    try:
        session_id = service.create_session()["id"]
        pending = service.send_message(session_id, "查订单", "a")["pending"]
        state = service.resume(session_id, "b", pending["id"], decision=decision,
                               feedback="请调整查询范围" if decision != "reject" else None)
        assert state["status"] == "idle"
        assert state["messages"][-1]["kind"] == "query"
        assert state["messages"][-1]["payload"]["sql"] == "SELECT 2 AS id"
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
        if decision == "edit_schema":
            assert len(calls["link"]) == 2
        if decision == "edit_sql":
            assert calls["revised"][0][-1] == "请调整查询范围"
    finally:
        service.close()


def test_interrupted_turn_requires_retry(tmp_path, monkeypatch):
    class FailingRouting(FakeRoutingLLM):
        def invoke(self, messages):
            raise RuntimeError("offline failure")

    service, _ = service_for(tmp_path, monkeypatch, [])
    service.llm.responses = []
    service.graph = __import__("mini.agents.main_graph", fromlist=["build_agent_graph"]).build_agent_graph(
        FailingRouting([]), __import__("langgraph.checkpoint.sqlite", fromlist=["SqliteSaver"]).SqliteSaver(service.connection),
        service.history, lambda: None,
    )
    session_id = service.create_session()["id"]
    try:
        with pytest.raises(ServiceError, match="执行中断"):
            service.send_message(session_id, "你好", "req")
        assert service.get_session(session_id)["status"] == "interrupted"
        with pytest.raises(ServiceError, match="显式重试"):
            service.send_message(session_id, "再试", "next")
        service.graph = __import__("mini.agents.main_graph", fromlist=["build_agent_graph"]).build_agent_graph(
            FakeRoutingLLM([AIMessage(content="恢复成功")]),
            __import__("langgraph.checkpoint.sqlite", fromlist=["SqliteSaver"]).SqliteSaver(service.connection),
            service.history, lambda: None,
        )
        assert service.retry(session_id)["messages"][-1]["payload"]["content"] == "恢复成功"
    finally:
        service.close()


def test_busy_execution_keeps_history_readable(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    class SlowRouting(FakeRoutingLLM):
        def invoke(self, messages):
            entered.set()
            assert release.wait(5)
            return AIMessage(content="完成")

    service, _ = service_for(tmp_path, monkeypatch, [])
    from mini.agents.main_graph import build_agent_graph
    from langgraph.checkpoint.sqlite import SqliteSaver
    service.graph = build_agent_graph(SlowRouting([]), SqliteSaver(service.connection),
                                      service.history, lambda: None)
    first = service.create_session()["id"]
    second = service.create_session()["id"]
    thread = Thread(target=lambda: service.send_message(first, "慢任务", "one"))
    try:
        thread.start()
        assert entered.wait(5)
        assert service.get_session(first)["status"] == "running"
        assert len(service.list_sessions()) == 2
        with pytest.raises(ServiceError, match="正在执行另一项任务"):
            service.send_message(second, "另一个任务", "two")
        assert service.get_session(second)["messages"] == []
    finally:
        release.set()
        thread.join(5)
        service.close()


def test_retry_after_graph_finished_does_not_repeat_turn(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, [AIMessage(content="只回答一次")])
    real_graph = service.graph

    class FailAfterInvoke:
        def __init__(self):
            self.failed = False

        def invoke(self, *args, **kwargs):
            result = real_graph.invoke(*args, **kwargs)
            self.failed = True
            return result

        def get_state(self, *args, **kwargs):
            if self.failed:
                self.failed = False
                raise RuntimeError("process stopped after checkpoint")
            return real_graph.get_state(*args, **kwargs)

    session_id = service.create_session()["id"]
    try:
        service.graph = FailAfterInvoke()
        with pytest.raises(ServiceError, match="执行中断"):
            service.send_message(session_id, "你好", "once")
        service.graph = real_graph
        state = service.retry(session_id)
        assert [item["kind"] for item in state["messages"]] == ["text", "text"]
        assert state["messages"][-1]["payload"]["content"] == "只回答一次"
    finally:
        service.close()


def test_http_contract(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, [AIMessage(content="收到")])
    # TestClient owns the injected service and closes it via lifespan.
    with TestClient(create_app(lambda: service)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        session = client.post("/api/sessions").json()
        assert client.post(f"/api/sessions/{session['id']}/messages",
                           json={"content": "你好", "request_id": "a"}).json()["messages"][-1]["payload"]["content"] == "收到"
        assert client.get(f"/api/sessions/{session['id']}").status_code == 200
        assert client.get("/api/sessions/missing").status_code == 404
        assert client.post(f"/api/sessions/{session['id']}/messages",
                           json={"content": " ", "request_id": "b"}).status_code == 422
