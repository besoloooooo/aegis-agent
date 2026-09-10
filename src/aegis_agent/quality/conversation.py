"""Opt-in ExecutionRecord capture and reconstruction for conversations."""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from aegis_agent import __version__
from aegis_agent.models.base import Message, Role, ToolCall
from aegis_agent.observability import NoopObservability, deterministic_trace_id
from aegis_agent.observability.sanitize import sanitize
from aegis_agent.observability.tracer import Observation
from aegis_agent.quality.models import (
    AgentIdentity,
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionStep,
    ExecutionSummary,
)
from aegis_agent.quality.recorder import ExecutionRecorder
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.sessions.repository import SessionRepository

logger = logging.getLogger(__name__)


class ProcessRecordEvaluator(Protocol):
    """Small boundary needed for optional post-turn process evaluation."""

    def evaluate(self, record: ExecutionRecord) -> Any: ...


class ConversationExecutionRecorder:
    """Create one durable ExecutionRecord for each root conversation turn.

    The backend is attached to a long-lived interactive runtime, while the
    existing :class:`ExecutionRecorder` remains deliberately single-record.
    A context variable routes nested model/tool/subagent observations to the
    recorder belonging to the current root turn.
    """

    enabled = True

    def __init__(
        self,
        *,
        store: ExecutionRecordStore,
        evaluator: ProcessRecordEvaluator | None = None,
    ) -> None:
        self._store = store
        self._evaluator = evaluator
        self._noop = NoopObservability()
        self._active: contextvars.ContextVar[ExecutionRecorder | None] = (
            contextvars.ContextVar("aegis_conversation_execution_recorder", default=None)
        )

    def agent_run(self, **kwargs: Any) -> AbstractContextManager[Observation]:
        active = self._active.get()
        if active is not None:
            return active.agent_run(**kwargs)
        if bool(kwargs.get("is_subagent")):
            return self._noop.agent_run(**kwargs)
        return self._root_agent_run(kwargs)

    @contextmanager
    def _root_agent_run(self, kwargs: Mapping[str, Any]) -> Iterator[Observation]:
        execution_id = _string_or_none(kwargs.get("execution_id")) or str(uuid.uuid4())
        trace_id = _string_or_none(kwargs.get("trace_id")) or deterministic_trace_id(
            execution_id
        )
        task = str(kwargs.get("task") or "")
        metadata = dict(kwargs.get("metadata") or {})
        metadata["run_kind"] = "conversation"
        record = ExecutionRecord(
            run_kind="conversation",
            identity=ExecutionIdentity(
                execution_id=execution_id,
                session_id=_string_or_none(kwargs.get("session_id")),
                trace_id=trace_id,
                task_name=_sanitized_preview(task),
            ),
            agent=AgentIdentity(
                name=_string_or_none(kwargs.get("agent_name")),
                version=_string_or_none(kwargs.get("version")),
            ),
            metadata=sanitize(metadata),
        )
        recorder = ExecutionRecorder(record, on_complete=self._complete)
        token = self._active.set(recorder)
        delegated = dict(kwargs)
        delegated["execution_id"] = execution_id
        delegated["trace_id"] = trace_id
        delegated["metadata"] = metadata
        try:
            with recorder.agent_run(**delegated) as observation:
                yield observation
        finally:
            self._active.reset(token)

    def model_call(self, **kwargs: Any) -> AbstractContextManager[Observation]:
        active = self._active.get()
        return (
            active.model_call(**kwargs)
            if active is not None
            else self._noop.model_call(**kwargs)
        )

    def tool_call(self, **kwargs: Any) -> AbstractContextManager[Observation]:
        active = self._active.get()
        return (
            active.tool_call(**kwargs)
            if active is not None
            else self._noop.tool_call(**kwargs)
        )

    def final_result(self) -> AbstractContextManager[Observation]:
        active = self._active.get()
        return active.final_result() if active is not None else self._noop.final_result()

    def flush(self) -> None:
        active = self._active.get()
        if active is not None:
            active.flush()

    def shutdown(self) -> None:
        self.flush()

    def _complete(self, record: ExecutionRecord) -> None:
        if self._evaluator is not None:
            try:
                self._evaluator.evaluate(record)
            except Exception:
                logger.warning(
                    "Conversation process evaluation failed for %s; saving raw record",
                    record.identity.execution_id,
                    exc_info=True,
                )
        self._store.save(record)


