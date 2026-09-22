from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from aegis_agent.cli import app
from aegis_agent.exceptions import ModelProviderError
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.quality.models import (
    EvaluationSummary,
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionStep,
    ExecutionSummary,
    UsageSummary,
)
from aegis_agent.quality.process import ProcessEvaluator, ProcessEvaluatorConfig
from aegis_agent.quality.store import ExecutionRecordStore

NOW = datetime(2026, 9, 8, tzinfo=UTC)


class _FailingJudgeProvider:
    name = "failing-judge"

    def stream(self, messages, tools=None):
        raise RuntimeError("judge unavailable")


class _RecordingJudgeProvider(FakeModelProvider):
    def __init__(self, script):
        super().__init__(script)
        self.messages = []

    def stream(self, messages, tools=None):
        self.messages = list(messages)
        return super().stream(messages, tools)


def _step(
    sequence: int,
    tool: str,
    arguments: object,
    *,
    success: bool | None = True,
    error: str | None = None,
    parent: str | None = "root",
) -> ExecutionStep:
    return ExecutionStep(
        step_id=f"step-{sequence}",
        parent_step_id=parent,
        sequence=sequence,
        type="tool",
        name=f"Tool Call: {tool}",
        input=arguments,
        output={"ok": success} if success is not None else None,
        success=success,
        error=error,
        started_at=NOW,
        finished_at=NOW,
        latency_ms=10,
        metadata={"tool_name": tool},
    )


def _non_tool(
    sequence: int,
    step_type: str,
    *,
    success: bool | None = True,
    error: str | None = None,
    parent: str | None = "root",
    metadata: dict | None = None,
) -> ExecutionStep:
    return ExecutionStep(
        step_id=f"step-{sequence}",
        parent_step_id=parent,
        sequence=sequence,
        type=step_type,
        name=step_type.title(),
        success=success,
        error=error,
        started_at=NOW,
        finished_at=NOW,
        metadata=metadata or {},
    )


def _record(
    *steps: ExecutionStep,
    success: bool | None = True,
    metadata: dict | None = None,
    evaluation: EvaluationSummary | None = None,
    usage: UsageSummary | None = None,
) -> ExecutionRecord:
    return ExecutionRecord(
        identity=ExecutionIdentity(
            execution_id="process-case",
            task_name="test process evaluation",
        ),
        execution=ExecutionSummary(success=success, latency_ms=100),
        usage=usage or UsageSummary(),
        steps=list(steps),
        evaluation=evaluation or EvaluationSummary(),
        metadata=metadata or {},
    )


def _grades(record: ExecutionRecord):
    result = ProcessEvaluator().evaluate(record)
    return result, {grade.grader_name: grade for grade in result.grades}


def test_case_repeated_tool_call():
    record = _record(
        _step(1, "read_file", {"path": "a.py"}),
        _step(2, "read_file", {"path": "a.py"}),
    )

    _, grades = _grades(record)

    grade = grades["repeated_tool_call"]
    assert grade.status == "warning"
    assert grade.affected_steps == ["step-1", "step-2"]
    assert grade.metadata["repeated_call_count"] == 1


def test_repeated_verification_after_state_change_is_not_a_false_positive():
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False),
        _step(2, "patch", {"path": "a.py", "old_string": "x", "new_string": "y"}),
        _step(3, "terminal", {"command": "pytest"}, success=True),
    )

    _, grades = _grades(record)

    assert grades["repeated_tool_call"].status == "pass"


def test_same_read_with_changed_or_missing_result_is_not_declared_no_progress():
    changed = _record(
        _step(1, "read_file", {"path": "a.py"}),
        _step(2, "read_file", {"path": "a.py"}),
    )
    changed.steps[1].output = {"content": "changed externally"}
    missing = _record(
        _step(1, "read_file", {"path": "a.py"}, success=None),
        _step(2, "read_file", {"path": "a.py"}, success=None),
    )

    _, changed_grades = _grades(changed)
    _, missing_grades = _grades(missing)

    assert changed_grades["repeated_tool_call"].status == "pass"
    assert missing_grades["repeated_tool_call"].status == "pass"


def test_case_repeated_failure():
    record = _record(
        *[
            _step(
                index,
                "read_file",
                {"path": "missing"},
                success=False,
                error="not found",
            )
            for index in range(1, 4)
        ],
        success=False,
    )

    _, grades = _grades(record)

    grade = grades["repeated_failure"]
    assert grade.status == "fail"
    assert grade.metadata["runs"][0]["attempt_count"] == 3


