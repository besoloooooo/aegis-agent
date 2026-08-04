"""Unified exception types for Aegis Agent.

All runtime-raised errors derive from :class:`AegisError` so callers (CLI,
tests, future gateways) can catch one base type.  Modules raise the most
specific subclass; nothing here depends on any other ``aegis_agent`` module.
"""

from __future__ import annotations


class AegisError(Exception):
    """Base class for all Aegis Agent errors."""


class ModelProviderError(AegisError):
    """A model provider failed to produce a usable response.

    Raised by providers themselves (transport/contract failures) and by
    :func:`aegis_agent.events.collect_response` when a streamed ``ERROR``
    event is received.
    """


class ModelTimeoutError(ModelProviderError):
    """A model call exceeded its allotted time.

    A specialization of :class:`ModelProviderError` so the loop's single
    model-error handler catches it uniformly, while callers that care can
    distinguish a timeout from other provider failures.
    """


class OperationCancelled(AegisError):
    """A cooperative interrupt (Ctrl+C / cancel event) stopped an operation.

    Raised while consuming a model stream when the caller's interrupt event is
    set, so a partially-streamed response is discarded rather than persisted.
    """


class SessionNotFoundError(AegisError):
    """An operation referenced a session id that does not exist."""


class ToolExecutionError(AegisError):
    """A tool handler raised unexpectedly.

    The executor normally converts tool exceptions into ``{"error": ...}``
    results rather than raising; this type is reserved for failures in the
    executor/registry machinery itself.
    """


__all__ = [
    "AegisError",
    "ModelProviderError",
    "ModelTimeoutError",
    "OperationCancelled",
    "SessionNotFoundError",
    "ToolExecutionError",
]
