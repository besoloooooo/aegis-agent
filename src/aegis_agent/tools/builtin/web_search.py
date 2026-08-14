"""``web_search`` builtin tool — search the web.

REWRITE (behaviour-equivalent to the generic surface of Hermes' ``web_search``):
``{query, limit=5}`` → ``{results: [{title, url, description, position}]}`` or
``{"error": ...}``.  The actual search is delegated to
:func:`aegis_agent.tools.web.backends.web_search` (DuckDuckGo by default; a paid
backend when its API key is set).  Never raises — backend failures come back as
an ``{"error"}`` result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.registry import ToolContext
from aegis_agent.tools.web import backends


class WebSearchTool:
    """Search the web via the configured backend."""

    definition = schemas.WEB_SEARCH

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        query = arguments.get("query")
        if not query or not isinstance(query, str):
            return _error("web_search: missing required field 'query'.")

        limit = _as_int(arguments.get("limit", 5), default=5)

        is_cancelled = context.is_cancelled if context is not None else None
        if is_cancelled is None:
            result = backends.web_search(query, limit)
        else:
            result = backends.web_search(query, limit, is_cancelled=is_cancelled)
        if "error" in result:
            return _error(result["error"])

        payload = {
            "query": query,
            "results": result.get("results", []),
            "count": len(result.get("results", [])),
        }
        if result.get("backend"):
            payload["backend"] = result["backend"]
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="web_search",
        content=json.dumps({"error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["WebSearchTool"]