def test_diagnostic_success_and_changed_read_target_are_not_recovery():
    record = _record(
        _step(1, "read_file", {"path": "missing"}, success=False, error="not found"),
        _step(2, "list_directory", {"path": "."}),
        _step(3, "read_file", {"path": "present"}),
    )

    _, grades = _grades(record)

    grade = grades["failure_recovery"]
    assert grade.status == "fail"
    assert grade.metadata["recovered_count"] == 0
    assert "queried_environment" in grade.metadata["assessments"][0]["adjustments"]
    episode = grade.metadata["episodes"][0]
    assert episode["diagnostic_steps"] == ["step-2", "step-3"]
    assert episode["rule_recovery_step"] is None


def test_rule_episode_extracts_diagnosis_mutation_and_related_retry():
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False, error="failed"),
        _step(2, "read_file", {"path": "test.log"}),
        _step(3, "patch", {"path": "a.py", "old_string": "x", "new_string": "y"}),
        _step(4, "terminal", {"command": "pytest"}),
    )

    _, grades = _grades(record)

    grade = grades["failure_recovery"]
    episode = grade.metadata["episodes"][0]
    assert grade.status == "pass"
    assert episode["diagnostic_steps"] == ["step-2"]
    assert episode["mutation_steps"] == ["step-3"]
    assert episode["related_retries"][-1] == {
        "step_id": "step-4",
        "success": True,
        "diagnostic_only": False,
        "same_tool": True,
        "same_target": True,
    }
    assert episode["rule_recovery_step"] == "step-4"


def test_case_failed_recovery():
    record = _record(
        _step(1, "read_file", {"path": "missing"}, success=False, error="not found"),
        _step(2, "read_file", {"path": "missing"}, success=False, error="not found"),
        success=False,
    )

    _, grades = _grades(record)

    assert grades["failure_recovery"].status == "fail"
    assert grades["failure_recovery"].metadata["recovered_count"] == 0


def test_model_retry_is_runtime_owned_and_not_agent_recovery():
    record = _record(
        _non_tool(1, "model", success=False, error="temporary provider error"),
        _non_tool(2, "model", success=True),
        _non_tool(3, "final", success=True),
    )

    _, grades = _grades(record)

    recovery = grades["failure_recovery"]
    assert recovery.status == "insufficient_data"
    episode = recovery.metadata["episodes"][0]
    assert episode["retry_owner"] == "runtime"
    assert episode["rule_recovered"] is True
    assert episode["recovery_opportunity"] is False


def test_llm_judge_refines_only_failure_recovery_with_one_call():
    record = _record(
        _step(
            1,
            "terminal",
            {"command": "pytest tests/test_api.py"},
            success=False,
            error="failed",
        ),
        _step(2, "read_file", {"path": "test.log"}),
        _step(3, "patch", {"path": "api.py", "old_string": "x", "new_string": "y"}),
        _step(4, "pytest", {"path": "tests/test_api.py", "quiet": True}),
    )
    provider = FakeModelProvider(
        [
            FakeReply(
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "failure_step_id": "step-1",
                                "effective_diagnosis": True,
                                "adjustment_targets_failure": True,
                                "target_recovered": True,
                                "recovery_step_id": "step-4",
                                "confidence": 0.95,
                                "reason": "The failing test passed after inspecting logs and patching its code.",
                            }
                        ]
                    }
                )
            )
        ]
    )

    result = ProcessEvaluator(
        failure_recovery_judge=provider,
        config=ProcessEvaluatorConfig(final_verification_llm_enabled=False),
    ).evaluate(record)
    grades = {grade.grader_name: grade for grade in result.grades}

    recovery = grades["failure_recovery"]
    assert provider.calls == 1
    assert len(result.grades) == 6
    assert recovery.status == "pass"
    assert recovery.score == 1.0
    assert recovery.metadata["evaluation_mode"] == "rules+llm"
    assert recovery.metadata["rule_result"]["status"] == "fail"
    assert recovery.metadata["llm_judge"]["status"] == "applied"


def test_llm_judge_is_not_called_without_failure_or_when_disabled():
    no_failure_provider = FakeModelProvider([FakeReply(text="unused")])
    disabled_provider = FakeModelProvider([FakeReply(text="unused")])
    no_failure = _record(_step(1, "read_file", {"path": "a.py"}))
    disabled = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False),
        metadata={"process_evaluation_config": {"failure_recovery_llm_enabled": False}},
        success=False,
    )

    ProcessEvaluator(failure_recovery_judge=no_failure_provider).evaluate(no_failure)
    result = ProcessEvaluator(failure_recovery_judge=disabled_provider).evaluate(
        disabled
    )

    assert no_failure_provider.calls == 0
    assert disabled_provider.calls == 0
    recovery = next(
        grade for grade in result.grades if grade.grader_name == "failure_recovery"
    )
    assert recovery.metadata["evaluation_mode"] == "skipped"


