"""Server-owned tool wrappers: all executions and failures become durable evidence."""

import hashlib
import json
from dataclasses import replace
from typing import Annotated

import sqlglot
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.errors import GraphInterrupt
from sqlglot import exp

from mini.analysis.models import ClaimInput, ExecutionEvidence
from mini.analysis.store import encode
from mini.analysis.tools.statistics import NAMES, calculate
from mini.analysis.validation import submit_report
from mini.query.database import ExecutionOutcome, empty_result
from mini.runtime.limits import ExecutionBudgetExceeded


def response(ev, store):
    previews = [
        store.get(ev["session_id"], i, "dataset")["preview"]
        for i in ev["output_dataset_ids"]
    ]
    return dict(
        status=ev["status"],
        evidence_id=ev["evidence_id"],
        output_dataset_ids=ev["output_dataset_ids"],
        artifact_ids=ev["artifact_ids"],
        artifacts=[
            store.get(ev["session_id"], identity, "artifact")
            for identity in ev["artifact_ids"]
        ],
        result=ev["result"],
        datasets=[
            store.get(ev["session_id"], identity, "dataset")
            for identity in dict.fromkeys(
                ev["input_dataset_ids"] + ev["output_dataset_ids"]
            )
        ],
        preview=previews,
        warnings=ev["warnings"],
        error=ev["error"],
    )


