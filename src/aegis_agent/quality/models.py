"""Provider-neutral records for one complete Aegis task execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

RunKind: TypeAlias = Literal["conversation", "task", "evaluation"]
ProcessStatus: TypeAlias = Literal["pass", "warning", "fail", "insufficient_data"]
ProcessSeverity: TypeAlias = Literal["info", "low", "medium", "high", "critical"]


class ExecutionIdentity(BaseModel):
    execution_id: str
    task_id: str | None = None
    task_name: str | None = None
    trial_id: str | None = None
    job_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None


class AgentIdentity(BaseModel):
    name: str | None = None
    version: str | None = None
    model: str | None = None
    provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionSummary(BaseModel):
    started_at: datetime | None = None
    finished_at: datetime | None = None
    latency_ms: float | None = None
    success: bool | None = None
    final_output: Any = None
    error: str | None = None
    exception_type: str | None = None
    stop_reason: str | None = None


class UsageSummary(BaseModel):
    """Mutually-exclusive runtime buckets plus Harbor's combined cache field."""

    input_tokens: int | None = None
    input_tokens_including_cache: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_tokens: int | None = None
    cost: float | None = None


class ExecutionStep(BaseModel):
    step_id: str
    parent_step_id: str | None = None
    sequence: int
    type: Literal["agent", "model", "tool", "final", "span"]
    name: str
    input: Any = None
    output: Any = None
    success: bool | None = None
    error: str | None = None
    exception_type: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    latency_ms: float | None = None
    usage: UsageSummary | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationSummary(BaseModel):
    verifier_result: dict[str, Any] | None = None
    rewards: dict[str, float | int] | None = None
    passed: bool | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class ArtifactSummary(BaseModel):
    logs: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProcessGrade(BaseModel):
    """One explainable, trace-addressable rule or optional judge result."""

    grader_name: str
    grader_version: str
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    status: ProcessStatus
    severity: ProcessSeverity
    category: str
    message: str
    evidence: list[str] = Field(default_factory=list)
    affected_steps: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProcessEvaluationResult(BaseModel):
    """Stable aggregate written independently from Harbor outcome evaluation."""

    schema_version: Literal["1.0"] = "1.0"
    evaluated_at: datetime
    evaluator_version: str
    overall_score: float | None = Field(default=None, ge=0.0, le=1.0)
    status: ProcessStatus
    summary: str
    grades: list[ProcessGrade] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QualitySummary(BaseModel):
    process_evaluation: ProcessEvaluationResult | None = None


class ExecutionRecord(BaseModel):
    """Stable Aegis Quality input assembled from Runtime and Harbor data."""

    schema_version: Literal["1.0"] = "1.0"
    run_kind: RunKind = "task"
    identity: ExecutionIdentity
    agent: AgentIdentity = Field(default_factory=AgentIdentity)
    execution: ExecutionSummary = Field(default_factory=ExecutionSummary)
    usage: UsageSummary = Field(default_factory=UsageSummary)
    steps: list[ExecutionStep] = Field(default_factory=list)
    evaluation: EvaluationSummary = Field(default_factory=EvaluationSummary)
    quality: QualitySummary = Field(default_factory=QualitySummary)
    artifacts: ArtifactSummary = Field(default_factory=ArtifactSummary)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AgentIdentity",
    "ArtifactSummary",
    "EvaluationSummary",
    "ExecutionIdentity",
    "ExecutionRecord",
    "ExecutionStep",
    "ExecutionSummary",
    "ProcessEvaluationResult",
    "ProcessGrade",
    "ProcessSeverity",
    "ProcessStatus",
    "QualitySummary",
    "RunKind",
    "UsageSummary",
]
