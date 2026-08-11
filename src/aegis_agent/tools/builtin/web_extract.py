"""``web_extract`` builtin tool — fetch pages and extract readable content.

REWRITE (behaviour-equivalent to the generic surface of Hermes' ``web_extract``):
``{urls: [...]}`` (≤5) → ``{results: [{url, title, content, error}]}``.  Each URL
is SSRF-checked before fetching; per-URL failures are reported inline (never
raised).  Fetch + extraction is delegated to
:func:`aegis_agent.tools.web.backends.web_extract` (httpx + trafilatura).
Hermes' optional LLM summarisation is not ported.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aegis_agent.models.base import ToolResult
from aegis_agent.tools import schemas
from aegis_agent.tools.registry import ToolContext
from aegis_agent.tools.web import backends

_MAX_URLS = 5


class WebExtractTool:
    """Fetch up to 5 URLs and extract their readable content."""

    definition = schemas.WEB_EXTRACT

    def run(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> ToolResult:
        urls = arguments.get("urls")
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            return _error("web_extract: 'urls' must be a non-empty list of URL strings.")

        urls = urls[:_MAX_URLS]
        results = backends.web_extract(urls)

        # Normalise each entry to {url, title, content, error}.
        normalised = []
        for entry in results:
            normalised.append({
                "url": entry.get("url", ""),
                "title": entry.get("title", ""),
                "content": entry.get("content", ""),
                "error": entry.get("error"),
            })

        succeeded = sum(1 for e in normalised if not e["error"])
        payload = {"results": normalised, "count": succeeded}
        return ToolResult(
            tool_call_id="",
            name=self.definition.name,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _error(message: str) -> ToolResult:
    return ToolResult(
        tool_call_id="",
        name="web_extract",
        content=json.dumps({"error": message}, ensure_ascii=False),
        is_error=True,
    )


__all__ = ["WebExtractTool"]
