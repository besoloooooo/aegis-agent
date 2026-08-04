# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (decoupled and simplified):
#   * ``agent/message_sanitization.py`` — ``_SURROGATE_RE`` /
#     ``_sanitize_surrogates`` (lines 24-39): the full surrogate range
#     U+D800-U+DFFF is invalid in UTF-8 and crashes ``json.dumps`` inside the
#     OpenAI SDK; replace with U+FFFD.  And ``_repair_tool_call_arguments`` /
#     ``_escape_invalid_chars_in_json_strings`` (lines 143-279): repair
#     malformed tool-call argument JSON (Python ``None``, trailing commas,
#     unclosed structures, literal control chars) with a last-resort ``"{}"``
#     so a bad tool call never crashes the session.
"""Message / tool-payload sanitisation helpers.

Pure, stateless functions used when handing content to the model provider:

* :func:`sanitize_surrogates` — some models (byte-level reasoning models and
  certain DashScope/Qwen responses) emit lone surrogate code points in their
  output.  Once such text lands in history, the *next* request crashes JSON
  serialisation.  Scrubbing happens on the wire path so contaminated history
  can be replayed safely.
* :func:`repair_tool_call_arguments` — streamed tool-call arguments can arrive
  truncated or malformed; apply common repairs so the call can still execute
  with best-effort arguments instead of failing the turn.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Lone surrogate code points (high AND low) are invalid in UTF-8 and crash
# json.dumps inside the OpenAI SDK.  Covering the full D800-DFFF range (not
# just the DC80-DCFF that Python's surrogateescape produces) also catches
# unpaired high surrogates some models emit.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD (replacement character).

    Fast no-op when the text contains no surrogates.
    """
    if not text:
        return text
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("�", text)
    return text


def repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Attempt to repair malformed tool_call argument JSON.

    Models can produce truncated JSON, trailing commas, Python ``None``, or
    literal control characters inside string values.  Already-valid JSON is
    returned unchanged (a pure formatting difference is not a repair and does
    not log); malformed input gets common repairs applied, with a last-resort
    ``"{}"`` so the request/execution succeeds (better than crashing the
    session).  All actual repairs are logged at WARNING level.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    # Fast-path: empty / whitespace-only -> empty object.
    if not raw_stripped:
        logger.warning("Sanitized empty tool_call arguments for %s", tool_name)
        return "{}"

    # Python-literal None -> normalise to {}.
    if raw_stripped == "None":
        logger.warning("Sanitized Python-None tool_call arguments for %s", tool_name)
        return "{}"

    # Pass 0: valid JSON passes through UNCHANGED.  A mere formatting
    # difference (e.g. spaces after colons, which many models emit) is not a
    # repair — it must neither rewrite the payload nor log a warning.
    # (Deviation from Hermes, which compacts + warns on any formatting diff.)
    try:
        json.loads(raw_stripped)
        return raw_stripped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Repair pass 1: literal control characters (tabs, newlines) inside JSON
    # string values.  json.loads(strict=False) accepts these and lets us
    # re-serialise into wire-valid JSON without any string surgery.  Only
    # reached when strict parsing failed, so this is a genuine repair.
    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"))
        logger.warning("Repaired unescaped control chars in tool_call arguments for %s", tool_name)
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    fixed = raw_stripped
    # 1. Strip trailing commas before } or ].
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    # 2. Close unclosed structures.
    open_curly = fixed.count("{") - fixed.count("}")
    open_bracket = fixed.count("[") - fixed.count("]")
    if open_curly > 0:
        fixed += "}" * open_curly
    if open_bracket > 0:
        fixed += "]" * open_bracket
    # 3. Remove excess closing braces/brackets (bounded to 50 iterations).
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith("}") and fixed.count("}") > fixed.count("{") or fixed.endswith("]") and fixed.count("]") > fixed.count("["):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning(
            "Repaired malformed tool_call arguments for %s: %s → %s",
            tool_name,
            raw_stripped[:80],
            fixed[:80],
        )
        return fixed
    except json.JSONDecodeError:
        pass

    # Repair pass 4: escape unescaped control chars inside JSON strings,
    # then retry.  Catches cases where strict=False alone fails because other
    # malformations are present too.
    try:
        escaped = _escape_invalid_chars_in_json_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            logger.warning(
                "Repaired control-char-laced tool_call arguments for %s: %s → %s",
                tool_name,
                raw_stripped[:80],
                escaped[:80],
            )
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Last resort: replace with empty object so the session doesn't crash.
    logger.warning(
        "Unrepairable tool_call arguments for %s — replaced with empty object (was: %s)",
        tool_name,
        raw_stripped[:80],
    )
    return "{}"


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control chars inside JSON string values.

    Walks the raw JSON character-by-character, tracking whether we are inside
    a double-quoted string.  Inside strings, replaces literal control
    characters (0x00-0x1F) that aren't already part of an escape sequence
    with their ``\\uXXXX`` equivalents.  Pass-through for everything else.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                # Already-escaped char — pass through as-is.
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


__all__ = [
    "repair_tool_call_arguments",
    "sanitize_surrogates",
]
