"""PostgreSQL 业务数据库适配器。"""

import os
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from mini.query.database import ExecutionOutcome, empty_result, match_catalog_tables, register_adapter, reject_unsafe_sql

try:
    import psycopg
except ImportError:  # SQLite 默认部署不需要安装 PostgreSQL 驱动。
    psycopg = None


class PostgreSQLAdapter:
    """通过只读事务查询 PostgreSQL，按 SQLSTATE 识别可修复错误。"""

    dialect = "postgres"

    def __init__(self, dsn: str):
        """保存连接字符串；创建对象时不连接数据库。"""
        self.dsn = dsn

    def _connect(self):
        """延迟导入可选驱动，并创建有限连接等待时间的连接。"""
        if psycopg is None:
            raise RuntimeError("PostgreSQL 查询需要安装 psycopg。")
        connection = psycopg.connect(self.dsn, connect_timeout=5)
        connection.read_only = True
        return connection

    def list_columns(self, catalog_tables: list[str]) -> dict[str, set[str]]:
        """读取当前 schema 中用户可见的基础表和字段。"""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT c.table_name, c.column_name
                    FROM information_schema.columns AS c
                    JOIN information_schema.tables AS t
                      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                    WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'
                    ORDER BY c.table_name, c.ordinal_position
                """)
                real: dict[str, set[str]] = {}
                for table, column in cursor.fetchall():
                    real.setdefault(table, set()).add(column)
                return match_catalog_tables(real, catalog_tables)

    def execute_readonly(self, sql: str, row_limit: int = 100, timeout_seconds: float = 2.0) -> ExecutionOutcome:
        """在只读事务内执行单条查询，使用服务端语句超时。"""
        rejected = reject_unsafe_sql(sql, self.dialect)
        if rejected:
            return ExecutionOutcome(empty_result("rejected", rejected))
        if row_limit <= 0:
            raise ValueError("row_limit 必须大于 0。")
        if timeout_seconds <= 0:
            return ExecutionOutcome(empty_result("timeout", f"查询超过 {timeout_seconds} 秒，已中断。"))
        if psycopg is None:
            raise RuntimeError("PostgreSQL 查询需要安装 psycopg。")
        try:
            with self._connect() as connection:
                milliseconds = max(1, int(timeout_seconds * 1000))
                connection.execute("SELECT set_config('statement_timeout', %s, true)", (f"{milliseconds}ms",))
                with connection.cursor(name="mini_readonly_query") as cursor:
                    cursor.execute(sql)
                    fetched = cursor.fetchmany(row_limit + 1)
                    result = empty_result("success")
                    result["columns"] = [column.name for column in cursor.description]
                    result["truncated"] = len(fetched) > row_limit
                    result["rows"] = [[_json_value(value) for value in row] for row in fetched[:row_limit]]
                    return ExecutionOutcome(result)
        except psycopg.Error as error:
            code = error.sqlstate
            if code == "57014":
                return ExecutionOutcome(empty_result("timeout", f"查询超过 {timeout_seconds} 秒，已中断。"))
            repairable = code in {"42601", "42P01", "42703", "42883", "42702", "42803", "42804"}
            return ExecutionOutcome(empty_result(error=f"SQL 执行失败：{error}"), repairable)


def _json_value(value: Any) -> Any:
    """将 PostgreSQL 常见标量转为检查点和网页可序列化的值。"""
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _factory(config: dict, _default_path: Any) -> PostgreSQLAdapter:
    """从环境变量获取 PostgreSQL 连接信息，避免密钥进入配置文件。"""
    variable = config.get("url_env", "MINI_DATABASE_URL")
    if not isinstance(variable, str) or not variable:
        raise ValueError("database.url_env 必须是环境变量名称。")
    dsn = os.environ.get(variable)
    if not dsn:
        raise ValueError(f"PostgreSQL 连接环境变量未设置：{variable}")
    return PostgreSQLAdapter(dsn)


register_adapter("postgres", _factory)
register_adapter("postgresql", _factory)
