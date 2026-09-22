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
HARBOR_IMPORT_VERSION = "2"


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
    final_path = trial_dir / FINAL_RECORD_NAME
    trial_id = str(raw.get("id") or "")
    local_store = store or ExecutionRecordStore()
    if runtime_path.is_file():
        record = ExecutionRecord.model_validate_json(
            runtime_path.read_text(encoding="utf-8")
        )
        if trial_id and record.identity.execution_id != trial_id:
            raise ValueError(
                "Aegis execution_id does not match Harbor trial id: "
                f"{record.identity.execution_id} != {trial_id}"
            )
    else:
        if not trial_id:
            raise ValueError("Harbor TrialResult does not contain an id")
        record = ExecutionRecord(identity=ExecutionIdentity(execution_id=trial_id))

    if record.quality.process_evaluation is None and trial_id:
        for existing_path in (final_path, local_store.path_for(trial_id)):
            if not existing_path.is_file():
                continue
            try:
                existing = ExecutionRecord.model_validate_json(
                    existing_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if existing.identity.execution_id != trial_id:
                continue
            record.quality.process_evaluation = existing.quality.process_evaluation
            if record.quality.process_evaluation is not None:
                break

    config = raw.get("config") or {}
    record.run_kind = "evaluation"
    record.identity.trial_id = trial_id or record.identity.trial_id
    record.identity.job_id = _string_or_none(config.get("job_id"))
    record.identity.task_id = _stable_string(raw.get("task_id"))
    record.identity.task_name = _string_or_none(raw.get("task_name"))
    record.metadata["harbor_trial_name"] = raw.get("trial_name")
    record.metadata["harbor_trial_uri"] = raw.get("trial_uri")
    record.metadata["harbor_source"] = raw.get("source")
    record.metadata["harbor_import_version"] = HARBOR_IMPORT_VERSION

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
    failure_phase = (
        _failure_phase(raw, record) if exception
        else "agent" if record.execution.success is False else None
    )
    if exception and failure_phase != "verifier":
        record.execution.success = False
        record.execution.error = _string_or_none(exception.get("exception_message"))
        record.execution.exception_type = _string_or_none(
            exception.get("exception_type")
        )
    record.metadata["failure_phase"] = failure_phase
    record.metadata["runtime_success"] = record.execution.success
    record.metadata["evidence_status"] = (
        "missing" if not runtime_path.is_file() or not (
            record.steps or record.execution.final_output
        ) else "complete" if record.execution.success is True
        and failure_phase not in {"setup", "agent"} else "partial"
    )
    record.metadata["infrastructure_error"] = (
        {"phase": failure_phase, **exception} if exception else None
    )
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
        record.usage.input_tokens_including_cache = (
            harbor_usage.input_tokens_including_cache
        )
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
        passed=None if exception else _conventional_pass(rewards),
        metrics=record.evaluation.metrics,
    )
    record.metadata["outcome_success"] = record.evaluation.passed
    record.artifacts = _artifacts(trial_dir, record.artifacts)

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


def sync_harbor_jobs(
    jobs_dir: str | Path,
    *,
    store: ExecutionRecordStore | None = None,
) -> dict[str, Any]:
    """Import new or changed Harbor TrialResults below one jobs directory."""
    root = Path(jobs_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Harbor jobs directory not found: {root}")

    local_store = store or ExecutionRecordStore()
    report: dict[str, Any] = {
        "jobs_dir": str(root),
        "scanned": 0,
        "imported": 0,
        "unchanged": 0,
        "ignored": 0,
        "failed": 0,
        "execution_ids": [],
        "errors": [],
    }
    for result_path in sorted(root.rglob("result.json")):
        report["scanned"] += 1
        if result_path.is_symlink():
            report["ignored"] += 1
            continue
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _record_sync_error(report, root, result_path, exc)
            continue
        if (
            not isinstance(raw, dict)
            or "trial_name" not in raw
            or "task_name" not in raw
        ):
            report["ignored"] += 1
            continue
        trial_id = _string_or_none(raw.get("id"))
        if not trial_id:
            _record_sync_error(
                report,
                root,
                result_path,
                ValueError("Harbor TrialResult does not contain an id"),
            )
            continue

        try:
            runtime_path = result_path.parent / "agent" / RUNTIME_RECORD_NAME
            source_mtime = max(
                result_path.stat().st_mtime_ns,
                runtime_path.stat().st_mtime_ns if runtime_path.is_file() else 0,
            )
            final_path = result_path.parent / FINAL_RECORD_NAME
            central_path = local_store.path_for(trial_id)
            if _current_harbor_record(
                final_path, trial_id, source_mtime
            ) and _current_harbor_record(central_path, trial_id, source_mtime):
                report["unchanged"] += 1
                continue
            record = finalize_harbor_trial(result_path, store=local_store)
        except (OSError, ValueError) as exc:
            _record_sync_error(report, root, result_path, exc)
            continue
        report["imported"] += 1
        report["execution_ids"].append(record.identity.execution_id)
    return report


def _current_harbor_record(path: Path, trial_id: str, source_mtime: int) -> bool:
    if not path.is_file() or path.stat().st_mtime_ns < source_mtime:
        return False
    try:
        record = ExecutionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        record.identity.execution_id == trial_id
        and record.run_kind == "evaluation"
        and record.metadata.get("harbor_import_version") == HARBOR_IMPORT_VERSION
    )


def _failure_phase(raw: dict[str, Any], record: ExecutionRecord) -> str:
    """Use Harbor phase timings, with conservative legacy-record fallbacks."""
    sources = [raw, *(raw.get("step_results") or [])]
    for source in reversed(sources):
        if not isinstance(source, dict):
            continue
        for phase, field in (("verifier", "verifier"), ("agent", "agent_execution")):
            timing = source.get(field)
            if isinstance(timing, dict) and timing.get("started_at"):
                return phase
    exception_type = str((raw.get("exception_info") or {}).get("exception_type", ""))
    if any(word in exception_type.lower() for word in ("verifier", "reward")):
        return "verifier"
    if record.steps or record.execution.success is not None or raw.get("agent_result"):
        return "agent"
    return "setup"


def _record_sync_error(
    report: dict[str, Any],
    root: Path,
    result_path: Path,
    exc: Exception,
) -> None:
    report["failed"] += 1
    if len(report["errors"]) < 20:
        report["errors"].append(
            {
                "path": str(result_path.relative_to(root)),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


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
        values = [
            value
            for context in contexts
            if isinstance((value := context.get(field)), int)
        ]
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
        return [
            str(path.relative_to(trial_dir))
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]

    return ArtifactSummary(
        logs=sorted(
            {*existing.logs, *relative_files("agent"), *relative_files("verifier")}
        ),
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
    "sync_harbor_jobs",
]
