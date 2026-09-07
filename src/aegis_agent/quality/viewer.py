"""Read-only local web viewer for ExecutionRecords and Langfuse traces."""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from aegis_agent.env import load_dotenv
from aegis_agent.observability.sanitize import sanitize
from aegis_agent.quality.models import ExecutionRecord
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.quality.viewer_ui import VIEWER_HTML

_OBSERVATION_FIELDS = "basic,time,io,metadata,model,usage,metrics,trace_context"
_REQUEST_OPTIONS = {"timeout_in_seconds": 5, "max_retries": 0}


class LangfuseReader:
    """Small fail-open reader around Langfuse v4's observations API."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._error: str | None = None
        if client is not None:
            return
        load_dotenv()
        load_dotenv(Path.home() / ".aegis" / ".env")
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
        if not public_key or not secret_key:
            return
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=os.environ.get("LANGFUSE_BASE_URL"),
            )
        except Exception as exc:  # noqa: BLE001 - optional reader must fail open
            self._error = _safe_error(exc)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def error(self) -> str | None:
        return self._error

    def list_roots(
        self, *, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._client is None:
            return [], self._error
        try:
            response = self._client.api.observations.get_many(
                fields=_OBSERVATION_FIELDS,
                limit=min(max(limit, 1), 1000),
                is_root_observation=True,
                request_options=_REQUEST_OPTIONS,
            )
            self._error = None
            return [_json_value(item) for item in response.data], None
        except Exception as exc:  # noqa: BLE001 - remote API errors are contained
            self._error = _safe_error(exc)
            return [], self._error

    def get_trace(
        self,
        trace_id: str,
        *,
        max_observations: int = 5000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._client is None:
            return [], self._error
        observations: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            while len(observations) < max_observations:
                response = self._client.api.observations.get_many(
                    fields=_OBSERVATION_FIELDS,
                    limit=min(1000, max_observations - len(observations)),
                    cursor=cursor,
                    trace_id=trace_id,
                    request_options=_REQUEST_OPTIONS,
                )
                observations.extend(_json_value(item) for item in response.data)
                next_cursor = response.meta.cursor
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            observations.sort(key=lambda item: str(item.get("startTime") or ""))
            self._error = None
            return observations, None
        except Exception as exc:  # noqa: BLE001 - return partial remote data
            self._error = _safe_error(exc)
            return observations, self._error

    def list_trace_stats(
        self,
        trace_ids: set[str],
        *,
        max_observations: int = 5000,
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Aggregate recent child observations for the requested root traces."""
        if self._client is None or not trace_ids:
            return {}, self._error
        observations: list[dict[str, Any]] = []
        cursor: str | None = None
        fetched = 0
        try:
            while fetched < max_observations:
                response = self._client.api.observations.get_many(
                    fields="basic,time,metadata,usage,metrics,trace_context",
                    limit=min(1000, max_observations - fetched),
                    cursor=cursor,
                    request_options=_REQUEST_OPTIONS,
                )
                fetched += len(response.data)
                for item in response.data:
                    value = _json_value(item)
                    if _string(value.get("traceId")) in trace_ids:
                        observations.append(value)
                next_cursor = response.meta.cursor
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            self._error = None
            return _stats_by_trace(observations), None
        except Exception as exc:  # noqa: BLE001 - summary enrichment must fail open
            self._error = _safe_error(exc)
            return _stats_by_trace(observations), self._error

    def list_model_usage(
        self,
        trace_ids: set[str],
        *,
        max_observations: int = 1000,
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Compatibility wrapper for callers that only need model usage."""
        stats, error = self.list_trace_stats(
            trace_ids, max_observations=max_observations
        )
        return {
            trace_id: dict(value.get("usage") or {})
            for trace_id, value in stats.items()
            if value.get("usage")
        }, error

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            return


class TraceViewerService:
    def __init__(
        self,
        store: ExecutionRecordStore,
        langfuse: LangfuseReader,
    ) -> None:
        self.store = store
        self.langfuse = langfuse

    def list_executions(self, *, limit: int = 100) -> dict[str, Any]:
        local = self.store.list(limit=limit)
        executions = [_local_summary(record) for record in local]
        executions.sort(
            key=lambda item: str(item.get("started_at") or ""), reverse=True
        )
        return {
            "executions": executions[:limit],
            "summary": _summarize_executions(executions[:limit]),
            "sessions": _summarize_sessions(executions[:limit]),
            "status": {
                "records_dir": str(self.store.directory),
                "local_count": len(local),
                "langfuse_enabled": self.langfuse.enabled,
                "langfuse_error": self.langfuse.error,
            },
        }

    def list_langfuse_executions(self, *, limit: int = 100) -> dict[str, Any]:
        roots, error = self.langfuse.list_roots(limit=limit)
        trace_ids = {
            trace_id
            for item in roots
            if (trace_id := _string(item.get("traceId") or item.get("id")))
        }
        stats_by_trace, stats_error = self.langfuse.list_trace_stats(trace_ids)
        executions = [
            _cloud_summary(
                observation,
                stats=stats_by_trace.get(
                    _string(observation.get("traceId") or observation.get("id")) or ""
                ),
            )
            for observation in roots
        ]
        return {
            "executions": executions,
            "summary": _summarize_executions(executions),
            "sessions": _summarize_sessions(executions),
            "error": error,
            "stats_error": stats_error,
            # Kept for the older UI/API contract.
            "usage_error": stats_error,
        }

    def get_execution(self, identifier: str) -> dict[str, Any] | None:
        record = next(
            (
                item
                for item in self.store.list()
                if item.identity.execution_id == identifier
                or item.identity.trace_id == identifier
            ),
            None,
        )
        if record is None:
            return None
        safe_record = sanitize(
            record.model_dump(mode="json", exclude_none=True), max_depth=16
        )
        return {
            "record": dict(safe_record) if isinstance(safe_record, Mapping) else {},
            "langfuse": {
                "trace_id": record.identity.trace_id,
                "observations": [],
                "error": None,
            },
        }

    def get_langfuse_trace(self, trace_id: str) -> dict[str, Any]:
        observations, error = self.langfuse.get_trace(trace_id)
        trace_stats = _stats_by_trace(observations).get(
            trace_id,
            {"model_calls": 0, "tool_calls": 0, "errors": 0, "usage": {}},
        )
        aggregate = _aggregate_usage(
            [item for item in observations if _observation_kind(item) == "model"]
        )
        return {
            "trace_id": trace_id,
            "observations": observations,
            "usage": aggregate.get("usage", {}),
            "cost": aggregate.get("cost", {}),
            "stats": {
                "model_calls": trace_stats.get("model_calls"),
                "tool_calls": trace_stats.get("tool_calls"),
                "errors": trace_stats.get("errors"),
            },
            "error": error,
        }


class _ViewerServer(ThreadingHTTPServer):
    aegis_viewer_service: TraceViewerService


def build_server(
    host: str,
    port: int,
    *,
    records_dir: str | Path | None = None,
    store: ExecutionRecordStore | None = None,
    langfuse: LangfuseReader | None = None,
) -> _ViewerServer:
    service = TraceViewerService(
        store or ExecutionRecordStore(records_dir),
        langfuse or LangfuseReader(),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "AegisTraceViewer/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html(VIEWER_HTML)
                return
            if parsed.path == "/api/executions":
                self._json(service.list_executions())
                return
            if parsed.path == "/api/langfuse/roots":
                self._json(service.list_langfuse_executions())
                return
            langfuse_prefix = "/api/langfuse/traces/"
            if parsed.path.startswith(langfuse_prefix):
                trace_id = unquote(parsed.path[len(langfuse_prefix) :])
                if not trace_id or "/" in trace_id or "\\" in trace_id:
                    self._json({"error": "invalid trace id"}, HTTPStatus.BAD_REQUEST)
                    return
                self._json(service.get_langfuse_trace(trace_id))
                return
            prefix = "/api/executions/"
            if parsed.path.startswith(prefix):
                identifier = unquote(parsed.path[len(prefix) :])
                if not identifier or "/" in identifier or "\\" in identifier:
                    self._json(
                        {"error": "invalid execution id"}, HTTPStatus.BAD_REQUEST
                    )
                    return
                detail = service.get_execution(identifier)
                if detail is None:
                    self._json({"error": "execution not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(detail)
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _headers(self, content_type: str, length: int, status: HTTPStatus) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _html(self, value: str) -> None:
            payload = value.encode("utf-8")
            self._headers("text/html; charset=utf-8", len(payload), HTTPStatus.OK)
            self.wfile.write(payload)

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(payload), status)
            self.wfile.write(payload)

    server = _ViewerServer((host, port), Handler)
    server.daemon_threads = True
    server.aegis_viewer_service = service
    return server


def serve_viewer(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    records_dir: str | Path | None = None,
    open_browser: bool = True,
) -> None:
    server = build_server(host, port, records_dir=records_dir)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"Aegis Trace Viewer: {url}")
    print(f"ExecutionRecords: {ExecutionRecordStore(records_dir).directory}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "Warning: viewer has no authentication; use a loopback host unless access is trusted."
        )
    if open_browser:
        timer = threading.Timer(0.25, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        server.aegis_viewer_service.langfuse.close()


def _local_summary(record: ExecutionRecord) -> dict[str, Any]:
    model_calls = sum(step.type == "model" for step in record.steps)
    tool_calls = sum(step.type == "tool" for step in record.steps)
    errors = sum(_local_step_is_error(step) for step in record.steps)
    return {
        "id": record.identity.execution_id,
        "execution_id": record.identity.execution_id,
        "trace_id": record.identity.trace_id,
        "session_id": record.identity.session_id,
        "source": "local",
        "task": sanitize(record.identity.task_name),
        "started_at": _iso(record.execution.started_at),
        "success": record.execution.success,
        "has_error": errors > 0,
        "passed": record.evaluation.passed,
        "model": record.agent.model,
        "provider": record.agent.provider,
        "latency_ms": record.execution.latency_ms,
        "usage": record.usage.model_dump(exclude_none=True),
        "stats": {
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "errors": errors,
        },
        "langfuse_available": False,
    }


def _summarize_sessions(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in executions:
        session_id = _string(item.get("session_id"))
        key = session_id or f"execution:{item.get('id')}"
        grouped.setdefault(key, []).append(item)
    return [
        {
            "session_id": next(
                (
                    _string(item.get("session_id"))
                    for item in items
                    if item.get("session_id")
                ),
                None,
            ),
            **_summarize_executions(items),
        }
        for items in grouped.values()
    ]


def _summarize_executions(executions: list[dict[str, Any]]) -> dict[str, Any]:
    usages = [
        item.get("usage") if isinstance(item.get("usage"), Mapping) else {}
        for item in executions
    ]
    stats = [
        item.get("stats") if isinstance(item.get("stats"), Mapping) else {}
        for item in executions
    ]

    def usage_value(usage: Mapping[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def known_sum(values: list[float | None]) -> float | None:
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    input_values = [usage_value(item, "input_tokens", "input") for item in usages]
    output_values = [usage_value(item, "output_tokens", "output") for item in usages]
    total_values = [usage_value(item, "total_tokens", "total") for item in usages]
    inclusive_input_values = [
        usage_value(item, "input_tokens_including_cache", "prompt_tokens")
        for item in usages
    ]
    cache_read_values = [
        usage_value(item, "cache_read_tokens", "cache_read_input_tokens", "cache_read")
        for item in usages
    ]
    cache_write_values = [
        usage_value(
            item,
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
            "cache_write",
        )
        for item in usages
    ]
    input_tokens = known_sum(input_values)
    cache_read_tokens = known_sum(cache_read_values)
    cache_write_tokens = known_sum(cache_write_values)
    cache_input_totals = [
        _cache_input_total(
            input_tokens=input_value,
            cache_read_tokens=cache_read_value,
            cache_write_tokens=cache_write_value,
            input_tokens_including_cache=inclusive_input_value,
            total_tokens=total_value,
            output_tokens=output_value,
        )
        for (
            input_value,
            cache_read_value,
            cache_write_value,
            inclusive_input_value,
            total_value,
            output_value,
        ) in zip(
            input_values,
            cache_read_values,
            cache_write_values,
            inclusive_input_values,
            total_values,
            output_values,
            strict=True,
        )
    ]
    cache_hit_rate_pct = _cache_hit_rate_pct(
        cache_read_tokens,
        known_sum(cache_input_totals),
        complete=bool(executions)
        and all(value is not None for value in cache_read_values)
        and all(value is not None for value in cache_input_totals),
    )

    error_values: list[float | None] = []
    for item, item_stats in zip(executions, stats, strict=True):
        error_count = usage_value(item_stats, "errors")
        if error_count is None and (
            item.get("has_error") or item.get("success") is False
        ):
            error_count = 1.0
        error_values.append(error_count)

    return {
        "sessions": len(
            {
                _string(item.get("session_id")) or f"execution:{item.get('id')}"
                for item in executions
            }
        ),
        "turns": len(executions),
        "model_calls": known_sum([usage_value(item, "model_calls") for item in stats]),
        "tool_calls": known_sum([usage_value(item, "tool_calls") for item in stats]),
        "duration_ms": known_sum(
            [usage_value(item, "latency_ms") for item in executions]
        ),
        "input_tokens": input_tokens,
        "output_tokens": known_sum(output_values),
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_hit_rate_pct": cache_hit_rate_pct,
        "cost": known_sum([usage_value(item, "cost", "total_cost") for item in usages]),
        "errors": known_sum(error_values),
    }


def _cache_hit_rate_pct(
    cache_read_tokens: float | None,
    cache_input_total: float | None,
    *,
    complete: bool = True,
) -> float | None:
    if not complete or cache_read_tokens is None or cache_input_total is None:
        return None
    if cache_input_total <= 0:
        return None
    return cache_read_tokens / cache_input_total * 100.0


def _cache_input_total(
    *,
    input_tokens: float | None,
    cache_read_tokens: float | None,
    cache_write_tokens: float | None,
    input_tokens_including_cache: float | None,
    total_tokens: float | None,
    output_tokens: float | None,
) -> float | None:
    """Return the exact input-token denominator for cache hit rate.

    OpenAI-compatible APIs such as Alibaba Cloud Model Studio report cached
    tokens inside ``prompt_tokens`` and may omit cache-write usage for implicit
    caching.  Aegis stores mutually-exclusive input/cache buckets, while
    retaining the provider total, so ``total - output`` recovers the inclusive
    prompt count without pretending an omitted cache-write value is zero.
    """
    if input_tokens_including_cache is not None:
        return input_tokens_including_cache
    if total_tokens is not None and output_tokens is not None:
        candidate = total_tokens - output_tokens
        return candidate if candidate >= 0 else None
    if (
        input_tokens is None
        or cache_read_tokens is None
        or cache_write_tokens is None
    ):
        return None
    return input_tokens + cache_read_tokens + cache_write_tokens


def _cloud_summary(
    observation: Mapping[str, Any],
    *,
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    trace_id = _string(observation.get("traceId")) or _string(observation.get("id"))
    metadata = observation.get("metadata")
    execution_id = (
        metadata.get("execution_id") if isinstance(metadata, Mapping) else None
    )
    session_id = metadata.get("session_id") if isinstance(metadata, Mapping) else None
    trace_stats = dict(stats or {})
    success = _cloud_success(observation)
    if int(trace_stats.get("errors") or 0) > 0:
        success = False
    return {
        "id": execution_id or trace_id,
        "execution_id": execution_id,
        "trace_id": trace_id,
        "session_id": session_id,
        "source": "langfuse",
        "task": _task_from_input(observation.get("input")),
        "name": observation.get("name"),
        "started_at": observation.get("startTime"),
        "success": success,
        "has_error": int(trace_stats.get("errors") or 0) > 0,
        "passed": None,
        "model": observation.get("providedModelName"),
        "latency_ms": _seconds_to_ms(observation.get("latency")),
        "usage": dict(
            trace_stats.get("usage") or observation.get("usageDetails") or {}
        ),
        "stats": {
            "model_calls": trace_stats.get("model_calls"),
            "tool_calls": trace_stats.get("tool_calls"),
            "errors": trace_stats.get("errors"),
        },
        "langfuse_available": True,
    }


def _stats_by_trace(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        trace_id = _string(observation.get("traceId"))
        if trace_id:
            grouped.setdefault(trace_id, []).append(observation)
    result: dict[str, dict[str, Any]] = {}
    for trace_id, items in grouped.items():
        models = [item for item in items if _observation_kind(item) == "model"]
        result[trace_id] = {
            "model_calls": len(models),
            "tool_calls": sum(_observation_kind(item) == "tool" for item in items),
            "errors": sum(_cloud_observation_is_error(item) for item in items),
            "usage": _aggregate_usage(models)["usage"],
        }
    return result


def _observation_kind(observation: Mapping[str, Any]) -> str:
    name = str(observation.get("name") or "").lower()
    observation_type = str(observation.get("type") or "").upper()
    if name == "model call" or observation_type == "GENERATION":
        return "model"
    if name.startswith("tool call:") or observation_type == "TOOL":
        return "tool"
    if name == "final result":
        return "final"
    return "span"


def _cloud_observation_is_error(observation: Mapping[str, Any]) -> bool:
    if str(observation.get("level") or "").upper() in {"ERROR", "FATAL"}:
        return True
    metadata = observation.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("success") is False:
        return True
    return observation.get("error") not in {None, ""}


def _local_step_is_error(step: Any) -> bool:
    return step.success is False or step.error not in {None, ""}


def _aggregate_usage(observations: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    usage: dict[str, float] = {}
    cost: dict[str, float] = {}
    for observation in observations:
        _sum_numeric_fields(usage, observation.get("usageDetails"))
        _sum_numeric_fields(cost, observation.get("costDetails"))
    if cost:
        usage["cost"] = cost.get("total", sum(cost.values()))
    return {"usage": usage, "cost": cost}


def _sum_numeric_fields(target: dict[str, float], value: Any) -> None:
    if not isinstance(value, Mapping):
        return
    for key, raw in value.items():
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            target[str(key)] = target.get(str(key), 0.0) + float(raw)


def _cloud_success(observation: Mapping[str, Any]) -> bool | None:
    """Infer Aegis completion for new and pre-success-metadata cloud traces."""
    level = str(observation.get("level") or "").upper()
    if level in {"ERROR", "FATAL"}:
        return False

    metadata = observation.get("metadata")
    if isinstance(metadata, Mapping):
        explicit = metadata.get("success")
        if isinstance(explicit, bool):
            return explicit
        stop_reason = str(metadata.get("stop_reason") or "").lower()
        if stop_reason == "final_answer":
            return True
        if stop_reason in {"error", "interrupted", "max_iterations"}:
            return False

    if level == "WARNING":
        return False
    if observation.get("endTime") is not None and observation.get("output") is not None:
        return True
    return None


def _task_from_input(value: Any) -> str | None:
    if isinstance(value, Mapping):
        task = value.get("task")
        return _string(task)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return _task_from_input(parsed)
    return None


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        dump = getattr(value, "model_dump", None)
        if not callable(dump):
            raise TypeError(
                f"unsupported Langfuse response type: {type(value).__name__}"
            )
        raw = dict(dump(mode="json", by_alias=True, exclude_none=True))
    safe = sanitize(raw)
    return dict(safe) if isinstance(safe, Mapping) else {}


def _safe_error(exc: BaseException) -> str:
    value = sanitize(str(exc), max_string_length=500)
    return value if isinstance(value, str) else type(exc).__name__


def _seconds_to_ms(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) * 1000
    return None


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["LangfuseReader", "TraceViewerService", "build_server", "serve_viewer"]
