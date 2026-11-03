"""Validate model delegation arguments and repair them before starting any data work."""

import json

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from mini.analysis.models import AnalysisTask

REPAIR_PROMPT = """上一条分析委派的参数不符合协议。请根据用户原始问题和错误修正参数，只调用一次 data_analysis。
metrics 必须是对象数组，每项 name、definition、unit，不能是字符串数组。
time_range 只允许 start、end，表示本期；comparison_range 只允许 start、end，表示基期。
日期均为实际 ISO8601 日期、左闭右开；不指定范围时用空对象。timezone、granularity 放在 task 外层字段中，不放进日期对象。
保留用户原始统计口径，不增加筛选条件。通常的变化贡献度是分组增减量/总体增减量，不是除以基期总量。
不要查询数据库，不要调用 run_text2sql，不要给出任何业务数值或结论。"""


def _errors(exc):
    if isinstance(exc, ValidationError):
        return [
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_input=False)
        ][:8]
    return [str(exc)[:500]]


def prepare_task(model, messages, question, *, max_repairs=2):
    """Return task/message updates/errors; bad model arguments never escape as a web 500."""
    candidate = messages[-1]
    updates, errors = [], []
    for attempt in range(max_repairs + 1):
        try:
            if (
                not isinstance(candidate, AIMessage)
                or len(candidate.tool_calls) != 1
                or candidate.tool_calls[0]["name"] != "data_analysis"
            ):
                raise ValueError("必须调用一次 data_analysis 以修正分析任务")
            raw = candidate.tool_calls[0].get("args", {}).get("task")
            if not isinstance(raw, dict):
                raise ValueError("task 必须是分析任务对象")
            # The exact user question remains authoritative over a model's rewritten summary.
            task = AnalysisTask.model_validate({**raw, "question": question})
            return task, updates, []
        except (ValidationError, ValueError, TypeError) as exc:
            errors = _errors(exc)
        feedback = json.dumps(
            {"status": "invalid_analysis_arguments", "errors": errors},
            ensure_ascii=False,
        )
        if isinstance(candidate, AIMessage) and candidate.tool_calls:
            updates.extend(
                ToolMessage(
                    content=feedback, tool_call_id=call["id"], name=call["name"]
                )
                for call in candidate.tool_calls
            )
        else:
            updates.append(SystemMessage(content=feedback))
        if attempt == max_repairs:
            break
        try:
            candidate = model.invoke(
                [SystemMessage(content=REPAIR_PROMPT), *messages[-16:], *updates]
            )
        except Exception as exc:
            errors = [f"参数修正请求未完成：{type(exc).__name__}"]
            break
        if isinstance(candidate, AIMessage):
            updates.append(candidate)
    return None, updates, errors
