"""Reproducible synthetic SQL → fixed/Python calculations → validated report.

This deterministic integration demonstration does NOT simulate an autonomous LLM.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from mini.analysis.execution.docker import DockerExecutor
from mini.analysis.models import AnalysisConfig, AnalysisTask, ExecutionEvidence
from mini.analysis.store import EvidenceStore
from mini.analysis.tools.runtime import AnalysisRuntime
from mini.analysis.validation import submit_report
from mini.query.sqlite_adapter import SQLiteAdapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, default=Path("var/analysis/demo"))
    parser.add_argument(
        "--python",
        action="store_true",
        help="Actually execute Python in Docker and require success",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve a read-only, clearly labeled synthetic Web demonstration on port 8765",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Run a REAL configured model against synthetic datasets; requires --config",
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args()
    store = EvidenceStore(args.store)
    task = AnalysisTask(
        question="比较两期合成销售额并按地区分解变化",
        metrics=[dict(name="sales", definition="按地区加总销售额", unit="元")],
        time_range={"start": "2026-10-01", "end": "2026-11-01"},
        comparison_range={"start": "2026-09-01", "end": "2026-10-01"},
        assumptions=["人工合成两期互斥完备地区 A/B，只演示执行和证据链"],
    )
    session = "synthetic-demo"
    run = store.create_run(session, 1, task.model_dump())
    db = args.store / "synthetic.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sales(period TEXT,region TEXT,sales REAL)"
        )
        conn.execute("DELETE FROM sales")
        conn.executemany(
            "INSERT INTO sales VALUES(?,?,?)",
            [
                ("baseline", "A", 600),
                ("baseline", "B", 400),
                ("current", "A", 360),
                ("current", "B", 440),
            ],
        )
    ids = []
    for period in ("current", "baseline"):
        sql = f"SELECT region, SUM(sales) AS sales FROM sales WHERE period = '{period}' GROUP BY region ORDER BY region"
        ev, _ = store.begin(
            ExecutionEvidence(
                session_id=session,
                run_id=run,
                operation_id=period,
                kind="sql",
                tool_name="execute_readonly",
                parameters={
                    "sql": sql,
                    "datasource": "synthetic.sqlite",
                    "question": period,
                    "sql_parameters": {},
                },
            )
        )
        out = SQLiteAdapter(db).execute_readonly(sql).result
        if out["status"] != "success":
            raise RuntimeError(out)
        ds = store.save_dataset(
            session,
            run,
            ev["evidence_id"],
            out["columns"],
            out["rows"],
            coverage={
                "period": period,
                "time_range": task.time_range
                if period == "current"
                else task.comparison_range,
                "timezone": task.timezone,
                "synthetic": True,
            },
            units={"sales": "元"},
        )
        ev.update(
            status="success",
            output_dataset_ids=[ds["dataset_id"]],
            artifact_ids=[ds["data_artifact_id"]],
        )
        store.finish(ev)
        ids.append(ds["dataset_id"])
    config = AnalysisConfig()
    runtime = AnalysisRuntime(
        store, session, run, config, lambda: None, DockerExecutor(config)
    )
    if args.llm:
        from mini.analysis.delegation import AnalysisService
        from mini.runtime.resources import _read_config, create_llm

        raw = _read_config(args.config)
        actual_config = AnalysisConfig.model_validate(raw.get("analysis") or {})
        model = create_llm(
            args.config,
            config={**raw, "default_llm": actual_config.model or raw["default_llm"]},
        )
        task.input_dataset_ids = ids
        task.objective = "调用固定贡献工具并自主编写、实际运行 Python，生成 PNG，对比结果，再提交逐条绑定证据的报告。合成数据已有，无需再次查询。"
        task.requested_outputs = ["validated_report", "python", "png"]
        record = store.get(session, run, "run")
        record["task"] = task.model_dump()
        store.save("run", run, session, run, record)

        def no_query_resources():
            raise ValueError("合成演示仅授权已有数据集，请使用 input_dataset_ids")

        service = AnalysisService(actual_config, model, no_query_resources, store=store)
        report = service.invoke(
            session, run, {"configurable": {"thread_id": session + ":" + run}}
        )
        print(
            json.dumps(
                {
                    "mode": "REAL_LLM",
                    "model": (actual_config.model or raw["default_llm"])["params"].get(
                        "model"
                    ),
                    "run_id": run,
                    "report": report,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.serve:
            serve_demo(store, session, report, model_used=True)
        return
    fixed = runtime.execute(
        "contribution_analysis",
        ids,
        dict(metric="sales", dimensions=["region"], additive=True, unit="元"),
        "fixed",
    )
    claims = []
    for path, value, unit in [
        ("/result/delta", -200, "元"),
        ("/result/change_rate", -0.2, "ratio"),
        ("/result/groups/0/contribution", 1.2, "ratio"),
        ("/result/groups/1/contribution", -0.2, "ratio"),
    ]:
        claims.append(
            dict(
                kind="fact",
                values=[
                    dict(
                        name=path,
                        value=value,
                        unit=unit,
                        evidence_id=fixed["evidence_id"],
                        result_pointer=path,
                    )
                ],
            )
        )
    python = None
    if args.python:
        source = f"""from analysis_sdk import load_dataset, emit_result, save_figure