def test_llm_judge_invalid_response_safely_preserves_rule_result():
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False, error="failed"),
        _step(2, "read_file", {"path": "test.log"}),
        success=False,
    )
    rule_result = ProcessEvaluator().evaluate(record)
    rule_grade = next(
        grade for grade in rule_result.grades if grade.grader_name == "failure_recovery"
    )
    provider = FakeModelProvider([FakeReply(text="not valid JSON")])

    hybrid_result = ProcessEvaluator(failure_recovery_judge=provider).evaluate(record)
    hybrid_grade = next(
        grade
        for grade in hybrid_result.grades
        if grade.grader_name == "failure_recovery"
    )

    assert provider.calls == 1
    assert hybrid_grade.status == rule_grade.status
    assert hybrid_grade.score == rule_grade.score
    assert hybrid_grade.message == rule_grade.message
    assert hybrid_grade.metadata["llm_judge"]["status"] == "fallback"
    assert hybrid_result.status == rule_result.status
    assert hybrid_result.overall_score == rule_result.overall_score


def test_llm_judge_provider_failure_safely_preserves_rule_result():
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False, error="failed"),
        _non_tool(2, "model"),
        success=False,
    )
    rule_result = ProcessEvaluator().evaluate(record)
    rule_grade = next(
        grade for grade in rule_result.grades if grade.grader_name == "failure_recovery"
    )

    hybrid_result = ProcessEvaluator(
        failure_recovery_judge=_FailingJudgeProvider()
    ).evaluate(record)
    hybrid_grade = next(
        grade
        for grade in hybrid_result.grades
        if grade.grader_name == "failure_recovery"
    )

    assert hybrid_grade.status == rule_grade.status
    assert hybrid_grade.score == rule_grade.score
    assert hybrid_grade.metadata["llm_judge"]["status"] == "fallback"
    assert hybrid_grade.metadata["llm_judge"]["reason"] == "RuntimeError"
    assert hybrid_result.status == rule_result.status
    assert hybrid_result.overall_score == rule_result.overall_score


def test_llm_judge_does_not_accept_unrelated_success_as_target_recovery():
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False, error="failed"),
        _step(2, "status", {"target": "service"}),
        _step(3, "write_file", {"path": "unrelated.txt", "content": "ok"}),
    )
    provider = FakeModelProvider(
        [
            FakeReply(
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "failure_step_id": "step-1",
                                "effective_diagnosis": True,
                                "adjustment_targets_failure": False,
                                "target_recovered": False,
                                "recovery_step_id": None,
                                "confidence": 0.9,
                                "reason": "The later write is unrelated to the failed test.",
                            }
                        ]
                    }
                )
            )
        ]
    )

    result = ProcessEvaluator(failure_recovery_judge=provider).evaluate(record)
    recovery = next(
        grade for grade in result.grades if grade.grader_name == "failure_recovery"
    )

    assert recovery.status == "fail"
    assert recovery.score == 0.25
    assert recovery.metadata["llm_judge"]["judgments"][0]["target_recovered"] is False


def test_llm_judge_prompt_uses_the_shared_secret_sanitizer():
    secret = "sk-this-must-never-leave-aegis"
    record = _record(
        _step(
            1,
            "terminal",
            {"command": "pytest", "api_key": secret},
            success=False,
        ),
        _step(2, "terminal", {"command": "pytest", "api_key": secret}),
    )
    provider = _RecordingJudgeProvider(
        [
            FakeReply(
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "failure_step_id": "step-1",
                                "effective_diagnosis": False,
                                "adjustment_targets_failure": False,
                                "target_recovered": True,
                                "recovery_step_id": "step-2",
                                "reason": "The same target succeeded on retry.",
                            }
                        ]
                    }
                )
            )
        ]
    )

    ProcessEvaluator(failure_recovery_judge=provider).evaluate(record)

    wire = "\n".join(message.content for message in provider.messages)
    assert secret not in wire
    assert "[REDACTED]" in wire