def reconstruct_session_records(
    repository: SessionRepository,
    session_id: str,
) -> list[ExecutionRecord]:
    """Reconstruct one conservative ExecutionRecord per persisted user turn.

    Session history contains canonical messages and tool-call correlation, but
    not historical observability timings, usage, cost, provider, or exact model
    request payloads. Those fields therefore remain unknown and the limitation
    is recorded explicitly in metadata.
    """

    session = repository.get_session(session_id)
    if session is None:
        from aegis_agent.exceptions import SessionNotFoundError

        raise SessionNotFoundError(f"session not found: {session_id}")
    messages = repository.list_messages(session_id)
    timestamps = _message_timestamps(repository, session_id, len(messages))
    turns = _partition_turns(messages)
    records: list[ExecutionRecord] = []
    for turn_index, (start_index, turn_messages) in enumerate(turns, start=1):
        turn_timestamps = timestamps[start_index : start_index + len(turn_messages)]
        records.append(
            _reconstruct_turn(
                session_id=session_id,
                session_title=session.title,
                turn_index=turn_index,
                messages=turn_messages,
                timestamps=turn_timestamps,
                session_created_at=session.created_at,
            )
        )
    return records


def persist_session_records(
    repository: SessionRepository,
    session_id: str,
    *,
    store: ExecutionRecordStore,
    evaluator: ProcessRecordEvaluator | None = None,
) -> list[ExecutionRecord]:
    """Reconstruct, optionally evaluate, and idempotently persist a session."""

    records = reconstruct_session_records(repository, session_id)
    for record in records:
        if evaluator is not None:
            evaluator.evaluate(record)
        store.save(record)
    return records


def _partition_turns(messages: Sequence[Message]) -> list[tuple[int, list[Message]]]:
    turns: list[tuple[int, list[Message]]] = []
    start: int | None = None
    current: list[Message] = []
    for index, message in enumerate(messages):
        if message.role is Role.USER:
            if start is not None:
                turns.append((start, current))
            start = index
            current = [message]
        elif start is not None:
            current.append(message)
    if start is not None:
        turns.append((start, current))
    return turns


