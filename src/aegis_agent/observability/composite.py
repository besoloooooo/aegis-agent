"""Fail-open fan-out for multiple observability backends."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Any

from aegis_agent.observability.tracer import Observability, Observation

logger = logging.getLogger(__name__)


class _CompositeObservation:
    def __init__(self, observations: list[Observation]) -> None:
        self._observations = observations

    def update(self, **kwargs: Any) -> None:
        for observation in self._observations:
            try:
                observation.update(**kwargs)
            except Exception:
                logger.debug("Observability update failed", exc_info=True)


class CompositeObservability:
    """Send the same Runtime observation to independent fail-open sinks."""

    def __init__(self, backends: list[Observability]) -> None:
        self._backends = list(backends)

    @property
    def enabled(self) -> bool:
        return any(backend.enabled for backend in self._backends)

    @contextmanager
    def _open(self, method: str, kwargs: Mapping[str, Any]) -> Iterator[Observation]:
        observations: list[Observation] = []
        with ExitStack() as stack:
            for backend in self._backends:
                try:
                    manager = getattr(backend, method)(**kwargs)
                    observations.append(stack.enter_context(manager))
                except Exception:
                    logger.debug("Observability context creation failed", exc_info=True)
            yield _CompositeObservation(observations)

    def agent_run(self, **kwargs: Any) -> AbstractContextManager[Observation]:
        return self._open("agent_run", kwargs)

    def model_call(self, **kwargs: Any) -> AbstractContextManager[Observation]:
        return self._open("model_call", kwargs)

    def tool_call(self, **kwargs: Any) -> AbstractContextManager[Observation]:
        return self._open("tool_call", kwargs)

    def final_result(self) -> AbstractContextManager[Observation]:
        return self._open("final_result", {})

    def flush(self) -> None:
        for backend in self._backends:
            try:
                backend.flush()
            except Exception:
                logger.debug("Observability flush failed", exc_info=True)

    def shutdown(self) -> None:
        for backend in self._backends:
            try:
                backend.shutdown()
            except Exception:
                logger.debug("Observability shutdown failed", exc_info=True)


__all__ = ["CompositeObservability"]
