"""Persistent, single-worker conversation service for the local web UI."""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from mini.agents.main_graph import build_agent_graph, new_agent_turn
from mini.runtime.query_history import QueryHistory
from mini.runtime.resources import PROJECT_DIR, create_llm, load_resources


logger = logging.getLogger(__name__)


class ServiceError(Exception):
    """携带 HTTP 状态码和可展示消息的网页业务异常。"""

    def __init__(self, status: int, message: str):
        """保存状态码与错误说明，供 API 层转换为 HTTPException。"""
        self.status = status
        self.message = message
        super().__init__(message)


def _now() -> str:
    """返回适合写入 SQLite 的 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    """递归转换查询结果，使 SQLite BLOB 等值可写入网页 JSON。"""
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _pending(snapshot: Any) -> dict[str, Any] | None:
    """从主图快照读取首个待恢复的子图交互，没有则返回 None。"""
    for task in snapshot.tasks:
        for item in task.interrupts:
            return _json_value(dict(item.value))
    return None


class WebSessionService:
    """管理网页会话、图执行和持久化；同一进程内串行运行 Agent。"""

    def __init__(
        self,
        path: str | Path = PROJECT_DIR / "web_checkpoints.sqlite",
        *,
        llm: Any = None,
        query_resources_factory: Any = None,
    ):
        """初始化检查点、成功查询历史和主图，并标记遗留运行请求。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._execution = threading.Lock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._init_tables()
        self.history = QueryHistory(self.path)
        self.llm = llm if llm is not None else create_llm()
        factory = query_resources_factory or (lambda: load_resources(llm=self.llm))
        self.graph = build_agent_graph(self.llm, SqliteSaver(self.connection), self.history, factory)
        # The previous process cannot still be running after a fresh startup.
        with self._db() as db:
            db.execute("UPDATE web_sessions SET status='interrupted', updated_at=? WHERE status='running'", (_now(),))
            db.execute("UPDATE web_requests SET status='interrupted' WHERE status='running'")

    def close(self) -> None:
        """关闭供 LangGraph 检查点使用的长连接。"""
        self.connection.close()

    def _db(self) -> sqlite3.Connection:
        """创建短连接，供网页会话表的独立事务使用。"""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _init_tables(self) -> None:
        """确保会话、页面消息和幂等请求记录表存在。"""
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                    turn_id INTEGER NOT NULL DEFAULT 0, pending_id TEXT,
                    pending_payload TEXT, active_request_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    turn_id INTEGER NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS web_messages_session ON web_messages(session_id, id);
                CREATE TABLE IF NOT EXISTS web_requests (
                    session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    kind TEXT NOT NULL, interaction_id TEXT, input_payload TEXT NOT NULL,
                    status TEXT NOT NULL, turn_id INTEGER NOT NULL, starting_checkpoint TEXT,
                    PRIMARY KEY(session_id, request_id)
                );
            """)

    def create_session(self) -> dict:
        """建立空会话并返回前端可显示的完整状态。"""
        session_id = uuid4().hex
        now = _now()
        with self._db() as db:
            db.execute(
                "INSERT INTO web_sessions (id,title,status,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session_id, "新会话", "idle", now, now),
            )
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict]:
        """返回按更新时间倒序排列的会话摘要。"""
        with self._db() as db:
            rows = db.execute(
                "SELECT id,title,status,created_at,updated_at FROM web_sessions ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict:
        """读取会话、待处理交互及按写入顺序排列的消息。"""
        with self._db() as db:
            session = db.execute("SELECT * FROM web_sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise ServiceError(404, "会话不存在。")
            messages = db.execute(
                "SELECT id,turn_id,role,kind,payload,created_at FROM web_messages WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        output = {key: session[key] for key in ("id", "title", "status", "turn_id", "created_at", "updated_at")}
        output["pending"] = (
            {"id": session["pending_id"], **json.loads(session["pending_payload"])}
            if session["pending_id"] else None
        )
        output["messages"] = [
            {**{key: row[key] for key in ("id", "turn_id", "role", "kind", "created_at")},
             "payload": json.loads(row["payload"])}
            for row in messages
        ]
        return output

    def _add_message(self, db: sqlite3.Connection, session_id: str, turn_id: int,
                     role: str, kind: str, payload: dict) -> None:
        """在调用方事务中追加一条已转换为 JSON 的页面消息。"""
        db.execute(
            "INSERT INTO web_messages (session_id,turn_id,role,kind,payload,created_at) VALUES (?,?,?,?,?,?)",
            (session_id, turn_id, role, kind, json.dumps(_json_value(payload), ensure_ascii=False), _now()),
        )

    def send_message(self, session_id: str, content: str, request_id: str) -> dict:
        """校验新问题并启动主图的一轮执行。"""
        if not isinstance(content, str) or not content.strip():
            raise ServiceError(422, "消息不能为空。")
        return self._submit(session_id, request_id, "message", {"content": content.strip()})

    def resume(self, session_id: str, request_id: str, interaction_id: str,
               answer: str | None = None, decision: str | None = None,
               feedback: str | None = None) -> dict:
        """验证交互 ID 后提交澄清回答或人工审核反馈。"""
        return self._submit(session_id, request_id, "resume", {
            "interaction_id": interaction_id, "answer": answer,
            "decision": decision, "feedback": feedback,
        })

    def _submit(self, session_id: str, request_id: str, kind: str, payload: dict) -> dict:
        """登记幂等请求，串行执行图，并返回最新会话状态。"""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ServiceError(422, "request_id 不能为空。")
        request_id = request_id.strip()
        with self._db() as db:
            session = db.execute("SELECT * FROM web_sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise ServiceError(404, "会话不存在。")
            previous = db.execute(
                "SELECT * FROM web_requests WHERE session_id=? AND request_id=?", (session_id, request_id)
            ).fetchone()
            if previous:
                if previous["kind"] != kind or json.loads(previous["input_payload"]) != payload:
                    raise ServiceError(409, "request_id 已用于另一项请求。")
                return self.get_session(session_id)
        if not self._execution.acquire(blocking=False):
            raise ServiceError(409, "服务正在执行另一项任务，请稍后重试。")
        try:
            starting_checkpoint = None
            if kind == "resume":
                starting_checkpoint = self.graph.get_state(
                    {"configurable": {"thread_id": session_id}}
                ).config.get("configurable", {}).get("checkpoint_id")
            with self._db() as db:
                session = db.execute("SELECT * FROM web_sessions WHERE id=?", (session_id,)).fetchone()
                previous = db.execute(
                    "SELECT * FROM web_requests WHERE session_id=? AND request_id=?", (session_id, request_id)
                ).fetchone()
                if previous:
                    return self.get_session(session_id)
                if session["status"] == "closed":
                    raise ServiceError(409, "会话已结束，不能继续提交。")
                if session["status"] == "interrupted":
                    raise ServiceError(409, "上次执行已中断，请先显式重试。")
                pending = json.loads(session["pending_payload"]) if session["pending_payload"] else None
                if kind == "message":
                    if pending or session["status"] != "idle":
                        raise ServiceError(409, "当前会话有待处理交互或正在执行。")
                    turn_id = session["turn_id"] + 1
                    title = payload["content"][:24] if turn_id == 1 else session["title"]
                    self._add_message(db, session_id, turn_id, "user", "text", payload)
                    db.execute("UPDATE web_sessions SET title=?,turn_id=? WHERE id=?", (title, turn_id, session_id))
                else:
                    if session["status"] != "pending" or not pending or session["pending_id"] != payload["interaction_id"]:
                        raise ServiceError(409, "交互状态不匹配或该审核操作已经失效。")
                    if pending["kind"] == "clarification":
                        if not isinstance(payload["answer"], str) or not payload["answer"].strip():
                            raise ServiceError(422, "澄清回答不能为空。")
                        feedback_value = payload["answer"].strip()
                        action = {"answer": feedback_value}
                        action_kind = "clarification_answer"
                    elif pending["kind"] == "sql_review":
                        decision = payload["decision"]
                        if decision not in pending.get("options", []):
                            raise ServiceError(422, "审核决定无效。")
                        if decision in {"edit_sql", "edit_schema"} and (
                            not isinstance(payload["feedback"], str) or not payload["feedback"].strip()
                        ):
                            raise ServiceError(422, "请填写自然语言修改说明。")
                        feedback_value = {"decision": decision}
                        if decision in {"edit_sql", "edit_schema"}:
                            feedback_value["feedback"] = payload["feedback"].strip()
                        action = feedback_value
                        action_kind = "review_action"
                    else:
                        raise ServiceError(409, "未知的待处理交互。")
                    turn_id = session["turn_id"]
                    self._add_message(db, session_id, turn_id, "user", action_kind, action)
                    db.execute("UPDATE web_sessions SET pending_id=NULL,pending_payload=NULL WHERE id=?", (session_id,))
                db.execute(
                    "INSERT INTO web_requests VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, request_id, kind, payload.get("interaction_id"),
                     json.dumps(payload, ensure_ascii=False), "running", turn_id, starting_checkpoint),
                )
                db.execute(
                    "UPDATE web_sessions SET status='running',active_request_id=?,updated_at=? WHERE id=?",
                    (request_id, _now(), session_id),
                )
            self._execute(session_id, request_id, kind, payload, turn_id)
            return self.get_session(session_id)
        finally:
            self._execution.release()

    def retry(self, session_id: str) -> dict:
        """从已保存的检查点重试意外中断的请求。"""
        if not self._execution.acquire(blocking=False):
            raise ServiceError(409, "服务正在执行另一项任务，请稍后重试。")
        try:
            with self._db() as db:
                session = db.execute("SELECT * FROM web_sessions WHERE id=?", (session_id,)).fetchone()
                if session is None:
                    raise ServiceError(404, "会话不存在。")
                if session["status"] != "interrupted":
                    raise ServiceError(409, "当前会话没有可重试的中断任务。")
                request = db.execute(
                    "SELECT * FROM web_requests WHERE session_id=? AND request_id=?",
                    (session_id, session["active_request_id"]),
                ).fetchone()
                if request is None:
                    raise ServiceError(409, "中断任务记录缺失。")
                db.execute("UPDATE web_sessions SET status='running',updated_at=? WHERE id=?", (_now(), session_id))
                db.execute("UPDATE web_requests SET status='running' WHERE session_id=? AND request_id=?",
                           (session_id, request["request_id"]))
            self._execute(session_id, request["request_id"], request["kind"],
                          json.loads(request["input_payload"]), request["turn_id"],
                          retry=True, starting_checkpoint=request["starting_checkpoint"])
            return self.get_session(session_id)
        finally:
            self._execution.release()

    def _execute(self, session_id: str, request_id: str, kind: str,
                 payload: dict, turn_id: int, *, retry: bool = False,
                 starting_checkpoint: str | None = None) -> None:
        """驱动主图，将最终结果或暂停交互转换成持久页面消息。"""
        config = {"configurable": {"thread_id": session_id}}
        try:
            if retry:
                snapshot = self.graph.get_state(config)
                waiting = _pending(snapshot)
                checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
                if waiting and kind == "resume" and checkpoint_id == starting_checkpoint:
                    value = self._resume_value(payload)
                    self.graph.invoke(Command(resume=value), config=config)
                elif waiting:
                    pass  # The earlier run reached a new interaction before exiting.
                elif snapshot.values and snapshot.next:
                    self.graph.invoke(None, config=config)
                elif kind == "message" and (not snapshot.values or snapshot.values.get("turn_id", 0) < turn_id):
                    self.graph.invoke(new_agent_turn(payload["content"], turn_id), config=config)
                elif not snapshot.values:
                    raise RuntimeError("恢复检查点不存在。")
            elif kind == "message":
                self.graph.invoke(new_agent_turn(payload["content"], turn_id), config=config)
            else:
                self.graph.invoke(Command(resume=self._resume_value(payload)), config=config)
            snapshot = self.graph.get_state(config)
            waiting = _pending(snapshot)
            with self._db() as db:
                if waiting:
                    interaction_id = uuid4().hex
                    self._add_message(db, session_id, turn_id, "assistant", waiting["kind"],
                                      {"interaction_id": interaction_id, **waiting})
                    db.execute(
                        "UPDATE web_sessions SET status='pending',pending_id=?,pending_payload=?,updated_at=? WHERE id=?",
                        (interaction_id, json.dumps(waiting, ensure_ascii=False), _now(), session_id),
                    )
                else:
                    values = dict(snapshot.values)
                    outcome = values.get("query_outcome")
                    if outcome is not None:
                        if outcome.get("status") == "success":
                            record = self.history.get(session_id, outcome["record_turn_id"])
                            if record is None:
                                raise RuntimeError("成功查询记录缺失。")
                            result = {"sql": record["sql"], "columns": record["columns"],
                                      "rows": record["rows"], "truncated": record["truncated"],
                                      "confidence": outcome.get("confidence"),
                                      "reasons": outcome.get("confidence_reasons", []),
                                      "human_decision": outcome.get("human_decision", "")}
                            self._add_message(db, session_id, turn_id, "assistant", "query", result)
                        else:
                            self._add_message(db, session_id, turn_id, "assistant", "error", {
                                "message": outcome.get("error") or outcome.get("status") or "查询未完成。",
                                "sql": outcome.get("sql", ""), "status": outcome.get("status"),
                            })
                    else:
                        self._add_message(db, session_id, turn_id, "assistant", "text",
                                          {"content": values.get("reply", "")})
                    db.execute("UPDATE web_sessions SET status=?,updated_at=? WHERE id=?",
                               ("closed" if values.get("session_closed") else "idle", _now(), session_id))
                db.execute("UPDATE web_requests SET status='complete' WHERE session_id=? AND request_id=?",
                           (session_id, request_id))
                db.execute("UPDATE web_sessions SET active_request_id=NULL WHERE id=?", (session_id,))
        except Exception as exc:
            # Retain the checkpoint and request for an explicit recovery attempt.
            logger.exception("Web session %s request %s interrupted", session_id, request_id)
            with self._db() as db:
                db.execute("UPDATE web_sessions SET status='interrupted',updated_at=? WHERE id=?", (_now(), session_id))
                db.execute("UPDATE web_requests SET status='interrupted' WHERE session_id=? AND request_id=?",
                           (session_id, request_id))
            raise ServiceError(500, "执行中断，请查看服务日志并显式重试。") from exc

    @staticmethod
    def _resume_value(payload: dict) -> Any:
        """把网页表单字段转换为 LangGraph Command.resume 的值。"""
        if payload.get("answer") is not None:
            return payload["answer"].strip()
        value = {"decision": payload["decision"]}
        if payload.get("feedback"):
            value["feedback"] = payload["feedback"].strip()
        return value
