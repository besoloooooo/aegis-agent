"""Sanitize and bound data before it leaves Aegis through observability."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

DEFAULT_MAX_STRING_LENGTH = 20_000
DEFAULT_MAX_COLLECTION_ITEMS = 100
DEFAULT_MAX_DEPTH = 8
REDACTED = "[REDACTED]"

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_COMMON_SECRET_RE = re.compile(r"\b(?:sk|rk|pk-lf)-[A-Za-z0-9_-]{12,}\b")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|access[_-]?token)\b(\s*[:=]\s*)([^\s,;]+)"
)
_KEY_NORMALIZER_RE = re.compile(r"[^a-z0-9]+")
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "secret_key",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer_token",
    "token",
}


def sanitize(
    value: Any,
    *,
    max_string_length: int = DEFAULT_MAX_STRING_LENGTH,
    max_collection_items: int = DEFAULT_MAX_COLLECTION_ITEMS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    _depth: int = 0,
) -> Any:
    """Return a JSON-friendly, redacted and size-bounded copy of ``value``.

    Long strings keep a prefix and explicit original-length metadata.  JSON
    strings are decoded before redaction so secrets in tool argument/result
    envelopes cannot bypass key-based filtering merely by being serialized.
    """
    if _depth >= max_depth:
        return {"_aegis_truncated": True, "reason": "max_depth"}

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return sanitize(
            value.value,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
            _depth=_depth + 1,
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)

    if isinstance(value, Mapping):
        items = list(value.items())
        output: dict[str, Any] = {}
        for key, item in items[:max_collection_items]:
            text_key = str(key)
            output[text_key] = (
                REDACTED
                if _is_sensitive_key(text_key)
                else sanitize(
                    item,
                    max_string_length=max_string_length,
                    max_collection_items=max_collection_items,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
            )
        if len(items) > max_collection_items:
            output["_aegis_collection"] = {
                "truncated": True,
                "original_length": len(items),
            }
        return output

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        original_length = len(value)
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                decoded = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                return sanitize(
                    decoded,
                    max_string_length=max_string_length,
                    max_collection_items=max_collection_items,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
        redacted = _BEARER_RE.sub("Bearer [REDACTED]", value)
        redacted = _COMMON_SECRET_RE.sub(REDACTED, redacted)
        redacted = _ASSIGNMENT_SECRET_RE.sub(r"\1\2[REDACTED]", redacted)
        if len(redacted) <= max_string_length:
            return redacted
        return {
            "content": redacted[:max_string_length] + "…",
            "_aegis_truncated": True,
            "_aegis_original_length": original_length,
        }

    if isinstance(value, Sequence):
        sequence_output = [
            sanitize(
                item,
                max_string_length=max_string_length,
                max_collection_items=max_collection_items,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value[:max_collection_items]
        ]
        if len(value) > max_collection_items:
            sequence_output.append(
                {
                    "_aegis_truncated": True,
                    "_aegis_original_length": len(value),
                }
            )
        return sequence_output

    return sanitize(
        str(value),
        max_string_length=max_string_length,
        max_collection_items=max_collection_items,
        max_depth=max_depth,
        _depth=_depth + 1,
    )


def _is_sensitive_key(key: str) -> bool:
    normalized = _KEY_NORMALIZER_RE.sub("_", key.lower()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(
        (
            "_api_key",
            "_authorization",
            "_cookie",
            "_password",
            "_passwd",
            "_secret",
            "_secret_key",
            "_access_token",
            "_refresh_token",
            "_id_token",
            "_bearer_token",
        )
    )


__all__ = [
    "DEFAULT_MAX_COLLECTION_ITEMS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_STRING_LENGTH",
    "REDACTED",
    "sanitize",
]
