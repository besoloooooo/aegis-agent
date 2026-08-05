"""Lightweight MCP client integration.

Discovers external MCP (Model Context Protocol) servers, converts their tool
schemas, and registers them as native Aegis tools.  The ``mcp`` Python SDK is
an *optional* dependency — when it is not installed all public APIs are no-ops
and :func:`is_available` returns ``False``.

Scope for this milestone: stdio + Streamable HTTP transports only.  SSE, OAuth,
sampling, circuit breaker, and dynamic ``tools/list_changed`` refresh are
explicitly deferred.
"""

from __future__ import annotations

from aegis_agent.mcp.config import load_mcp_config
from aegis_agent.mcp.schema_adapter import convert_mcp_tool, normalize_mcp_input_schema

__all__ = [
    "convert_mcp_tool",
    "is_available",
    "load_mcp_config",
    "normalize_mcp_input_schema",
]


def is_available() -> bool:
    """Return ``True`` when the ``mcp`` SDK is installed and usable."""
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False
