from __future__ import annotations

import json
import os

import pytest

from aegis_agent.quality.adapters.harbor import (
    FINAL_RECORD_NAME,
    RUNTIME_RECORD_NAME,
    finalize_harbor_job,
    finalize_harbor_trial,
    sync_harbor_jobs,
)
from aegis_agent.quality.models import (
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionSummary,
    UsageSummary,
)
from aegis_agent.quality.process import ProcessEvaluator
from aegis_agent.quality.store import ExecutionRecordStore


def _trial_result(*, trial_id: str = "trial-123", reward: float = 1.0) -> dict:
    return {
        "id": trial_id,
        "trial_name": "sample__abc123",
        "trial_uri": f"file:///tmp/job/{trial_id}",
        "task_id": {"dataset": "sample", "id": 7},
        "task_name": "sample-task",
        "source": "local",
        "started_at": "2026-09-01T01:00:00Z",
        "finished_at": "2026-09-01T01:00:03Z",
        "config": {"job_id": "job-456"},
        "agent_info": {
            "name": "aegis",
            "version": "0.1.0",
            "model_info": {"name": "qwen3-coder", "provider": "openai-compatible"},
        },
        "agent_result": {
            "n_input_tokens": 110,
            "n_output_tokens": 20,
            "n_cache_tokens": 90,
            "cost_usd": 0.12,
        },
        "verifier_result": {"rewards": {"reward": reward, "task_quality": 0.8}},
    }


def _write_trial(tmp_path, raw: dict, runtime: ExecutionRecord | None = None):
    trial_dir = tmp_path / "jobs" / "job-456" / "trial-123"
    agent_dir = trial_dir / "agent"
    verifier_dir = trial_dir / "verifier"
    artifacts_dir = trial_dir / "artifacts"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir()
    artifacts_dir.mkdir()
    (agent_dir / "aegis.txt").write_text("agent log", encoding="utf-8")
    (verifier_dir / "verifier.txt").write_text("verifier log", encoding="utf-8")
    (artifacts_dir / "answer.txt").write_text("answer", encoding="utf-8")
    if runtime is not None:
        (agent_dir / RUNTIME_RECORD_NAME).write_text(
            runtime.model_dump_json(indent=2),
            encoding="utf-8",
        )
    result_path = trial_dir / "result.json"
    result_path.write_text(json.dumps(raw), encoding="utf-8")
    return result_path


def test_finalize_harbor_trial_merges_runtime_and_verifier_without_losing_cache_buckets(
    tmp_path,
):
    runtime = ExecutionRecord(
        identity=ExecutionIdentity(execution_id="trial-123", trace_id="trace-123"),
        execution=ExecutionSummary(success=True, final_output="done"),
        usage=UsageSummary(
            input_tokens=20,
            output_tokens=20,
            total_tokens=130,
            cache_read_tokens=80,
            cache_write_tokens=10,
            cost=0.12,
        ),
    )
    result_path = _write_trial(tmp_path, _trial_result(), runtime)
    store = ExecutionRecordStore(tmp_path / "records")

    record = finalize_harbor_trial(result_path, store=store)

    assert record.run_kind == "evaluation"
    assert record.identity.execution_id == "trial-123"
    assert record.identity.trial_id == "trial-123"
    assert record.identity.job_id == "job-456"
    assert record.identity.trace_id == "trace-123"
    assert record.identity.task_id == '{"dataset":"sample","id":7}'
    assert record.execution.success is True
    assert record.evaluation.passed is True
    assert record.evaluation.rewards == {"reward": 1.0, "task_quality": 0.8}
    assert record.usage.input_tokens == 20
    assert record.usage.input_tokens_including_cache == 110
    assert record.usage.cache_read_tokens == 80
    assert record.usage.cache_write_tokens == 10
    assert record.usage.cache_tokens == 90
    assert record.usage.cost == 0.12
    assert record.execution.latency_ms == 3000
    assert "agent/aegis.txt" in record.artifacts.logs
    assert "verifier/verifier.txt" in record.artifacts.logs
    assert "artifacts/answer.txt" in record.artifacts.files
    assert (result_path.parent / FINAL_RECORD_NAME).is_file()
    assert store.load("trial-123") == record


def test_verifier_failure_does_not_turn_successful_execution_into_runtime_failure(
    tmp_path,
):
    runtime = ExecutionRecord(
        identity=ExecutionIdentity(execution_id="trial-123"),
        execution=ExecutionSummary(success=True, final_output="wrong answer"),
    )
    result_path = _write_trial(tmp_path, _trial_result(reward=0), runtime)

    record = finalize_harbor_trial(
        result_path,
        store=ExecutionRecordStore(tmp_path / "records"),
    )

    assert record.execution.success is True
    assert record.evaluation.passed is False