class AnalysisRuntime:
    def __init__(self, store, session, run, config, resources_factory, executor):
        self.store, self.session, self.run = store, session, run
        self.config, self.resources_factory, self.executor = (
            config,
            resources_factory,
            executor,
        )
        self.cancel_event = None
        self.remaining = lambda: config.run_seconds

    def consume(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ValueError("cancelled")
        if self.remaining() <= 0:
            raise ExecutionBudgetExceeded("budget_exhausted:wall_clock")
        try:
            self.store.consume(
                self.session, self.run, "tools", self.config.max_tool_calls
            )
        except ValueError as exc:
            raise ExecutionBudgetExceeded(str(exc)) from exc

    def abort_pending(self, reason):
        for evidence in self.store.list(self.session, self.run, "evidence"):
            if evidence["status"] == "running":
                evidence.update(status="error", error=reason[:2000])
                self.store.finish(evidence)

    def execute(self, name, ids, parameters, operation, source=None):
        parameters = dict(parameters)
        if source is not None:
            parameters["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        kind = "python" if source is not None else "analysis_tool"
        inputs = [self.store.dataset(self.session, identity) for identity in ids]
        parents = [meta["source_evidence_id"] for meta, _ in inputs]
        ev, fresh = self.store.begin(
            ExecutionEvidence(
                session_id=self.session,
                run_id=self.run,
                operation_id=operation,
                kind=kind,
                tool_name=name,
                input_dataset_ids=ids,
                parent_evidence_ids=parents,
                parameters=parameters,
            )
        )
        if not fresh:
            if ev["status"] == "running":
                if (
                    self.executor.recover(ev["parameters"].get("container_id"))
                    == "active"
                ):
                    return response(ev, self.store)
                ev["warnings"].append(
                    "宿主中断后的日志不可完整恢复；旧容器已终止，不重跑原操作"
                )
                ev.update(
                    status="error",
                    error="execution_interrupted: explicit new call required",
                )
                self.store.finish(ev)
            return response(ev, self.store)
        try:
            self.consume()
            if len(encode(parameters)) > 65536 or (
                source is not None and len(source.encode()) > 131072
            ):
                raise ValueError("tool_input_too_large")
            if source is None:
                if "metric" in parameters:
                    declared = parameters.get("unit", "")
                    known = {
                        meta["units"].get(parameters["metric"]) for meta, _ in inputs
                    }
                    if None in known or known != {declared}:
                        raise ValueError("input_metric_unit_mismatch")
                ev["result"], warnings = calculate(
                    name, [s for _, s in inputs], parameters
                )
                ev["warnings"] = warnings
            else:
                failures = [
                    e
                    for e in self.store.list(self.session, self.run, "evidence")
                    if e["kind"] == "python" and e["status"] in ("error", "timeout")
                ]
                if len(failures) > self.config.max_repairs:
                    raise ValueError("python_repair_budget_exhausted")
                source_id = self.store.artifact(
                    self.session, self.run, source.encode(), "text/plain", "analysis.py"
                )
                ev["artifact_ids"].append(source_id)
                ev["parameters"]["source_artifact_id"] = source_id
                ev["parameters"]["source_sha256"] = self.store.get(
                    self.session, source_id, "artifact"
                )["sha256"]
                if failures:
                    ev["parent_evidence_ids"].append(failures[-1]["evidence_id"])
                # Link source and inputs before execution so a crash preserves the attempted program.
                self.store.save(
                    "evidence", ev["evidence_id"], self.session, self.run, ev
                )

                def started(cid, image_digest, input_hashes):
                    ev["parameters"].update(
                        container_id=cid,
                        image_digest=image_digest,
                        input_hashes=input_hashes,
                    )
                    self.store.save(
                        "evidence", ev["evidence_id"], self.session, self.run, ev
                    )

                output = self.executor.run(
                    source,
                    {identity: s for identity, (_, s) in zip(ids, inputs)},
                    self.cancel_event,
                    self.remaining(),
                    on_started=started,
                )
                for channel in ("stdout", "stderr"):
                    log = self.store.artifact(
                        self.session,
                        self.run,
                        output[channel].encode(),
                        "text/plain",
                        channel + ".txt",
                    )
                    ev["artifact_ids"].append(log)
                    ev["parameters"][channel + "_artifact_id"] = log
                ev["parameters"].update(
                    {
                        k: v
                        for k, v in output.items()
                        if k
                        not in (
                            "stdout",
                            "stderr",
                            "result",
                            "files",
                            "status",
                            "error",
                        )
                    }
                )
                ev.update(
                    status=output["status"],
                    error=output["error"],
                    result=output["result"],
                )
                for name, content in output["files"]:
                    if name.endswith(".dataset.json"):
                        snapshot = json.loads(content)
                        ds = self.store.save_dataset(
                            self.session,
                            self.run,
                            ev["evidence_id"],
                            snapshot["columns"],
                            snapshot["rows"],
                            parents=ids,
                            truncated=any(m["source_truncated"] for m, _ in inputs),
                            coverage={"derived_from": ids},
                            units=snapshot.get("units", {}),
                        )
                        ev["output_dataset_ids"].append(ds["dataset_id"])
                    else:
                        ev["artifact_ids"].append(
                            self.store.artifact(
                                self.session, self.run, content, "image/png", name
                            )
                        )
                if ev["status"] != "success":
                    self.store.finish(ev)
                    return response(ev, self.store)
            if len(encode(ev["result"])) > self.config.output_bytes:
                raise ValueError("result_output_limit")
            for meta, _ in inputs:
                ev["warnings"].extend(meta["quality_warnings"])
                if meta["source_truncated"] or meta["coverage"].get("sql_limit"):
                    ev["warnings"].append("输入不完整；仅可用于显式限定的样本结论")
            ev["status"] = "success"
        except Exception as exc:
            ev.update(
                status="cancelled" if str(exc) == "cancelled" else "error",
                error=str(exc)[:2000],
            )
        self.store.finish(ev)
        return response(ev, self.store)

    def query(self, question, coverage, units, operation, config):
        from mini.agents.text2sql_graph import build_text2sql_graph, new_text2sql_turn

        # An envelope survives interruption; its subgraph identity is supplied by LangGraph's tool-call task.
        saved, fresh = self.store.begin(
            ExecutionEvidence(
                session_id=self.session,
                run_id=self.run,
                operation_id=operation,
                kind="sql",
                tool_name="query_dataset",
                parameters={"question": question, "coverage": coverage, "units": units},
            )
        )
        if not fresh and saved["status"] != "running":
            return response(saved, self.store)
        if fresh:
            self.consume()
        resources = self.resources_factory()
        runtime = self

        class RecordingAdapter:
            dialect = resources.adapter.dialect

            def list_columns(self, tables):
                return resources.adapter.list_columns(tables)

            def execute_readonly(self, sql, row_limit=100, timeout_seconds=2):
                previous = runtime.store.list(runtime.session, runtime.run, "evidence")
                attempts = [
                    e
                    for e in previous
                    if e["operation_id"].startswith(operation + ":sql:")
                ]
                matching = [e for e in attempts if e["parameters"].get("sql") == sql]
                if matching:
                    cached = matching[-1]
                    if cached["status"] == "success":
                        runtime.store.evidence(runtime.session, cached["evidence_id"])
                        meta, snapshot = runtime.store.dataset(
                            runtime.session, cached["output_dataset_ids"][0]
                        )
                        return ExecutionOutcome(
                            {
                                "status": "success",
                                "columns": snapshot["columns"],
                                "rows": snapshot["rows"],
                                "truncated": meta["source_truncated"],
                                "error": None,
                            }
                        )
                    if cached["status"] == "running":
                        raise ValueError(
                            "sql_execution_interrupted: explicit new operation required"
                        )
                    return ExecutionOutcome(
                        empty_result(
                            "timeout" if cached["status"] == "timeout" else "error",
                            cached["error"],
                        ),
                        repairable=cached["parameters"].get("repairable", False),
                    )
                attempt = len(attempts) + 1
                ev, _ = runtime.store.begin(
                    ExecutionEvidence(
                        session_id=runtime.session,
                        run_id=runtime.run,
                        operation_id=f"{operation}:sql:{attempt}",
                        attempt=attempt,
                        kind="sql",
                        tool_name="execute_readonly",
                        parent_evidence_ids=[attempts[-1]["evidence_id"]]
                        if attempts
                        else [],
                        parameters=dict(
                            sql=sql,
                            sql_parameters={},
                            datasource=self.dialect,
                            question=question,
                            coverage=coverage,
                            row_limit=runtime.config.max_query_rows,
                        ),
                    )
                )
                try:
                    runtime.consume()
                    outcome = resources.adapter.execute_readonly(
                        sql,
                        row_limit=runtime.config.max_query_rows,
                        timeout_seconds=timeout_seconds,
                    )
                    result = outcome.result
                    ev["parameters"]["repairable"] = outcome.repairable
                    ev["result"] = dict(
                        columns=result["columns"],
                        row_count=len(result["rows"]),
                        truncated=result["truncated"],
                    )
                    ev["status"] = (
                        "success"
                        if result["status"] == "success"
                        else "timeout"
                        if result["status"] == "timeout"
                        else "error"
                    )
                    ev["error"] = result.get("error")
                    if ev["status"] == "success":
                        if len(encode(result)) > runtime.config.max_query_bytes:
                            raise ValueError("query_byte_limit: aggregate in SQL")
                        tree = sqlglot.parse_one(sql, read=self.dialect)
                        limited = any(isinstance(n, exp.Limit) for n in tree.walk())
                        warnings = ["SQL 口径、过滤和关联粒度须人工确认"]
                        if limited:
                            warnings.append("SQL 包含主动 LIMIT，不能证明总体完整")
                        ds = runtime.store.save_dataset(
                            runtime.session,
                            runtime.run,
                            ev["evidence_id"],
                            result["columns"],
                            result["rows"],
                            truncated=result["truncated"],
                            coverage={
                                **coverage,
                                "sql_limit": limited,
                                "question": question,
                            },
                            units=units,
                            warnings=warnings,
                        )
                        ev["output_dataset_ids"] = [ds["dataset_id"]]
                        ev["artifact_ids"] = [ds["data_artifact_id"]]
                except ExecutionBudgetExceeded as exc:
                    ev.update(status="error", error=str(exc))
                    runtime.store.finish(ev)
                    raise
                except Exception as exc:
                    ev.update(status="error", error=str(exc))
                    outcome = ExecutionOutcome(empty_result("error", str(exc)))
                runtime.store.finish(ev)
                return outcome

        graph = build_text2sql_graph(
            replace(resources, adapter=RecordingAdapter()), None
        )
        context = encode(
            {
                "task": self.store.get(self.session, self.run, "run")["task"],
                "coverage": coverage,
                "expected_result_units": units,
            }
        ).decode()
        result = graph.invoke(
            new_text2sql_turn(question, context=context), config=config
        )
        if result.get("__interrupt__"):
            # Never turn a nested pause into a successful tool result.
            raise GraphInterrupt(result["__interrupt__"])
        executions = [
            e
            for e in self.store.list(self.session, self.run, "evidence")
            if e["operation_id"].startswith(operation + ":sql:")
        ]
        saved["parent_evidence_ids"] = [e["evidence_id"] for e in executions]
        saved["status"] = (
            "success"
            if result.get("status") == "success"
            else "cancelled"
            if result.get("session_closed")
            else "error"
        )
        saved["error"] = result.get("error") or None
        if saved["status"] == "success" and executions:
            saved["output_dataset_ids"] = executions[-1]["output_dataset_ids"]
            saved["result"] = executions[-1]["result"]
            saved["parent_evidence_ids"] = [executions[-1]["evidence_id"]]
        self.store.finish(saved)
        return response(saved, self.store)

    def operation(self, tool_call_id):
        return self.store.operation_id(self.session, self.run, tool_call_id)

    def tools(self):
        from langchain_core.runnables import RunnableConfig

        runtime = self

        @tool
        def query_dataset(
            question: str,
            coverage: dict,
            units: dict[str, str],
            tool_call_id: Annotated[str, InjectedToolCallId],
            config: RunnableConfig,
        ) -> dict:
            """查询本步骤问题，复用可暂停的 Text2SQL。coverage 记录口径；units 为列单位。"""
            return runtime.query(
                question, coverage, units, runtime.operation(tool_call_id), config
            )

        output = [query_dataset]

        def make(name):
            @tool(name)
            def fixed(
                dataset_ids: list[str],
                parameters: dict,
                tool_call_id: Annotated[str, InjectedToolCallId],
            ) -> dict:
                """使用已存数据集执行固定统计。两期数据顺序 current,baseline；参数显式提供。"""
                return runtime.execute(
                    name, dataset_ids, parameters, runtime.operation(tool_call_id)
                )

            descriptions = {
                "inspect_dataset": "只接收一个数据集。检查列、缺失、重复；返回 datasets 内的 schema/coverage/units/完整性和 result 中的统计。parameters 可为空。",
                "describe_dataset": "只接收一个数据集。parameters: columns 数值列列表, group_by 可选, ddof=1。结果 result.groups[i].columns[列名].sum/mean/count 等。",
                "compare_periods": "接收两个数据集，顺序当前、基期。parameters: metric, dimensions 列表, unit 原单位, aggregation=sum, missing_groups=reject 或显式 zero。结果 result.current,baseline,delta,change_rate,change_percent,groups。",
                "contribution_analysis": "接收两个数据集，顺序当前、基期。parameters: metric, dimensions=[唯一维度], unit, additive=true, aggregation=sum, missing_groups=reject 或 zero。结果 /result/delta、/result/change_rate、/result/groups/0/contribution 等；贡献度单位 ratio。",
                "analyze_trend": "只接收一个数据集。parameters: time_column,metric,unit,window,timezone,granularity=day,duplicate_dates=reject 或 sum。",
                "detect_anomalies": "只接收一个数据集。parameters: time_column,metric,unit,window,method=zscore,threshold。仅用历史窗口；score/threshold 单位 z-score。",
                "correlation_analysis": "只接收一个数据集。parameters: columns 至少两列, alignment_keys 唯一键列表, method=pearson 或 spearman, missing=pairwise_drop。相关系数单位 coefficient。",
            }
            fixed.description = descriptions[name]
            return fixed

        output.extend(make(name) for name in NAMES)

        @tool
        def run_python_analysis(
            source: str,
            dataset_ids: list[str],
            purpose: str,
            result_units: dict[str, str],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> dict:
            """在禁网 Docker 内实际执行 Python；使用 analysis_sdk 加载数据、emit_result、保存表格/PNG。result_units 为 /result/... 指针到单位。"""
            return runtime.execute(
                "run_python_analysis",
                dataset_ids,
                dict(purpose=purpose, result_units=result_units),
                runtime.operation(tool_call_id),
                source,
            )

        @tool
        def get_evidence(evidence_id: str) -> dict:
            """读取本会话执行证据，包括失败原因与源码/日志产物编号。"""
            runtime.consume()
            item = runtime.store.get(runtime.session, evidence_id, "evidence")
            item["text_artifacts"] = {}
            for identity in item["artifact_ids"]:
                meta, content = runtime.store.read_artifact(runtime.session, identity)
                if meta["media_type"] == "text/plain":
                    item["text_artifacts"][identity] = content[
                        : runtime.config.log_bytes
                    ].decode(errors="replace")
            return item

        @tool
        def submit_analysis_report(
            claims: list[ClaimInput],
            limitations: list[str],
            unresolved_questions: list[str],
        ) -> dict:
            """校验并保存结构化报告。fact 必须 values: name,value,unit,evidence_id,result_pointer,display_precision；不传验证状态。事实文本由服务端渲染。"""
            runtime.consume()
            report = submit_report(
                runtime.store,
                runtime.session,
                runtime.run,
                [claim.model_dump() for claim in claims],
                limitations,
                unresolved_questions,
            )
            return dict(
                report_id=report["report_id"],
                status=report["status"],
                validation_errors=report["validation_errors"],
                claims=report["claims"],
            )

        return [*output, run_python_analysis, get_evidence, submit_analysis_report]
