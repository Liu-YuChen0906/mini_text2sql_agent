"""Versioned protocols. Identity and verification fields are server-owned."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator, with_config
from typing_extensions import NotRequired, TypedDict


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Protocol(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: int = 1


@with_config(ConfigDict(extra="forbid"))
class MetricDefinition(TypedDict):
    name: str
    definition: NotRequired[str]
    unit: NotRequired[str]


@with_config(ConfigDict(extra="forbid"))
class AnalysisPeriod(TypedDict):
    start: str
    end: str


@with_config(ConfigDict(extra="forbid"))
class UnspecifiedPeriod(TypedDict):
    pass


class AnalysisTask(Protocol):
    question: str
    objective: str = ""
    metrics: list[MetricDefinition] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    time_range: AnalysisPeriod | UnspecifiedPeriod = Field(default_factory=dict)
    comparison_range: AnalysisPeriod | UnspecifiedPeriod = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    granularity: str = ""
    input_dataset_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    requested_outputs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_ranges(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone 必须是有效的 IANA 时区") from exc
        for interval in (self.time_range, self.comparison_range):
            if interval:
                if set(interval) != {"start", "end"}:
                    raise ValueError(
                        "time ranges require start and end (left closed, right open)"
                    )
                start, end = [
                    datetime.fromisoformat(interval[k]) for k in ("start", "end")
                ]
                if (start.tzinfo is None) != (end.tzinfo is None):
                    raise ValueError(
                        "同一时间区间的起止时间必须同时带时区或同时不带时区"
                    )
                if start >= end:
                    raise ValueError("time range start must precede end")
        return self


class DatasetRecord(Protocol):
    dataset_id: str = Field(default_factory=lambda: uid("ds"))
    session_id: str
    run_id: str
    created_at: str = Field(default_factory=now)
    source_evidence_id: str
    parent_dataset_ids: list[str] = Field(default_factory=list)
    columns: list[dict[str, str]]
    row_count: int
    preview: list[list[Any]]
    preview_truncated: bool
    data_artifact_id: str
    content_sha256: str
    source_truncated: bool = False
    coverage: dict[str, Any] = Field(default_factory=dict)
    quality_warnings: list[str] = Field(default_factory=list)
    units: dict[str, str] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    serialization: str = (
        "JSON: null preserved; Decimal lossless string; dates ISO8601; bytes hex"
    )


class ExecutionEvidence(Protocol):
    evidence_id: str = Field(default_factory=lambda: uid("ev"))
    session_id: str
    run_id: str
    operation_id: str
    attempt: int = 1
    kind: Literal["sql", "analysis_tool", "python"]
    tool_name: str
    tool_version: str = "1"
    input_dataset_ids: list[str] = Field(default_factory=list)
    parent_evidence_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    started_at: str = Field(default_factory=now)
    finished_at: str | None = None
    status: Literal["running", "success", "error", "timeout", "cancelled"] = "running"
    result: dict[str, Any] = Field(default_factory=dict)
    output_dataset_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class ClaimValue(Protocol):
    name: str
    value: float
    unit: str
    evidence_id: str = Field(
        description="必须使用包含此数值的同一次实际工具返回的 evidence_id"
    )
    result_pointer: str = Field(
        description="从该证据对象根开始的 JSON Pointer，例如 /result/delta；字段名须以实际工具结果为准"
    )
    display_precision: int = Field(default=2, ge=0, le=8)


class ClaimInput(Protocol):
    kind: Literal["fact", "interpretation", "hypothesis", "recommendation"] = "fact"
    text: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    fact_refs: list[str] = Field(default_factory=list)
    values: list[ClaimValue] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scope: Literal["population", "sample"] = "population"


class Claim(ClaimInput):
    claim_id: str = Field(default_factory=lambda: uid("claim"))
    verification_status: Literal["verified", "supported", "unverified", "rejected"] = (
        "unverified"
    )


class AnalysisReport(Protocol):
    report_id: str = Field(default_factory=lambda: uid("report"))
    session_id: str
    run_id: str
    status: Literal["success", "partial", "needs_clarification", "failed", "cancelled"]
    task: AnalysisTask
    claims: list[Claim] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class AnalysisConfig(Protocol):
    enabled: bool = False
    evidence_dir: str = "var/analysis"
    model: dict[str, Any] | None = None
    image: str = "betterchatbi-analysis:1"
    timeout_seconds: float = Field(default=30, gt=0)
    cpus: float = Field(default=1, gt=0)
    memory_mib: int = Field(default=512, ge=64)
    pids: int = Field(default=64, ge=16)
    output_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    file_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    file_count: int = Field(default=32, ge=1)
    log_bytes: int = Field(default=65536, ge=1024)
    max_query_rows: int = Field(default=10000, ge=1)
    max_query_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_tool_calls: int = Field(default=20, ge=1)
    max_model_calls: int = Field(default=20, ge=1)
    run_seconds: float = Field(default=180, gt=0)
    max_repairs: int = Field(default=2, ge=0, le=2)
    retention_days: int = Field(default=30, ge=1)
