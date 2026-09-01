"""ExecutionRecord backend powered by the existing Observability boundary."""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from aegis_agent.observability.sanitize import sanitize
from aegis_agent.observability.tracer import Observation
from aegis_agent.quality.models import (
    ExecutionRecord,
    ExecutionStep,
    UsageSummary,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _latency_ms(started_at: datetime, finished_at: datetime) -> float:
    return max((finished_at - started_at).total_seconds() * 1000.0, 0.0)


class _RecordObservation:
    def __init__(self, recorder: ExecutionRecorder, step: ExecutionStep) -> None:
        self._recorder = recorder
        self._step = step

    def update(
        self,
        *,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        error: BaseException | str | None = None,
        success: bool | None = None,
        usage_details: Mapping[str, int] | None = None,
        cost_details: Mapping[str, float] | None = None,
    ) -> None:
        with self._recorder._lock:
            self._step.output = sanitize(output)
            self._step.success = success
            if metadata:
                self._step.metadata.update(sanitize(dict(metadata)))
            if error is not None:
                self._step.error = str(error)
                self._step.exception_type = type(error).__name__
            if usage_details or cost_details:
                self._step.usage = UsageSummary(
                    input_tokens=(usage_details or {}).get("input"),
                    output_tokens=(usage_details or {}).get("output"),
                    total_tokens=(usage_details or {}).get("total"),
                    cache_read_tokens=(usage_details or {}).get("cache_read_input_tokens"),
                    cache_write_tokens=(usage_details or {}).get("cache_creation_input_tokens"),
                    cost=(cost_details or {}).get("total"),
                )
                self._recorder._recompute_usage()
            self._recorder._apply_record_summary(self._step)


class ExecutionRecorder:
    """An Observability backend that records the same model/tool tree locally."""

    enabled = True

    def __init__(
        self,
        record: ExecutionRecord,
        *,
        on_complete: Callable[[ExecutionRecord], None] | None = None,
    ) -> None:
        self.record = record
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._sequence = 0
        self._stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
            "aegis_execution_record_stack", default=()
        )

    def agent_run(
        self,
        *,
        task: str,
        session_id: str,
        agent_name: str,
        version: str,
        is_subagent: bool,
        execution_id: str | None = None,
        trace_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AbstractContextManager[Observation]:
        stack = self._stack.get()
        is_root = not stack
        if is_root:
            with self._lock:
                self.record.identity.execution_id = execution_id or self.record.identity.execution_id
                self.record.identity.session_id = session_id
                self.record.identity.trace_id = trace_id or self.record.identity.trace_id
                self.record.identity.task_name = self.record.identity.task_name or task
                self.record.agent.name = agent_name
                self.record.agent.version = version
                self.record.execution.started_at = self.record.execution.started_at or _now()
                if metadata:
                    self.record.metadata.update(sanitize(dict(metadata)))
        name = f"Subagent Run: {agent_name}" if is_subagent else "Aegis Run"
        return self._span(
            step_type="agent",
            name=name,
            input={"task": task},
            metadata={"session_id": session_id, "agent_name": agent_name, "is_subagent": is_subagent},
            root=is_root,
        )

    def model_call(
        self,
        *,
        provider: str,
        model: str | None,
        messages: Any,
    ) -> AbstractContextManager[Observation]:
        with self._lock:
            self.record.agent.provider = self.record.agent.provider or provider
            self.record.agent.model = self.record.agent.model or model
        return self._span(
            step_type="model",
            name="Model Call",
            input={"messages": messages},
            metadata={"provider": provider, "model": model},
        )

    def tool_call(self, *, tool_name: str, arguments: Any) -> AbstractContextManager[Observation]:
        return self._span(
            step_type="tool",
            name=f"Tool Call: {tool_name}",
            input=arguments,
            metadata={"tool_name": tool_name},
        )

    def final_result(self) -> AbstractContextManager[Observation]:
        return self._span(step_type="final", name="Final Result", input=None)

    @contextmanager
    def _span(
        self,
        *,
        step_type: Literal["agent", "model", "tool", "final", "span"],
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
        root: bool = False,
    ) -> Iterator[Observation]:
        stack = self._stack.get()
        with self._lock:
            self._sequence += 1
            step = ExecutionStep(
                step_id=uuid.uuid4().hex,
                parent_step_id=stack[-1] if stack else None,
                sequence=self._sequence,
                type=step_type,
                name=name,
                input=sanitize(input),
                started_at=_now(),
                metadata=sanitize(dict(metadata or {})),
            )
            self.record.steps.append(step)
        token = self._stack.set((*stack, step.step_id))
        observation = _RecordObservation(self, step)
        try:
            yield observation
        except BaseException as exc:
            observation.update(error=exc, success=False)
            raise
        finally:
            finished_at = _now()
            with self._lock:
                step.finished_at = finished_at
                step.latency_ms = _latency_ms(step.started_at, finished_at)
                if root:
                    self.record.execution.finished_at = finished_at
                    started = self.record.execution.started_at
                    if started is not None:
                        self.record.execution.latency_ms = _latency_ms(started, finished_at)
            self._stack.reset(token)
            if root:
                self._persist()

    def _apply_record_summary(self, step: ExecutionStep) -> None:
        if step.type == "final" or (step.parent_step_id is None and step.type == "agent"):
            self.record.execution.final_output = step.output
            self.record.execution.success = step.success
            self.record.execution.error = step.error
            self.record.execution.exception_type = step.exception_type
            stop_reason = step.metadata.get("stop_reason")
            if isinstance(stop_reason, str):
                self.record.execution.stop_reason = stop_reason

    def _recompute_usage(self) -> None:
        fields = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
        totals: dict[str, int | None] = {field: None for field in fields}
        cost: float | None = None
        for step in self.record.steps:
            if step.type != "model" or step.usage is None:
                continue
            for field in fields:
                value = getattr(step.usage, field)
                if value is not None:
                    totals[field] = (totals[field] or 0) + value
            if step.usage.cost is not None:
                cost = (cost or 0.0) + step.usage.cost
        self.record.usage = UsageSummary(**totals, cost=cost)

    def _persist(self) -> None:
        if self._on_complete is None:
            return
        try:
            self._on_complete(self.record)
        except Exception:
            logger.debug("ExecutionRecord persistence failed", exc_info=True)

    def flush(self) -> None:
        self._persist()

    def shutdown(self) -> None:
        self._persist()


__all__ = ["ExecutionRecorder"]
