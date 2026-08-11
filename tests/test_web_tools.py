"""web_search / web_extract tool tests (backends monkeypatched — no network)."""

from __future__ import annotations

import json

from aegis_agent.tools.builtin import WebExtractTool, WebSearchTool
from aegis_agent.tools.registry import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path))


# -- web_search --------------------------------------------------------------


def test_web_search_returns_results(tmp_path, monkeypatch):
    from aegis_agent.tools.web import backends

    def fake_search(query, limit=5):
        return {
            "results": [
                {"title": "Example", "url": "https://example.com", "description": "An example", "position": 1},
                {"title": "Other", "url": "https://other.com", "description": "Another", "position": 2},
            ],
            "backend": "ddgs",
        }

    monkeypatch.setattr(backends, "web_search", fake_search)
    result = WebSearchTool().run({"query": "test"}, _ctx(tmp_path))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 2
    assert payload["results"][0]["url"] == "https://example.com"
    assert payload["results"][0]["position"] == 1
    assert payload["backend"] == "ddgs"


def test_web_search_requires_query(tmp_path):
    result = WebSearchTool().run({}, _ctx(tmp_path))
    assert result.is_error
    assert "query" in json.loads(result.content)["error"]


def test_web_search_backend_error_surfaces(tmp_path, monkeypatch):
    from aegis_agent.tools.web import backends

    monkeypatch.setattr(backends, "web_search", lambda q, limit=5: {"error": "boom"})
    result = WebSearchTool().run({"query": "x"}, _ctx(tmp_path))
    assert result.is_error
    assert "boom" in json.loads(result.content)["error"]


# -- web_extract -------------------------------------------------------------


def test_web_extract_returns_content(tmp_path, monkeypatch):
    from aegis_agent.tools.web import backends

    def fake_extract(urls):
        return [{"url": u, "title": "T", "content": "# Hello\nSome text"} for u in urls]

    monkeypatch.setattr(backends, "web_extract", fake_extract)
    result = WebExtractTool().run({"urls": ["https://example.com"]}, _ctx(tmp_path))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 1
    assert payload["results"][0]["title"] == "T"
    assert "Hello" in payload["results"][0]["content"]
    assert payload["results"][0]["error"] is None


def test_web_extract_multiple_urls(tmp_path, monkeypatch):
    from aegis_agent.tools.web import backends

    def fake_extract(urls):
        results = []
        for u in urls:
            if "bad" in u:
                results.append({"url": u, "error": "Fetch failed: 404", "title": "", "content": ""})
            else:
                results.append({"url": u, "title": "ok", "content": "body"})
        return results

    monkeypatch.setattr(backends, "web_extract", fake_extract)
    result = WebExtractTool().run({"urls": ["https://a.com", "https://bad.com"]}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert payload["count"] == 1  # only one succeeded
    statuses = {r["url"]: r["error"] for r in payload["results"]}
    assert statuses["https://a.com"] is None
    assert statuses["https://bad.com"] is not None


def test_web_extract_caps_at_five_urls(tmp_path, monkeypatch):
    from aegis_agent.tools.web import backends

    seen = []
    monkeypatch.setattr(backends, "web_extract", lambda urls: (seen.extend(urls) or [{"url": u, "content": "x"} for u in urls]))
    urls = [f"https://example.com/{i}" for i in range(8)]
    WebExtractTool().run({"urls": urls}, _ctx(tmp_path))
    assert len(seen) == 5


def test_web_extract_requires_urls(tmp_path):
    assert WebExtractTool().run({}, _ctx(tmp_path)).is_error
    assert WebExtractTool().run({"urls": []}, _ctx(tmp_path)).is_error
    assert WebExtractTool().run({"urls": "https://x.com"}, _ctx(tmp_path)).is_error


def test_web_extract_blocks_private_url(tmp_path):
    # Use the REAL backend so the SSRF gate runs; a private URL is blocked
    # before any network access.
    result = WebExtractTool().run({"urls": ["http://169.254.169.254/latest/meta-data"]}, _ctx(tmp_path))
    payload = json.loads(result.content)
    assert payload["count"] == 0
    assert "Blocked" in payload["results"][0]["error"]