def _reconstruct_turn(
    *,
    session_id: str,
    session_title: str | None,
    turn_index: int,
    messages: Sequence[Message],
    timestamps: Sequence[datetime | None],
    session_created_at: float,
) -> ExecutionRecord:
    user = messages[0]
    identity_seed = user.client_msg_id or str(user.seq if user.seq is not None else turn_index)
    execution_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aegis:conversation-turn:{session_id}:{identity_seed}",
        )
    )
    fallback_time = datetime.fromtimestamp(session_created_at, tz=UTC)
    started_at = next((value for value in timestamps if value is not None), fallback_time)
    finished_at = next(
        (value for value in reversed(timestamps) if value is not None), started_at
    )
    record = ExecutionRecord(
        run_kind="conversation",
        identity=ExecutionIdentity(
            execution_id=execution_id,
            session_id=session_id,
            trace_id=deterministic_trace_id(execution_id),
            task_name=_sanitized_preview(user.content),
        ),
        agent=AgentIdentity(name="Aegis Agent", version=__version__),
        execution=ExecutionSummary(started_at=started_at, finished_at=finished_at),
        metadata={
            "source": "session-backfill",
            "session_title": session_title,
            "conversation_turn_index": turn_index,
            "reconstructed_from_session": True,
            "reconstruction_limitations": [
                "provider/model, usage, cost, and exact call latency were not retained",
                "model request contexts and tool success are reconstructed conservatively",
            ],
        },
    )
    sequence = 1
    root = ExecutionStep(
        step_id=f"{execution_id}:agent",
        sequence=sequence,
        type="agent",
        name="Aegis Run",
        input=sanitize({"task": user.content}),
        started_at=started_at,
        finished_at=finished_at,
        metadata={
            "session_id": session_id,
            "agent_name": "Aegis Agent",
            "is_subagent": False,
            "reconstructed": True,
        },
    )
    record.steps.append(root)
    pending_calls: dict[str, ToolCall] = {}
    final_text: str | None = None

    for offset, message in enumerate(messages[1:], start=1):
        timestamp = timestamps[offset] if offset < len(timestamps) else None
        step_time = timestamp or started_at
        if message.role is Role.ASSISTANT:
            sequence += 1
            output_calls = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ]
            record.steps.append(
                ExecutionStep(
                    step_id=f"{execution_id}:model:{sequence}",
                    parent_step_id=root.step_id,
                    sequence=sequence,
                    type="model",
                    name="Model Call",
                    output=sanitize(
                        {
                            "content": message.content,
                            "tool_calls": output_calls,
                            "finish_reason": "tool_calls" if output_calls else "stop",
                        }
                    ),
                    success=True,
                    started_at=step_time,
                    finished_at=step_time,
                    metadata={"reconstructed": True},
                )
            )
            pending_calls.update({call.id: call for call in message.tool_calls})
            if not message.tool_calls:
                final_text = message.content
        elif message.role is Role.TOOL:
            call = pending_calls.pop(message.tool_call_id or "", None)
            tool_name = message.name or (call.name if call is not None else "unknown")
            arguments = call.arguments if call is not None else None
            success, error = _infer_historical_tool_result(message.content)
            sequence += 1
            record.steps.append(
                ExecutionStep(
                    step_id=f"{execution_id}:tool:{sequence}",
                    parent_step_id=root.step_id,
                    sequence=sequence,
                    type="tool",
                    name=f"Tool Call: {tool_name}",
                    input=sanitize(arguments),
                    output=sanitize(message.content),
                    success=success,
                    error=sanitize(error),
                    started_at=step_time,
                    finished_at=step_time,
                    metadata={
                        "tool_name": tool_name,
                        "tool_call_id": message.tool_call_id,
                        "reconstructed": True,
                        "success_inferred": True,
                    },
                )
            )

    if final_text is not None:
        sequence += 1
        record.steps.append(
            ExecutionStep(
                step_id=f"{execution_id}:final",
                parent_step_id=root.step_id,
                sequence=sequence,
                type="final",
                name="Final Result",
                output=sanitize(final_text),
                success=True,
                started_at=finished_at,
                finished_at=finished_at,
                metadata={"stop_reason": "final_answer", "reconstructed": True},
            )
        )
        root.output = sanitize(final_text)
        root.success = True
        root.metadata["stop_reason"] = "final_answer"
        record.execution.success = True
        record.execution.final_output = sanitize(final_text)
        record.execution.stop_reason = "final_answer"
    return record


def _infer_historical_tool_result(content: str) -> tuple[bool, str | None]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return True, None
    if not isinstance(payload, Mapping):
        return True, None
    error = payload.get("error")
    failed = error is not None and error is not False and error != ""
    failed = failed or payload.get("success") is False
    status = payload.get("status")
    if not failed and isinstance(status, str) and status in {
        "error",
        "failed",
        "not_found",
    }:
        failed = True
    if not failed:
        exit_code = payload.get("exit_code")
        failed = (
            isinstance(exit_code, int)
            and exit_code != 0
            and not payload.get("exit_code_meaning")
        )
    return (False, str(error or content)) if failed else (True, None)


def _message_timestamps(
    repository: SessionRepository,
    session_id: str,
    expected: int,
) -> list[datetime | None]:
    getter = getattr(repository, "get_messages_dict", None)
    if not callable(getter):
        return [None] * expected
    try:
        rows = getter(session_id)
    except Exception:  # noqa: BLE001 - timestamps are optional reconstruction evidence
        return [None] * expected
    values: list[datetime | None] = []
    for row in rows[:expected]:
        raw = row.get("timestamp") if isinstance(row, Mapping) else None
        if not isinstance(raw, (int, float, str)):
            values.append(None)
            continue
        try:
            values.append(datetime.fromtimestamp(float(raw), tz=UTC))
        except (TypeError, ValueError, OSError):
            values.append(None)
    values.extend([None] * (expected - len(values)))
    return values


def _preview(value: str, limit: int = 160) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"


def _sanitized_preview(value: str) -> str:
    rendered = sanitize(_preview(value))
    return str(rendered)


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "ConversationExecutionRecorder",
    "persist_session_records",
    "reconstruct_session_records",
]
