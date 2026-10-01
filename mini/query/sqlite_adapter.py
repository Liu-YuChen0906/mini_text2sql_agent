"""SQLite 业务数据库适配器。"""

import re
import sqlite3
import time
from pathlib import Path

from mini.query.database import ExecutionOutcome, empty_result, match_catalog_tables, register_adapter, reject_unsafe_sql


_REPAIRABLE = re.compile(
    r"syntax error|no such (?:table|column|function)|ambiguous column|misuse of aggregate|"
    r"wrong number of arguments|incomplete input", re.IGNORECASE,
)


class SQLiteAdapter:
    """读取 SQLite 表结构并使用只读连接执行查询。"""

    dialect = "sqlite"

    def __init__(self, path: str | Path):
        """保存业务数据库文件路径；不立即建立连接。"""
        self.path = Path(path).resolve()

    def _connect(self) -> sqlite3.Connection:
        """打开只读文件连接，并禁止临时写入。"""
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    def list_columns(self, catalog_tables: list[str]) -> dict[str, set[str]]:
        """读取真实业务表及字段，并对齐文件目录的表名。"""
        connection = self._connect()
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            real = {}
            for table in tables:
                quoted = table.replace('"', '""')
                real[table] = {row[1] for row in connection.execute(f'PRAGMA table_info("{quoted}")')}
            return match_catalog_tables(real, catalog_tables)
        finally:
            connection.close()

    def execute_readonly(self, sql: str, row_limit: int = 100, timeout_seconds: float = 2.0) -> ExecutionOutcome:
        """只读执行并限制时间、结果行数；错误附带可修复判断。"""
        rejected = reject_unsafe_sql(sql, self.dialect)
        if rejected:
            return ExecutionOutcome(empty_result("rejected", rejected))
        if row_limit <= 0:
            raise ValueError("row_limit 必须大于 0。")
        if not self.path.is_file():
            return ExecutionOutcome(empty_result(error=f"数据库文件不存在：{self.path}"))
        connection = self._connect()
        deadline = time.monotonic() + timeout_seconds
        timed_out = False

        def check_timeout() -> int:
            """达到本次查询的截止时间时中断 SQLite 虚拟机。"""
            nonlocal timed_out
            if time.monotonic() >= deadline:
                timed_out = True
                return 1
            return 0

        try:
            connection.set_progress_handler(check_timeout, 1000)
            cursor = connection.execute(sql)
            rows = cursor.fetchmany(row_limit + 1)
            result = empty_result("success")
            result["columns"] = [column[0] for column in cursor.description]
            result["truncated"] = len(rows) > row_limit
            result["rows"] = [list(row) for row in rows[:row_limit]]
            return ExecutionOutcome(result)
        except sqlite3.Error as error:
            if timed_out:
                return ExecutionOutcome(empty_result("timeout", f"查询超过 {timeout_seconds} 秒，已中断。"))
            message = f"SQL 执行失败：{error}"
            return ExecutionOutcome(empty_result(error=message), bool(_REPAIRABLE.search(str(error))))
        finally:
            connection.close()


def _factory(config: dict, default_path: Path) -> SQLiteAdapter:
    """从配置创建 SQLite 适配器；相对路径相对于配置文件目录。"""
    path = Path(config.get("path", default_path))
    if not path.is_absolute():
        path = Path(default_path).parent / path
    return SQLiteAdapter(path)


register_adapter("sqlite", _factory)
