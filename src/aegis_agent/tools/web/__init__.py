"""Web tool support: SSRF safety gate and search/extract backends."""

from __future__ import annotations

from aegis_agent.tools.web.url_safety import is_safe_url

__all__ = ["is_safe_url"]
