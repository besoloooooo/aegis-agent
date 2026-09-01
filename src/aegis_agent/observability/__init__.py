"""Fail-open observability interfaces and Langfuse integration.

Runtime modules depend on the small Aegis-facing API exported here, not on the
Langfuse SDK.  :func:`create_observability` returns a no-op implementation when
credentials or the optional SDK are unavailable.
"""

from aegis_agent.observability.composite import CompositeObservability
from aegis_agent.observability.tracer import (
    NoopObservability,
    Observability,
    Observation,
    create_observability,
    deterministic_trace_id,
)

__all__ = [
    "CompositeObservability",
    "NoopObservability",
    "Observability",
    "Observation",
    "create_observability",
    "deterministic_trace_id",
]
