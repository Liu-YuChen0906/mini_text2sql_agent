"""执行成功后的 SQL 质量评分，供低置信度人工审核使用。"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from mini.query.schema_linking import _model_json


@dataclass(frozen=True)
class QualityScore:
    """类用途：保存 SQL 质量评分及模型给出的原因。

    属性：score 是 0 到 1 的分数；reasons 是供人工审核查看的原因列表。
    """

    score: float
    reasons: list[str]
    failed_checks: tuple[str, ...] = ()


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
        '{"score":0到1之间的数字,"reasons":["简短原因"],'
        '"checks":{"select_columns":true,"where":true,"calc":true,"subquery":true,"joins":true,"exec_result":true}}。'
        "六项必须逐项返回布尔值：输出字段、筛选条件、计算与聚合粒度、子查询语义、连接、结果合理性。"
        "任何一项不正确就标 false 并说明具体修正方法；无法确认也标 false。"
        "等价 JOIN/EXISTS/IN 写法不扣分，除非改变粒度。"
        "重点检查一对多重复计数、外连接遗漏零记录、日期范围、NULL、状态值与用户修正。"
        "比较用户问题、表字段含义和 SQL：检查指标是否真能由字段计算、聚合口径、连接、筛选及排序。"
        "SQL 能执行成功不代表业务含义正确；编号求和不能代表金额。"
        "不确定时给低分并写明原因，原因必须清晰明了，对非技术人员友好。",
        f"当前业务时间：{datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')}\n问题：{question}\n结构：{schema}\nSQL：{sql}\n"
        f"结果列：{result.get('columns', [])}\n前五行：{preview}",
    )
    value = answer.get("score")
    reasons = answer.get("reasons")
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError("SQL 质量评分必须是 0 到 1 的数字。")
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise ValueError("SQL 质量评分 reasons 必须是字符串数组。")
    keys = ("select_columns", "where", "calc", "subquery", "joins", "exec_result")
    checks = answer.get("checks")
    if checks is None:
        # Older/nonconforming responses must never bypass review on a self-reported score.
        return QualityScore(min(float(value), 0.69), [*reasons, "评分未提供逐项校验，需人工确认。"])
    if not isinstance(checks, dict) or any(type(checks.get(key)) is not bool for key in keys):
        raise ValueError("SQL 质量 checks 必须包含六项布尔值。")
    failed = tuple(key for key in keys if not checks[key])
    return QualityScore(min(float(value), 0.69) if failed else float(value), reasons, failed)
