"""Calibration helpers for task-scoped execution-efficiency baselines."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

from aegis_agent.quality.models import ExecutionRecord
from aegis_agent.quality.process import ProcessEvaluator

LABELS = {"pass", "fail", "partial", "unknown"}
STATUS_LABEL = {"warning": "partial", "insufficient_data": "unknown"}
EFFICIENCY_METRICS = (
    "step_count", "model_call_count", "tool_call_count", "failed_tool_call_count",
    "repeated_call_count", "token_count", "cost", "latency_ms",
)
MIN_SUCCESSFUL_SAMPLES = 5


def _metrics(record: ExecutionRecord) -> dict[str, int | float | None]:
    tools = [step for step in record.steps if step.type == "tool"]
    # Keep this calculation independent of a ProcessEvaluator instance and its judge.
    total_tokens = record.usage.total_tokens
    if total_tokens is None:
        input_tokens = record.usage.input_tokens_including_cache
        if input_tokens is None:
            parts = [record.usage.input_tokens, record.usage.cache_read_tokens,
                     record.usage.cache_write_tokens]
            if any(value is not None for value in parts):
                input_tokens = sum(value or 0 for value in parts)
        if input_tokens is not None or record.usage.output_tokens is not None:
            total_tokens = (input_tokens or 0) + (record.usage.output_tokens or 0)
    return {
        "step_count": len(record.steps),
        "model_call_count": sum(step.type == "model" for step in record.steps),
        "tool_call_count": len(tools),
        "failed_tool_call_count": sum(step.success is False for step in tools),
        # Repeated calls are deliberately not inferred for calibration; the value is
        # still preserved as a supported metric when an evaluator supplies it.
        "repeated_call_count": None,
        "token_count": total_tokens,
        "cost": record.usage.cost,
        "latency_ms": record.execution.latency_ms,
    }


def _successful(record: ExecutionRecord) -> bool:
    metadata = record.metadata
    phase = str(metadata.get("failure_phase") or "").casefold()
    if phase in {"setup", "verifier"} or metadata.get("runtime_success") is False:
        return False
    if record.execution.success is not True:
        return False
    return not (
        record.evaluation.passed is False
        or metadata.get("outcome_success") is False
    )


def build_baseline(
    records: list[ExecutionRecord],
    task_id: str | None,
    *,
    exclude_execution_id: str | None = None,
    minimum_samples: int = MIN_SUCCESSFUL_SAMPLES,
) -> dict[str, Any]:
    """Return a task baseline, using only successful historical executions."""
    candidates = [
        record for record in records
        if task_id and record.identity.task_id == task_id
        and record.identity.execution_id != exclude_execution_id
        and _successful(record)
    ]
    result: dict[str, Any] = {
        "task_id": task_id,
        "successful_sample_count": len(candidates),
        "required_sample_count": minimum_samples,
        "calibration_status": "calibrated" if len(candidates) >= minimum_samples else "uncalibrated",
        "baseline_metrics": {},
    }
    if len(candidates) < minimum_samples:
        return result
    for metric in EFFICIENCY_METRICS:
        values = [value for record in candidates
                  if (value := _metrics(record).get(metric)) is not None]
        if values:
            result["baseline_metrics"][metric] = median(values)
    return result


def evaluate_corpus(
    cases: list[dict[str, Any]],
    records_directory: Path,
    *,
    repeats: int = 2,
    evaluator: ProcessEvaluator | None = None,
) -> dict[str, Any]:
    """Count false alarms, misses, abstentions, and disagreements separately."""
    if repeats < 1:
        raise ValueError("repeats must be positive")
    runner = evaluator or ProcessEvaluator()
    totals: dict[str, Counter] = {}
    details = []
    missing = []
    root = records_directory.resolve()
    for case in cases:
        execution_id = case["execution_id"]
        path = (root / f"{execution_id}.json").resolve()
        if path.parent != root:
            raise ValueError("execution_id must identify a file within the record directory")
        labels = case["labels"]
        if any(label not in LABELS for label in labels.values()):
            raise ValueError("labels must be pass, fail, partial, or unknown")
        if not path.is_file():
            missing.append(execution_id)
            continue
        source = ExecutionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        runs = [runner.evaluate(source.model_copy(deep=True)) for _ in range(repeats)]
        for name, expected in labels.items():
            observed = []
            for run in runs:
                grade = next((g for g in run.grades if g.grader_name == name), None)
                status = grade.status if grade else "insufficient_data"
                observed.append((STATUS_LABEL.get(status, status), grade.score if grade else None))
            counts = totals.setdefault(name, Counter())
            counts["cases"] += 1
            counts["unstable_cases"] += int(len(set(observed)) > 1)
            first = observed[0][0]
            counts["matches"] += int(first == expected)
            counts["abstentions"] += int(first == "unknown")
            counts["false_alarms"] += int(expected == "pass" and first == "fail")
            counts["misses"] += int(expected == "fail" and first == "pass")
            counts["overclaims"] += int(expected in {"partial", "unknown"} and first == "pass")
            counts["mismatches"] += int(first != expected)
            details.append({"execution_id": execution_id, "grader": name,
                            "expected": expected, "observed": observed,
                            "reason": case.get("reason")})
    return {"repeats": repeats, "graders": {k: dict(v) for k, v in totals.items()},
            "missing_records": missing, "cases": details,
            "scope": "Selected regression examples; not population error rates or model rankings."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("records_directory", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    cases = json.loads(args.labels.read_text(encoding="utf-8"))["cases"]
    print(json.dumps(evaluate_corpus(cases, args.records_directory, repeats=args.repeats),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
