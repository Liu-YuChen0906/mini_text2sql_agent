"""Regression coverage for real model argument shapes that previously broke web retries."""

import pytest
from langchain_core.messages import AIMessage

from mini.analysis.models import AnalysisTask
from mini.analysis.store import EvidenceStore
from mini.analysis.validation import submit_report
from mini.web.service import ServiceError, WebSessionService
from tests.test_agent_graph import FakeRoutingLLM

QUESTION = "分析2026年5月相比4月的订单量变化，按客户所在州分解贡献并生成Python图表。"


def call(task, identity="analysis-call"):
    return AIMessage(
        content="",
        tool_calls=[{"name": "data_analysis", "args": {"task": task}, "id": identity}],
    )


def valid_task():
    return {
        "question": "model summary",
        "metrics": [{"name": "订单量", "definition": "去重订单计数", "unit": "条"}],
        "time_range": {"start": "2026-05-01", "end": "2026-06-01"},
        "comparison_range": {"start": "2026-04-01", "end": "2026-05-01"},
    }


class StubAnalysis:
    """Offline service records preparation, without pretending to execute an analysis."""

    def __init__(self, root):
        self.store = EvidenceStore(root)
        self.tasks = []
        self.executions = 0

    def start(self, session, turn, task, request=""):
        self.tasks.append(task)
        return self.store.create_run(session, turn, task.model_dump(), request)

    def invoke(self, session, run, config):
        self.executions += 1
        report = submit_report(
            self.store, session, run, [], ["offline preparation test"], status="failed"
        )
        record = self.store.get(session, run, "run")
        record["status"] = "failed"
        self.store.save("run", run, session, run, record)
        return report


def test_delegation_schema_names_metric_and_period_fields():
    schema = AnalysisTask.model_json_schema()
    assert schema["properties"]["metrics"]["items"]["$ref"].endswith(
        "/MetricDefinition"
    )
    period = schema["$defs"]["AnalysisPeriod"]
    assert (
        set(period["required"]) == {"start", "end"}
        and period["additionalProperties"] is False
    )
    assert set(schema["$defs"]["MetricDefinition"]["properties"]) == {
        "name",
        "definition",
        "unit",
    }


@pytest.mark.parametrize(
    "fault", ["metric_strings", "range_annotations", "nested_periods"]
)
def test_bad_delegation_arguments_are_repaired_before_work(tmp_path, fault):
    raw = valid_task()
    if fault == "metric_strings":
        raw["metrics"] = ["订单量", "环比变化率"]
    elif fault == "range_annotations":
        raw["time_range"].update(description="自然月", tz="Asia/Shanghai")
    else:
        raw["time_range"] = {"start": "2026-04-01", "end": "2026-06-01"}
        raw["comparison_range"] = {
            "current": "2026-05-01 至 2026-06-01",
            "baseline": "2026-04-01 至 2026-05-01",
        }
    model = FakeRoutingLLM([call(raw), call(valid_task(), "corrected")])
    analysis = StubAnalysis(tmp_path / "evidence")
    service = WebSessionService(
        tmp_path / "web.sqlite",
        llm=model,
        query_resources_factory=lambda: None,
        analysis_service=analysis,
    )
    try:
        sid = service.create_session()["id"]
        result = service.send_message(sid, QUESTION, "request")
        assert result["status"] == "idle" and analysis.executions == 1
        assert len(model.prompts) == 2 and len(analysis.tasks) == 1
        assert analysis.tasks[0].question == QUESTION
        assert analysis.tasks[0].time_range == {
            "start": "2026-05-01",
            "end": "2026-06-01",
        }
        assert result["messages"][-1]["kind"] == "analysis"
        service.send_message(sid, QUESTION, "request")
        assert analysis.executions == 1 and len(model.prompts) == 2
    finally:
        service.close()


def test_repair_exhaustion_returns_a_failed_report_without_sticking_session(tmp_path):
    raw = {"question": QUESTION, "metrics": ["bad metric"]}
    model = FakeRoutingLLM([call(raw, str(i)) for i in range(3)])
    analysis = StubAnalysis(tmp_path / "evidence")
    service = WebSessionService(
        tmp_path / "web.sqlite",
        llm=model,
        query_resources_factory=lambda: None,
        analysis_service=analysis,
    )
    try:
        sid = service.create_session()["id"]
        result = service.send_message(sid, QUESTION, "bad-task")
        assert result["status"] == "idle" and analysis.executions == 0
        report = result["messages"][-1]["payload"]
        assert (
            report["status"] == "failed"
            and report["validation_errors"]
            and not report["claims"]
        )
        assert len(model.prompts) == 3
    finally:
        service.close()


def test_old_interrupted_prepare_checkpoint_can_now_be_retried(tmp_path, monkeypatch):
    import mini.analysis.preparation as preparation

    raw = valid_task()
    raw["time_range"]["description"] = "五月自然月"
    path = tmp_path / "web.sqlite"
    analysis = StubAnalysis(tmp_path / "evidence")
    service = WebSessionService(
        path,
        llm=FakeRoutingLLM([call(raw)]),
        query_resources_factory=lambda: None,
        analysis_service=analysis,
    )
    sid = service.create_session()["id"]

    def old_uncaught_parser(model, messages, question):
        AnalysisTask.model_validate(messages[-1].tool_calls[0]["args"]["task"])

    with monkeypatch.context() as patch:
        patch.setattr(preparation, "prepare_task", old_uncaught_parser)
        with pytest.raises(ServiceError):
            service.send_message(sid, QUESTION, "old-request")
    assert service.get_session(sid)["status"] == "interrupted"
    service.close()
    model = FakeRoutingLLM([call(valid_task(), "repaired-after-restart")])
    reopened = WebSessionService(
        path, llm=model, query_resources_factory=lambda: None, analysis_service=analysis
    )
    try:
        result = reopened.retry(sid)
        assert result["status"] == "idle" and analysis.executions == 1
        assert len(model.prompts) == 1 and analysis.tasks[0].question == QUESTION
    finally:
        reopened.close()


def test_bad_timezone_and_mixed_aware_dates_are_controlled_validation_errors():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalysisTask(question="x", timezone="not-a-zone")
    with pytest.raises(ValidationError):
        AnalysisTask(
            question="x",
            time_range={"start": "2026-05-01T00:00:00+08:00", "end": "2026-06-01"},
        )


def test_recovery_budget_is_bounded_and_audited(tmp_path):
    from mini.analysis.delegation import AnalysisService
    from mini.analysis.models import AnalysisConfig

    service = AnalysisService(
        AnalysisConfig(max_model_calls=20, evidence_dir=str(tmp_path / "audit")),
        None,
        lambda: None,
    )
    run = service.start("owner", 1, AnalysisTask(question="recover"))
    service.extend_model_budget(
        "owner", run, 40, "recover already-paid SQL from the previous checkpoint"
    )
    record = service.store.get("owner", run, "run")
    assert (
        record["model_call_limit"] == 40
        and record["budget_adjustments"][0]["old_limit"] == 20
    )
    with pytest.raises(ValueError):
        service.extend_model_budget("owner", run, 41, "cannot extend repeatedly")
    with pytest.raises(KeyError):
        service.extend_model_budget("other", run, 40, "not owner")
    with pytest.raises(ValueError):
        AnalysisTask.model_validate(
            {"question": "model injection", "model_call_limit": 40}
        )
