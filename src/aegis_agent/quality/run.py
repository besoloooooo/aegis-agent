"""Non-interactive Aegis task execution with a durable ExecutionRecord."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aegis_agent import __version__
from aegis_agent.models.base import ModelProvider
from aegis_agent.observability import (
    CompositeObservability,
    create_observability,
    deterministic_trace_id,
)
from aegis_agent.quality.models import AgentIdentity, ExecutionIdentity, ExecutionRecord
from aegis_agent.quality.recorder import ExecutionRecorder
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.runtime import AgentRuntime, TurnResult
from aegis_agent.sessions.memory_store import InMemorySessionRepository


@dataclass(frozen=True)
class TaskRun:
    """Result and durable record produced by :func:`run_task`."""

    result: TurnResult
    record: ExecutionRecord
    record_path: Path

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.record.identity.execution_id,
            "session_id": self.record.identity.session_id,
            "trace_id": self.record.identity.trace_id,
            "record_path": str(self.record_path),
            "success": self.record.execution.success,
            "final_text": self.result.final_text,
            "stop_reason": self.result.stop_reason.value,
            "iterations": self.result.iterations,
            "tool_calls_made": self.result.tool_calls_made,
            "usage": self.record.usage.model_dump(exclude_none=True),
        }


def run_task(
    instruction: str,
    *,
    provider: ModelProvider,
    session_id: str | None = None,
    execution_id: str | None = None,
    task_id: str | None = None,
    task_name: str | None = None,
    record_path: str | Path | None = None,
    records_dir: str | Path | None = None,
    cwd: str | None = None,
    max_iterations: int = 50,
    allow_dangerous_shell: bool = False,
    enable_skills: bool = False,
    skills_dir: str | None = None,
    enable_mcp: bool = False,
    mcp_config_path: str | None = None,
    enable_subagents: bool = True,
    enable_memory: bool = False,
    metadata: dict[str, Any] | None = None,
) -> TaskRun:
    """Execute exactly one user task and persist its provider-neutral record.

    Langfuse remains a fail-open side channel. The local recorder always runs,
    so Harbor evaluation does not depend on Langfuse availability.
    """
    execution_id = execution_id or str(uuid.uuid4())
    session_id = session_id or execution_id
    trace_id = deterministic_trace_id(execution_id)
    store = ExecutionRecordStore(records_dir)
    explicit_path = Path(record_path).expanduser() if record_path else None
    record = ExecutionRecord(
        identity=ExecutionIdentity(
            execution_id=execution_id,
            task_id=task_id,
            task_name=task_name,
            session_id=session_id,
            trace_id=trace_id,
        ),
        agent=AgentIdentity(
            name="Aegis Agent",
            version=__version__,
            model=_string_or_none(getattr(provider, "model", None)),
            provider=_string_or_none(getattr(provider, "name", None)),
        ),
        metadata=dict(metadata or {}),
    )

    def persist(current: ExecutionRecord) -> None:
        store.save(current)
        if explicit_path is not None:
            store.save(current, explicit_path)

    recorder = ExecutionRecorder(record, on_complete=persist)
    observability = CompositeObservability([create_observability(), recorder])
    runtime = AgentRuntime.with_defaults(
        provider=provider,
        repository=InMemorySessionRepository(),
        max_iterations=max_iterations,
        cwd=cwd,
        allow_dangerous_shell=allow_dangerous_shell,
        enable_skills=enable_skills,
        skills_dir=skills_dir,
        enable_mcp=enable_mcp,
        mcp_config_path=mcp_config_path,
        enable_subagents=enable_subagents,
        enable_memory=enable_memory,
        observability=observability,
    )
    try:
        result = runtime.run_turn(
            session_id,
            instruction,
            execution_id=execution_id,
            trace_id=trace_id,
            trace_metadata={"execution_id": execution_id, **dict(metadata or {})},
        )
    finally:
        runtime.shutdown()

    persisted_path = explicit_path or store.path_for(execution_id)
    return TaskRun(result=result, record=record, record_path=persisted_path)


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


__all__ = ["TaskRun", "run_task"]
