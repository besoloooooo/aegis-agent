"""Aegis observability API with a fail-open Langfuse v4 adapter."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol, runtime_checkable

from aegis_agent.env import load_dotenv
from aegis_agent.observability.sanitize import sanitize

logger = logging.getLogger(__name__)


@runtime_checkable
class Observation(Protocol):
    """One active Aegis observation, independent of a concrete backend."""

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
        """Best-effort update. Implementations must never raise."""


@runtime_checkable
class Observability(Protocol):
    """Domain-level tracing interface consumed by the Aegis runtime."""

    @property
    def enabled(self) -> bool: ...

    def agent_run(
        self,
        *,
        task: str,
        session_id: str,
        agent_name: str,
        version: str,
        is_subagent: bool,
    ) -> AbstractContextManager[Observation]: ...

    def model_call(
        self,
        *,
        provider: str,
        model: str | None,
        messages: Any,
    ) -> AbstractContextManager[Observation]: ...

    def tool_call(
        self,
        *,
        tool_name: str,
        arguments: Any,
    ) -> AbstractContextManager[Observation]: ...

    def final_result(self) -> AbstractContextManager[Observation]: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class _NoopObservation:
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
        return None


_NOOP_OBSERVATION = _NoopObservation()


class NoopObservability:
    """Zero-cost implementation used when Langfuse is not configured."""

    enabled = False

    @staticmethod
    @contextmanager
    def _observation() -> Iterator[Observation]:
        yield _NOOP_OBSERVATION

    def agent_run(self, **_: Any) -> AbstractContextManager[Observation]:
        return self._observation()

    def model_call(self, **_: Any) -> AbstractContextManager[Observation]:
        return self._observation()

    def tool_call(self, **_: Any) -> AbstractContextManager[Observation]:
        return self._observation()

    def final_result(self) -> AbstractContextManager[Observation]:
        return self._observation()

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _LangfuseObservation:
    """Exception-swallowing wrapper around one Langfuse observation."""

    def __init__(self, observation: Any) -> None:
        self._observation = observation

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
        kwargs: dict[str, Any] = {"output": sanitize(output)}
        safe_metadata = dict(metadata or {})
        if error is not None:
            kwargs["level"] = "ERROR"
            safe_error = sanitize(str(error), max_string_length=1_000)
            kwargs["status_message"] = (
                safe_error
                if isinstance(safe_error, str)
                else json.dumps(safe_error, ensure_ascii=False)
            )
            safe_metadata.setdefault("error_type", type(error).__name__)
        elif success is False:
            kwargs["level"] = "WARNING"
        if safe_metadata:
            kwargs["metadata"] = sanitize(safe_metadata)
        if usage_details:
            kwargs["usage_details"] = dict(usage_details)
        if cost_details:
            kwargs["cost_details"] = dict(cost_details)
        try:
            self._observation.update(**kwargs)
        except Exception:
            logger.debug("Langfuse observation update failed", exc_info=True)


class LangfuseObservability:
    """Langfuse v4 implementation of the Aegis observability interface."""

    enabled = True

    def __init__(self, client: Any, propagate_attributes: Any | None = None) -> None:
        self._client = client
        self._propagate_attributes = propagate_attributes
        self._agent_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
            "aegis_observability_agent_stack", default=()
        )

    def agent_run(
        self,
        *,
        task: str,
        session_id: str,
        agent_name: str,
        version: str,
        is_subagent: bool,
    ) -> AbstractContextManager[Observation]:
        return self._agent_run(
            task=task,
            session_id=session_id,
            agent_name=agent_name,
            version=version,
            is_subagent=is_subagent,
        )

    @contextmanager
    def _agent_run(
        self,
        *,
        task: str,
        session_id: str,
        agent_name: str,
        version: str,
        is_subagent: bool,
    ) -> Iterator[Observation]:
        name = f"Subagent Run: {agent_name}" if is_subagent else "Aegis Run"
        stack = self._agent_stack.get()
        parent_agent = stack[-1] if stack else None
        propagation = None
        if not is_subagent and self._propagate_attributes is not None:
            propagation = {
                "session_id": session_id,
                "trace_name": "Aegis Run",
                "version": version,
            }
        metadata = {
            "session_id": session_id,
            "agent_name": agent_name,
            "aegis_version": version,
            "is_subagent": is_subagent,
        }
        if parent_agent is not None:
            metadata["parent_agent"] = parent_agent
        with self._start(
            name=name,
            as_type="agent",
            input={"task": task},
            metadata=metadata,
            version=version,
            propagation=propagation,
        ) as observation:
            token = self._agent_stack.set((*stack, agent_name))
            try:
                yield observation
            finally:
                self._agent_stack.reset(token)

    def model_call(
        self,
        *,
        provider: str,
        model: str | None,
        messages: Any,
    ) -> AbstractContextManager[Observation]:
        return self._start(
            name="Model Call",
            as_type="generation",
            input={"messages": messages},
            metadata={"provider": provider},
            model=model,
        )

    def tool_call(
        self,
        *,
        tool_name: str,
        arguments: Any,
    ) -> AbstractContextManager[Observation]:
        return self._start(
            name=f"Tool Call: {tool_name}",
            as_type="tool",
            input=arguments,
            metadata={"tool_name": tool_name},
        )

    def final_result(self) -> AbstractContextManager[Observation]:
        return self._start(
            name="Final Result",
            as_type="span",
            input=None,
        )

    @contextmanager
    def _start(
        self,
        *,
        name: str,
        as_type: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
        version: str | None = None,
        model: str | None = None,
        propagation: Mapping[str, Any] | None = None,
    ) -> Iterator[Observation]:
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": as_type,
            "input": sanitize(input),
            "metadata": sanitize(metadata or {}),
        }
        if version is not None:
            kwargs["version"] = version
        if model is not None:
            kwargs["model"] = model

        try:
            manager = self._client.start_as_current_observation(**kwargs)
            raw_observation = manager.__enter__()
        except Exception:
            logger.debug("Langfuse observation creation failed", exc_info=True)
            yield _NOOP_OBSERVATION
            return

        propagation_manager = None
        if propagation is not None and self._propagate_attributes is not None:
            try:
                propagation_manager = self._propagate_attributes(**propagation)
                propagation_manager.__enter__()
            except Exception:
                logger.debug("Langfuse attribute propagation failed", exc_info=True)
                propagation_manager = None

        try:
            yield _LangfuseObservation(raw_observation)
        finally:
            exc_info = sys.exc_info()
            if propagation_manager is not None:
                try:
                    propagation_manager.__exit__(*exc_info)
                except Exception:
                    logger.debug("Langfuse propagation cleanup failed", exc_info=True)
            try:
                manager.__exit__(*exc_info)
            except Exception:
                logger.debug("Langfuse observation cleanup failed", exc_info=True)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            logger.debug("Langfuse flush failed", exc_info=True)

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception:
            logger.debug("Langfuse shutdown failed", exc_info=True)


def create_observability() -> Observability:
    """Create Langfuse tracing from environment, otherwise return No-op.

    Both credentials are required.  Import, initialization and later reporting
    failures are intentionally contained so observability cannot change Agent
    Runtime behavior.
    """
    load_dotenv()
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        return NoopObservability()

    try:
        from langfuse import Langfuse, propagate_attributes

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=os.environ.get("LANGFUSE_BASE_URL"),
        )
        return LangfuseObservability(client, propagate_attributes)
    except Exception:
        logger.debug("Langfuse initialization failed; observability disabled", exc_info=True)
        return NoopObservability()


__all__ = [
    "LangfuseObservability",
    "NoopObservability",
    "Observability",
    "Observation",
    "create_observability",
]
