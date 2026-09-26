"""执行成功后的 SQL 质量评分，供低置信度人工审核使用。"""

from dataclasses import dataclass
from typing import Any

from schema_linking import _model_json


@dataclass(frozen=True)
class QualityScore:
    """类用途：保存 SQL 质量评分及模型给出的原因。

    属性：score 是 0 到 1 的分数；reasons 是供人工审核查看的原因列表。
    """

    score: float
    reasons: list[str]


def score_sql(llm: Any, question: str, sql: str, schema: str, result: dict[str, Any]) -> QualityScore:
    """用途：让模型评估已执行 SQL 是否符合问题的业务含义。

    参数输入：llm 是聊天模型；question 是改写问题；sql 是已执行查询；
        schema 是生成时使用的结构上下文；result 是执行器的结果字典。
    输出：QualityScore，含 0 到 1 的评分和原因；只给模型前五行结果预览。
    异常：模型返回的分数越界、原因类型不符或 JSON 无效时抛 ValueError。
    """
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
