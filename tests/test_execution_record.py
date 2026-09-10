from __future__ import annotations

import hashlib
import json

from typer.testing import CliRunner

from aegis_agent.cli import app
from aegis_agent.models.base import ModelUsage
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.observability import (
    CompositeObservability,
    NoopObservability,
    deterministic_trace_id,
)
from aegis_agent.quality.models import ExecutionIdentity, ExecutionRecord
from aegis_agent.quality.recorder import ExecutionRecorder
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.runtime import AgentRuntime


def _runtime(provider: FakeModelProvider, recorder: ExecutionRecorder) -> AgentRuntime:
    return AgentRuntime.with_defaults(
        provider=provider,
        observability=CompositeObservability([NoopObservability(), recorder]),
        enable_skills=False,
        enable_mcp=False,
        enable_memory=False,
        enable_subagents=False,
    )


def test_runtime_observability_builds_and_persists_execution_record(tmp_path):
    execution_id = "trial-123"
    trace_id = deterministic_trace_id(execution_id)
    record = ExecutionRecord(identity=ExecutionIdentity(execution_id=execution_id))
    assert record.run_kind == "task"
    store = ExecutionRecordStore(tmp_path)
    recorder = ExecutionRecorder(record, on_complete=store.save)
    provider = FakeModelProvider(
        script=[
            FakeReply.tool("missing_tool", {"password": "secret"}, call_id="bad"),
            FakeReply(
                text="recovered",
                usage=ModelUsage(
                    input_tokens=10,
                    output_tokens=2,
                    total_tokens=22,
                    cache_read_tokens=8,
                    cache_write_tokens=2,
                    cost=0.25,
                ),
            ),
        ]
    )

    result = _runtime(provider, recorder).run_turn(
        "harbor-session",
        "do the task",
        execution_id=execution_id,
        trace_id=trace_id,
        trace_metadata={"harbor_trial_id": execution_id},
    )

    assert result.final_text == "recovered"
    saved = store.load(execution_id)
    assert saved.identity.session_id == "harbor-session"
    assert saved.identity.trace_id == trace_id
    assert saved.execution.success is True
    assert saved.execution.final_output == "recovered"
    assert saved.usage.input_tokens == 10
    assert saved.usage.output_tokens == 2
    assert saved.usage.cache_read_tokens == 8
    assert saved.usage.cache_write_tokens == 2
    assert saved.usage.cost == 0.25
    tool = next(step for step in saved.steps if step.type == "tool")
    assert tool.success is False
    assert "Unknown tool" in (tool.error or "")
    assert tool.input["password"] == "[REDACTED]"
    assert all(step.finished_at is not None for step in saved.steps)


def test_missing_usage_and_no_langfuse_do_not_change_runtime(tmp_path):
    record = ExecutionRecord(identity=ExecutionIdentity(execution_id="no-usage"))
    recorder = ExecutionRecorder(
        record,
        on_complete=ExecutionRecordStore(tmp_path).save,
    )

    result = _runtime(
        FakeModelProvider(script=[FakeReply(text="ok")]),
        recorder,
    ).run_turn("session", "hello", execution_id="no-usage")

    assert result.final_text == "ok"
    assert record.usage.input_tokens is None
    assert record.usage.cost is None


def test_deterministic_trace_id_matches_langfuse_seed_contract():
    execution_id = "594025f3-7d65-4655-8576-4bee95002eae"
    expected = hashlib.sha256(execution_id.encode()).digest()[:16].hex()

    assert deterministic_trace_id(execution_id) == expected


def test_noninteractive_cli_uses_selected_provider_and_writes_record(tmp_path):
    output_path = tmp_path / "harbor" / "execution-record.runtime.json"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--instruction",
            "hello",
            "--model-backend",
            "fake",
            "--execution-id",
            "trial-cli",
            "--record-path",
            str(output_path),
            "--records-dir",
            str(tmp_path / "records"),
            "--run-kind",
            "evaluation",
            "--no-subagents",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["execution_id"] == "trial-cli"
    assert payload["success"] is True
    assert payload["final_text"] == "Echo: hello"
    record = ExecutionRecord.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert record.agent.provider == "fake"
    assert record.run_kind == "evaluation"
    assert record.identity.trace_id == deterministic_trace_id("trial-cli")


def test_noninteractive_cli_rejects_unknown_run_kind():
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--instruction",
            "hello",
            "--model-backend",
            "fake",
            "--run-kind",
            "benchmark-ish",
        ],
    )

    assert result.exit_code == 2
    assert "unsupported run kind: benchmark-ish" in result.output
