"""Offline protocol, numeric, integrity, isolation and publication regression tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mini.analysis.execution.docker import DockerExecutor
from mini.analysis.models import (
    AnalysisConfig,
    AnalysisTask,
    ClaimInput,
    ExecutionEvidence,
)
from mini.analysis.store import EvidenceStore
from mini.analysis.tools.runtime import AnalysisRuntime
from mini.analysis.tools.statistics import calculate
from mini.analysis.validation import read_report, submit_report

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/analysis/periods.json").read_text()
)
PARAMS = dict(metric="sales", dimensions=["region"], additive=True, unit="元")


@pytest.fixture
def runtime(tmp_path):
    store = EvidenceStore(tmp_path / "analysis")
    task = AnalysisTask(
        question="分析销售变化",
        time_range={"start": "2026-10-01", "end": "2026-11-01"},
        assumptions=["同口径合成样本"],
    )
    run = store.create_run("session", 1, task.model_dump())
    return AnalysisRuntime(
        store,
        "session",
        run,
        AnalysisConfig(),
        lambda: None,
        DockerExecutor(AnalysisConfig()),
    )


def dataset(runtime, name="current", truncated=False, coverage=None):
    snapshot = FIXTURE[name]
    ev, _ = runtime.store.begin(
        ExecutionEvidence(
            session_id=runtime.session,
            run_id=runtime.run,
            operation_id=name + str(truncated) + str(coverage),
            kind="sql",
            tool_name="fixture_sql",
            parameters={
                "sql": "SELECT region, SUM(sales) FROM fixture GROUP BY region"
            },
        )
    )
    record = runtime.store.save_dataset(
        runtime.session,
        runtime.run,
        ev["evidence_id"],
        snapshot["columns"],
        snapshot["rows"],
        truncated=truncated,
        coverage=coverage,
        units={"sales": "元"},
    )
    ev.update(
        status="success",
        output_dataset_ids=[record["dataset_id"]],
        artifact_ids=[record["data_artifact_id"]],
    )
    runtime.store.finish(ev)
    return record["dataset_id"]


def contribution(runtime, **kwargs):
    ids = [dataset(runtime, **kwargs), dataset(runtime, "baseline")]
    return runtime.execute("contribution_analysis", ids, PARAMS, "contribution")


def fact(evidence, value=-0.2, pointer="/result/change_rate", unit="ratio", **extra):
    return dict(
        kind="fact",
        text="伪造的下降 999 元不会被发布",
        values=[
            dict(
                name="增长率",
                value=value,
                unit=unit,
                evidence_id=evidence,
                result_pointer=pointer,
            )
        ],
        **extra,
    )


def test_contribution_accepts_negative_and_above_one(runtime):
    output = contribution(runtime)
    r = output["result"]
    assert output["status"] == "success"
    assert (r["current"], r["baseline"], r["delta"], r["change_rate"]) == (
        800,
        1000,
        -200,
        -0.2,
    )
    assert [(g["delta"], g["contribution"]) for g in r["groups"]] == [
        (-240, 1.2),
        (40, -0.2),
    ]
    assert r["contribution_sum"] == 1 and r["reconciliation_error"] == 0
    report = submit_report(
        runtime.store, "session", runtime.run, [fact(output["evidence_id"])]
    )
    assert (
        report["status"] == "success"
        and report["claims"][0]["verification_status"] == "verified"
    )
    assert "999" not in report["claims"][0]["text"]
    assert "2026-10-01" in report["claims"][0]["text"]


@pytest.mark.parametrize(
    "value,pointer,unit",
    [
        (-0.3, "/result/change_rate", "ratio"),
        (-0.2, "/result/missing", "ratio"),
        (-0.2, "/result/change_rate", "%"),
        (-200, "/result/delta", "万元"),
    ],
)
def test_invalid_numbers_pointers_and_units_rejected(runtime, value, pointer, unit):
    output = contribution(runtime)
    report = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(output["evidence_id"], value, pointer, unit)],
    )
    assert (
        report["status"] == "failed"
        and not report["claims"]
        and report["validation_errors"]
    )


@pytest.mark.parametrize("identity", ["ev_forged", "../../secrets"])
def test_forged_ids(runtime, identity):
    report = submit_report(runtime.store, "session", runtime.run, [fact(identity)])
    assert not report["claims"]


def test_cross_session_and_idempotent_operations(runtime):
    output = contribution(runtime)
    again = runtime.execute(
        "contribution_analysis",
        runtime.store.get("session", output["evidence_id"])["input_dataset_ids"],
        PARAMS,
        "contribution",
    )
    assert again["evidence_id"] == output["evidence_id"]
    with pytest.raises(ValueError, match="operation_identity_conflict"):
        runtime.execute(
            "contribution_analysis",
            [dataset(runtime), dataset(runtime, "baseline")],
            PARAMS,
            "contribution",
        )
    op = runtime.store.operation_id("session", runtime.run, "provider-call-1")
    assert op.startswith("op_") and op == runtime.store.operation_id(
        "session", runtime.run, "provider-call-1"
    )
    with pytest.raises(KeyError):
        runtime.store.get("other", output["evidence_id"])
    run = runtime.store.create_run(
        "other", 1, AnalysisTask(question="other").model_dump()
    )
    report = submit_report(runtime.store, "other", run, [fact(output["evidence_id"])])
    assert not report["claims"]


@pytest.mark.parametrize(
    "truncated,coverage", [(True, {}), (False, {"sql_limit": True})]
)
def test_incomplete_data_rejects_population(runtime, truncated, coverage):
    output = contribution(runtime, truncated=truncated, coverage=coverage)
    report = submit_report(
        runtime.store, "session", runtime.run, [fact(output["evidence_id"])]
    )
    assert not report["claims"]
    report = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(output["evidence_id"], scope="sample", limitations=["仅实际返回的样本"])],
    )
    assert report["claims"]


def test_artifact_tamper_missing_and_symlink_revoke_report(runtime, tmp_path):
    output = contribution(runtime)
    report = submit_report(
        runtime.store, "session", runtime.run, [fact(output["evidence_id"])]
    )
    ev = runtime.store.get("session", output["evidence_id"])
    ds, _ = runtime.store.dataset("session", ev["input_dataset_ids"][0])
    path = runtime.store.objects / ds["data_artifact_id"]
    original = path.read_bytes()
    path.write_bytes(b"{}")
    assert not read_report(runtime.store, "session", report)["claims"]
    path.unlink()
    assert not read_report(runtime.store, "session", report)["claims"]
    other = tmp_path / "elsewhere"
    other.write_bytes(original)
    path.symlink_to(other)
    assert not read_report(runtime.store, "session", report)["claims"]


def test_independent_recalculation_rejects_altered_result(runtime):
    output = contribution(runtime)
    ev = runtime.store.get("session", output["evidence_id"])
    ev["result"]["change_rate"] = -0.3
    runtime.store.save("evidence", ev["evidence_id"], "session", runtime.run, ev)
    report = submit_report(
        runtime.store, "session", runtime.run, [fact(ev["evidence_id"], -0.3)]
    )
    assert not report["claims"] and "recalculation" in report["validation_errors"][0]


def test_failed_execution_cannot_be_cited(runtime):
    ev, _ = runtime.store.begin(
        ExecutionEvidence(
            session_id="session",
            run_id=runtime.run,
            operation_id="bad",
            kind="python",
            tool_name="python",
        )
    )
    ev.update(status="error", result={"change_rate": -0.2}, error="syntax")
    runtime.store.finish(ev)
    assert not submit_report(
        runtime.store, "session", runtime.run, [fact(ev["evidence_id"])]
    )["claims"]


def test_freeform_fact_and_model_verification_status_rejected(runtime):
    assert not submit_report(
        runtime.store, "session", runtime.run, [{"kind": "fact", "text": "销售下降20%"}]
    )["claims"]
    with pytest.raises(ValueError):
        ClaimInput.model_validate(dict(kind="fact", verification_status="verified"))


def test_zero_negative_baseline_missing_groups_and_empty():
    current = dict(columns=["region", "sales"], rows=[["A", 10]])
    base = dict(columns=["region", "sales"], rows=[["A", 0]])
    result, warnings = calculate("compare_periods", [current, base], PARAMS)
    assert result["change_rate"] is None and warnings
    base["rows"] = [["A", -10]]
    result, warnings = calculate("compare_periods", [current, base], PARAMS)
    assert result["change_rate"] == -2 and warnings
    base["rows"] = [["B", 10]]
    with pytest.raises(ValueError, match="missing_groups"):
        calculate("compare_periods", [current, base], PARAMS)
    result, _ = calculate(
        "contribution_analysis", [current, base], {**PARAMS, "missing_groups": "zero"}
    )
    assert result["delta"] == 0 and result["contribution_sum"] is None
    with pytest.raises(ValueError, match="empty_dataset"):
        calculate("inspect_dataset", [dict(columns=["x"], rows=[])], {})


def test_describe_ddof_and_nulls():
    result, _ = calculate(
        "describe_dataset",
        [dict(columns=["x"], rows=[[1], [3], [None]])],
        {"columns": ["x"]},
    )
    stats = result["groups"][0]["columns"]["x"]
    assert (
        stats["mean"] == 2
        and stats["missing"] == 1
        and stats["std"] == pytest.approx(2**0.5)
    )
    assert stats["q25"] == 1.5


def test_correlation_alignment_constant_and_sample_count():
    snap = dict(columns=["id", "x", "y"], rows=[[1, 1, 2], [2, 1, 3], [3, 1, None]])
    p = dict(columns=["x", "y"], alignment_keys=["id"])
    result, _ = calculate("correlation_analysis", [snap], p)
    assert result["pairs"][0]["reason"] == "insufficient_samples"
    snap["rows"].append([4, 1, 5])
    assert (
        calculate("correlation_analysis", [snap], p)[0]["pairs"][0]["reason"]
        == "constant_column"
    )
    snap["rows"].append([1, 2, 5])
    with pytest.raises(ValueError, match="duplicate_alignment"):
        calculate("correlation_analysis", [snap], p)


def test_anomaly_has_no_future_leakage_and_missing_days():
    snap = dict(
        columns=["day", "x"],
        rows=[
            ["2026-01-01", 1],
            ["2026-01-02", 2],
            ["2026-01-04", 10],
            ["2026-01-05", 100],
        ],
    )
    p = dict(time_column="day", metric="x", window=2)
    result, warnings = calculate("detect_anomalies", [snap], p)
    assert result["series"][2]["baseline"] == 1.5 and result["series"][2]["anomaly"]
    assert result["series"][0]["score"] is None
    assert result["missing_dates"] == ["2026-01-03"] and warnings
    snap["rows"].append(["2026-01-04", 10])
    with pytest.raises(ValueError, match="duplicate_dates"):
        calculate("analyze_trend", [snap], p)


def test_timezone_cross_day():
    snap = dict(
        columns=["day", "x"],
        rows=[["2026-01-01T18:00:00+00:00", 1], ["2026-01-02T18:00:00+00:00", 2]],
    )
    result, _ = calculate(
        "analyze_trend",
        [snap],
        dict(time_column="day", metric="x", window=2, timezone="Asia/Shanghai"),
    )
    assert result["series"][0]["day"] == "2026-01-02"


def test_docker_unavailable_never_executes_host_source():
    with (
        patch("mini.analysis.execution.docker.shutil.which", return_value=None),
        patch("subprocess.Popen") as popen,
    ):
        out = DockerExecutor(AnalysisConfig()).run('raise RuntimeError("never")', {})
        assert out["error"] == "executor_unavailable"
        popen.assert_not_called()


def test_budget_running_and_delete(runtime):
    runtime.config = AnalysisConfig(max_tool_calls=1)
    ids = [dataset(runtime), dataset(runtime, "baseline")]
    assert runtime.execute("compare_periods", ids, PARAMS, "one")["status"] == "success"
    assert (
        "budget_exhausted"
        in runtime.execute("compare_periods", ids, PARAMS, "two")["error"]
    )
    runtime.store.delete_session("session")
    with pytest.raises(KeyError):
        runtime.store.dataset("session", ids[0])
    assert not list(runtime.store.objects.iterdir())


def test_metric_unit_not_overridden_by_tool(runtime):
    ids = [dataset(runtime), dataset(runtime, "baseline")]
    out = runtime.execute(
        "compare_periods", ids, {**PARAMS, "unit": "万元"}, "wrong-unit"
    )
    assert out["status"] == "error" and "unit_mismatch" in out["error"]


def test_decimal_date_null_snapshot_serialization(runtime):
    from datetime import date
    from decimal import Decimal

    ev, _ = runtime.store.begin(
        ExecutionEvidence(
            session_id="session",
            run_id=runtime.run,
            operation_id="decimal",
            kind="sql",
            tool_name="fixture",
        )
    )
    ds = runtime.store.save_dataset(
        "session",
        runtime.run,
        ev["evidence_id"],
        ["amount", "day"],
        [[Decimal("123456789.123456789"), date(2026, 1, 1)], [None, None]],
    )
    meta, snap = runtime.store.dataset("session", ds["dataset_id"])
    assert snap["rows"][0] == ["123456789.123456789", "2026-01-01"]
    assert snap["rows"][1] == [None, None]
    assert snap["column_types"] == {"amount": "Decimal", "day": "date"}


def test_interpretation_requires_and_resolves_fact_references(runtime):
    out = contribution(runtime)
    report = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [
            fact(out["evidence_id"]),
            dict(
                kind="interpretation",
                text="A 的负向变化抵消了 B 的正向变化，原因仍需核查",
                evidence_refs=[out["evidence_id"]],
                fact_refs=["fact:0"],
            ),
        ],
    )
    assert (
        len(report["claims"]) == 2
        and report["claims"][1]["verification_status"] == "unverified"
    )
    assert report["claims"][1]["fact_refs"] == [report["claims"][0]["claim_id"]]


def test_invalid_time_ranges_and_timezone():
    with pytest.raises(ValueError):
        AnalysisTask(
            question="x", time_range={"start": "2026-11-01", "end": "2026-10-01"}
        )


def test_sqlite_write_failure_never_returns_success(runtime, monkeypatch):
    ids = [dataset(runtime), dataset(runtime, "baseline")]
    import sqlite3

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(runtime.store, "finish", broken)
    with pytest.raises(sqlite3.OperationalError):
        runtime.execute("contribution_analysis", ids, PARAMS, "disk-failure")


def test_requested_outputs_cannot_be_silently_omitted(runtime):
    out = contribution(runtime)
    record = runtime.store.get("session", runtime.run, "run")
    record["task"]["requested_outputs"] = ["python", "png"]
    runtime.store.save("run", runtime.run, "session", runtime.run, record)
    report = submit_report(
        runtime.store, "session", runtime.run, [fact(out["evidence_id"])]
    )
    assert report["status"] == "partial"
    assert "required_output_missing:python" in report["validation_errors"]
    assert "required_output_missing:png" in report["validation_errors"]


def test_descriptive_units_come_from_snapshot(runtime):
    identity = dataset(runtime)
    out = runtime.execute(
        "describe_dataset",
        [identity],
        {"columns": ["sales"], "unit": "万元"},
        "description",
    )
    pointer = "/result/groups/0/columns/sales/sum"
    rejected = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(out["evidence_id"], 800, pointer, "万元")],
    )
    accepted = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(out["evidence_id"], 800, pointer, "元")],
    )
    assert rejected["status"] == "failed" and accepted["status"] == "success"


def test_sql_completed_before_envelope_crash_is_reused(runtime, monkeypatch):
    from mini.agents import text2sql_graph
    from mini.query.database import ExecutionOutcome
    from mini.runtime.resources import RuntimeResources

    calls = []

    class Adapter:
        dialect = "sqlite"

        def execute_readonly(self, sql, **kwargs):
            calls.append(sql)
            return ExecutionOutcome(
                {
                    "status": "success",
                    "columns": ["sales"],
                    "rows": [[800]],
                    "truncated": False,
                    "error": None,
                }
            )

    resource = RuntimeResources(None, None, None, Adapter())
    runtime.resources_factory = lambda: resource

    def build(resources, checkpointer):
        class Graph:
            def invoke(self, inputs, config):
                result = resources.adapter.execute_readonly(
                    "SELECT 800 AS sales"
                ).result
                return {"status": "success", "result": result}

        return Graph()

    monkeypatch.setattr(text2sql_graph, "build_text2sql_graph", build)
    original = runtime.store.finish
    fail = [True]

    def finish(ev):
        if ev["tool_name"] == "query_dataset" and fail[0]:
            fail[0] = False
            raise OSError("crash before envelope checkpoint")
        original(ev)

    monkeypatch.setattr(runtime.store, "finish", finish)
    with pytest.raises(OSError):
        runtime.query("sales", {}, {"sales": "元"}, "query-one", {})
    result = runtime.query("sales", {}, {"sales": "元"}, "query-one", {})
    assert result["status"] == "success" and calls == ["SELECT 800 AS sales"]
    assert len(runtime.store.list("session", runtime.run, "dataset")) == 1


def test_parallel_sessions_keep_independent_evidence(runtime):
    from concurrent.futures import ThreadPoolExecutor

    other_run = runtime.store.create_run(
        "other", 1, AnalysisTask(question="other synthetic").model_dump()
    )
    other = AnalysisRuntime(
        runtime.store,
        "other",
        other_run,
        AnalysisConfig(),
        lambda: None,
        DockerExecutor(AnalysisConfig()),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(contribution, runtime)
        right = pool.submit(contribution, other)
        a, b = left.result(), right.result()
    assert a["evidence_id"] != b["evidence_id"]
    assert runtime.store.get("session", a["evidence_id"])["session_id"] == "session"
    assert runtime.store.get("other", b["evidence_id"])["session_id"] == "other"
    with pytest.raises(KeyError):
        runtime.store.get("other", a["evidence_id"])


def test_report_tool_exposes_claim_schema_and_server_artifact_metadata(runtime):
    submit = next(t for t in runtime.tools() if t.name == "submit_analysis_report")
    schema = submit.tool_call_schema.model_json_schema()
    assert schema["properties"]["claims"]["items"]["$ref"].endswith("/ClaimInput")
    assert "values" in schema["$defs"]["ClaimInput"]["properties"]
    assert "verification_status" not in schema["$defs"]["ClaimInput"]["properties"]
    out = contribution(runtime)
    assert len(out["datasets"]) == 2 and out["datasets"][0]["units"]["sales"] == "元"


def test_unused_prior_sql_attempt_does_not_contaminate_current_coverage(runtime):
    out = contribution(runtime)
    prior_id = dataset(runtime, truncated=True)
    prior, _ = runtime.store.dataset("session", prior_id)
    ev = runtime.store.get("session", out["evidence_id"])
    ev["parent_evidence_ids"].append(prior["source_evidence_id"])
    runtime.store.save("evidence", ev["evidence_id"], "session", runtime.run, ev)
    report = submit_report(
        runtime.store, "session", runtime.run, [fact(out["evidence_id"])]
    )
    assert report["status"] == "success"


def test_percentage_difference_requires_percentage_points(runtime):
    from mini.analysis.tools.statistics import calculate

    snapshots = [
        dict(columns=["rate"], rows=[[30]]),
        dict(columns=["rate"], rows=[[20]]),
    ]
    ev, _ = runtime.store.begin(
        ExecutionEvidence(
            session_id="session",
            run_id=runtime.run,
            operation_id="percent",
            kind="analysis_tool",
            tool_name="compare_periods",
            parameters={"metric": "rate", "dimensions": [], "unit": "%"},
        )
    )
    ids = []
    for i, snap in enumerate(snapshots):
        source, _ = runtime.store.begin(
            ExecutionEvidence(
                session_id="session",
                run_id=runtime.run,
                operation_id=f"percent-source-{i}",
                kind="sql",
                tool_name="fixture",
            )
        )
        ds = runtime.store.save_dataset(
            "session",
            runtime.run,
            source["evidence_id"],
            snap["columns"],
            snap["rows"],
            units={"rate": "%"},
        )
        source.update(status="success", output_dataset_ids=[ds["dataset_id"]])
        runtime.store.finish(source)
        ids.append(ds["dataset_id"])
    ev.update(
        status="success",
        input_dataset_ids=ids,
        result=calculate("compare_periods", snapshots, ev["parameters"])[0],
    )
    runtime.store.finish(ev)
    bad = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(ev["evidence_id"], 10, "/result/delta", "%")],
    )
    good = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(ev["evidence_id"], 10, "/result/delta", "百分点")],
    )
    assert bad["status"] == "failed" and good["status"] == "success"


def test_large_monetary_values_do_not_gain_relative_error_allowance(
    runtime, monkeypatch
):
    monkeypatch.setitem(
        FIXTURE["current"], "rows", [["A", 1_000_000_000_000], ["B", 0]]
    )
    out = contribution(runtime)
    report = submit_report(
        runtime.store,
        "session",
        runtime.run,
        [fact(out["evidence_id"], 999_999_999_950, "/result/current", "元")],
    )
    assert (
        report["status"] == "failed"
        and "numeric_value_mismatch" in report["validation_errors"][0]
    )
