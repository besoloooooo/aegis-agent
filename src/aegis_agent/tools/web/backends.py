"""Web search/extract backends — the seam between the web tools and providers.

Original Aegis code (no Hermes derivation): Hermes dispatches through a plugin
registry of paid SDK backends; Aegis instead uses a small, dependency-light
strategy:

  * **search** — DuckDuckGo via the ``ddgs`` package by default (no API key);
    if ``TAVILY_API_KEY`` or ``EXA_API_KEY`` is set, that provider is used via a
    direct ``httpx`` REST call (no vendor SDK).
  * **extract** — ``httpx`` GET + ``trafilatura`` HTML→markdown by default (no
    API key); if ``TAVILY_API_KEY`` is set the Tavily ``/extract`` endpoint is
    used as a batched call (matching Hermes' ``web.extract_backend: tavily``
    configuration).

Both degrade gracefully: a missing optional dependency or provider yields a
clear error dict rather than an exception, so the tool contract (never raise)
holds.  The functions are module-level and monkeypatchable so tests run with no
network access.

Env vars:
  ``TAVILY_API_KEY`` — enables Tavily for both search + extract
  ``TAVILY_BASE_URL`` — override the default ``https://api.tavily.com`` base URL
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import httpx

from aegis_agent.exceptions import OperationCancelled
from aegis_agent.tools.web.url_safety import is_safe_url

logger = logging.getLogger(__name__)

_USER_AGENT = "aegis-agent/0.1 (+https://github.com/aegis-agent)"
_FETCH_TIMEOUT = 30.0
_MAX_EXTRACT_CHARS = 20_000


def search_backend_name() -> str:
    """Return the active search backend id (for diagnostics)."""
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("EXA_API_KEY"):
        return "exa"
    return "ddgs"


def _check_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    """Raise :class:`OperationCancelled` when the cancel flag is set."""
    if is_cancelled is not None and is_cancelled():
        raise OperationCancelled("web request cancelled by interrupt")


def web_search(
    query: str,
    limit: int = 5,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Search the web; return ``{"results": [...]}`` or ``{"error": ...}``.

    Each result is ``{title, url, description, position}``.  Prefers a paid
    provider when its API key is set, otherwise DuckDuckGo via ``ddgs``.
    ``is_cancelled`` (when set) is polled between requests/items and raises
    :class:`~aegis_agent.exceptions.OperationCancelled` to abort early.
    """
    limit = max(1, min(int(limit), 100))
    try:
        _check_cancelled(is_cancelled)
        if os.environ.get("TAVILY_API_KEY"):
            return _search_tavily(query, limit, is_cancelled)
        if os.environ.get("EXA_API_KEY"):
            return _search_exa(query, limit, is_cancelled)
        return _search_ddgs(query, limit, is_cancelled)
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as an error dict, never raise
        logger.warning("web_search failed: %s", exc)
        return {"error": f"web_search failed: {type(exc).__name__}: {exc}"}


