from typing import Any

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
                "You are a professional SQL engineer. Generate exactly one read-only "
                "{dialect} SELECT query. Only use tables and columns in the schema. "
                "Use unquoted table and column names unless the schema explicitly requires quotes. "
                "Return SQL only, without explanations.\n\nSchema:\n{schema}",
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
    content = chain.invoke({"dialect": dialect, "schema": schema, "question": question})
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
        "Generate a new SQL query that addresses the feedback. Return SQL only."
        f"\nPrevious SQL: {previous_sql}\nHuman feedback: {human_feedback}"
    )
    return generate_sql(revision_request, schema, llm, dialect)
