"""SQLite metadata and immutable, hashed artifacts. Never accepts a caller path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from mini.analysis.models import DatasetRecord, ExecutionEvidence, now, uid


def normalize(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


def encode(value) -> bytes:
    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


class EvidenceStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True)
        if self.objects.is_symlink():
            raise ValueError("symlink artifact directory")
        self.path = self.root / "evidence.sqlite"
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, session TEXT NOT NULL,
              run TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_run ON records(session,run,kind);
            CREATE TABLE IF NOT EXISTS operations(run TEXT, operation TEXT, attempt INTEGER,
              evidence TEXT UNIQUE, PRIMARY KEY(run,operation,attempt));
            CREATE TABLE IF NOT EXISTS call_operations(run TEXT, call_id TEXT, operation TEXT,
              PRIMARY KEY(run,call_id));
            CREATE TABLE IF NOT EXISTS budgets(run TEXT, category TEXT, used INTEGER,
              PRIMARY KEY(run,category));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, kind: str, identity: str, session: str, run: str, body: dict):
        payload = encode(body).decode()
        with self.db() as db:
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (identity, session, run, kind, payload),
            )

    def get(self, session: str, identity: str, kind: str | None = None) -> dict:
        with self.db() as db:
            row = db.execute(
                "SELECT kind,body FROM records WHERE id=? AND session=?",
                (identity, session),
            ).fetchone()
        if not row or (kind and row[0] != kind):
            raise KeyError("不可用")
        return json.loads(row[1])

    def list(self, session: str, run: str, kind: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute(
                "SELECT body FROM records WHERE session=? AND run=? AND kind=? ORDER BY rowid",
                (session, run, kind),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recent_runs(self, session: str, limit: int = 5) -> list[dict]:
        with self.db() as db:
            rows = db.execute(
                "SELECT body FROM records WHERE session=? AND kind='run' ORDER BY rowid DESC LIMIT ?",
                (session, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create_run(self, session: str, turn: int, task: dict, request: str = "") -> str:
        identity = uid("run")
        self.save(
            "run",
            identity,
            session,
            identity,
            dict(
                run_id=identity,
                session_id=session,
                turn_id=turn,
                request_id=request,
                task=task,
                created_at=now(),
                status="running",
                active_seconds=0.0,
            ),
        )
        return identity

    def operation_id(self, session: str, run: str, call_id: str) -> str:
        self.get(session, run, "run")
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO call_operations VALUES(?,?,?)",
                (run, call_id, uid("op")),
            )
            return db.execute(
                "SELECT operation FROM call_operations WHERE run=? AND call_id=?",
                (run, call_id),
            ).fetchone()[0]

    def consume(self, session: str, run: str, category: str, limit: int):
        self.get(session, run, "run")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT used FROM budgets WHERE run=? AND category=?", (run, category)
            ).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                raise ValueError(f"budget_exhausted:{category}")
            db.execute(
                "INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(run,category) DO UPDATE SET used=excluded.used",
                (run, category, used + 1),
            )

    def artifact(
        self, session: str, run: str, content: bytes, media: str, name: str
    ) -> str:
        if self.objects.is_symlink():
            raise ValueError("symlink artifact directory")
        identity = uid("art")
        target = self.objects / identity
        temporary = self.objects / f".tmp-{uuid4().hex}"
        with temporary.open("xb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
        self.save(
            "artifact",
            identity,
            session,
            run,
            dict(
                artifact_id=identity,
                media_type=media,
                name=name,
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            ),
        )
        return identity

    def read_artifact(self, session: str, identity: str) -> tuple[dict, bytes]:
        meta = self.get(session, identity, "artifact")
        if not re.fullmatch(r"art_[a-f0-9]{32}", identity):
            raise KeyError("不可用")
        target = self.objects / identity
        if (
            self.objects.is_symlink()
            or target.is_symlink()
            or not target.is_file()
            or target.resolve().parent != self.objects.resolve()
        ):
            raise KeyError("不可用")
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != meta["sha256"]:
            raise ValueError("artifact_hash_mismatch")
        return meta, content

    def begin(self, evidence: ExecutionEvidence) -> tuple[dict, bool]:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT evidence FROM operations WHERE run=? AND operation=? AND attempt=?",
                (evidence.run_id, evidence.operation_id, evidence.attempt),
            ).fetchone()
            if row:
                existing = self.get(evidence.session_id, row[0], "evidence")
                if (
                    existing["kind"] != evidence.kind
                    or existing["tool_name"] != evidence.tool_name
                    or existing["input_dataset_ids"] != evidence.input_dataset_ids
                    or any(
                        existing["parameters"].get(k) != v
                        for k, v in evidence.parameters.items()
                    )
                ):
                    raise ValueError("operation_identity_conflict")
                return existing, False
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?)",
                (
                    evidence.run_id,
                    evidence.operation_id,
                    evidence.attempt,
                    evidence.evidence_id,
                ),
            )
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?)",
                (
                    evidence.evidence_id,
                    evidence.session_id,
                    evidence.run_id,
                    "evidence",
                    evidence.model_dump_json(),
                ),
            )
        return evidence.model_dump(), True

    def finish(self, evidence: dict):
        checked = ExecutionEvidence.model_validate({**evidence, "finished_at": now()})
        # Artifacts must exist and verify before publishing a successful execution.
        if checked.status == "success":
            for identity in checked.artifact_ids:
                self.read_artifact(checked.session_id, identity)
            for identity in checked.output_dataset_ids:
                self.dataset(checked.session_id, identity)
        self.save(
            "evidence",
            checked.evidence_id,
            checked.session_id,
            checked.run_id,
            checked.model_dump(),
        )

    def save_dataset(
        self,
        session: str,
        run: str,
        evidence: str,
        columns: list[str],
        rows: list[list[Any]],
        *,
        parents=None,
        truncated=False,
        coverage=None,
        units=None,
        warnings=None,
    ) -> dict:
        if len(set(columns)) != len(columns) or any(
            len(row) != len(columns) for row in rows
        ):
            raise ValueError("invalid_dataset_shape")
        types = []
        for i, name in enumerate(columns):
            types.append(
                dict(
                    name=name,
                    type="|".join(
                        sorted(
                            {
                                type(row[i]).__name__
                                for row in rows
                                if row[i] is not None
                            }
                        )
                    )
                    or "null",
                )
            )
        content = encode(
            dict(
                columns=columns,
                rows=rows,
                column_types={t["name"]: t["type"] for t in types},
            )
        )
        artifact = self.artifact(
            session, run, content, "application/json", "dataset.json"
        )
        dataset = DatasetRecord(
            session_id=session,
            run_id=run,
            source_evidence_id=evidence,
            timezone=self.get(session, run, "run")["task"].get(
                "timezone", "Asia/Shanghai"
            ),
            parent_dataset_ids=parents or [],
            columns=types,
            row_count=len(rows),
            preview=normalize(rows[:5]),
            preview_truncated=len(rows) > 5,
            data_artifact_id=artifact,
            content_sha256=hashlib.sha256(content).hexdigest(),
            source_truncated=truncated,
            coverage=coverage or {},
            units=units or {},
            quality_warnings=warnings or [],
        )
        self.save("dataset", dataset.dataset_id, session, run, dataset.model_dump())
        return dataset.model_dump()

    def dataset(self, session: str, identity: str) -> tuple[dict, dict]:
        meta = self.get(session, identity, "dataset")
        artifact, content = self.read_artifact(session, meta["data_artifact_id"])
        if artifact["sha256"] != meta["content_sha256"]:
            raise ValueError("dataset_hash_mismatch")
        return meta, json.loads(content)

    def evidence(
        self, session: str, identity: str, seen=None, require_success=True
    ) -> dict:
        seen = set() if seen is None else seen
        if identity in seen:
            raise ValueError("evidence_cycle")
        seen.add(identity)
        item = self.get(session, identity, "evidence")
        if require_success and item["status"] != "success":
            raise ValueError("evidence_not_successful")
        for artifact in item["artifact_ids"]:
            self.read_artifact(session, artifact)
        for dataset in item["input_dataset_ids"] + item["output_dataset_ids"]:
            meta, _ = self.dataset(session, dataset)
            if dataset in item["input_dataset_ids"]:
                source = self.get(session, meta["source_evidence_id"], "evidence")
                if source["status"] != "success":
                    raise ValueError("input_source_not_successful")
        for parent in item["parent_evidence_ids"]:
            self.evidence(session, parent, seen.copy(), require_success=False)
        return item

    def delete_session(self, session: str):
        with self.db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM records WHERE session=? AND kind='artifact'",
                    (session,),
                )
            ]
            runs = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM records WHERE session=? AND kind='run'", (session,)
                )
            ]
            for run in runs:
                db.execute("DELETE FROM operations WHERE run=?", (run,))
                db.execute("DELETE FROM budgets WHERE run=?", (run,))
                db.execute("DELETE FROM call_operations WHERE run=?", (run,))
            db.execute("DELETE FROM records WHERE session=?", (session,))
        for identity in ids:
            (self.objects / identity).unlink(missing_ok=True)

    def prune(self, days: int):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.db() as db:
            sessions = {
                s
                for s, body in db.execute(
                    "SELECT session,body FROM records WHERE kind='run'"
                )
                if json.loads(body)["created_at"] < cutoff
            }
        # Retention is session-level: only remove sessions whose newest run has expired.
        for session in sessions:
            with self.db() as db:
                dates = [
                    json.loads(r[0])["created_at"]
                    for r in db.execute(
                        "SELECT body FROM records WHERE session=? AND kind='run'",
                        (session,),
                    )
                ]
            if max(dates) < cutoff:
                self.delete_session(session)
