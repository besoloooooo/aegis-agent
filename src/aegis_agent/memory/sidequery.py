# Behavioural reference (adapted and simplified):
#   * Claude Code Auto Memory / ``src/memdir/findRelevantMemories.ts`` — the
#     relevance ranking is a *side query*: a lightweight, separate LLM call
#     (``sideQuery``) whose output is forced into a JSON schema, run off the
#     main conversation so it never blocks the answer.  See
#     ``Claude-Code/docs/08-memory.md``.
#
# Aegis has no dedicated ``sideQuery`` transport, so this reuses the ordinary
# ``ModelProvider`` Protocol: one non-streaming-style call folded via
# ``collect_response``, then the assistant text is parsed as JSON.  Any failure
# (provider error, timeout, unparseable output) is swallowed and surfaced as
# ``None`` so callers degrade to "no result" rather than breaking.
"""Run a one-shot side-query against a model provider and parse JSON out.

Both recall (rank memories) and extraction (propose memory actions) need the
same primitive: send a single prompt to a (usually cheaper) model and read back
a small structured JSON object.  :func:`run_side_query` centralises the call,
the tolerant JSON extraction, and the never-raises contract.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aegis_agent.events import collect_response
from aegis_agent.models.base import Message, ModelProvider, Role

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort parse of a single JSON object from ``text``.

    Handles the common shapes a model returns: a bare object, an object wrapped
    in ```` ```json ```` fences, or an object embedded in prose.  Returns
    ``None`` when nothing object-shaped can be decoded.
    """
    if not text:
        return None
    stripped = text.strip()

    # Direct parse first.
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to the first {...} span (greedy to the last closing brace).
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = stripped[start : end + 1]
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def run_side_query(
    provider: ModelProvider,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any] | None:
    """Call ``provider`` once and return the parsed JSON object, or ``None``.

    A system + user message pair is sent with no tools.  The response text is
    folded via :func:`collect_response` and parsed with
    :func:`_extract_json_object`.  Every failure mode — provider raises, empty
    output, non-JSON output — returns ``None`` and logs at debug, so the caller
    (recall / extraction) can treat a side query as strictly best-effort.
    """
    messages = [
        Message(role=Role.SYSTEM, content=system_prompt),
        Message(role=Role.USER, content=user_prompt),
    ]
    try:
        response = collect_response(provider.stream(messages, tools=None))
    except Exception:
        logger.debug("memory side query failed", exc_info=True)
        return None
    return _extract_json_object(response.content)


__all__ = ["run_side_query"]
