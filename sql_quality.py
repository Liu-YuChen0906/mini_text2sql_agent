"""Post-execution SQL quality evaluation for the optional human review route."""

from dataclasses import dataclass
from typing import Any

from schema_linking import _model_json


@dataclass(frozen=True)
class QualityScore:
    score: float
    reasons: list[str]


def score_sql(llm: Any, question: str, sql: str, schema: str, result: dict[str, Any]) -> QualityScore:
    """Evaluate semantic fit, not merely whether SQLite accepted the query."""
    preview = result.get("rows", [])[:5]
    answer = _model_json(
        llm,
        "你是 Text2SQL 审核员。仅返回 JSON："
        '{"score":0到1之间的数字,"reasons":["简短原因"]}。'
        "比较用户问题、表字段含义和 SQL：检查指标是否真能由字段计算、聚合口径、连接、筛选及排序。"
        "SQL 能执行成功不代表业务含义正确；编号求和不能代表金额。"
        "不确定时给低分并写明原因。",
        f"问题：{question}\n结构：{schema}\nSQL：{sql}\n"
        f"结果列：{result.get('columns', [])}\n前五行：{preview}",
    )
    value = answer.get("score")
    reasons = answer.get("reasons")
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError("SQL 质量评分必须是 0 到 1 的数字。")
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise ValueError("SQL 质量评分 reasons 必须是字符串数组。")
    return QualityScore(float(value), reasons)
