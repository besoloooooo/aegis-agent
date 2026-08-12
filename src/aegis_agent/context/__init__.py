"""Context subsystem: build the derived model-facing context, compress it."""

from __future__ import annotations

from aegis_agent.context.builder import DEFAULT_SYSTEM_PROMPT, ContextBuilder
from aegis_agent.context.compress import (
    compress_context,
    dict_to_message,
    estimate_tokens,
    message_to_dict,
)
from aegis_agent.context.prompt_sections import (
    EnvironmentContributor,
    ModelIdentityContributor,
    TaskCompletionContributor,
    TimestampContributor,
    ToolUseEnforcementContributor,
)
from aegis_agent.context.system_prompt import (
    DEFAULT_IDENTITY,
    PromptContributor,
    SystemPromptBuilder,
)

__all__ = [
    "DEFAULT_IDENTITY",
    "DEFAULT_SYSTEM_PROMPT",
    "ContextBuilder",
    "EnvironmentContributor",
    "ModelIdentityContributor",
    "PromptContributor",
    "SystemPromptBuilder",
    "TaskCompletionContributor",
    "TimestampContributor",
    "ToolUseEnforcementContributor",
    "compress_context",
    "dict_to_message",
    "estimate_tokens",
    "message_to_dict",
]
