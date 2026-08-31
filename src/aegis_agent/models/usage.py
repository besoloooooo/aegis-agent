"""Normalize provider-specific usage objects into :class:`ModelUsage`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from aegis_agent.models.base import ModelUsage

_T = TypeVar("_T")


def parse_openai_usage(raw: Any) -> ModelUsage | None:
    """Normalize an OpenAI-compatible Chat Completions ``usage`` object.

    OpenAI's ``prompt_tokens`` includes cached and cache-write tokens.  Aegis
    converts those inclusive counts to mutually-exclusive buckets so a
    downstream observability backend cannot double-count usage or inferred
    cost.  Gateways that expose ``input_tokens`` instead of ``prompt_tokens``
    are treated as already-normalized because their inclusion semantics are
    provider-specific.
    """
    if raw is None:
        return None

    prompt_tokens = _int_field(raw, "prompt_tokens")
    input_tokens = prompt_tokens
    if input_tokens is None:
        input_tokens = _int_field(raw, "input_tokens")
    output_tokens = _int_field(raw, "completion_tokens", "output_tokens")
    total_tokens = _int_field(raw, "total_tokens")

    details = _field(raw, "prompt_tokens_details", "input_tokens_details")
    cache_read_tokens = _int_field(
        details,
        "cached_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
    )
    if cache_read_tokens is None:
        cache_read_tokens = _int_field(
            raw,
            "cached_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
        )

    cache_write_tokens = _int_field(
        details,
        "cache_write_tokens",
        "cache_creation_tokens",
        "cache_creation_input_tokens",
    )
    if cache_write_tokens is None:
        cache_write_tokens = _int_field(
            raw,
            "cache_write_tokens",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
        )

    # OpenAI prompt_tokens is inclusive of its detail buckets.  Preserve the
    # provider total while emitting exclusive input/cache buckets for Langfuse.
    if prompt_tokens is not None:
        cached = (cache_read_tokens or 0) + (cache_write_tokens or 0)
        input_tokens = max(prompt_tokens - cached, 0)

    cost = _float_field(raw, "cost", "total_cost")
    if cost is None:
        cost_details = _field(raw, "cost_details")
        cost = _float_field(cost_details, "total", "total_cost")

    usage = ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost=cost,
    )
    if not usage.usage_details() and usage.cost is None:
        return None
    return usage


def parse_anthropic_usage(
    raw: Any,
    previous: ModelUsage | None = None,
) -> ModelUsage | None:
    """Normalize and cumulatively merge an Anthropic Messages usage object.

    Anthropic streaming reports input/cache buckets on ``message_start`` and
    the final output count on ``message_delta``.  Some final events include
    zero-valued input/cache fields, so those zeroes must not erase a non-zero
    value already observed at stream start.  Unlike OpenAI ``prompt_tokens``,
    Anthropic's ``input_tokens`` is already exclusive of its cache buckets.

    Anthropic does not currently return a total or monetary cost.  Those
    fields remain ``None`` unless a compatible gateway explicitly supplies
    them; Aegis does not derive or estimate either value here.
    """
    if raw is None:
        return previous

    input_tokens = _preserve_nonzero(
        _int_field(raw, "input_tokens"),
        previous.input_tokens if previous is not None else None,
    )
    output_tokens = _prefer_present(
        _int_field(raw, "output_tokens"),
        previous.output_tokens if previous is not None else None,
    )
    cache_read_tokens = _preserve_nonzero(
        _int_field(raw, "cache_read_input_tokens", "cache_read_tokens"),
        previous.cache_read_tokens if previous is not None else None,
    )
    cache_write_tokens = _preserve_nonzero(
        _int_field(raw, "cache_creation_input_tokens", "cache_write_tokens"),
        previous.cache_write_tokens if previous is not None else None,
    )
    total_tokens = _prefer_present(
        _int_field(raw, "total_tokens"),
        previous.total_tokens if previous is not None else None,
    )
    cost = _prefer_present(
        _float_field(raw, "cost", "total_cost"),
        previous.cost if previous is not None else None,
    )

    usage = ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost=cost,
    )
    if not usage.usage_details() and usage.cost is None:
        return None
    return usage


def _field(value: Any, *names: str) -> Any:
    if value is None:
        return None
    for name in names:
        if isinstance(value, Mapping) and name in value:
            candidate = value[name]
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _int_field(value: Any, *names: str) -> int | None:
    candidate = _field(value, *names)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        return None
    return max(candidate, 0)


def _float_field(value: Any, *names: str) -> float | None:
    candidate = _field(value, *names)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        return None
    return float(candidate)


def _prefer_present(current: _T | None, previous: _T | None) -> _T | None:
    return current if current is not None else previous


def _preserve_nonzero(current: int | None, previous: int | None) -> int | None:
    if current is None or (current == 0 and previous not in (None, 0)):
        return previous
    return current


__all__ = ["parse_anthropic_usage", "parse_openai_usage"]
