"""Regression evidence for the September process-evaluator review."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.quality.models import ExecutionIdentity, ExecutionRecord, ExecutionStep
from aegis_agent.quality.process import ProcessEvaluatorConfig
from aegis_agent.quality.verification import grade_verification, mutation_artifacts


def action(seq, command=None, *, path=None, output="1 passed", success=True):
    value = {"command": command} if command else {"path": path, "content": "changed"}
    step = ExecutionStep(step_id=str(seq), sequence=seq, type="tool", name="shell",
                         started_at=datetime(2026, 9, 19, tzinfo=UTC), input=value,
                         output=output, success=success)
    return SimpleNamespace(step=step, tool_name="shell" if command else "write_file")


def context(*actions, requirements=None):
    record = ExecutionRecord(identity=ExecutionIdentity(execution_id="verification-case"),
                             metadata={"verification_requirements": requirements or []})
    return SimpleNamespace(record=record, config=ProcessEvaluatorConfig(),
                           actions=actions, steps=tuple(a.step for a in actions))


def judge(*, coverage="full", check="2", logic="2", artifact="result.py",
          input_evidence="assert actual == expected", output_evidence="PASS", uncovered=None):
    return FakeModelProvider([FakeReply(text=json.dumps({
        "coverage": coverage, "chains": [{"mutation_step_id": "1", "check_step_id": check,
        "logic_step_id": logic, "artifact": artifact, "input_evidence": input_evidence,
        "output_evidence": output_evidence, "confidence": 0.95, "reason": "comparison passed"}],
        "uncovered_requirements": uncovered or [], "reason": "Observed comparison coverage",
    }))])


def test_inline_python_write_requires_verification():
    write = action(1, "python3 -c \"open('summary.csv', 'w').write('a,b')\"")
    assert mutation_artifacts(write) == {"summary.csv"}
    grade = grade_verification(context(write))
    assert grade.metadata["required"] is True
    assert grade.status == "insufficient_data"


@pytest.mark.parametrize("command", ["echo pytest", "printf 'pytest'", "git status",
    "python3 -c \"print('PASS')\"", "pytest unrelated/file.py"])
def test_name_and_unrelated_checks_are_not_confirmation(command):
    grade = grade_verification(context(action(1, path="result.py"), action(2, command)))
    assert grade.status != "pass"


def test_test_write_is_not_test_execution():
    grade = grade_verification(context(action(1, path="result.py"),
        action(2, "echo 'pytest' > test_generated.py")))
    assert grade.status == "insufficient_data"


def test_framework_suite_after_change_passes_and_stale_check_does_not():
    assert grade_verification(context(action(1, path="result.py"),
                                     action(2, "pytest"))).status == "pass"
    assert grade_verification(context(action(1, "pytest"),
                                     action(2, path="result.py"))).status != "pass"


def test_failure_output_overrides_success_flag():
    assert grade_verification(context(action(1, path="result.py"),
        action(2, "pytest", output="FAIL result"))).status != "pass"


def test_latest_framework_result_wins_and_failure_does_not_need_judge():
    ctx = context(action(1, path="result.py"), action(2, "pytest"),
                  action(3, "pytest", output="FAILED result"))
    provider = judge()
    assert grade_verification(ctx, provider).status == "fail"
    assert provider.calls == 0
    ctx = context(action(1, path="result.py"), action(2, "pytest", output="FAILED result"),
                  action(3, "pytest"))
    assert grade_verification(ctx).status == "pass"


def test_custom_heredoc_checks_require_grounded_judge():
    check = action(2, "python3 <<'PY'\nactual = 1\nexpected = 1\nassert actual == expected\nprint('PASS')\nPY",
                   output="PASS")
    ctx = context(action(1, path="result.py"), check)
    assert grade_verification(ctx).status == "insufficient_data"
    grade = grade_verification(ctx, judge())
    assert grade.status == "pass"
    assert grade.metadata["llm_judge"]["status"] == "applied"
    assert grade.metadata["llm_judge"]["latency_ms"] >= 0


def test_nonexistent_judge_step_and_wrong_quote_are_unknown():
    ctx = context(action(1, path="result.py"), action(2,
        "python3 -c 'assert actual == expected'", output="PASS"))
    assert grade_verification(ctx, judge(check="invented")).status == "insufficient_data"
    assert grade_verification(ctx, judge(output_evidence="imaginary")).status == "insufficient_data"


def test_judge_cannot_promote_echo_or_print_only():
    for command in ["echo 'assert actual == expected'", "python3 -c \"print('assert actual == expected')\""]:
        ctx = context(action(1, path="result.py"), action(2, command, output="PASS"))
        assert grade_verification(ctx, judge()).status == "insufficient_data"


def test_partial_functional_coverage_preserves_missing_directory_constraint():
    ctx = context(action(1, path="result.py"), action(2,
        "python3 -c 'assert actual == expected'", output="PASS"),
        requirements=["directory contains only result.py"])
    grade = grade_verification(ctx, judge(coverage="partial", uncovered=["directory cleanup"]))
    assert grade.status == "warning"
    assert grade.metadata["coverage"] == "partial"
    assert grade.metadata["uncovered_requirements"] == ["directory cleanup"]


def test_custom_script_uses_recorded_source_logic():
    source = action(1, path="result.py")
    source.step.input["content"] = "assert actual == expected"
    grade = grade_verification(context(source, action(2, "python3 result.py", output="PASS")),
                               judge(logic="1"))
    assert grade.status == "pass"


def test_same_shell_rules_never_assume_order():
    for command in ["echo data > result.py; pytest", "pytest; echo data > result.py"]:
        assert grade_verification(context(action(1, command))).status == "insufficient_data"


def test_same_python_step_order_is_checked_locally():
    good = action(1, "python3 -c \"open('result.py', 'w').write('1'); assert actual == expected\"",
                  output="PASS")
    bad = action(1, "python3 -c \"assert actual == expected; open('result.py', 'w').write('1')\"",
                 output="PASS")
    assert grade_verification(context(good), judge(check="1", logic="1")).status == "pass"
    assert grade_verification(context(bad), judge(check="1", logic="1")).status == "insufficient_data"


def test_script_written_by_cat_is_grounded_and_unrelated_script_is_rejected():
    source = action(1, "cat > result.py <<'PY'\nassert actual == expected\nprint('PASS')\nPY")
    assert grade_verification(context(source, action(2, "python3 result.py", output="PASS")),
                              judge(logic="1")).status == "pass"
    assert grade_verification(context(source, action(2, "python3 unrelated.py", output="PASS")),
                              judge(logic="1")).status == "insufficient_data"


def test_configured_tools_keep_explicit_target_relationship():
    mutation = action(1, path="preview")
    mutation.tool_name = "deploy"
    check = action(2, path="preview")
    check.tool_name = "smoke"
    ctx = context(mutation, check)
    ctx.config.mutation_tool_patterns = ["deploy"]
    ctx.config.verification_tool_patterns = ["smoke"]
    assert grade_verification(ctx).status == "pass"


def test_markdown_fenced_verification_judgment_is_parsed():
    check = action(2, "python3 <<'PY'\nactual = 1\nexpected = 1\nassert actual == expected\nPY",
                   output="PASS")
    plain = json.dumps({
        "coverage": "full",
        "chains": [{"mutation_step_id": "1", "check_step_id": "2",
                     "logic_step_id": "2", "artifact": "result.py",
                     "input_evidence": "assert actual == expected",
                     "output_evidence": "PASS", "confidence": 0.95,
                     "reason": "comparison passed"}],
        "uncovered_requirements": [], "reason": "Observed comparison coverage",
    })
    fenced = FakeModelProvider([FakeReply(text="```json\\n" + plain + "\\n```")])
    grade = grade_verification(context(action(1, path="result.py"), check), fenced)
    assert grade.status == "pass"
    assert grade.metadata["llm_judge"]["status"] == "applied"


def test_judge_timeout_records_fallback_telemetry():
    class Unavailable:
        name = "test-timeout"

        def stream(self, messages, tools=None):
            raise TimeoutError("unavailable")

    grade = grade_verification(context(action(1, path="result.py")), Unavailable())
    assert grade.status == "insufficient_data"
    assert grade.metadata["llm_judge"]["reason"] == "TimeoutError"
    assert grade.metadata["llm_judge"]["latency_ms"] >= 0