def test_case_missing_final_verification():
    record = _record(_step(1, "write_file", {"path": "a.py", "content": "x"}))

    _, grades = _grades(record)

    verification = grades["final_verification"]
    assert verification.status == "insufficient_data"
    assert verification.score is None
    assert "does not establish final verification" in verification.message
    assert verification.metadata["coverage"] == "unknown"


def test_case_good_final_verification():
    record = _record(
        _step(1, "patch", {"path": "a.py", "old_string": "x", "new_string": "y"}),
        _step(2, "terminal", {"command": "uv run pytest -q"}),
    )

    _, grades = _grades(record)

    assert grades["final_verification"].status == "pass"
    assert grades["final_verification"].metadata["verification_step"] == "step-2"


def test_failed_final_verification_is_distinct_from_missing_verification():
    record = _record(
        _step(1, "write_file", {"path": "a.py", "content": "x"}),
        _step(2, "terminal", {"command": "pytest"}, success=False, error="failed"),
        success=False,
    )

    _, grades = _grades(record)

    verification = grades["final_verification"]
    assert verification.status == "fail"
    assert "reported failure" in verification.message
    assert verification.metadata["failed_verification_steps"] == ["step-2"]


def test_task_metadata_can_disable_or_customize_final_verification():
    disabled = _record(
        _step(1, "write_file", {"path": "a.py", "content": "x"}),
        metadata={"process_evaluation_config": {"requires_verification": False}},
    )
    custom = _record(
        _step(1, "deploy", {"target": "preview"}),
        _step(2, "smoke", {"target": "preview"}),
        metadata={
            "process_evaluation_config": {
                "requires_verification": True,
                "mutation_tool_patterns": ["deploy"],
                "verification_tool_patterns": ["smoke"],
            }
        },
    )
    empty_required = _record(
        metadata={"process_evaluation_config": {"requires_verification": True}}
    )

    _, disabled_grades = _grades(disabled)
    _, custom_grades = _grades(custom)
    _, empty_grades = _grades(empty_required)

    assert disabled_grades["final_verification"].status == "pass"
    assert custom_grades["final_verification"].status == "pass"
    assert empty_grades["final_verification"].status == "insufficient_data"


def test_case_simple_loop():
    record = _record(
        *[_step(index, "search_files", {"pattern": "todo"}) for index in range(1, 4)]
    )

    _, grades = _grades(record)

    loop = grades["loop_detection"]
    assert loop.status == "fail"
    assert loop.metadata == {
        "evaluation_mode": "rules",
        "loop_detected": True,
        "loop_start_step": "step-1",
        "loop_start_sequence": 1,
        "loop_length": 1,
        "repeat_count": 3,
        "pattern": ["search_files"],
    }


def test_case_periodic_loop():
    calls = [
        ("search_files", {"pattern": "todo"}),
        ("read_file", {"path": "a.py"}),
    ] * 3
    record = _record(
        *[_step(index, tool, args) for index, (tool, args) in enumerate(calls, 1)]
    )

    _, grades = _grades(record)

    loop = grades["loop_detection"]
    assert loop.status == "fail"
    assert loop.metadata["loop_length"] == 2
    assert loop.metadata["repeat_count"] == 3


def test_case_no_loop():
    record = _record(
        _step(1, "search_files", {"pattern": "one"}),
        _step(2, "read_file", {"path": "a.py"}),
        _step(3, "search_files", {"pattern": "two"}),
        _step(4, "read_file", {"path": "b.py"}),
        _step(5, "terminal", {"command": "pytest"}),
    )

    _, grades = _grades(record)

    assert grades["loop_detection"].status == "pass"


def test_case_low_efficiency():
    record = _record(
        _step(1, "read_file", {"path": "a"}, success=False, error="bad"),
        _step(2, "list_directory", {"path": "."}),
        _step(3, "read_file", {"path": "b"}, success=False, error="bad"),
        _step(4, "search_files", {"pattern": "b"}),
    )

    _, grades = _grades(record)

    efficiency = grades["execution_efficiency"]
    assert efficiency.status == "not_scored"
    assert efficiency.metadata["metrics"]["failed_tool_call_count"] == 2
    assert efficiency.metadata["calibration_status"] == "uncalibrated"


def test_efficiency_uses_configured_baseline_but_has_no_default_step_limit():
    steps = [
        _step(index, "read_file", {"path": f"{index}.txt"}) for index in range(1, 10)
    ]
    without_baseline = _record(*steps)
    with_baseline = _record(
        *steps,
        metadata={
            "process_evaluation_config": {
                "baseline_metrics": {"tool_call_count": 3},
                "baseline_multiplier": 2,
            }
        },
    )

    _, normal = _grades(without_baseline)
    _, compared = _grades(with_baseline)

    assert normal["execution_efficiency"].status == "not_scored"
    assert normal["execution_efficiency"].score is None
    assert compared["execution_efficiency"].status == "warning"


