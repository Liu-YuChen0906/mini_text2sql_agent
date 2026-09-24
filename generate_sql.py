import sqlite3
from pathlib import Path

import yaml
from langchain_deepseek import ChatDeepSeek
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate


PROJECT_DIR = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_DIR / "data" / "tracking_orders.sqlite"
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DIALECT = "SQLite"


def load_schema(database_path: str | Path) -> str:
    """读取 SQLite 中所有业务表的建表语句。"""
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
    for name, create_sql in rows:
        if create_sql:
            create_statements.append(create_sql)
    return "\n\n".join(create_statements)


def build_prompt() -> ChatPromptTemplate:
    """明确构造 LangChain Chat Prompt，不依赖原项目的提示词封装。"""
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


def load_llm_config(config_path: str | Path = CONFIG_PATH) -> dict:
    """读取 Mini 项目自己的 YAML 模型配置。"""
    config_file = Path(config_path)
    if not config_file.is_file():
        raise FileNotFoundError(f"找不到 Mini 配置文件：{config_file}")

    with config_file.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("Mini 配置文件需要包含 default_llm 配置。")

    llm_config = config.get("default_llm")
    if not isinstance(llm_config, dict):
        raise ValueError("Mini 配置文件缺少 default_llm。")
    if llm_config.get("class") != "langchain_deepseek.ChatDeepSeek":
        raise ValueError("Mini 当前只支持 langchain_deepseek.ChatDeepSeek。")

    params = llm_config.get("params")
    if not isinstance(params, dict):
        raise ValueError("default_llm 缺少 params。")
    api_key = params.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip() or api_key == "YOUR_API_KEY_HERE":
        raise ValueError("请在 mini_openchatbi/config.yaml 中填写 DeepSeek API Key。")

    return params


def create_llm() -> ChatDeepSeek:
    """用 Mini 项目的 YAML 参数创建模型对象。"""
    params = load_llm_config()
    return ChatDeepSeek(**params)


def generate_sql(question: str, schema: str) -> str:
    """Prompt → 模型 → 字符串解析器，展示最小 LangChain LCEL 链。"""
    chain = build_prompt() | create_llm() | StrOutputParser()
    content = chain.invoke({"dialect": DIALECT, "schema": schema, "question": question})
    return content.replace("```sql", "").replace("```", "").strip()


def main() -> None:
    """读取用户输入并打印生成的 SQL。"""
    question = input("请输入问题：").strip()
    schema = load_schema(DATABASE_PATH)
    print(generate_sql(question, schema))


if __name__ == "__main__":
    main()
