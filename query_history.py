"""独立于 LangGraph 状态快照，持久化每轮成功查询的完整返回结果。"""

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any, TypedDict


class QueryRecord(TypedDict):
    """类用途：定义一轮成功查询的结构化存档字段。

    内容：原始问题、完整改写、最终 SQL、列名、实际返回行和截断标记。
    """
    turn_id: int
    question: str
    rewrite_question: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def _encode(value: Any) -> Any:
    """用途：将 SQLite 查询结果中的字节值转换为可写入 JSON 的结构。

    参数输入：任意结果值，可包含嵌套列表或字典。
    输出：保持普通值不变，并以 Base64 包装字节值。
    """
    if isinstance(value, bytes):
        return {"__mini_blob__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any) -> Any:
    """用途：读取存档时还原经过 JSON 编码的字节值。

    参数输入：从存档 JSON 解析得到的值。
    输出：递归还原列表、字典中的字节值。
    """
    if isinstance(value, dict) and set(value) == {"__mini_blob__"}:
        return base64.b64decode(value["__mini_blob__"])
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    return value


class QueryHistory:
    """类用途：按会话 ID 和轮次 ID 保存、读取成功查询。

    存储：在检查点 SQLite 文件中建立独立的 successful_queries 表；
        每个成功轮次对应一条记录，失败轮不由此类主动写入。
    """

    def __init__(self, path: str | Path):
        """用途：记录 SQLite 文件路径，并确保成功查询存档表存在。"""
        self.path = Path(path)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS successful_queries ("
                "session_id TEXT NOT NULL, turn_id INTEGER NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY (session_id, turn_id))"
            )

    def save(self, session_id: str, record: QueryRecord) -> None:
        """用途：写入或更新指定会话、指定轮次的一条成功查询记录。"""
        payload = json.dumps(_encode(record), ensure_ascii=False)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO successful_queries (session_id, turn_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id, turn_id) DO UPDATE SET payload=excluded.payload",
                (session_id, record["turn_id"], payload),
            )

    def list(self, session_id: str) -> list[QueryRecord]:
        """用途：按轮次顺序读取一个会话中的全部成功查询记录。"""
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT payload FROM successful_queries WHERE session_id=? ORDER BY turn_id",
                (session_id,),
            ).fetchall()
        return [_decode(json.loads(row[0])) for row in rows]

    def get(self, session_id: str, turn_id: int) -> QueryRecord | None:
        """用途：读取指定轮次的完整记录；轮次不存在时返回 None。"""
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM successful_queries WHERE session_id=? AND turn_id=?",
                (session_id, turn_id),
            ).fetchone()
        return _decode(json.loads(row[0])) if row else None


def history_index(records: list[QueryRecord], *, preview_rows: int = 3, max_chars: int = 16000) -> str:
    """用途：为主 Agent 生成有长度上限的成功查询历史摘要。

    参数输入：records 为成功记录；preview_rows 限制最近轮次的预览行数；
        max_chars 限制摘要字符数。
    输出：最近轮次包含 SQL、列名和结果预览；较早轮次保留简要问题信息。
        完整行仍保存在 SQLite 中，可按轮次 ID 另行读取。
    """
    if not records:
        return "暂无成功查询历史。"
    def short(value: Any, limit: int = 400) -> str:
        """用途：压缩单个历史字段，避免超长文本占满模型上下文。"""
        rendered = repr(value)
        return rendered if len(rendered) <= limit else rendered[:limit] + "…"

    lines = []
    used = 0
    omitted = 0
    for record in reversed(records):
        recent = len(lines) < 5
        line = (
            f"第 {record['turn_id']} 轮：用户问题={short(record['question'])}；"
            f"完整问题={short(record['rewrite_question'])}；"
            + (f"SQL={short(record['sql'])}；列={short(record['columns'])}；" if recent else "")
            + f"已返回 {len(record['rows'])} 行；截断={record['truncated']}"
            + (f"；预览={short(record['rows'][:preview_rows], 900)}" if recent else "")
        )
        if used + len(line) > max_chars:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    if omitted:
        lines.insert(0, f"另有 {omitted} 轮较早记录未展开；可按轮次 ID 请求读取已保存结果。")
    return "\n".join(lines)


def selected_context(records: list[QueryRecord]) -> str:
    """用途：将主 Agent 指定轮次的完整已返回结果提供给查询子图。

    参数输入：已按会话及轮次 ID 取出的成功记录。
    输出：每轮的完整改写、最终 SQL、列名、实际返回行和截断标记。
    """
    return "\n".join(
        f"第 {record['turn_id']} 轮已保存查询：问题={record['rewrite_question']!r}；"
        f"SQL={record['sql']}；列={record['columns']!r}；"
        f"结果行={record['rows']!r}；截断={record['truncated']}"
        for record in records
    )
