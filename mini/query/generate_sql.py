from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

def build_prompt() -> ChatPromptTemplate:
    """用途：构造要求模型只生成一条只读查询的聊天提示模板。

    参数输入：无；模板本身不读取配置或数据库。
    输出：
        ChatPromptTemplate：系统消息要求生成一条只读 SELECT，用户消息放置
            问题；调用时需填 dialect（str，SQL 方言）、schema（str，结构上下文）
            和 question（str，自然语言问题）三个变量。
    """
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是专业 SQL 工程师。只生成一条只读 {dialect} SELECT / WITH 查询。"
                "只能使用结构中列出的表、字段和明确定义的派生指标。"
                "先确定指标、结果粒度、过滤、关联路径和排序，再生成 SQL。"
                "无法计算的指标或没有依据的业务映射必须返回 NULL，不得用编号、数量冒充金额，"
                "不得猜测字段、状态值或连接关系。字段展示名可作为结果列别名。"
                "一对多连接不能放大计数或求和：按目标实体去重，或先聚合再连接；"
                "存在性过滤优先 EXISTS；没有记录的实体使用 LEFT JOIN 并 COUNT(子表主键)，"
                "排除 NULL 时注意 NOT IN 的语义。比例使用浮点数并防止除零。"
                "保留所有用户条件；修正意见优先于旧 SQL，不要照搬旧 SQL 的错误。"
                "示例仅供语法和计算方式参考，不得复制示例中的日期、ID、状态或 LIMIT。"
                "只回答当前问题，不执行历史中未请求的任务。"
                "除非结构要求，引号不要用于表名和字段名。只返回 SQL 或 NULL，不含解释。"
                "\n当前业务时间（Asia/Shanghai）：{current_time}"
                "\n方言规则：{dialect_rules}\n\nSchema:\n{schema}",
            ),
            ("human", "{question}"),
        ]
    )


def generate_sql(question: str, schema: str, llm: Any, dialect: str = "sqlite") -> str:
    """用途：把问题和整理好的结构信息交给模型，生成 SQL 文本。

    参数输入：
        question（str）：需要转成 SQL 的自然语言问题；当前主流程传入改写后的问题。
        schema（str）：提供给模型的结构上下文；当前主流程由 sql_context 生成，
            内容含选中表、字段说明、规则和相关示例，并非必须是 CREATE TABLE。
        llm（Any）：已创建的 LangChain 聊天模型；实现与提示模板相连的
            Runnable 接口，由主流程显式传入，不在此函数内读取配置。
    输出：
        str：模型生成的文本经过 StrOutputParser 转为字符串，再移除 ```sql
            和 ``` 标记并去掉首尾空白。此函数不校验 SQL，也不执行 SQL。
    """
    chain = build_prompt() | llm | StrOutputParser()
    rules = {
        "sqlite": "用 strftime 格式化日期，date/datetime 做日期计算；月份用 %Y-%m，不能用 DATE_TRUNC 或 INTERVAL。",
        "postgres": "用 date_trunc、EXTRACT 和 INTERVAL；不要用 strftime。未加引号的标识符会转小写。",
    }.get(dialect, f"遵循 {dialect} 的日期、类型转换和标识符规则，不混用其他方言函数。")
    content = chain.invoke({
        "dialect": dialect, "schema": schema, "question": question,
        "current_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "dialect_rules": rules,
    })
    return content.replace("```sql", "").replace("```", "").strip()


def regenerate_sql(question: str, schema: str, previous_sql: str, error: str, llm: Any, dialect: str = "sqlite") -> str:
    """用途：把上次 SQL 和数据库错误反馈给模型，要求按原问题重新生成查询。

    参数输入：question 为改写后的问题，schema 为已选表的结构上下文，
        previous_sql 为失败的 SQL，error 为执行错误，llm 为复用的聊天模型。
    输出：str，经过 generate_sql 清理代码围栏后的新 SQL；此处不执行或校验。
    """
    feedback = (
        f"{question}\n\nThe previous {dialect} query failed. Correct it and return SQL only."
        f"\nPrevious SQL: {previous_sql}\n{dialect} error: {error}"
    )
    return generate_sql(feedback, schema, llm, dialect)


def revise_sql(question: str, schema: str, previous_sql: str, human_feedback: str, llm: Any, dialect: str = "sqlite") -> str:
    """用途：根据人工自然语言反馈重新生成 SQL，不直接使用用户提供的 SQL。

    参数输入：question 是改写问题，schema 是当前表结构上下文，previous_sql
        是待修改的 SQL，human_feedback 是用户对结果或 SQL 的自然语言说明，
        llm 是复用的聊天模型。
    输出：str，由模型重新生成并清理代码围栏的 SQL；此处不执行查询。
    """
    revision_request = (
        f"{question}\n\nThe previous query needs revision based on human feedback. "
        "Recheck every requirement against the schema; do not merely patch the old query. "
        "The cumulative human feedback overrides the old SQL. Return SQL only."
        f"\nPrevious SQL: {previous_sql}\nHuman feedback: {human_feedback}"
    )
    return generate_sql(revision_request, schema, llm, dialect)