import matplotlib.pyplot as plt
c=load_dataset({ids[0]!r}).set_index('region')['sales']
b=load_dataset({ids[1]!r}).set_index('region')['sales']
d=c-b; total=float(d.sum())
emit_result({{'delta':total,'change_rate':total/float(b.sum()),'A':float(d['A'])/total,'B':float(d['B'])/total}})
fig,ax=plt.subplots(); ax.bar(d.index,d.values); ax.set_ylabel('sales delta'); save_figure(fig,'contribution.png')
"""
        python = runtime.execute(
            "run_python_analysis",
            ids,
            dict(result_units={"/result/change_rate": "ratio"}),
            "python",
            source,
        )
        if python["status"] != "success":
            print(json.dumps(python, ensure_ascii=False, indent=2))
            raise SystemExit(1)
        assert python["result"]["change_rate"] == fixed["result"]["change_rate"]
        claims.append(
            dict(
                kind="fact",
                values=[
                    dict(
                        name="Python change rate",
                        value=-0.2,
                        unit="ratio",
                        evidence_id=python["evidence_id"],
                        result_pointer="/result/change_rate",
                    )
                ],
            )
        )
    report = submit_report(
        store, session, run, claims, limitations=["脚本集成演示；未使用 LLM"]
    )
    if args.serve:
        serve_demo(store, session, report)
    print(
        json.dumps(
            dict(
                session_id=session,
                run_id=run,
                evidence_id=fixed["evidence_id"],
                python_evidence_id=python["evidence_id"] if python else None,
                report=report,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


def serve_demo(store, session, report, model_used=False):
    import uvicorn

    from mini.analysis.delegation import AnalysisService
    from mini.analysis.models import now
    from mini.web.api import create_app
    from mini.web.service import WebSessionService

    class NoModel:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, *args, **kwargs):
            raise RuntimeError("只读合成演示不调用模型")

    def factory():
        analysis = AnalysisService(
            AnalysisConfig(), NoModel(), lambda: None, store=store
        )
        service = WebSessionService(
            store.root / "demo-web.sqlite",
            llm=NoModel(),
            query_resources_factory=lambda: None,
            analysis_service=analysis,
        )
        with service._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO web_sessions(id,title,status,turn_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    session,
                    "合成数据真实模型演示"
                    if model_used
                    else "合成数据执行演示（未使用 LLM）",
                    "closed",
                    1,
                    now(),
                    now(),
                ),
            )
            db.execute("DELETE FROM web_messages WHERE session_id=?", (session,))
            service._add_message(db, session, 1, "assistant", "analysis", report)
        return service

    uvicorn.run(create_app(factory), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
