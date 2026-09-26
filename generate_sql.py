import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

DIALECT = "SQLite"


def load_schema(database_path: str | Path) -> str:
    """用途：读取 SQLite 中所有业务表的建表语句。

    参数输入：
        database_path（str | Path）：现有 SQLite 数据库文件路径，以只读 URI 打开。
    输出：
        str：从 sqlite_master 取得的各业务表 CREATE TABLE 语句，按表名排序并
            用空行连接；过滤 sqlite_ 开头的系统表。没有业务表时返回空字符串。
    说明：这是早期直接提供完整建表语句的辅助函数；当前 main.py 改用
        schema_linking.sql_context 生成更有针对性的结构上下文。
    """
    query = """
    SELECT name, sql
    FROM sqlite_master
    WHERE type = 'table'
      AND name NOT LIKE 'sqlite_%'
    ORDER BY name;
    """

    db_path = Path(database_path).resolve()
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(query).fetchall()
    create_statements = []
    for _name, create_sql in rows:
        if create_sql:
            create_statements.append(create_sql)
    return "\n\n".join(create_statements)


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
                "Return SQL only, without explanations.\n\nSchema:\n{schema}",
            ),
            ("human", "{question}"),
        ]
    )


def generate_sql(question: str, schema: str, llm: Any) -> str:
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
    content = chain.invoke({"dialect": DIALECT, "schema": schema, "question": question})
    return content.replace("```sql", "").replace("```", "").strip()


def regenerate_sql(question: str, schema: str, previous_sql: str, error: str, llm: Any) -> str:
    """用途：把上次 SQL 和 SQLite 错误反馈给模型，要求按原问题重新生成查询。

    参数输入：question 为改写后的问题，schema 为已选表的结构上下文，
        previous_sql 为失败的 SQL，error 为执行错误，llm 为复用的聊天模型。
    输出：str，经过 generate_sql 清理代码围栏后的新 SQL；此处不执行或校验。
    """
    feedback = (
        f"{question}\n\nThe previous SQLite query failed. Correct it and return SQL only."
        f"\nPrevious SQL: {previous_sql}\nSQLite error: {error}"
    )
    return generate_sql(feedback, schema, llm)
