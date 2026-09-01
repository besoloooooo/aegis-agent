"""Merge Harbor 0.22 TrialResult JSON with Aegis runtime records."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aegis_agent.quality.models import (
    AgentIdentity,
    ArtifactSummary,
    EvaluationSummary,
    ExecutionIdentity,
    ExecutionRecord,
    UsageSummary,
)
from aegis_agent.quality.store import ExecutionRecordStore

RUNTIME_RECORD_NAME = "execution-record.runtime.json"
FINAL_RECORD_NAME = "execution-record.json"


def finalize_harbor_trial(
    result_path: str | Path,
    *,
    store: ExecutionRecordStore | None = None,
) -> ExecutionRecord:
    """Finalize one record from Harbor's result.json and Aegis agent log."""
    result_path = Path(result_path)
    raw = json.loads(result_path.read_text(encoding="utf-8"))
    if "trial_name" not in raw or "task_name" not in raw:
        raise ValueError(f"not a Harbor TrialResult: {result_path}")

    trial_dir = result_path.parent
    runtime_path = trial_dir / "agent" / RUNTIME_RECORD_NAME
    trial_id = str(raw.get("id") or "")
    if runtime_path.is_file():
        record = ExecutionRecord.model_validate_json(runtime_path.read_text(encoding="utf-8"))
        if trial_id and record.identity.execution_id != trial_id:
            raise ValueError(
                "Aegis execution_id does not match Harbor trial id: "
                f"{record.identity.execution_id} != {trial_id}"
            )
    else:
        if not trial_id:
            raise ValueError("Harbor TrialResult does not contain an id")
        record = ExecutionRecord(identity=ExecutionIdentity(execution_id=trial_id))

    config = raw.get("config") or {}
    record.identity.trial_id = trial_id or record.identity.trial_id
    record.identity.job_id = _string_or_none(config.get("job_id"))
    record.identity.task_id = _stable_string(raw.get("task_id"))
    record.identity.task_name = _string_or_none(raw.get("task_name"))
    record.metadata["harbor_trial_name"] = raw.get("trial_name")
    record.metadata["harbor_trial_uri"] = raw.get("trial_uri")
    record.metadata["harbor_source"] = raw.get("source")

    agent_info = raw.get("agent_info") or {}
    model_info = agent_info.get("model_info") or {}
    record.agent = AgentIdentity(
        name=_string_or_none(agent_info.get("name")) or record.agent.name,
        version=_string_or_none(agent_info.get("version")) or record.agent.version,
        model=_string_or_none(model_info.get("name")) or record.agent.model,
        provider=_string_or_none(model_info.get("provider")) or record.agent.provider,
        config=record.agent.config,
        metadata=record.agent.metadata,
    )

    harbor_started_at = _datetime_or_none(raw.get("started_at"))
    harbor_finished_at = _datetime_or_none(raw.get("finished_at"))
    started_at = record.execution.started_at or harbor_started_at
    finished_at = record.execution.finished_at or harbor_finished_at
    exception = raw.get("exception_info") or {}
    if exception:
        record.execution.success = False
        record.execution.error = _string_or_none(exception.get("exception_message"))
        record.execution.exception_type = _string_or_none(exception.get("exception_type"))
    record.execution.started_at = started_at
    record.execution.finished_at = finished_at
    if started_at is not None and finished_at is not None:
        record.execution.latency_ms = max(
            (finished_at - started_at).total_seconds() * 1000.0,
            0.0,
        )
    if harbor_started_at is not None:
        record.metadata["harbor_started_at"] = harbor_started_at.isoformat()
    if harbor_finished_at is not None:
        record.metadata["harbor_finished_at"] = harbor_finished_at.isoformat()

    contexts = _agent_contexts(raw)
    harbor_usage = _aggregate_harbor_usage(contexts)
    if record.usage == UsageSummary():
        record.usage = harbor_usage
    else:
        record.usage.input_tokens_including_cache = harbor_usage.input_tokens_including_cache
        record.usage.cache_tokens = harbor_usage.cache_tokens
        if record.usage.output_tokens is None:
            record.usage.output_tokens = harbor_usage.output_tokens
        if record.usage.cost is None:
            record.usage.cost = harbor_usage.cost

    verifier = raw.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    record.evaluation = EvaluationSummary(
        verifier_result=verifier if isinstance(verifier, dict) else None,
        rewards=rewards if isinstance(rewards, dict) else None,
        passed=_conventional_pass(rewards),
        metrics=record.evaluation.metrics,
    )
    record.artifacts = _artifacts(trial_dir, record.artifacts)

    final_path = trial_dir / FINAL_RECORD_NAME
    local_store = store or ExecutionRecordStore()
    local_store.save(record, final_path)
    local_store.save(record)
    return record


def finalize_harbor_job(
    job_dir: str | Path,
    *,
    store: ExecutionRecordStore | None = None,
) -> list[ExecutionRecord]:
    """Finalize every Harbor TrialResult found below a job directory."""
    records: list[ExecutionRecord] = []
    for result_path in sorted(Path(job_dir).rglob("result.json")):
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "trial_name" not in raw or "task_name" not in raw:
            continue
        records.append(finalize_harbor_trial(result_path, store=store))
    return records


def _agent_contexts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    agent_result = raw.get("agent_result")
    if isinstance(agent_result, dict):
        return [agent_result]
    contexts: list[dict[str, Any]] = []
    for step in raw.get("step_results") or []:
        if isinstance(step, dict) and isinstance(step.get("agent_result"), dict):
            contexts.append(step["agent_result"])
    return contexts


def _aggregate_harbor_usage(contexts: list[dict[str, Any]]) -> UsageSummary:
    def total(field: str) -> int | None:
        values = [value for context in contexts if isinstance((value := context.get(field)), int)]
        return sum(values) if values else None

    costs = [
        float(value)
        for context in contexts
        if isinstance((value := context.get("cost_usd")), (int, float))
        and not isinstance(value, bool)
    ]
    return UsageSummary(
        input_tokens_including_cache=total("n_input_tokens"),
        output_tokens=total("n_output_tokens"),
        cache_tokens=total("n_cache_tokens"),
        cost=sum(costs) if costs else None,
    )


def _conventional_pass(rewards: Any) -> bool | None:
    if not isinstance(rewards, dict):
        return None
    for key in ("pass", "reward"):
        value = rewards.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value > 0
    return None


def _artifacts(trial_dir: Path, existing: ArtifactSummary) -> ArtifactSummary:
    def relative_files(directory: str) -> list[str]:
        root = trial_dir / directory
        if not root.is_dir():
            return []
        return [str(path.relative_to(trial_dir)) for path in sorted(root.rglob("*")) if path.is_file()]

    return ArtifactSummary(
        logs=sorted({*existing.logs, *relative_files("agent"), *relative_files("verifier")}),
        files=sorted({*existing.files, *relative_files("artifacts")}),
        metadata=existing.metadata,
    )


def _datetime_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _stable_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "FINAL_RECORD_NAME",
    "RUNTIME_RECORD_NAME",
    "finalize_harbor_job",
    "finalize_harbor_trial",
]
