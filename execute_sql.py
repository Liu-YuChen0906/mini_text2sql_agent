"""Mini 项目的第一版 SQLite 查询执行器。"""

import sqlite3
import time
from pathlib import Path
from typing import Any, Literal, TypedDict


class QueryResult(TypedDict):
    """类用途：声明 SQLite 查询结果字典的固定字段及其类型。

    支持功能：给调用方提供 status、columns、rows、truncated、error 的
    静态类型信息；运行时仍是普通 dict，不改变原有返回格式。
    """

    status: Literal["success", "rejected", "error", "timeout"]
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    error: str | None


def execute_readonly_sql(
    sql: str, database_path: str | Path, row_limit: int = 100, timeout_seconds: float = 2.0
) -> QueryResult:
    """用途：在只读 SQLite 连接中执行一条 SELECT 或 WITH 查询。

    参数输入：
        sql（str）：待执行的一条 SQL 文本；首词仅允许 SELECT 或 WITH。
        database_path（str | Path）：现有 SQLite 数据库文件路径，使用 mode=ro 打开。
        row_limit（int）：最多返回的行数，默认 100，必须大于 0；为判断是否
            截断，会实际读取 row_limit + 1 行。
        timeout_seconds（float）：查询允许的秒数，默认 2.0；SQLite 每执行约
            1000 条虚拟机指令检查一次，故超时检查不是精确到毫秒。
    输出：
        QueryResult：运行时为 dict；status（str，success/rejected/error/timeout）、columns
            （list[str]，结果列名）、rows（list[list[Any]]，查询值组成的行）、
            truncated（bool，是否还有未返回的行）、error（str | None，错误说明）。
            无效 SQL 和数据库错误以状态及错误文本返回；row_limit 不合法时抛
            ValueError。只读连接负责禁止数据库写入。
    """
    result: QueryResult = {
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

    # 先检查语句首词；实际的写入限制由下面的只读数据库连接提供。
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
        """用途：供 SQLite 进度回调检查当前查询是否超时。

        参数输入：无；闭包读取外层的 deadline（float，monotonic 时间戳），
            并在超时时修改 timed_out（bool）标记。
        输出：
            int：当前时间达到截止时间时返回 1，请 SQLite 中断查询；
                尚未超时时返回 0，允许 SQLite 继续执行。
        """
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
