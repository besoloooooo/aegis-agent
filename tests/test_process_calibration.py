import json
from datetime import UTC, datetime

import pytest

from aegis_agent.quality.calibration import evaluate_corpus
from aegis_agent.quality.models import (
    ExecutionIdentity,
    ExecutionRecord,
    ProcessEvaluationResult,
    ProcessGrade,
)


class AlternatingEvaluator:
    def __init__(self):
        self.calls = 0

    def evaluate(self, record):
        # Each evaluation must get a fresh copy, even if a custom judge mutates it.
        assert record.metadata == {}
        record.metadata["derived"] = True
        self.calls += 1
        status = "fail" if self.calls % 2 else "pass"
        return ProcessEvaluationResult(
            evaluated_at=datetime.now(UTC), evaluator_version="test",
            status=status, summary="test", grades=[ProcessGrade(
                grader_name="final_verification", grader_version="test",
                status=status, score=0 if status == "fail" else 1,
                severity="info", category="verification", message="test",
            )],
        )


def test_calibration_reports_disagreement_without_rewriting_trace(tmp_path):
    record = ExecutionRecord(identity=ExecutionIdentity(execution_id="sample"))
    path = tmp_path / "sample.json"
    original = record.model_dump_json()
    path.write_text(original)
    cases = [{"execution_id": "sample", "labels": {"final_verification": "pass"}}]
    result = evaluate_corpus(cases, tmp_path, evaluator=AlternatingEvaluator())
    counts = result["graders"]["final_verification"]
    assert counts["false_alarms"] == 1
    assert counts["unstable_cases"] == 1
    assert counts["misses"] == 0
    assert path.read_text() == original


def test_missing_records_are_not_scored_as_success(tmp_path):
    result = evaluate_corpus([
        {"execution_id": "absent", "labels": {"final_verification": "unknown"}},
    ], tmp_path)
    assert result["missing_records"] == ["absent"]
    assert result["graders"] == {}


@pytest.mark.parametrize("case", [
    {"execution_id": "../escape", "labels": {}},
    {"execution_id": "sample", "labels": {"final_verification": "invented"}},
])
def test_invalid_calibration_inputs_are_rejected(tmp_path, case):
    with pytest.raises(ValueError):
        evaluate_corpus([case], tmp_path)


def test_review_corpus_keeps_reward_and_verification_coverage_distinct():
    from pathlib import Path

    labels = json.loads((Path(__file__).parent / "fixtures" /
                         "process_review_labels.json").read_text())["cases"]
    polyglot = next(c for c in labels if c["execution_id"].startswith("d23d"))
    assert polyglot["labels"]["final_verification"] == "partial"
    assert polyglot["labels"]["failure_recovery"] == "pass"