def test_case_clean_success():
    record = _record(
        _step(1, "list_directory", {"path": "."}),
        _step(2, "read_file", {"path": "a.py"}),
        _non_tool(3, "final"),
        usage=UsageSummary(
            input_tokens=10, output_tokens=5, total_tokens=15, cost=0.01
        ),
    )

    result, grades = _grades(record)

    assert result.status == "pass"
    assert result.overall_score == 1.0
    assert all(
        grade.status == "pass"
        for name, grade in grades.items()
        if name != "execution_efficiency"
    )
    assert (
        grades["execution_efficiency"].metadata["calibration_status"] == "uncalibrated"
    )


def test_missing_mutation_success_is_insufficient_data():
    record = _record(
        _step(1, "write_file", {"path": "a.py", "content": "x"}, success=None)
    )

    _, grades = _grades(record)

    grade = grades["final_verification"]
    assert grade.status == "insufficient_data"
    assert grade.score is None
    assert grade.metadata["missing_field"] == "steps[].success"


def test_old_execution_record_and_pure_chat_remain_compatible():
    raw = {
        "schema_version": "1.0",
        "identity": {"execution_id": "old-chat"},
        "execution": {"success": True},
        "steps": [],
    }
    record = ExecutionRecord.model_validate(raw)

    result, grades = _grades(record)

    assert record.quality.process_evaluation == result
    assert grades["final_verification"].status == "insufficient_data"
    assert result.status == "insufficient_data"
    assert result.overall_score is None
    record.execution.final_output = "Complete answer"
    result, _ = _grades(record)
    assert result.metadata["evidence_status"] == "complete"
    assert result.status == "pass"


def test_subagent_steps_preserve_parent_addressability():
    record = _record(
        _non_tool(
            1,
            "agent",
            parent=None,
            metadata={"is_subagent": True},
        ),
        _step(2, "read_file", {"path": "a.py"}, parent="step-1"),
    )

    result, _ = _grades(record)

    assert len(result.grades) == 6
    assert all(
        step_id in {step.step_id for step in record.steps}
        for grade in result.grades
        for step_id in grade.affected_steps
    )


def test_runtime_failure_is_not_confused_with_harbor_outcome():
    runtime_failed = _record(
        _non_tool(1, "agent", success=False, error="runtime crashed", parent=None),
        success=False,
    )
    harbor_failed = _record(
        _step(1, "read_file", {"path": "a.py"}),
        success=True,
        evaluation=EvaluationSummary(passed=False, rewards={"reward": 0}),
    )

    runtime_result, runtime_grades = _grades(runtime_failed)
    harbor_result, harbor_grades = _grades(harbor_failed)

    assert runtime_grades["failure_recovery"].status == "insufficient_data"
    assert runtime_result.metadata["runtime_success"] is False
    assert runtime_result.metadata["outcome_success"] is None
    assert harbor_grades["failure_recovery"].status == "pass"
    assert harbor_result.metadata["harbor_passed"] is False
    assert harbor_result.status == "pass"


def test_evaluation_is_repeatable_and_saves_grader_versions():
    record = _record(_step(1, "read_file", {"path": "a.py"}))
    evaluator = ProcessEvaluator()

    first = evaluator.evaluate(record)
    second = evaluator.evaluate(record)

    assert [grade.model_dump() for grade in first.grades] == [
        grade.model_dump() for grade in second.grades
    ]
    versions = {grade.grader_name: grade.grader_version for grade in second.grades}
    assert versions["failure_recovery"] == "1.2.0"
    assert {
        version
        for name, version in versions.items()
        if name not in {"failure_recovery", "final_verification"}
    } == {"1.0.0"}
    assert record.quality.process_evaluation == second


