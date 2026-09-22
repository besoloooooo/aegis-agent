from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from aegis_agent.quality.models import (
    EvaluationSummary,
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionStep,
    ExecutionSummary,
    UsageSummary,
)
from aegis_agent.quality.process import ProcessEvaluator
from aegis_agent.quality.store import ExecutionRecordStore
from aegis_agent.quality.viewer import (
    LangfuseReader,
    TraceViewerService,
    _cloud_summary,
    _stats_by_root,
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


def _evaluation_record() -> ExecutionRecord:
    record = _record()
    record.run_kind = "evaluation"
    record.identity.execution_id = "eval-trial-1"
    record.identity.trace_id = "eval-trace-1"
    record.identity.session_id = "eval-session-1"
    record.identity.job_id = "harbor-job-1"
    record.identity.trial_id = "eval-trial-1"
    record.identity.task_id = '{"path":"examples/tasks/hello-world"}'
    record.identity.task_name = "harbor/hello-world"
    record.evaluation = EvaluationSummary(
        verifier_result={"rewards": {"reward": 1.0}},
        rewards={"reward": 1.0},
        passed=True,
    )
    return record


def _observation(
    trace_id: str,
    observation_id: str | None = None,
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
        "id": observation_id or f"root-{trace_id}",
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


def test_cloud_roots_with_a_reused_trace_id_remain_independently_addressable(
    tmp_path,
):
    roots = [
        _observation("shared-trace", "root-one"),
        _observation("shared-trace", "root-two"),
    ]

    listing = TraceViewerService(
        ExecutionRecordStore(tmp_path),
        _FakeLangfuse(roots=roots),
    ).list_langfuse_executions()

    assert [item["id"] for item in listing["executions"]] == [
        "langfuse:root-one",
        "langfuse:root-two",
    ]
    assert {item["trace_id"] for item in listing["executions"]} == {"shared-trace"}


def test_shared_trace_stats_are_scoped_to_each_root_observation():
    first_root = _observation("shared-trace", "root-one")
    second_root = _observation("shared-trace", "root-two", level="ERROR")
    first_model = {
        "id": "model-one",
        "traceId": "shared-trace",
        "parentObservationId": "root-one",
        "name": "Model Call",
        "type": "GENERATION",
        "usageDetails": {"input": 10},
    }
    second_tool = {
        "id": "tool-two",
        "traceId": "shared-trace",
        "parentObservationId": "root-two",
        "name": "Tool Call: terminal",
        "type": "TOOL",
    }

    stats = _stats_by_root(
        [first_root, first_model, second_root, second_tool],
        {"root-one", "root-two"},
    )

    assert stats["root-one"] == {
        "model_calls": 1,
        "tool_calls": 0,
        "errors": 0,
        "usage": {"input": 10.0},
    }
    assert stats["root-two"] == {
        "model_calls": 0,
        "tool_calls": 1,
        "errors": 1,
        "usage": {},
    }


def test_service_classifies_harbor_evaluation_identity_and_reward(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    store.save(_evaluation_record())

    listing = TraceViewerService(store, _FakeLangfuse()).list_executions()

    evaluation = listing["executions"][0]
    assert evaluation["run_kind"] == "evaluation"
    assert evaluation["job_id"] == "harbor-job-1"
    assert evaluation["trial_id"] == "eval-trial-1"
    assert evaluation["task_id"] == '{"path":"examples/tasks/hello-world"}'
    assert evaluation["reward"] == 1.0
    assert evaluation["rewards"] == {"reward": 1.0}
    assert evaluation["passed"] is True


def test_service_exposes_process_evaluation_separately_from_harbor_outcome(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    record = _evaluation_record()
    record.evaluation.passed = True
    ProcessEvaluator().evaluate(record)
    assert record.quality.process_evaluation is not None
    record.quality.process_evaluation.status = "fail"
    record.quality.process_evaluation.overall_score = 0.74
    record.quality.process_evaluation.grades[0].status = "warning"
    store.save(record)

    service = TraceViewerService(store, _FakeLangfuse())
    summary = service.list_executions()["executions"][0]
    detail = service.get_execution(record.identity.execution_id)

    assert summary["passed"] is True
    assert summary["process_status"] == "fail"
    assert summary["process_score"] == 0.74
    assert summary["process_issue_count"] == 1
    assert detail is not None
    assert detail["record"]["evaluation"]["passed"] is True
    assert detail["record"]["quality"]["process_evaluation"]["status"] == "fail"


def test_service_classifies_legacy_harbor_record_without_run_kind(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    record = _evaluation_record()
    record.run_kind = (
        "task"  # Default when loading records written before this field existed.
    )
    store.save(record)

    listing = TraceViewerService(store, _FakeLangfuse()).list_executions()

    assert listing["executions"][0]["run_kind"] == "evaluation"


def test_viewer_keeps_legacy_verifier_interruption_separate_from_runtime(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    record = _evaluation_record()
    record.execution.success = False
    record.execution.stop_reason = "final_answer"
    record.execution.exception_type = "VerifierTimeoutError"
    record.evaluation.passed = False
    ProcessEvaluator().evaluate(record)
    store.save(record)

    summary = TraceViewerService(store, _FakeLangfuse()).list_executions()["executions"][0]

    assert summary["success"] is True
    assert summary["passed"] is None
    assert summary["failure_phase"] == "verifier"
    assert record.execution.success is False  # Original evidence is unchanged.


def test_cloud_run_kind_prefers_metadata_and_defaults_to_conversation(tmp_path):
    conversation = _observation("conversation")
    task = _observation("task")
    task["metadata"]["run_kind"] = "task"
    evaluation = _observation("evaluation")
    evaluation["metadata"]["source"] = "harbor"

    listing = TraceViewerService(
        ExecutionRecordStore(tmp_path),
        _FakeLangfuse(roots=[conversation, task, evaluation]),
    ).list_langfuse_executions()["executions"]

    by_trace = {item["trace_id"]: item for item in listing}
    assert by_trace["conversation"]["run_kind"] == "conversation"
    assert by_trace["task"]["run_kind"] == "task"
    assert by_trace["evaluation"]["run_kind"] == "evaluation"


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


def test_langfuse_root_listing_follows_cursors_and_reports_safety_truncation():
    calls = []

    def get_many(**kwargs):
        calls.append(kwargs)
        if kwargs.get("cursor") is None:
            return SimpleNamespace(
                data=[_observation("trace-1"), _observation("trace-2")],
                meta=SimpleNamespace(cursor="next-page"),
            )
        return SimpleNamespace(
            data=[_observation("trace-3")],
            meta=SimpleNamespace(cursor=None),
        )

    client = SimpleNamespace(
        api=SimpleNamespace(observations=SimpleNamespace(get_many=get_many)),
        shutdown=lambda: None,
    )
    reader = LangfuseReader(client)

    roots, error = reader.list_roots(limit=3)

    assert error is None
    assert [item["traceId"] for item in roots] == [
        "trace-1",
        "trace-2",
        "trace-3",
    ]
    assert [call.get("cursor") for call in calls] == [None, "next-page"]
    assert reader.roots_has_more is False

    calls.clear()
    roots, error = reader.list_roots(limit=2)
    assert error is None
    assert len(roots) == 2
    assert reader.roots_has_more is True


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
            assert "Conversations" in html
            assert "Evaluations" in html
            assert "All Runs" in html
            assert "Sync Harbor" in html
            assert "HARBOR EVALUATIONS" in html
            assert "EVALUATION JOB SUMMARY" in html
            assert 'state.view === "evaluation"' in html
            assert "Sessions / Turns" in html
            assert "Model / Tool Calls" in html
            assert "Failed Runs / Error Obs" in html
            assert 'const runFailed = (item) => item.success === false' in html
            assert 'id="refresh-icon"' in html
            assert '$("refresh-icon").classList.add("spin")' in html
            assert "errorRollups" not in html
            assert "matchedLocalTraces" in html
            assert "matchingSummaries.length === 1" in html
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
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]

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


def test_http_viewer_syncs_harbor_with_same_origin_token(tmp_path):
    store = ExecutionRecordStore(tmp_path / "records")
    jobs_dir = tmp_path / "harbor" / "jobs"
    result_path = jobs_dir / "job-1" / "trial-1" / "result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "id": "sync-trial-1",
                "trial_name": "hello-world__sync",
                "task_name": "harbor/hello-world",
                "task_id": {"path": "examples/tasks/hello-world"},
                "config": {"job_id": "sync-job-1"},
                "verifier_result": {"rewards": {"reward": 1}},
            }
        ),
        encoding="utf-8",
    )
    server = build_server(
        "127.0.0.1",
        0,
        store=store,
        langfuse=_FakeLangfuse(),
        harbor_jobs_dir=jobs_dir,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/api/executions", timeout=2) as response:
            status = json.load(response)["status"]
        assert status["harbor_jobs_exists"] is True
        assert status["harbor_jobs_dir"] == str(jobs_dir.resolve())

        denied = Request(
            f"{base}/api/harbor/sync",
            data=b"",
            method="POST",
            headers={"X-Aegis-Sync-Token": "wrong"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(denied, timeout=2)
        assert error.value.code == 403

        allowed = Request(
            f"{base}/api/harbor/sync",
            data=b"",
            method="POST",
            headers={"X-Aegis-Sync-Token": status["sync_token"]},
        )
        with urlopen(allowed, timeout=2) as response:
            report = json.load(response)
        assert report["imported"] == 1
        assert report["failed"] == 0

        with urlopen(f"{base}/api/executions", timeout=2) as response:
            listing = json.load(response)
        assert listing["executions"][0]["execution_id"] == "sync-trial-1"
        assert listing["executions"][0]["run_kind"] == "evaluation"
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
    assert raw_bailian_summary["cache_hit_rate_pct"] == pytest.approx(2048 / 3019 * 100)
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


def test_recovered_local_observation_error_does_not_fail_run(tmp_path):
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


def test_recovered_cloud_observation_error_does_not_fail_root():
    root = _observation("root", stop_reason="final_answer")

    summary = _cloud_summary(
        root,
        stats={"model_calls": 1, "tool_calls": 1, "errors": 1, "usage": {}},
    )

    assert summary["success"] is True
    assert summary["has_error"] is True
    assert summary["stats"]["errors"] == 1


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
