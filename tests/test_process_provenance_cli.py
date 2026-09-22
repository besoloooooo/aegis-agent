from __future__ import annotations

import json

import pytest

from aegis_agent.cli import _print_process_evaluation
from aegis_agent.quality.adapters.harbor import (
    RUNTIME_RECORD_NAME,
    finalize_harbor_trial,
)
from aegis_agent.quality.models import (
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionSummary,
)
from aegis_agent.quality.process import ProcessEvaluator
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.quality.viewer import _local_summary


@pytest.mark.parametrize("phase", ["setup", "agent", "verifier"])
def test_harbor_failure_provenance_preserves_source_and_runtime(tmp_path, phase):
    raw = {
        "id": "trial", "task_name": "task", "trial_name": "trial",
        "exception_info": {
            "exception_type": "TimeoutError", "exception_message": "download timeout",
        },
    }
    if phase != "setup":
        raw["agent_execution"] = {"started_at": "2026-09-18T01:00:00Z"}
    if phase == "verifier":
        raw["verifier"] = {"started_at": "2026-09-18T01:01:00Z"}
        # A stale reward cannot establish outcome after an interrupted verifier.
        raw["verifier_result"] = {"rewards": {"reward": 1}}
    trial_path = tmp_path / "result.json"
    trial_path.write_text(json.dumps(raw))
    source = trial_path.read_bytes()
    runtime_path = tmp_path / "agent" / RUNTIME_RECORD_NAME
    if phase != "setup":
        runtime_path.parent.mkdir()
        runtime_path.write_text(ExecutionRecord(
            identity=ExecutionIdentity(execution_id="trial"),
            execution=ExecutionSummary(success=True, final_output="answer"),
        ).model_dump_json())
    runtime_source = runtime_path.read_bytes() if runtime_path.exists() else None

    record = finalize_harbor_trial(trial_path, store=ExecutionRecordStore(tmp_path / "store"))

    assert record.metadata["failure_phase"] == phase
    assert record.metadata["evidence_status"] == {
        "setup": "missing", "agent": "partial", "verifier": "complete",
    }[phase]
    assert record.execution.success is (phase == "verifier")
    assert record.metadata["runtime_success"] is (phase == "verifier")
    assert record.evaluation.passed is None
    assert record.metadata["outcome_success"] is None
    assert record.metadata["infrastructure_error"]["phase"] == phase
    assert trial_path.read_bytes() == source
    if runtime_source is not None:
        assert runtime_path.read_bytes() == runtime_source

    process = ProcessEvaluator().evaluate(record)
    evaluated_runtime = None if phase == "setup" else phase == "verifier"
    assert process.metadata["runtime_success"] is evaluated_runtime
    assert _local_summary(record)["success"] is evaluated_runtime


@pytest.mark.parametrize("mode,reason", [
    ("rules", "deterministic evidence"),
    ("rules+llm", "confirmed evidence"),
    ("skipped", "no_failure_episodes"),
    ("skipped", "no_model_configured"),
    ("fallback", "TimeoutError"),
])
def test_cli_prints_grader_mode_reason_and_judge_telemetry(capsys, mode, reason):
    judge = {} if mode == "rules" else {
        "status": "applied" if mode == "rules+llm" else mode,
        "reason": reason, "model": "fake-model", "latency_ms": 12,
        "usage": {"input_tokens": 10}, "prompt_version": "v2",
        "prompt_truncated": False,
    }
    _print_process_evaluation({
        "execution_id": "trial", "task": "task", "process_evaluation": {
            "status": "pass", "overall_score": 1,
            "evidence_status": "complete", "failure_phase": "verifier",
            "grades": [{"grader_name": "failure_recovery", "status": "pass",
                        "message": reason, "metadata": {
                            "evaluation_mode": mode, "llm_judge": judge,
                        }}],
        },
    })
    output = capsys.readouterr().out
    assert f"[{mode}]" in output
    assert reason in output
    assert "Evidence: complete" in output
    assert "Failure phase: verifier" in output
    if judge:
        for field in ("fake-model", "latency_ms", "input_tokens", "v2", "prompt_truncated"):
            assert field in output
