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

    def list_roots(self, *, limit: int = 100) -> tuple[list[dict[str, Any]], str | None]:
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
                cursor = response.meta.cursor
                if not cursor:
                    break
            observations.sort(key=lambda item: str(item.get("startTime") or ""))
            self._error = None
            return observations, None
        except Exception as exc:  # noqa: BLE001 - return partial remote data
            self._error = _safe_error(exc)
            return observations, self._error

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
        executions.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return {
            "executions": executions[:limit],
            "status": {
                "records_dir": str(self.store.directory),
                "local_count": len(local),
                "langfuse_enabled": self.langfuse.enabled,
                "langfuse_error": self.langfuse.error,
            },
        }

    def list_langfuse_executions(self, *, limit: int = 100) -> dict[str, Any]:
        roots, error = self.langfuse.list_roots(limit=limit)
        return {
            "executions": [_cloud_summary(observation) for observation in roots],
            "error": error,
        }

    def get_execution(self, identifier: str) -> dict[str, Any] | None:
        record = next(
            (
                item
                for item in self.store.list()
                if item.identity.execution_id == identifier or item.identity.trace_id == identifier
            ),
            None,
        )
        if record is None:
            return None
        return {
            "record": record.model_dump(mode="json", exclude_none=True),
            "langfuse": {
                "trace_id": record.identity.trace_id,
                "observations": [],
                "error": None,
            },
        }

    def get_langfuse_trace(self, trace_id: str) -> dict[str, Any]:
        observations, error = self.langfuse.get_trace(trace_id)
        return {"trace_id": trace_id, "observations": observations, "error": error}


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
                    self._json({"error": "invalid execution id"}, HTTPStatus.BAD_REQUEST)
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
        print("Warning: viewer has no authentication; use a loopback host unless access is trusted.")
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
    return {
        "id": record.identity.execution_id,
        "execution_id": record.identity.execution_id,
        "trace_id": record.identity.trace_id,
        "source": "local",
        "task": record.identity.task_name,
        "started_at": _iso(record.execution.started_at),
        "success": record.execution.success,
        "passed": record.evaluation.passed,
        "model": record.agent.model,
        "provider": record.agent.provider,
        "latency_ms": record.execution.latency_ms,
        "usage": record.usage.model_dump(exclude_none=True),
        "langfuse_available": False,
    }


def _cloud_summary(observation: Mapping[str, Any]) -> dict[str, Any]:
    trace_id = _string(observation.get("traceId")) or _string(observation.get("id"))
    metadata = observation.get("metadata")
    execution_id = metadata.get("execution_id") if isinstance(metadata, Mapping) else None
    return {
        "id": execution_id or trace_id,
        "execution_id": execution_id,
        "trace_id": trace_id,
        "source": "langfuse",
        "task": _task_from_input(observation.get("input")),
        "name": observation.get("name"),
        "started_at": observation.get("startTime"),
        "success": False if str(observation.get("level")) == "ERROR" else None,
        "passed": None,
        "model": observation.get("providedModelName"),
        "latency_ms": _seconds_to_ms(observation.get("latency")),
        "usage": observation.get("usageDetails") or {},
        "langfuse_available": True,
    }


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
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json", by_alias=True, exclude_none=True))
    raise TypeError(f"unsupported Langfuse response type: {type(value).__name__}")


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
