"""Context subsystem: build the derived model-facing context."""

from __future__ import annotations

from aegis_agent.context.builder import DEFAULT_SYSTEM_PROMPT, ContextBuilder
from aegis_agent.context.system_prompt import (
    DEFAULT_IDENTITY,
    PromptContributor,
    SystemPromptBuilder,
)

__all__ = [
    "DEFAULT_IDENTITY",
    "DEFAULT_SYSTEM_PROMPT",
    "ContextBuilder",
    "PromptContributor",
    "SystemPromptBuilder",
]
