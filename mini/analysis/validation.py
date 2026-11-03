"""Deterministic reference and formula checks. No free-form factual prose is published."""

import math
import sqlite3

from mini.analysis.models import AnalysisReport, AnalysisTask, Claim, ClaimInput
from mini.analysis.tools.statistics import calculate

BOUNDARY = "校验仅证明引用和规定计算可核对，不证明 SQL 业务语义、输入真实性或因果关系。"


def pointer(document, path):
    if not path.startswith("/result/"):
        raise ValueError("result_pointer_required")
    current = document
    for token in path.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        current = (
            current[int(key)]
            if isinstance(current, list) and key.isdigit()
            else current[key]
        )
    return current


def validate_claim(store, session, task, raw):
    incoming = ClaimInput.model_validate(raw)
    claim = Claim(**incoming.model_dump())
    if claim.kind == "fact" and not claim.values:
        raise ValueError("facts_require_structured_values")
    refs = set(claim.evidence_refs) | {v.evidence_id for v in claim.values}
    if not refs:
        raise ValueError("evidence_required")
    supported = False
    evidence = {}
    for ref in refs:
        ev = store.evidence(session, ref)
        evidence[ref] = ev
        chain = [ev]
        visited = set()
        while chain:
            ancestor = chain.pop()
            if ancestor["evidence_id"] in visited:
                continue
            visited.add(ancestor["evidence_id"])
            if ancestor["kind"] == "python":
                supported = True
            for ds_id in ancestor["input_dataset_ids"] + ancestor["output_dataset_ids"]:
                ds_meta, _ = store.dataset(session, ds_id)
                if ds_meta["source_truncated"] or ds_meta["coverage"].get("sql_limit"):
                    if claim.scope != "sample" or not claim.limitations:
                        raise ValueError("incomplete_data_cannot_support_population")
                if ds_meta["source_evidence_id"] != ancestor["evidence_id"]:
                    chain.append(
                        store.get(session, ds_meta["source_evidence_id"], "evidence")
                    )
        snapshots = []
        for ds in ev["input_dataset_ids"] + ev["output_dataset_ids"]:
            meta, snapshot = store.dataset(session, ds)
            if meta["source_truncated"] or meta["coverage"].get("sql_limit"):
                if claim.scope != "sample" or not claim.limitations:
                    raise ValueError("incomplete_data_cannot_support_population")
            snapshots.append(snapshot)
        if ev["kind"] == "analysis_tool":
            recomputed, _ = calculate(
                ev["tool_name"],
                snapshots[: len(ev["input_dataset_ids"])],
                ev["parameters"],
            )
            if recomputed != ev["result"]:
                raise ValueError("independent_recalculation_mismatch")
        if ev["kind"] == "python":
            if (
                not ev["parameters"].get("source_artifact_id")
                or not ev["parameters"].get("image_digest")
                or not ev["input_dataset_ids"]
            ):
                raise ValueError("incomplete_python_chain")
            source_meta, _ = store.read_artifact(
                session, ev["parameters"]["source_artifact_id"]
            )
            if (
                source_meta["sha256"] != ev["parameters"].get("source_sha256")
                or ev["parameters"].get("exit_code") != 0
            ):
                raise ValueError("python_source_or_exit_mismatch")
            for channel in ("stdout", "stderr"):
                store.read_artifact(session, ev["parameters"][channel + "_artifact_id"])
            for dataset_id in ev["input_dataset_ids"]:
                meta, _ = store.dataset(session, dataset_id)
                if meta["content_sha256"] != ev["parameters"].get(
                    "input_hashes", {}
                ).get(dataset_id):
                    raise ValueError("python_input_hash_mismatch")
            supported = True
    for value in claim.values:
        ev = evidence[value.evidence_id]
        actual = pointer(ev, value.result_pointer)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(actual)
        ):
            raise ValueError("pointer_must_resolve_finite_number")
        if not (
            math.isclose(actual, value.value, rel_tol=0.0, abs_tol=1e-10)
            or round(actual, value.display_precision) == value.value
        ):
            raise ValueError("numeric_value_mismatch")
        units = ev["parameters"].get("result_units", {})
        unit = units.get(value.result_pointer)
        if unit is None:
            # Only fixed suffix semantics may supply derived units.
            suffix = value.result_pointer.rsplit("/", 1)[-1]
            if suffix in {
                "change_rate",
                "contribution",
                "contribution_sum",
            }:
                unit = "ratio"
            elif suffix == "coefficient":
                unit = "coefficient"
            elif suffix in {"score", "threshold"}:
                unit = "z-score"
            elif suffix == "change_percent":
                unit = "%"
            elif suffix in {
                "count",
                "missing",
                "row_count",
                "sample_count",
                "duplicate_rows",
                "ddof",
                "window",
            }:
                unit = "count"
            else:
                unit = ev["parameters"].get("unit", "")
        tokens = value.result_pointer.split("/")
        if (
            ev["kind"] == "analysis_tool"
            and ev["tool_name"] == "describe_dataset"
            and len(tokens) == 7
            and tokens[4] == "columns"
        ):
            column, stat = tokens[5], tokens[6]
            if stat in {"count", "missing"}:
                unit = "count"
            else:
                source_meta, _ = store.dataset(session, ev["input_dataset_ids"][0])
                unit = source_meta["units"].get(column, "")
        if ev["tool_name"] == "inspect_dataset" and value.result_pointer.startswith(
            "/result/missing/"
        ):
            unit = "count"
        if (
            ev["tool_name"] in {"compare_periods", "contribution_analysis"}
            and value.result_pointer.rsplit("/", 1)[-1]
            in {"delta", "reconciliation_error"}
            and ev["parameters"].get("unit") == "%"
        ):
            unit = "百分点"
        if value.unit != unit:
            raise ValueError("unit_mismatch")
    claim.evidence_refs = sorted(refs)
    claim.verification_status = (
        ("supported" if supported else "verified")
        if claim.kind == "fact"
        else "unverified"
    )
    if claim.kind == "fact":
        parts = []
        for v in claim.values:
            label = v.result_pointer.removeprefix("/result/")
            labels = {
                "current": "当前值",
                "baseline": "基期值",
                "delta": "绝对变化",
                "change_rate": "相对变化",
                "change_percent": "变化百分比",
                "contribution": "变化贡献度",
                "contribution_sum": "贡献度合计",
                "reconciliation_error": "对账误差",
                "mean": "均值",
                "median": "中位数",
                "sum": "合计",
                "min": "最小值",
                "max": "最大值",
                "std": "标准差",
                "count": "有效数量",
                "missing": "缺失数量",
                "row_count": "返回行数",
                "coefficient": "相关系数",
                "score": "异常分数",
            }
            ev = evidence[v.evidence_id]
            tokens = label.split("/")
            if len(tokens) >= 3 and tokens[0] == "groups" and tokens[1].isdigit():
                group = ev["result"]["groups"][int(tokens[1])].get("group", [])
                metric = ev["parameters"].get("metric", "")
                if len(tokens) > 3 and tokens[2] == "columns":
                    metric = tokens[3]
                label = f"分组 {group} · {metric} {labels.get(tokens[-1], tokens[-1])}"
            else:
                label = labels.get(label, label)
            display = f"{v.value:.{v.display_precision}f} {v.unit}"
            if v.unit == "ratio":
                display += f"（{pointer(ev, v.result_pointer) * 100:.{v.display_precision}f}%）"
            parts.append(f"{label} = {display}")
        scope = "样本范围" if claim.scope == "sample" else "查询范围"
        time_scope = (
            f"[{task.time_range['start']}, {task.time_range['end']})"
            if task.time_range
            else "时间范围未指定"
        )
        if task.comparison_range:
            time_scope += f"；对比 [{task.comparison_range['start']}, {task.comparison_range['end']})"
        claim.text = f"{scope} {time_scope}（{task.timezone}）：" + "；".join(parts)
    elif claim.kind == "hypothesis":
        claim.text = "待验证：" + claim.text
    return claim


