from __future__ import annotations

import json

from typer.testing import CliRunner

from aegis_agent.cli import _resolve_failure_recovery_judge, app
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.observability import CompositeObservability, deterministic_trace_id
from aegis_agent.quality.conversation import (
    ConversationExecutionRecorder,
    persist_session_records,
    reconstruct_session_records,
)
from aegis_agent.quality.process import ProcessEvaluator
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.runtime import AgentRuntime
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.sessions.sqlite_store import SQLiteSessionRepository


def test_live_conversation_recorder_writes_one_record_per_turn(tmp_path):
    repository = InMemorySessionRepository()
    repository.create_session("shared-session")
    store = ExecutionRecordStore(tmp_path)
    recorder = ConversationExecutionRecorder(store=store)
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider([FakeReply(text="one"), FakeReply(text="two")]),
        repository=repository,
        observability=CompositeObservability([recorder]),
        enable_skills=False,
        enable_mcp=False,
        enable_memory=False,
        enable_subagents=False,
    )

    runtime.run_turn(
        "shared-session",
        "first",
        execution_id="turn-one",
        trace_id=deterministic_trace_id("turn-one"),
        trace_metadata={"run_kind": "conversation"},
    )
    runtime.run_turn(
        "shared-session",
        "second",
        execution_id="turn-two",
        trace_id=deterministic_trace_id("turn-two"),
        trace_metadata={"run_kind": "conversation"},
    )

    records = store.list()
    assert {record.identity.execution_id for record in records} == {
        "turn-one",
        "turn-two",
    }
    assert {record.identity.session_id for record in records} == {"shared-session"}
    assert all(record.run_kind == "conversation" for record in records)
    assert {record.identity.task_name for record in records} == {"first", "second"}
    assert all(len([step for step in record.steps if step.type == "agent"]) == 1 for record in records)


def test_live_conversation_evaluation_failure_does_not_drop_record(tmp_path):
    class BrokenEvaluator:
        def evaluate(self, _record):
            raise RuntimeError("judge exploded")

    repository = InMemorySessionRepository()
    repository.create_session("session")
    store = ExecutionRecordStore(tmp_path)
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider([FakeReply(text="saved")]),
        repository=repository,
        observability=CompositeObservability(
            [ConversationExecutionRecorder(store=store, evaluator=BrokenEvaluator())]
        ),
        enable_skills=False,
        enable_mcp=False,
        enable_memory=False,
        enable_subagents=False,
    )

    result = runtime.run_turn("session", "hello", execution_id="safe-turn")

    assert result.final_text == "saved"
    assert store.load("safe-turn").execution.success is True


def test_reconstruct_session_creates_stable_records_and_tool_failures(tmp_path):
    repository = InMemorySessionRepository()
    repository.create_session("history", title="Historical chat")
    repository.append_message(
        "history", Message(role=Role.USER, content="fix it", client_msg_id="u1")
    )
    repository.append_message(
        "history",
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="terminal",
                    arguments='{"command":"bad","password":"secret"}',
                )
            ],
        ),
    )
    repository.append_message(
        "history",
        Message(
            role=Role.TOOL,
            name="terminal",
            tool_call_id="call-1",
            content='{"error":"command failed","exit_code":1}',
        ),
    )
    repository.append_message(
        "history", Message(role=Role.ASSISTANT, content="I could not fix it.")
    )
    repository.append_message(
        "history", Message(role=Role.USER, content="next", client_msg_id="u2")
    )
    repository.append_message(
        "history", Message(role=Role.ASSISTANT, content="done")
    )

    first = reconstruct_session_records(repository, "history")
    second = reconstruct_session_records(repository, "history")

    assert len(first) == 2
    assert [record.identity.execution_id for record in first] == [
        record.identity.execution_id for record in second
    ]
    assert all(record.metadata["reconstructed_from_session"] is True for record in first)
    failed_tool = next(step for step in first[0].steps if step.type == "tool")
    assert failed_tool.success is False
    assert failed_tool.metadata["tool_call_id"] == "call-1"
    assert failed_tool.input["password"] == "[REDACTED]"
    assert first[0].usage.total_tokens is None

    persisted = persist_session_records(
        repository,
        "history",
        store=ExecutionRecordStore(tmp_path),
        evaluator=ProcessEvaluator(),
    )
    assert all(record.quality.process_evaluation is not None for record in persisted)
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_quality_record_session_cli_backfills_existing_sqlite_session(tmp_path):
    db_path = tmp_path / "state.db"
    repository = SQLiteSessionRepository(db_path)
    repository.create_session("old-session")
    repository.append_message(
        "old-session", Message(role=Role.USER, content="old question", client_msg_id="old-u")
    )
    repository.append_message(
        "old-session", Message(role=Role.ASSISTANT, content="old answer")
    )
    repository.close()
    records_dir = tmp_path / "records"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "quality:\n  conversations:\n    evaluate: true\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "quality",
            "record-session",
            "old-session",
            "--db",
            str(db_path),
            "--records-dir",
            str(records_dir),
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["count"] == 1
    assert payload["evaluated"] is True
    record = ExecutionRecordStore(records_dir).load(payload["execution_ids"][0])
    assert record.identity.session_id == "old-session"
    assert record.quality.process_evaluation is not None


def test_interactive_cli_record_conversations_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    records_dir = tmp_path / "records"

    result = CliRunner().invoke(
        app,
        [
            "--model-backend",
            "fake",
            "--no-mcp",
            "--no-memory",
            "--record-conversations",
            "--records-dir",
            str(records_dir),
        ],
        input="hello\nexit\n",
    )

    assert result.exit_code == 0, result.output
    records = ExecutionRecordStore(records_dir).list()
    assert len(records) == 1
    assert records[0].run_kind == "conversation"
    assert records[0].identity.session_id is not None
    assert records[0].identity.trace_id == deterministic_trace_id(
        records[0].identity.execution_id
    )


def test_interactive_conversation_record_and_evaluate_can_come_from_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AEGIS_DB_PATH", str(tmp_path / "state.db"))
    records_dir = tmp_path / "records"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "quality:\n"
        "  conversations:\n"
        "    record: false\n"
        "    evaluate: true\n"
        "  failure_recovery_judge:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "--model-backend",
            "fake",
            "--no-mcp",
            "--no-memory",
            "--mcp-config",
            str(config_path),
            "--records-dir",
            str(records_dir),
        ],
        input="evaluate me\nexit\n",
    )

    assert result.exit_code == 0, result.output
    records = ExecutionRecordStore(records_dir).list()
    assert len(records) == 1
    assert records[0].quality.process_evaluation is not None


def test_judge_provider_model_can_be_selected_from_config(monkeypatch):
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.delenv("AEGIS_MODEL", raising=False)
    warnings: list[str] = []
    cfg = {
        "quality": {
            "failure_recovery_judge": {
                "enabled": True,
                "provider": "openai",
                "model": "judge-model",
                "base_url": "https://judge.invalid/v1",
            }
        }
    }

    provider = _resolve_failure_recovery_judge(None, cfg, warn=warnings.append)

    assert provider is not None
    assert provider.name == "openai-compatible"
    assert provider.model == "judge-model"
    assert warnings == []


def test_judge_disabled_in_config_does_not_build_provider():
    provider = _resolve_failure_recovery_judge(
        None,
        {"quality": {"failure_recovery_judge": {"enabled": False}}},
        warn=lambda _message: None,
    )

    assert provider is None
