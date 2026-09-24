"""Mini 项目的第一版 SQLite 查询执行器。"""

import sqlite3
from pathlib import Path
import time


def execute_readonly_sql(sql: str, database_path: str | Path, row_limit: int = 100, timeout_seconds: float = 2.0) -> dict:
    """执行一条 SELECT 或 WITH 查询，返回列名和最多 row_limit 行。"""
    result = {
        "status": "error",
        "columns": [],
        "rows": [],
        "truncated": False,
        "error": None,
    }

    if not sql.strip():
        result["status"] = "rejected"
        result["error"] = "SQL 不能为空。"
        return result

    # 第一版支持 SELECT 和 WITH；数据库连接仍以只读方式打开。
    first_word = sql.strip().split()[0].upper()
    if first_word not in {"SELECT", "WITH"}:
        result["status"] = "rejected"
        result["error"] = "目前只支持 SELECT 或 WITH 查询。"
        return result

    if row_limit <= 0:
        raise ValueError("row_limit 必须大于 0。")

    db_path = Path(database_path).resolve()
    if not db_path.is_file():
        result["error"] = f"数据库文件不存在：{db_path}"
        return result


    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)

    deadline = time.monotonic() + timeout_seconds
    timed_out = False

    def check_timeout():
        nonlocal timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return 1
        return 0

    try:
        connection.set_progress_handler(check_timeout, 1000)

        cursor = connection.execute(sql)
        result["columns"] = [column[0] for column in cursor.description]

        fetched_rows = cursor.fetchmany(row_limit + 1)
        result["truncated"] = len(fetched_rows) > row_limit
        result["rows"] = [list(row) for row in fetched_rows[:row_limit]]
        result["status"] = "success"
    except sqlite3.ProgrammingError:
        result["status"] = "rejected"
        result["error"] = "一次只能执行一条 SQL。"
    except sqlite3.Error as error:
        if timed_out:
            result["status"] = "timeout"
            result["error"] = f"查询超过 {timeout_seconds} 秒，已中断。"
        else:
            result["error"] = f"SQL 执行失败：{error}"
    finally:
        connection.close()

    return result
