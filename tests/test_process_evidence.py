from datetime import UTC, datetime

from aegis_agent.quality.models import (
    ExecutionIdentity,
    ExecutionRecord,
    ExecutionStep,
    ExecutionSummary,
)
from aegis_agent.quality.process import (
    ExecutionEfficiencyGrader,
    FailureRecoveryGrader,
    ProcessEvaluator,
)


def step(sequence, kind="tool", success=True, **metadata):
    return ExecutionStep(
        step_id=str(sequence),
        sequence=sequence,
        type=kind,
        name="terminal",
        input={"cmd": "false"},
        success=success,
        started_at=datetime.now(UTC),
        metadata=metadata,
    )


def record(steps=(), **execution):
    return ExecutionRecord(
        identity=ExecutionIdentity(execution_id="evidence"),
        steps=list(steps),
        execution=ExecutionSummary(**execution),
    )


def test_setup_and_empty_trace_are_unscored_without_rewriting_source():
    for exception in (None, "AgentSetupTimeoutError"):
        source = record(success=False, exception_type=exception)
        original = source.model_dump(exclude={"quality"})
        result = ProcessEvaluator().evaluate(source)
        assert result.overall_score is None
        assert result.status == "insufficient_data"
        assert result.metadata["evidence_status"] == "missing"
        assert all(grade.score is None for grade in result.grades)
        assert source.model_dump(exclude={"quality"}) == original


def test_complete_no_tool_answer_has_evidence():
    source = record(
        success=True, final_output="The answer is 42.", stop_reason="final_answer"
    )
    result = ProcessEvaluator().evaluate(source)
    assert result.metadata["evidence_status"] == "complete"
    assert result.status == "pass"


def test_legacy_verifier_failure_preserves_runtime_completion():
    source = record(
        [step(1, "model")],
        success=False,
        stop_reason="final_answer",
        exception_type="VerifierTimeoutError",
        error="download failed",
    )
    result = ProcessEvaluator().evaluate(source)
    assert result.metadata["failure_phase"] == "verifier"
    assert result.metadata["runtime_success"] is True
    assert result.metadata["outcome_success"] is None
    assert result.metadata["evidence_status"] == "complete"
    assert source.execution.success is False


def test_terminal_model_failure_is_runtime_owned_not_agent_recovery_failure():
    source = record([step(1, "model", False)], success=False)
    result = ProcessEvaluator(graders=[FailureRecoveryGrader()]).evaluate(source)
    grade = result.grades[0]
    assert grade.status == "insufficient_data"
    assert grade.score is None
    episode = grade.metadata["episodes"][0]
    assert episode["error_source"] == "model"
    assert episode["exposed_to_agent"] is False
    assert episode["recovery_opportunity"] is False
    assert episode["retry_owner"] == "runtime"
    assert result.metadata["evidence_status"] == "partial"


def test_tool_failure_with_next_model_action_has_recovery_opportunity():
    source = record([step(1, success=False), step(2, "model")], success=True)
    grade = (
        ProcessEvaluator(graders=[FailureRecoveryGrader()]).evaluate(source).grades[0]
    )
    assert grade.status == "fail"
    assert grade.metadata["episodes"][0]["recovery_opportunity"] is True
    assert grade.metadata["episodes"][0]["exposed_to_agent"] is True


def test_efficiency_without_baseline_retains_metrics_but_no_score():
    source = record([step(1, "model")], success=True)
    grade = (
        ProcessEvaluator(graders=[ExecutionEfficiencyGrader()])
        .evaluate(source)
        .grades[0]
    )
    assert grade.score is None
    assert grade.metadata["calibration_status"] == "uncalibrated"
    assert grade.metadata["metrics"]["model_call_count"] == 1


class FailingJudge:
    name = "fake-timeout"
    model = "fake"

    def stream(self, messages, tools=None):
        raise TimeoutError("offline test")


def test_judge_timeout_has_telemetry_and_rules_fallback():
    source = record([step(1, success=False), step(2, "model")], success=True)
    grade = (
        ProcessEvaluator(
            graders=[FailureRecoveryGrader()], failure_recovery_judge=FailingJudge()
        )
        .evaluate(source)
        .grades[0]
    )
    judge = grade.metadata["llm_judge"]
    assert judge["status"] == "fallback"
    assert judge["reason"] == "TimeoutError"
    assert judge["latency_ms"] >= 0
    assert judge["prompt_version"]
    assert judge["usage"] is None
    assert judge["model"] == "fake"


def test_partial_trace_never_reports_a_complete_numeric_score():
    source = record([step(1, success=False), step(2, "model")], success=False)
    result = ProcessEvaluator(graders=[FailureRecoveryGrader()]).evaluate(source)
    assert result.status == "fail"
    assert result.overall_score is None


def test_terminal_failure_does_not_invoke_judge():
    class UnusedJudge:
        name = "unused"
        calls = 0

        def stream(self, messages, tools=None):
            self.calls += 1
            raise AssertionError("No Agent recovery opportunity")

    judge = UnusedJudge()
    result = ProcessEvaluator(
        graders=[FailureRecoveryGrader()], failure_recovery_judge=judge
    ).evaluate(record([step(1, "model", False)], success=False))
    assert judge.calls == 0
    assert result.grades[0].metadata["llm_judge"]["reason"] == "no_recovery_opportunity"