def test_harbor_exception_marks_execution_failed(tmp_path):
    raw = _trial_result()
    raw["exception_info"] = {
        "exception_type": "TimeoutError",
        "exception_message": "agent timed out",
    }
    runtime = ExecutionRecord(
        identity=ExecutionIdentity(execution_id="trial-123"),
        execution=ExecutionSummary(success=True),
    )
    result_path = _write_trial(tmp_path, raw, runtime)

    record = finalize_harbor_trial(
        result_path,
        store=ExecutionRecordStore(tmp_path / "records"),
    )

    assert record.execution.success is False
    assert record.execution.exception_type == "TimeoutError"
    assert record.execution.error == "agent timed out"


def test_missing_runtime_record_uses_harbor_combined_usage_without_guessing_buckets(
    tmp_path,
):
    _write_trial(tmp_path, _trial_result())

    records = finalize_harbor_job(
        tmp_path / "jobs",
        store=ExecutionRecordStore(tmp_path / "records"),
    )

    assert len(records) == 1
    usage = records[0].usage
    assert usage.input_tokens is None
    assert usage.input_tokens_including_cache == 110
    assert usage.output_tokens == 20
    assert usage.cache_tokens == 90
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.total_tokens is None


def test_trial_id_mismatch_is_rejected(tmp_path):
    runtime = ExecutionRecord(identity=ExecutionIdentity(execution_id="another-trial"))
    result_path = _write_trial(tmp_path, _trial_result(), runtime)

    with pytest.raises(ValueError, match="does not match"):
        finalize_harbor_trial(
            result_path,
            store=ExecutionRecordStore(tmp_path / "records"),
        )


def test_sparse_harbor_result_keeps_unknown_fields_none(tmp_path):
    raw = {"id": "trial-123", "trial_name": "sparse", "task_name": "task"}
    result_path = _write_trial(tmp_path, raw)

    record = finalize_harbor_trial(
        result_path,
        store=ExecutionRecordStore(tmp_path / "records"),
    )

    assert record.execution.success is None
    assert record.evaluation.passed is None
    assert record.usage.input_tokens is None
    assert record.identity.job_id is None


def test_sync_harbor_jobs_imports_only_new_or_changed_trials(tmp_path):
    result_path = _write_trial(tmp_path, _trial_result())
    store = ExecutionRecordStore(tmp_path / "records")

    first = sync_harbor_jobs(tmp_path / "jobs", store=store)
    second = sync_harbor_jobs(tmp_path / "jobs", store=store)

    assert first["scanned"] == 1
    assert first["imported"] == 1
    assert first["unchanged"] == 0
    assert first["failed"] == 0
    assert first["execution_ids"] == ["trial-123"]
    assert second["imported"] == 0
    assert second["unchanged"] == 1

    changed = _trial_result(reward=0)
    result_path.write_text(json.dumps(changed), encoding="utf-8")
    newest_target = max(
        (result_path.parent / FINAL_RECORD_NAME).stat().st_mtime_ns,
        store.path_for("trial-123").stat().st_mtime_ns,
    )
    os.utime(result_path, ns=(newest_target + 1_000_000, newest_target + 1_000_000))

    third = sync_harbor_jobs(tmp_path / "jobs", store=store)

    assert third["imported"] == 1
    assert third["unchanged"] == 0
    assert store.load("trial-123").evaluation.passed is False


def test_sync_harbor_jobs_contains_bad_results_and_keeps_scanning(tmp_path):
    _write_trial(tmp_path, _trial_result())
    broken = tmp_path / "jobs" / "job-broken" / "trial-broken" / "result.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not-json", encoding="utf-8")

    report = sync_harbor_jobs(
        tmp_path / "jobs",
        store=ExecutionRecordStore(tmp_path / "records"),
    )

    assert report["scanned"] == 2
    assert report["imported"] == 1
    assert report["failed"] == 1
    assert report["errors"][0]["path"] == "job-broken/trial-broken/result.json"


def test_sync_harbor_jobs_rejects_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="Harbor jobs directory not found"):
        sync_harbor_jobs(tmp_path / "missing")


def test_harbor_refinalization_preserves_offline_process_evaluation(tmp_path):
    runtime = ExecutionRecord(
        identity=ExecutionIdentity(execution_id="trial-123"),
        execution=ExecutionSummary(success=True),
    )
    result_path = _write_trial(tmp_path, _trial_result(), runtime)
    original_result = result_path.read_bytes()
    store = ExecutionRecordStore(tmp_path / "records")
    first = finalize_harbor_trial(result_path, store=store)
    process = ProcessEvaluator().evaluate(first)
    store.save(first)
    store.save(first, result_path.parent / FINAL_RECORD_NAME)

    refreshed = finalize_harbor_trial(result_path, store=store)

    assert refreshed.quality.process_evaluation is not None
    assert (
        refreshed.quality.process_evaluation.evaluator_version
        == process.evaluator_version
    )
    assert result_path.read_bytes() == original_result
