"""旧版 SQLite 查询入口；主流程通过数据库适配器执行。"""

from pathlib import Path

from mini.query.database import QueryResult
from mini.query.sqlite_adapter import SQLiteAdapter


def execute_readonly_sql(
    sql: str, database_path: str | Path, row_limit: int = 100, timeout_seconds: float = 2.0
) -> QueryResult:
    """兼容旧调用方，使用 SQLite 适配器返回原有结果字典。"""
    return SQLiteAdapter(database_path).execute_readonly(sql, row_limit, timeout_seconds).result
