from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from aegis_agent.quality.models import (
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionStep,
    ExecutionSummary,
)
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.quality.viewer import (
    LangfuseReader,
    TraceViewerService,
    build_server,
)


class _FakeLangfuse:
    enabled = True
    error = None

    def __init__(self, roots=None, traces=None, error=None):
        self.roots = roots or []
        self.traces = traces or {}
        self.returned_error = error

    def list_roots(self, *, limit=100):
        return self.roots[:limit], self.returned_error

    def get_trace(self, trace_id, *, max_observations=5000):
        return self.traces.get(trace_id, [])[:max_observations], self.returned_error

    def close(self):
        return None


def _record() -> ExecutionRecord:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return ExecutionRecord(
        identity=ExecutionIdentity(
            execution_id="trial-1",
            trace_id="trace-1",
            task_name="inspect the workspace",
        ),
        execution=ExecutionSummary(started_at=now, success=True, latency_ms=25),
        steps=[
            ExecutionStep(
                step_id="root",
                sequence=1,
                type="agent",
                name="Aegis Run",
                started_at=now,
                success=True,
            )
        ],
    )


def _observation(trace_id: str, observation_id: str = "root") -> dict:
    return {
        "id": observation_id,
        "traceId": trace_id,
        "startTime": "2026-09-01T00:00:00Z",
        "type": "AGENT",
        "name": "Aegis Run",
        "isRootObservation": True,
        "input": {"task": "cloud task"},
        "metadata": {"execution_id": "trial-1" if trace_id == "trace-1" else "cloud-only"},
    }


def test_service_exposes_local_records_immediately_and_cloud_as_supplement(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    store.save(_record())
    langfuse = _FakeLangfuse(
        roots=[_observation("trace-1"), _observation("trace-cloud")],
        traces={"trace-1": [_observation("trace-1")]},
    )
    service = TraceViewerService(store, langfuse)

    listing = service.list_executions()

    assert len(listing["executions"]) == 1
    local = listing["executions"][0]
    assert local["id"] == "trial-1"
    cloud_listing = service.list_langfuse_executions()
    assert len(cloud_listing["executions"]) == 2
    cloud = next(
        item for item in cloud_listing["executions"] if item["trace_id"] == "trace-cloud"
    )
    assert cloud["trace_id"] == "trace-cloud"

    detail = service.get_execution("trial-1")
    assert detail is not None
    assert detail["record"]["identity"]["execution_id"] == "trial-1"
    assert detail["langfuse"]["observations"] == []
    cloud_detail = service.get_langfuse_trace("trace-1")
    assert cloud_detail["observations"][0]["traceId"] == "trace-1"


def test_langfuse_reader_uses_v4_observations_api_and_contains_failures():
    calls = []

    def get_many(**kwargs):
        calls.append(kwargs)
        if kwargs.get("trace_id") == "broken":
            raise TimeoutError("network timed out")
        return SimpleNamespace(
            data=[_observation(kwargs.get("trace_id") or "trace-1")],
            meta=SimpleNamespace(cursor=None),
        )

    client = SimpleNamespace(
        api=SimpleNamespace(observations=SimpleNamespace(get_many=get_many)),
        shutdown=lambda: None,
    )
    reader = LangfuseReader(client)

    roots, error = reader.list_roots(limit=4)
    assert error is None
    assert roots[0]["traceId"] == "trace-1"
    assert calls[0]["is_root_observation"] is True
    assert "usage" in calls[0]["fields"]

    observations, error = reader.get_trace("broken")
    assert observations == []
    assert "timed out" in error


def test_http_viewer_is_read_only_and_serves_record_detail(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    store.save(_record())
    langfuse = _FakeLangfuse(traces={"trace-1": [_observation("trace-1")]})
    server = build_server("127.0.0.1", 0, store=store, langfuse=langfuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            html = response.read().decode()
            assert "Aegis Trace Viewer" in html
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Frame-Options"] == "DENY"

        with urlopen(f"{base}/api/executions", timeout=2) as response:
            listing = json.load(response)
            assert listing["executions"][0]["execution_id"] == "trial-1"

        with urlopen(f"{base}/api/executions/trial-1", timeout=2) as response:
            detail = json.load(response)
            assert detail["record"]["steps"][0]["name"] == "Aegis Run"

        with urlopen(f"{base}/api/langfuse/traces/trace-1", timeout=2) as response:
            cloud = json.load(response)
            assert cloud["observations"][0]["name"] == "Aegis Run"

        with pytest.raises(HTTPError) as error:
            urlopen(f"{base}/api/executions/not%2Fsafe", timeout=2)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
