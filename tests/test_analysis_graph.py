"""Real DeepAgents graph with an explicit scripted offline model, not an LLM demonstration."""

import json
import sqlite3

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import Field

from mini.agents.main_graph import build_agent_graph, new_agent_turn
from mini.analysis.delegation import AnalysisService
from mini.analysis.models import AnalysisConfig
from mini.query.schema_linking import QuestionInfo
from mini.query.sql_quality import QualityScore
from mini.runtime.query_history import QueryHistory
from mini.runtime.resources import RuntimeResources
from tests.test_agent_graph import FakeRoutingLLM
from tests.test_text2sql_graph import _make_graph, _success

pytest.importorskip("deepagents", reason="optional analysis dependencies not installed")


class ScriptedAnalysisModel(BaseChatModel):
    seen: list = Field(default_factory=list)
    steps: int = 0
    calls: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-scripted-analysis"

    def bind_tools(self, tools, **kwargs):
        self.seen.append(
            {(t.get("name") if isinstance(t, dict) else t.name) for t in tools}
        )
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.steps += 1
        completed = []
        for m in messages:
            if isinstance(m, ToolMessage) and m.name == "query_dataset":
                completed.append(json.loads(m.content))
        if len(completed) < 2:
            args = {
                "question": f"query {len(completed) + 1}",
                "coverage": {"range": "synthetic"},
                "units": {"id": "count"},
            }
            name = "query_dataset"
        elif not any(
            isinstance(m, ToolMessage) and m.name == "submit_analysis_report"
            for m in messages
        ):
            args = {
                "claims": [],
                "limitations": ["离线图测试只验证查询暂停恢复"],
                "unresolved_questions": [],
            }
            name = "submit_analysis_report"
        else:
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content="report submitted"))
                ]
            )
        call = {"name": name, "args": args, "id": f"step-{self.steps}"}
        self.calls.append(call)
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content="", tool_calls=[call]))
            ]
        )


def analysis_parent(tmp_path, monkeypatch, model, *, reopen=False):
    unused, conn, calls = _make_graph(
        tmp_path,
        monkeypatch,
        extracts=[
            (QuestionInfo("first", [], [], []), None),
            (QuestionInfo("second", [], [], []), None),
        ],
        sqls=["SELECT 1 AS id", "SELECT 2 AS id"],
        results=[_success(), _success()],
        scores=[QualityScore(0.9, []), QualityScore(0.2, ["review second"])],
    )
    conn.close()
    connection = sqlite3.connect(tmp_path / "parent.sqlite", check_same_thread=False)
    resources = RuntimeResources(
        catalog=object(),
        indexes=type("Indexes", (), {"text2sql": object()})(),
        llm=model,
        adapter=calls["adapter"],
    )
    service = AnalysisService(
        AnalysisConfig(evidence_dir=str(tmp_path / "evidence")),
        model,
        lambda: resources,
    )
    routing = FakeRoutingLLM(
        []
        if reopen
        else [
            AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="data_analysis",
                        args={"task": {"question": "multi step"}},
                        id="analysis",
                    )
                ],
            )
        ]
    )
    graph = build_agent_graph(
        routing,
        SqliteSaver(connection),
        QueryHistory(tmp_path / "history.sqlite"),
        lambda: resources,
        analysis_service=service,
    )
    return graph, connection, service, calls


def test_two_query_nested_review_resumes_without_reexecution(tmp_path, monkeypatch):
    model = ScriptedAnalysisModel()
    graph, connection, service, calls = analysis_parent(tmp_path, monkeypatch, model)
    config = {"configurable": {"thread_id": "session"}}
    try:
        first = graph.invoke(new_agent_turn("复杂分析", 1), config)
        assert "__interrupt__" in first, first.get("analysis_outcome")
        assert first["__interrupt__"][0].value["kind"] == "sql_review"
        run = graph.get_state(config).values["analysis_run_id"]
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
        resumed = graph.invoke(Command(resume={"decision": "approve"}), config)
        assert resumed["analysis_outcome"]["run_id"] == run
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
        queries = [
            e
            for e in service.store.list("session", run, "evidence")
            if e["tool_name"] == "query_dataset"
        ]
        assert len(queries) == 2 and all(e["status"] == "success" for e in queries)
        assert len({e["output_dataset_ids"][0] for e in queries}) == 2
        assert model.steps == 4
        assert all(
            not names
            & {
                "execute",
                "task",
                "read_file",
                "write_file",
                "edit_file",
                "ls",
                "glob",
                "grep",
            }
            for names in model.seen
        )
    finally:
        connection.close()


def test_analysis_model_budget_is_enforced(tmp_path, monkeypatch):
    model = ScriptedAnalysisModel()
    graph, connection, service, calls = analysis_parent(tmp_path, monkeypatch, model)
    service.config = service.config.model_copy(update={"max_model_calls": 1})
    try:
        state = graph.invoke(
            new_agent_turn("analysis", 1), {"configurable": {"thread_id": "s"}}
        )
        assert state["analysis_outcome"]["status"] == "failed"
        assert "budget_exhausted" in str(state["analysis_outcome"])
    finally:
        connection.close()


def test_restart_then_resume_analysis_query(tmp_path, monkeypatch):
    model = ScriptedAnalysisModel()
    graph, connection, service, calls = analysis_parent(tmp_path, monkeypatch, model)
    config = {"configurable": {"thread_id": "restart"}}
    first = graph.invoke(new_agent_turn("multi step", 1), config)
    assert "__interrupt__" in first
    run = graph.get_state(config).values["analysis_run_id"]
    connection.close()
    reopened, conn, service2, calls2 = analysis_parent(
        tmp_path, monkeypatch, model, reopen=True
    )
    try:
        result = reopened.invoke(Command(resume={"decision": "approve"}), config)
        assert result["analysis_outcome"]["run_id"] == run
        assert calls2["executed"] == []
        assert calls["executed"] == ["SELECT 1 AS id", "SELECT 2 AS id"]
        assert len(service2.store.list("restart", run, "dataset")) == 2
    finally:
        conn.close()