def submit_report(
    store, session, run, claims, limitations=None, unresolved=None, status="success"
):
    run_record = store.get(session, run, "run")
    task = AnalysisTask.model_validate(run_record["task"])
    accepted, errors = [], []
    fact_aliases = {}
    for i, raw in enumerate(claims):
        try:
            claim = validate_claim(store, session, task, raw)
            accepted.append(claim)
            if claim.kind == "fact":
                fact_aliases[f"fact:{i}"] = claim.claim_id
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            errors.append(f"claim[{i}]: {exc}")
    facts = {c.claim_id for c in accepted if c.kind == "fact"}
    published = []
    for claim in accepted:
        claim.fact_refs = [fact_aliases.get(ref, ref) for ref in claim.fact_refs]
        if claim.kind == "interpretation" and (
            not claim.fact_refs or not set(claim.fact_refs) <= facts
        ):
            errors.append(
                f"{claim.claim_id}: interpretation_requires_accepted_fact_refs"
            )
        else:
            published.append(claim)
    if status == "success" and (errors or not published):
        status = "partial" if published else "failed"
    artifacts = set()
    observed_warnings = []
    has_python = False
    for claim in published:
        for ref in claim.evidence_refs:
            ev = store.evidence(session, ref)
            artifacts.update(ev["artifact_ids"])
            has_python = has_python or ev["kind"] == "python"
            observed_warnings.extend(ev["warnings"])
            for identity in ev["input_dataset_ids"] + ev["output_dataset_ids"]:
                meta, _ = store.dataset(session, identity)
                artifacts.add(meta["data_artifact_id"])
                if meta["run_id"] != run:
                    observed_warnings.append(
                        f"历史数据 {identity}；原快照时间 {meta['created_at']}，非本轮新查询"
                    )
    if "python" in task.requested_outputs and not has_python:
        errors.append("required_output_missing:python")
    if "png" in task.requested_outputs and not any(
        store.get(session, a, "artifact")["media_type"] == "image/png"
        for a in artifacts
    ):
        errors.append("required_output_missing:png")
    if status == "success" and (errors or unresolved):
        status = "partial" if published else "failed"
    report = AnalysisReport(
        session_id=session,
        run_id=run,
        status=status,
        task=task,
        claims=published,
        artifact_ids=sorted(artifacts),
        validation_errors=errors,
        unresolved_questions=unresolved or [],
        limitations=list(
            dict.fromkeys(
                [BOUNDARY, *task.assumptions, *(limitations or []), *observed_warnings]
            )
        ),
    )
    store.save("report", report.report_id, session, run, report.model_dump())
    return report.model_dump()


def read_report(store, session, report):
    """Recheck artifacts on every display: a deleted/tampered snapshot revokes publication."""
    output = dict(report)
    claims, errors = [], list(report["validation_errors"])
    for claim in report["claims"]:
        try:
            for ref in claim["evidence_refs"]:
                store.evidence(session, ref)
            claims.append(claim)
        except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
            errors.append(f"{claim['claim_id']}: 证据不可用 ({exc})")
    fact_ids = {c["claim_id"] for c in claims if c["kind"] == "fact"}
    dependent = [
        c
        for c in claims
        if c["kind"] != "interpretation" or set(c["fact_refs"]) <= fact_ids
    ]
    if len(dependent) != len(claims):
        errors.append("解释所引用的事实已不可用")
    claims = dependent
    output.update(claims=claims, validation_errors=errors)
    if len(claims) != len(report["claims"]):
        output["status"] = "partial" if claims else "failed"
    return output
