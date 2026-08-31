"""Model subsystem: core types, the ModelProvider Protocol and the fake provider."""

from __future__ import annotations

from aegis_agent.models.anthropic import AnthropicProvider
from aegis_agent.models.base import (
    ChatResponse,
    Message,
    ModelProvider,
    ModelUsage,
    Role,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.models.openai_compat import OpenAICompatibleProvider
from aegis_agent.models.sanitize import repair_tool_call_arguments, sanitize_surrogates
from aegis_agent.models.stream import StreamAssembler, assemble_stream

__all__ = [
    "AnthropicProvider",
    "ChatResponse",
    "FakeModelProvider",
    "FakeReply",
    "Message",
    "ModelProvider",
    "ModelUsage",
    "OpenAICompatibleProvider",
    "Role",
    "StreamAssembler",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "assemble_stream",
    "repair_tool_call_arguments",
    "sanitize_surrogates",
]