def test_quality_evaluate_cli_updates_one_record_and_all_records(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    first = _record(_step(1, "read_file", {"path": "a.py"}))
    first.identity.execution_id = "one"
    second = _record(_step(1, "read_file", {"path": "b.py"}))
    second.identity.execution_id = "two"
    store.save(first)
    store.save(second)

    single = CliRunner().invoke(
        app,
        ["quality", "evaluate", "one", "--records-dir", str(tmp_path)],
    )
    batch = CliRunner().invoke(
        app,
        ["quality", "evaluate", "--all", "--records-dir", str(tmp_path), "--json"],
    )
    external = tmp_path / "external" / "record.json"
    external.parent.mkdir()
    third = _record(_step(1, "read_file", {"path": "c.py"}))
    third.identity.execution_id = "external-id"
    external.write_text(third.model_dump_json(), encoding="utf-8")
    by_path = CliRunner().invoke(
        app,
        ["quality", "evaluate", str(external), "--records-dir", str(tmp_path)],
    )

    assert single.exit_code == 0, single.output
    assert "Overall Score: 1.00" in single.output
    assert store.load("one").quality.process_evaluation is not None
    assert batch.exit_code == 0, batch.output
    payload = json.loads(batch.output)
    assert {item["execution_id"] for item in payload["evaluated"]} == {"one", "two"}
    assert payload["failed"] == []
    assert by_path.exit_code == 0, by_path.output
    assert (
        ExecutionRecord.model_validate_json(
            external.read_text(encoding="utf-8")
        ).quality.process_evaluation
        is not None
    )
    assert store.load("external-id").quality.process_evaluation is not None


def test_quality_evaluate_cli_can_enable_optional_failure_judge(tmp_path, monkeypatch):
    store = ExecutionRecordStore(tmp_path)
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False),
        _step(2, "terminal", {"command": "pytest"}),
    )
    record.identity.execution_id = "judge-case"
    store.save(record)
    provider = FakeModelProvider(
        [
            FakeReply(
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "failure_step_id": "step-1",
                                "effective_diagnosis": False,
                                "adjustment_targets_failure": False,
                                "target_recovered": True,
                                "recovery_step_id": "step-2",
                                "reason": "The same failing command later succeeded.",
                            }
                        ]
                    }
                )
            )
        ]
    )
    monkeypatch.setattr(
        "aegis_agent.cli._select_provider",
        lambda _backend: (provider, "test judge"),
    )

    result = CliRunner().invoke(
        app,
        [
            "quality",
            "evaluate",
            "judge-case",
            "--records-dir",
            str(tmp_path),
            "--failure-recovery-judge",
            "openai",
        ],
    )

    assert result.exit_code == 0, result.output
    assert provider.calls == 1
    saved = store.load("judge-case").quality.process_evaluation
    assert saved is not None
    recovery = next(
        grade for grade in saved.grades if grade.grader_name == "failure_recovery"
    )
    assert recovery.metadata["evaluation_mode"] == "rules+llm"


def test_quality_evaluate_cli_missing_judge_model_falls_back_to_rules(
    tmp_path, monkeypatch
):
    store = ExecutionRecordStore(tmp_path)
    record = _record(
        _step(1, "terminal", {"command": "pytest"}, success=False),
        success=False,
    )
    record.identity.execution_id = "judge-unavailable"
    store.save(record)

    def unavailable(_backend):
        raise ModelProviderError("judge model is not configured")

    monkeypatch.setattr("aegis_agent.cli._select_provider", unavailable)

    result = CliRunner().invoke(
        app,
        [
            "quality",
            "evaluate",
            "judge-unavailable",
            "--records-dir",
            str(tmp_path),
            "--failure-recovery-judge",
            "openai",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "using rules" in result.output
    saved = store.load("judge-unavailable").quality.process_evaluation
    assert saved is not None
    recovery = next(
        grade for grade in saved.grades if grade.grader_name == "failure_recovery"
    )
    assert recovery.metadata["evaluation_mode"] == "skipped"


def test_quality_evaluate_cli_rejects_ambiguous_selection(tmp_path):
    missing = CliRunner().invoke(
        app,
        ["quality", "evaluate", "--records-dir", str(tmp_path)],
    )
    both = CliRunner().invoke(
        app,
        ["quality", "evaluate", "one", "--all", "--records-dir", str(tmp_path)],
    )

    assert missing.exit_code == 2
    assert both.exit_code == 2


def test_process_fail_is_reported_without_becoming_a_quality_gate(tmp_path):
    store = ExecutionRecordStore(tmp_path)
    record = _record(
        *[
            _step(index, "read_file", {"path": "missing"}, success=False)
            for index in range(1, 4)
        ],
        success=False,
    )
    record.identity.execution_id = "bad-process"
    store.save(record)

    result = CliRunner().invoke(
        app,
        ["quality", "evaluate", "bad-process", "--records-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Process Status: FAIL" in result.output
    assert "FAIL" in result.output