def _search_ddgs(
    query: str,
    limit: int,
    is_cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    try:
        from ddgs import DDGS
    except ImportError:
        return {
            "error": "DuckDuckGo search requires the 'ddgs' package. "
                     "Install it with: uv sync --extra web"
        }
    results: list[dict[str, Any]] = []
    with DDGS() as ddgs:
        for i, hit in enumerate(ddgs.text(query, max_results=limit)):
            _check_cancelled(is_cancelled)
            results.append({
                "title": hit.get("title", ""),
                "url": hit.get("href", hit.get("url", "")),
                "description": hit.get("body", hit.get("description", "")),
                "position": i + 1,
            })
    return {"results": results, "backend": "ddgs"}


def _tavily_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the Tavily API and return the parsed JSON response.

    Raises ``ValueError`` when ``TAVILY_API_KEY`` is unset.
    """
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "TAVILY_API_KEY environment variable not set. "
            "Get your API key at https://app.tavily.com/home"
        )
    base = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
    payload = dict(payload)
    payload["api_key"] = api_key
    resp = httpx.post(f"{base}/{endpoint.lstrip('/')}", json=payload, timeout=_FETCH_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _search_tavily(
    query: str,
    limit: int,
    is_cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    _check_cancelled(is_cancelled)
    data = _tavily_request(
        "search",
        {
            "query": query,
            "max_results": min(limit, 20),
            "include_raw_content": False,
            "include_images": False,
        },
    )
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("content", ""),
            "position": i + 1,
        }
        for i, r in enumerate(data.get("results", []))
    ]
    return {"results": results, "backend": "tavily"}


def _search_exa(
    query: str,
    limit: int,
    is_cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    _check_cancelled(is_cancelled)
    api_key = os.environ["EXA_API_KEY"]
    resp = httpx.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"query": query, "numResults": limit},
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("text", "")[:300],
            "position": i + 1,
        }
        for i, r in enumerate(data.get("results", []))
    ]
    return {"results": results, "backend": "exa"}


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def _extract_tavily(
    urls: list[str],
    is_cancelled: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    """Extract content from URLs via the Tavily ``/extract`` endpoint.

    Returns a list of ``{url, title, content, error}`` dicts — one per input URL.
    Per-URL failures are reported inline (never raised).  The Tavily endpoint
    accepts up to 20 URLs per call.
    """
    _check_cancelled(is_cancelled)
    try:
        data = _tavily_request(
            "extract",
            {"urls": urls, "include_images": False},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tavily extract call failed: %s", exc)
        return [{"url": u, "title": "", "content": "", "error": f"Tavily extract failed: {exc}"} for u in urls]

    results: list[dict[str, Any]] = []
    for result in data.get("results", []):
        raw = result.get("raw_content", "") or result.get("content", "")
        content = _strip_base64_images(raw)
        if len(content) > _MAX_EXTRACT_CHARS:
            content = content[:_MAX_EXTRACT_CHARS] + "\n... [content truncated]"
        results.append({
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "content": content,
        })
    for fail in data.get("failed_results", []):
        results.append({
            "url": fail.get("url", ""),
            "title": "",
            "content": "",
            "error": fail.get("error", "extraction failed"),
        })
    for fail_url in data.get("failed_urls", []):
        url_str = fail_url if isinstance(fail_url, str) else str(fail_url)
        results.append({
            "url": url_str,
            "title": "",
            "content": "",
            "error": "extraction failed",
        })
    return results


def _extract_direct(
    url: str,
    is_cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Fetch a single URL and extract readable content via trafilatura."""
    if not is_safe_url(url):
        return {"url": url, "error": "Blocked: URL targets a private/internal address or unsupported scheme."}

    _check_cancelled(is_cancelled)
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — return as a per-URL error, never raise
        return {"url": url, "error": f"Fetch failed: {type(exc).__name__}: {exc}"}

    html = resp.text
    title = ""
    content = ""
    try:
        import trafilatura
        content = trafilatura.extract(
            html, output_format="markdown", include_links=True, favor_recall=True
        ) or ""
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None and getattr(metadata, "title", None):
            title = metadata.title
    except ImportError:
        # Fall back to a raw-text strip when trafilatura is unavailable.
        content = _strip_html(html)
    except Exception as exc:  # noqa: BLE001
        logger.debug("trafilatura extract failed for %s: %s", url, exc)
        content = _strip_html(html)

    if not content:
        content = _strip_html(html)

    content = _strip_base64_images(content)
    if len(content) > _MAX_EXTRACT_CHARS:
        content = content[:_MAX_EXTRACT_CHARS] + "\n... [content truncated]"

    return {"url": url, "title": title, "content": content}


def web_extract(
    urls: list[str],
    is_cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Fetch one or more URLs and extract readable content.

    When ``TAVILY_API_KEY`` is set the call is batched through the Tavily
    ``/extract`` endpoint (up to 20 URLs).  Otherwise each URL is fetched
    individually via ``httpx`` + ``trafilatura``.

    Returns a list of ``{url, title, content, error}`` dicts — one per input
    URL, with per-URL failures reported inline.  Each URL is SSRF-checked
    before any network access.  ``is_cancelled`` (when set) is polled between
    URLs and raises :class:`~aegis_agent.exceptions.OperationCancelled` to
    abort the batch early.
    """
    if not urls:
        return []

    _check_cancelled(is_cancelled)
    if os.environ.get("TAVILY_API_KEY", "").strip():
        return _extract_tavily(urls, is_cancelled)

    results: list[dict[str, Any]] = []
    for u in urls:
        _check_cancelled(is_cancelled)
        results.append(_extract_direct(u, is_cancelled))
    return results


def _strip_html(html: str) -> str:
    """Very small tag-stripper fallback when trafilatura is unavailable."""
    import re as _re
    text = _re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = _re.sub(r"(?s)<[^>]+>", " ", text)
    return _re.sub(r"\s+", " ", text).strip()


def _strip_base64_images(text: str) -> str:
    """Remove inline base64 data-URI images (they blow up the context)."""
    import re as _re
    return _re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "[image]", text)


__all__ = ["search_backend_name", "web_extract", "web_search"]
