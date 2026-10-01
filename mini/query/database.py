"""业务数据库的统一接口、查询校验和适配器注册。"""

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, TypedDict

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError


class QueryResult(TypedDict):
    """供 Agent、CLI 和网页使用的稳定查询结果格式。"""

    status: Literal["success", "rejected", "error", "timeout"]
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    error: str | None


@dataclass(frozen=True)
class ExecutionOutcome:
    """执行结果及是否值得让模型修复；修复标记不进入展示结果。"""

    result: QueryResult
    repairable: bool = False


class DatabaseAdapter(Protocol):
    """业务数据源必须提供的能力；新增数据库无需修改 Agent 图。"""

    dialect: str

    def list_columns(self, catalog_tables: list[str]) -> dict[str, set[str]]:
        """返回数据库中与目录表名匹配的真实字段。"""

    def execute_readonly(self, sql: str, row_limit: int = 100, timeout_seconds: float = 2.0) -> ExecutionOutcome:
        """执行一条只读查询，返回统一结果和错误修复标记。"""


AdapterFactory = Callable[[dict, Any], DatabaseAdapter]
_FACTORIES: dict[str, AdapterFactory] = {}


def register_adapter(name: str, factory: AdapterFactory) -> None:
    """注册一种业务数据库；名称冲突时显式报错。"""
    if not name or name in _FACTORIES:
        raise ValueError(f"数据库适配器名称无效或已注册：{name}")
    _FACTORIES[name] = factory


def create_adapter(config: dict, default_path: Any) -> DatabaseAdapter:
    """按 database.type 创建已注册实现，未配置时默认使用 SQLite。"""
    from mini.query import postgres_adapter, sqlite_adapter  # noqa: F401  注册内置实现

    database = config.get("database") or {}
    if not isinstance(database, dict):
        raise ValueError("database 配置必须是对象。")
    kind = database.get("type", "sqlite")
    try:
        factory = _FACTORIES[kind]
    except (KeyError, TypeError):
        raise ValueError(f"不支持的业务数据库类型：{kind}") from None
    return factory(database, default_path)


def empty_result(status: Literal["success", "rejected", "error", "timeout"] = "error", error: str | None = None) -> QueryResult:
    """构造字段齐全的空查询结果。"""
    return {"status": status, "columns": [], "rows": [], "truncated": False, "error": error}


def reject_unsafe_sql(sql: str, dialect: str) -> str | None:
    """解析并限制为单条 SELECT；数据库只读权限承担最终写入防护。"""
    if not sql.strip():
        return "SQL 不能为空。"
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level="RAISE")
    except (ParseError, TokenError, ValueError):
        return "SQL 语法无法识别。"
    if len(statements) != 1 or statements[0] is None:
        return "一次只能执行一条 SQL。"
    statement = statements[0]
    if not isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return "目前只支持 SELECT 或 WITH 查询。"
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Into, exp.Command)
    if any(isinstance(node, forbidden) for node in statement.walk()):
        return "查询中不能包含写入或管理语句。"
    return None


def match_catalog_tables(real: dict[str, set[str]], catalog_tables: list[str]) -> dict[str, set[str]]:
    """按不区分大小写的名称匹配目录表，保留目录名称供后续选表使用。"""
    folded: dict[str, list[str]] = {}
    for table in real:
        folded.setdefault(table.casefold(), []).append(table)
    result = {}
    for table in catalog_tables:
        names = folded.get(table.casefold(), [])
        if len(names) == 1:
            result[table] = real[names[0]]
        elif len(names) > 1:
            raise ValueError(f"数据库中有多个仅大小写不同的同名表：{table}")
    return result
