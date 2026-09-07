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
    UsageSummary,
)
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.quality.viewer import (
    LangfuseReader,
    TraceViewerService,
    _stats_by_trace,
    _summarize_executions,
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

    def list_trace_stats(self, trace_ids, *, max_observations=1000):
        del max_observations
        observations = [
            item for trace_id in trace_ids for item in self.traces.get(trace_id, [])
        ]
        return _stats_by_trace(observations), self.returned_error

    def close(self):
        return None


def _record() -> ExecutionRecord:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return ExecutionRecord(
        identity=ExecutionIdentity(
            execution_id="trial-1",
            trace_id="trace-1",
            session_id="session-1",
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


def _observation(
    trace_id: str,
    observation_id: str = "root",
    *,
    session_id: str = "session-1",
    stop_reason: str | None = "final_answer",
    success: bool | None = None,
    level: str = "DEFAULT",
    completed: bool = True,
) -> dict:
    metadata = {
        "execution_id": "trial-1" if trace_id == "trace-1" else "cloud-only",
        "session_id": session_id,
    }
    if stop_reason is not None:
        metadata["stop_reason"] = stop_reason
    if success is not None:
        metadata["success"] = success
    return {
        "id": observation_id,
        "traceId": trace_id,
        "startTime": "2026-09-01T00:00:00Z",
        "endTime": "2026-09-01T00:00:01Z" if completed else None,
        "type": "AGENT",
        "name": "Aegis Run",
        "level": level,
        "isRootObservation": True,
        "input": {"task": "cloud task"},
        "output": "done" if completed else None,
        "metadata": metadata,
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
    assert local["stats"] == {"model_calls": 0, "tool_calls": 0, "errors": 0}
    cloud_listing = service.list_langfuse_executions()
    assert len(cloud_listing["executions"]) == 2
    cloud = next(
        item
        for item in cloud_listing["executions"]
        if item["trace_id"] == "trace-cloud"
    )
    assert cloud["trace_id"] == "trace-cloud"
    assert cloud["session_id"] == "session-1"
    assert cloud["success"] is True
    assert listing["summary"]["turns"] == 1
    assert listing["sessions"][0]["session_id"] == "session-1"

    detail = service.get_execution("trial-1")
    assert detail is not None
    assert detail["record"]["identity"]["execution_id"] == "trial-1"
    assert detail["langfuse"]["observations"] == []
    cloud_detail = service.get_langfuse_trace("trace-1")
    assert cloud_detail["observations"][0]["traceId"] == "trace-1"


def test_cloud_status_supports_explicit_current_and_legacy_completion_signals(tmp_path):
    roots = [
        _observation("explicit-ok", success=True, stop_reason=None),
        _observation(
            "explicit-failed", success=False, stop_reason=None, level="WARNING"
        ),
        _observation("legacy-ok", stop_reason="final_answer"),
        _observation("legacy-error", stop_reason="error", level="ERROR"),
        _observation("running", stop_reason=None, completed=False),
    ]
    listing = TraceViewerService(
        ExecutionRecordStore(tmp_path),
        _FakeLangfuse(roots=roots),
    ).list_langfuse_executions()["executions"]

    by_trace = {item["trace_id"]: item for item in listing}
    assert by_trace["explicit-ok"]["success"] is True
    assert by_trace["explicit-failed"]["success"] is False
    assert by_trace["legacy-ok"]["success"] is True
    assert by_trace["legacy-error"]["success"] is False
    assert by_trace["running"]["success"] is None


def test_cloud_listing_aggregates_model_usage_including_cache_tokens(tmp_path):
    model_one = {
        "id": "model-1",
        "traceId": "trace-usage",
        "name": "Model Call",
        "usageDetails": {
            "input": 100,
            "output": 20,
            "total": 120,
            "cache_read_input_tokens": 80,
        },
    }
    model_two = {
        "id": "model-2",
        "traceId": "trace-usage",
        "name": "Model Call",
        "usageDetails": {
            "input": 50,
            "output": 10,
            "total": 65,
            "cache_creation_input_tokens": 5,
        },
    }
    listing = TraceViewerService(
        ExecutionRecordStore(tmp_path),
        _FakeLangfuse(
            roots=[_observation("trace-usage")],
            traces={"trace-usage": [model_one, model_two]},
        ),
    ).list_langfuse_executions()

    assert listing["usage_error"] is None
    assert listing["executions"][0]["usage"] == {
        "input": 150,
        "output": 30,
        "total": 185,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 5,
    }
    assert listing["executions"][0]["stats"] == {
        "model_calls": 2,
        "tool_calls": 0,
        "errors": 0,
    }


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

    raw = _observation("trace-redacted")
    raw["metadata"]["scope.attributes.public_key"] = "pk-lf-example-key-123456"
    client.api.observations.get_many = lambda **kwargs: SimpleNamespace(
        data=[raw], meta=SimpleNamespace(cursor=None)
    )
    sanitized, error = reader.list_roots()
    assert error is None
    assert sanitized[0]["metadata"]["scope.attributes.public_key"] == "[REDACTED]"

    client.api.observations.get_many = get_many

    usage, error = reader.list_model_usage({"trace-1"})
    assert error is None
    assert usage == {}
    assert calls[1].get("name") is None
    assert "trace_context" in calls[1]["fields"]

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
            assert "ALL SESSIONS" in html
            assert "Sessions / Turns" in html
            assert "Model / Tool Calls" in html
            assert "Errors" in html
            assert "expandedSessions" in html
            assert "Session conversation" in html
            assert "Cache read / write" in html
            assert "Cache hit rate" in html
            assert "cacheInputTotal" in html
            assert "usage.total - usage.output" in html
            assert "Raw Payload" in html
            assert "displayTotal" in html
            assert "previousInput = current" in html
            assert "growth >= 0" in html
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


def test_global_session_and_turn_summaries_preserve_zero_and_unknown():
    executions = [
        {
            "id": "turn-1",
            "session_id": "session-a",
            "latency_ms": 1000,
            "success": True,
            "has_error": False,
            "stats": {"model_calls": 2, "tool_calls": 1, "errors": 0},
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost": 0,
            },
        },
        {
            "id": "turn-2",
            "session_id": "session-a",
            "latency_ms": 2000,
            "success": True,
            "has_error": False,
            "stats": {"model_calls": 1, "tool_calls": 0, "errors": 0},
            "usage": {
                "input_tokens": 200,
                "output_tokens": 20,
                "cache_read_tokens": 50,
                "cache_write_tokens": 0,
                "cost": 0.25,
            },
        },
        {
            "id": "turn-3",
            "session_id": "session-b",
            "latency_ms": None,
            "success": None,
            "stats": {"model_calls": None, "tool_calls": None, "errors": None},
            "usage": {},
        },
    ]

    summary = _summarize_executions(executions)

    assert summary == {
        "sessions": 2,
        "turns": 3,
        "model_calls": 3,
        "tool_calls": 1,
        "duration_ms": 3000,
        "input_tokens": 300,
        "output_tokens": 30,
        "cache_read_tokens": 50,
        "cache_write_tokens": 0,
        "cache_hit_rate_pct": None,
        "cost": 0.25,
        "errors": 0,
    }
    assert _summarize_executions(executions[:2])["cache_hit_rate_pct"] == pytest.approx(
        50 / 350 * 100
    )
    assert _summarize_executions(executions[:1])["cache_hit_rate_pct"] == 0.0
    bailian = {
        "id": "bailian-implicit-cache",
        "usage": {
            # Aegis normalizes 百炼 prompt_tokens=3019 into exclusive buckets.
            "input_tokens": 971,
            "output_tokens": 104,
            "total_tokens": 3123,
            "cache_read_tokens": 2048,
            # Implicit cache responses do not provide cache write usage.
        },
        "stats": {},
    }
    bailian_summary = _summarize_executions([bailian])
    assert bailian_summary["cache_write_tokens"] is None
    assert bailian_summary["cache_hit_rate_pct"] == pytest.approx(2048 / 3019 * 100)
    raw_bailian_summary = _summarize_executions(
        [
            {
                "id": "bailian-raw-usage",
                "usage": {"prompt_tokens": 3019, "cache_read_tokens": 2048},
                "stats": {},
            }
        ]
    )
    assert raw_bailian_summary["cache_hit_rate_pct"] == pytest.approx(
        2048 / 3019 * 100
    )
    assert (
        _summarize_executions(
            [
                {
                    "id": "missing-denominator",
                    "usage": {"input_tokens": 971, "cache_read_tokens": 2048},
                    "stats": {},
                }
            ]
        )["cache_hit_rate_pct"]
        is None
    )
    assert (
        _summarize_executions([{"id": "legacy", "usage": {}, "stats": {}}])["cost"]
        is None
    )
    assert (
        _summarize_executions([{"id": "legacy", "usage": {}, "stats": {}}])[
            "cache_hit_rate_pct"
        ]
        is None
    )


def test_local_observation_error_rolls_up_to_turn_and_session(tmp_path):
    record = _record()
    record.steps.append(
        ExecutionStep(
            step_id="tool-error",
            parent_step_id="root",
            sequence=2,
            type="tool",
            name="Tool Call: terminal",
            started_at=datetime(2026, 9, 1, tzinfo=UTC),
            success=False,
            error="exit 1",
        )
    )
    store = ExecutionRecordStore(tmp_path)
    store.save(record)

    listing = TraceViewerService(store, _FakeLangfuse()).list_executions()

    turn = listing["executions"][0]
    assert turn["success"] is True
    assert turn["has_error"] is True
    assert turn["stats"]["errors"] == 1
    assert listing["summary"]["errors"] == 1
    assert listing["sessions"][0]["errors"] == 1


def test_model_usage_cost_cache_and_error_keep_zero_distinct_from_unknown():
    observations = [
        {
            "id": "model-zero",
            "traceId": "trace-zero",
            "name": "Model Call",
            "type": "GENERATION",
            "usageDetails": {
                "input": 0,
                "output": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "costDetails": {"total": 0},
            "metadata": {"success": True},
        },
        {
            "id": "tool-error",
            "traceId": "trace-zero",
            "name": "Tool Call: terminal",
            "metadata": {"success": False},
        },
        {
            "id": "model-unknown",
            "traceId": "trace-unknown",
            "name": "Model Call",
            "type": "GENERATION",
            "usageDetails": {},
            "costDetails": {},
        },
    ]

    stats = _stats_by_trace(observations)

    assert stats["trace-zero"] == {
        "model_calls": 1,
        "tool_calls": 1,
        "errors": 1,
        "usage": {
            "input": 0,
            "output": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost": 0,
        },
    }
    assert stats["trace-unknown"]["usage"] == {}


def test_old_sparse_record_remains_viewable_and_detail_is_sanitized(tmp_path):
    now = datetime(2026, 9, 1, tzinfo=UTC)
    record = ExecutionRecord.model_validate(
        {
            "identity": {"execution_id": "legacy"},
            "execution": {"started_at": now.isoformat()},
            "metadata": {"api_key": "sk-example-secret-123456789"},
        }
    )
    assert record.usage == UsageSummary()
    store = ExecutionRecordStore(tmp_path)
    store.save(record)

    service = TraceViewerService(store, _FakeLangfuse())
    listing = service.list_executions()
    detail = service.get_execution("legacy")

    assert listing["executions"][0]["stats"] == {
        "model_calls": 0,
        "tool_calls": 0,
        "errors": 0,
    }
    assert listing["summary"]["cost"] is None
    assert detail is not None
    assert detail["record"]["metadata"]["api_key"] == "[REDACTED]"
