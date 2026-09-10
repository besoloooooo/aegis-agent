"""Rule-first, optionally LLM-assisted process evaluation for ExecutionRecord 1.0."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Protocol

from pydantic import BaseModel, Field

from aegis_agent.events import collect_response
from aegis_agent.models.base import Message, ModelProvider, Role
from aegis_agent.observability.sanitize import sanitize
from aegis_agent.quality.models import (
    ExecutionRecord,
    ExecutionStep,
    ProcessEvaluationResult,
    ProcessGrade,
    ProcessSeverity,
    ProcessStatus,
)

EVALUATOR_VERSION = "1.1.0"
GRADER_VERSION = "1.0.0"
FAILURE_RECOVERY_GRADER_VERSION = "1.1.0"
FAILURE_RECOVERY_LLM_GRADER_VERSION = "1.0.0"

logger = logging.getLogger(__name__)


def _default_weights() -> dict[str, float]:
    return {
        "repeated_tool_call": 1.0,
        "repeated_failure": 1.0,
        "failure_recovery": 1.0,
        "final_verification": 1.0,
        "loop_detection": 1.0,
        "execution_efficiency": 1.0,
    }


class ProcessEvaluatorConfig(BaseModel):
    """Versionable rule and optional judge settings, overridable through metadata."""

    argument_similarity_threshold: float = Field(default=0.96, ge=0.0, le=1.0)
    repeated_call_min_occurrences: int = Field(default=2, ge=2)
    repeated_failure_min_occurrences: int = Field(default=3, ge=2)
    loop_min_repeats: int = Field(default=3, ge=2)
    loop_max_length: int = Field(default=4, ge=1)
    failed_tool_ratio_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    failed_tool_ratio_min_calls: int = Field(default=4, ge=1)
    baseline_multiplier: float = Field(default=2.0, gt=1.0)
    requires_verification: bool | None = None
    mutation_tool_patterns: list[str] = Field(
        default_factory=lambda: [
            "write",
            "edit",
            "patch",
            "delete",
            "remove",
            "move",
            "copy",
            "create",
            "apply",
        ]
    )
    verification_tool_patterns: list[str] = Field(
        default_factory=lambda: [
            "test",
            "pytest",
            "unittest",
            "lint",
            "ruff",
            "mypy",
            "build",
            "check",
            "verify",
        ]
    )
    verification_command_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?:^|\s)(?:pytest|ruff|mypy|tox|nox)(?:\s|$)",
            r"(?:^|\s)(?:npm|pnpm|yarn)\s+(?:test|run\s+(?:test|lint|build|check))\b",
            r"(?:^|\s)(?:cargo|go)\s+(?:test|check|build)\b",
            r"(?:^|\s)git\s+(?:diff|status)\b",
            r"(?:^|\s)python(?:3)?\s+-m\s+(?:pytest|unittest|compileall)\b",
        ]
    )
    mutation_command_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?:^|[;&|]\s*)(?:rm|mv|cp|mkdir|touch|install)\b",
            r"(?:^|\s)(?:sed|perl)\s+[^;&|]*\s-i(?:\s|$)",
            r"(?:^|\s)git\s+(?:apply|commit|merge|rebase|cherry-pick)\b",
        ]
    )
    environment_query_tool_patterns: list[str] = Field(
        default_factory=lambda: ["read", "list", "search", "find", "status", "inspect"]
    )
    diagnostic_command_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?:^|[;&|]\s*)(?:cat|head|tail|ls|find|grep|rg|pwd|stat|which|env)\b",
            r"(?:^|\s)git\s+(?:status|diff|log|show)\b",
        ]
    )
    failure_recovery_llm_enabled: bool = True
    failure_recovery_llm_max_prompt_chars: int = Field(
        default=16_000,
        ge=2_000,
        le=100_000,
    )
    efficiency_thresholds: dict[str, float] = Field(default_factory=dict)
    baseline_metrics: dict[str, float] = Field(default_factory=dict)
    grader_weights: dict[str, float] = Field(default_factory=_default_weights)


@dataclass(frozen=True)
class _Action:
    step: ExecutionStep
    tool_name: str
    arguments: str


@dataclass(frozen=True)
class EvaluationContext:
    record: ExecutionRecord
    config: ProcessEvaluatorConfig
    steps: tuple[ExecutionStep, ...]
    actions: tuple[_Action, ...]


@dataclass(frozen=True)
class _EpisodeEvent:
    step: ExecutionStep
    diagnostic: bool = False
    mutation: bool = False
    related_retry: bool = False
    same_tool: bool = False
    same_target: bool = False
    adjustments: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureEpisode:
    """Rule-extracted trace slice for one failed tool/model/runtime step."""

    failure_step: ExecutionStep
    events: tuple[_EpisodeEvent, ...]
    rule_recovery_step: ExecutionStep | None = None


class _FailureEpisodeJudgment(BaseModel):
    failure_step_id: str
    effective_diagnosis: bool
    adjustment_targets_failure: bool
    target_recovered: bool
    recovery_step_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str


class _FailureRecoveryJudgments(BaseModel):
    episodes: list[_FailureEpisodeJudgment]


class ProcessGrader(Protocol):
    """Small extension seam for deterministic or future attribution graders."""

    grader_name: str
    grader_version: str
    category: str

    def grade(self, context: EvaluationContext) -> ProcessGrade: ...


class RepeatedToolCallGrader:
    grader_name = "repeated_tool_call"
    grader_version = GRADER_VERSION
    category = "redundancy"

    def grade(self, context: EvaluationContext) -> ProcessGrade:
        repeated = _repeated_call_occurrences(context)
        if not repeated:
            return _pass_grade(
                self,
                "No unexplained repeated tool calls were found.",
                [f"Compared {len(context.actions)} tool calls in sequence order."],
                metadata={"repeated_call_count": 0},
            )
        affected = _unique(step_id for item in repeated for step_id in item["step_ids"])
        ratio = min(len(repeated) / max(len(context.actions), 1), 1.0)
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=round(max(0.0, 1.0 - ratio), 4),
            status="warning",
            severity="medium",
            category=self.category,
            message=f"Found {len(repeated)} repeated tool call(s) without an observed state change.",
            evidence=[item["evidence"] for item in repeated],
            affected_steps=affected,
            metadata={
                "repeated_call_count": len(repeated),
                "occurrences": repeated,
                "similarity_threshold": context.config.argument_similarity_threshold,
            },
        )


class RepeatedFailureGrader:
    grader_name = "repeated_failure"
    grader_version = GRADER_VERSION
    category = "failure_handling"

    def grade(self, context: EvaluationContext) -> ProcessGrade:
        runs: list[list[_Action]] = []
        current: list[_Action] = []
        for action in context.actions:
            if action.step.success is not False:
                current = []
                continue
            if current and not _same_action(current[-1], action, context.config):
                current = []
            current.append(action)
            if len(current) == context.config.repeated_failure_min_occurrences:
                runs.append(list(current))
            elif len(current) > context.config.repeated_failure_min_occurrences:
                runs[-1].append(action)
        if not runs:
            return _pass_grade(
                self,
                "No unchanged operation exceeded the repeated-failure threshold.",
                [
                    (
                        "Failed tool calls were checked as an ordered stream; a changed "
                        "tool or changed arguments resets the run."
                    )
                ],
                metadata={"repeated_failure_count": 0},
            )
        affected = _unique(action.step.step_id for run in runs for action in run)
        evidence = [
            (
                f"{run[0].tool_name} failed {len(run)} times with unchanged/highly "
                f"similar arguments at steps {_sequence_range(run)}."
            )
            for run in runs
        ]
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=0.0,
            status="fail",
            severity="high",
            category=self.category,
            message="The agent repeated failed operations without an observable strategy change.",
            evidence=evidence,
            affected_steps=affected,
            metadata={
                "repeated_failure_count": sum(len(run) - 1 for run in runs),
                "runs": [
                    {
                        "tool": run[0].tool_name,
                        "step_ids": [item.step.step_id for item in run],
                        "sequences": [item.step.sequence for item in run],
                        "attempt_count": len(run),
                    }
                    for run in runs
                ],
                "minimum_occurrences": context.config.repeated_failure_min_occurrences,
            },
        )


class FailureRecoveryGrader:
    grader_name = "failure_recovery"
    grader_version = FAILURE_RECOVERY_GRADER_VERSION
    category = "failure_handling"

    def grade(self, context: EvaluationContext) -> ProcessGrade:
        episodes = _extract_failure_episodes(context)
        if not episodes:
            return _pass_grade(
                self,
                "No process failure required recovery.",
                ["No failed tool, model, or runtime step was recorded."],
                metadata={
                    "failure_count": 0,
                    "recovered_count": 0,
                    "episodes": [],
                    "evaluation_mode": "rules",
                },
            )

        episode_data = [_episode_metadata(episode) for episode in episodes]
        recovered = [episode for episode in episodes if episode.rule_recovery_step]
        adjusted = [episode for episode in recovered if _episode_adjustments(episode)]
        affected = _unique(
            step.step_id
            for episode in episodes
            for step in [episode.failure_step, episode.rule_recovery_step]
            if step is not None
        )
        status: ProcessStatus
        severity: ProcessSeverity
        if len(recovered) < len(episodes):
            status = "fail"
            severity = "high"
            message = (
                f"{len(episodes) - len(recovered)} of {len(episodes)} failure episode(s) "
                "had no rule-confirmed recovery of the original target."
            )
        elif len(adjusted) < len(recovered):
            status = "warning"
            severity = "low"
            message = (
                "Failures recovered, but some related retries showed no observable "
                "diagnosis or strategy adjustment."
            )
        else:
            status = "pass"
            severity = "info"
            message = (
                "Every failure was followed by an adjusted, successful retry of the "
                "original target."
            )
        recovered_ratio = len(recovered) / len(episodes)
        adjustment_ratio = len(adjusted) / len(episodes)
        score = 0.75 * recovered_ratio + 0.25 * adjustment_ratio
        assessments = [_episode_assessment(item) for item in episode_data]
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=round(score, 4),
            status=status,
            severity=severity,
            category=self.category,
            message=message,
            evidence=[str(item["evidence"]) for item in assessments],
            affected_steps=affected,
            metadata={
                "failure_count": len(assessments),
                "recovered_count": len(recovered),
                "adjusted_recovery_count": len(adjusted),
                "assessments": assessments,
                "episodes": episode_data,
                "evaluation_mode": "rules",
            },
        )


class FailureRecoveryLLMGrader:
    """Best-effort semantic refinement of rule-extracted failure episodes."""

    grader_name = "failure_recovery"
    grader_version = FAILURE_RECOVERY_GRADER_VERSION
    judge_version = FAILURE_RECOVERY_LLM_GRADER_VERSION
    category = "failure_handling"

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def grade(
        self,
        context: EvaluationContext,
        fallback: ProcessGrade | None = None,
    ) -> ProcessGrade:
        rule_grade = fallback or FailureRecoveryGrader().grade(context)
        episodes = _extract_failure_episodes(context)
        if not episodes or not context.config.failure_recovery_llm_enabled:
            return rule_grade

        judgments, error, prompt_truncated = _judge_failure_episodes(
            self.provider,
            context,
            episodes,
        )
        if judgments is None:
            metadata = dict(rule_grade.metadata)
            metadata["llm_judge"] = {
                "status": "fallback",
                "judge_version": self.judge_version,
                "provider": getattr(self.provider, "name", "unknown"),
                "model": getattr(self.provider, "model", None),
                "reason": error or "invalid_response",
                "prompt_truncated": prompt_truncated,
            }
            return rule_grade.model_copy(update={"metadata": metadata})

        verdicts = judgments.episodes
        recovered = sum(item.target_recovered for item in verdicts)
        strong = sum(
            item.target_recovered
            and item.effective_diagnosis
            and item.adjustment_targets_failure
            for item in verdicts
        )
        if recovered < len(verdicts):
            status: ProcessStatus = "fail"
            severity: ProcessSeverity = "high"
            message = (
                f"LLM Judge found {len(verdicts) - recovered} of {len(verdicts)} "
                "failure target(s) were not actually recovered."
            )
        elif strong < len(verdicts):
            status = "warning"
            severity = "low"
            message = (
                "LLM Judge confirmed recovery, but diagnosis or targeted adjustment "
                "was weak in at least one episode."
            )
        else:
            status = "pass"
            severity = "info"
            message = "LLM Judge confirmed effective diagnosis, targeted adjustment, and recovery."

        score = sum(
            0.25 * item.effective_diagnosis
            + 0.25 * item.adjustment_targets_failure
            + 0.5 * item.target_recovered
            for item in verdicts
        ) / len(verdicts)
        known_steps = {step.step_id for step in context.steps}
        affected = _unique(
            step_id
            for item in verdicts
            for step_id in [item.failure_step_id, item.recovery_step_id]
            if step_id in known_steps
        )
        metadata = dict(rule_grade.metadata)
        metadata.update(
            {
                "evaluation_mode": "rules+llm",
                "rule_result": {
                    "score": rule_grade.score,
                    "status": rule_grade.status,
                    "message": rule_grade.message,
                },
                "llm_judge": {
                    "status": "applied",
                    "judge_version": self.judge_version,
                    "provider": getattr(self.provider, "name", "unknown"),
                    "model": getattr(self.provider, "model", None),
                    "prompt_truncated": prompt_truncated,
                    "judgments": [item.model_dump(mode="json") for item in verdicts],
                },
            }
        )
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=round(score, 4),
            status=status,
            severity=severity,
            category=self.category,
            message=message,
            evidence=[f"{item.failure_step_id}: {item.reason}" for item in verdicts],
            affected_steps=affected,
            metadata=metadata,
        )


class FinalVerificationGrader:
    grader_name = "final_verification"
    grader_version = GRADER_VERSION
    category = "verification"

    def grade(self, context: EvaluationContext) -> ProcessGrade:
        config = context.config
        if config.requires_verification is False:
            return _pass_grade(
                self,
                "Final verification was explicitly disabled for this task.",
                [
                    "record.metadata process-evaluation configuration sets requires_verification=false."
                ],
                metadata={"required": False, "source": "task_metadata"},
            )

        candidates = [
            action for action in context.actions if _is_mutation(action, config)
        ]
        successful = [action for action in candidates if action.step.success is True]
        unknown = [action for action in candidates if action.step.success is None]
        required = config.requires_verification is True or bool(successful)
        if unknown and not successful:
            return _insufficient_grade(
                self,
                "Mutation-like steps exist, but their success fields are missing.",
                [
                    f"Cannot establish whether steps {_sequence_range(unknown)} changed state."
                ],
                [action.step.step_id for action in unknown],
                metadata={"required": None, "missing_field": "steps[].success"},
            )
        if config.requires_verification is True and not context.steps:
            return _insufficient_grade(
                self,
                "Final verification is required, but the record contains no steps.",
                [
                    "Cannot locate a modification or verification step in an empty trace."
                ],
                [],
                metadata={"required": True, "missing_field": "steps"},
            )
        if not required:
            return _pass_grade(
                self,
                "No material state modification requiring final verification was observed.",
                [
                    "The default mutation heuristic found no successful mutation tool or command."
                ],
                metadata={"required": False, "source": "default_heuristic"},
            )

        last_mutation_sequence = max(
            (action.step.sequence for action in successful),
            default=-1,
        )
        following = [
            action
            for action in context.actions
            if action.step.sequence > last_mutation_sequence
            and _is_verification(action, config)
        ]
        passed = [action for action in following if action.step.success is True]
        affected = [action.step.step_id for action in successful]
        if passed:
            verification = passed[-1]
            return ProcessGrade(
                grader_name=self.grader_name,
                grader_version=self.grader_version,
                score=1.0,
                status="pass",
                severity="info",
                category=self.category,
                message="A successful final verification followed the last material modification.",
                evidence=[
                    (
                        f"Last mutation was sequence {last_mutation_sequence}; successful "
                        f"verification was step {verification.step.step_id} "
                        f"(sequence {verification.step.sequence})."
                    )
                ],
                affected_steps=_unique([*affected, verification.step.step_id]),
                metadata={
                    "required": True,
                    "last_mutation_sequence": last_mutation_sequence,
                    "verification_step": verification.step.step_id,
                    "source": "task_metadata"
                    if config.requires_verification
                    else "default_heuristic",
                },
            )
        failed_verifications = [
            action for action in following if action.step.success is False
        ]
        if not affected and not failed_verifications:
            affected = [step.step_id for step in context.steps]
        message = (
            "Final verification ran after the last modification but did not succeed."
            if failed_verifications
            else "Missing final verification after a material state modification."
        )
        evidence = [
            (
                f"Last successful mutation was sequence {last_mutation_sequence}; no later "
                "successful verification tool or command was recorded."
            )
        ]
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=0.0,
            status="fail",
            severity="high" if failed_verifications else "medium",
            category=self.category,
            message=message,
            evidence=evidence,
            affected_steps=_unique(
                [*affected, *(item.step.step_id for item in failed_verifications)]
            ),
            metadata={
                "required": True,
                "last_mutation_sequence": last_mutation_sequence,
                "failed_verification_steps": [
                    action.step.step_id for action in failed_verifications
                ],
                "source": "task_metadata"
                if config.requires_verification
                else "default_heuristic",
            },
        )


class LoopDetectionGrader:
    grader_name = "loop_detection"
    grader_version = GRADER_VERSION
    category = "loop"

    def grade(self, context: EvaluationContext) -> ProcessGrade:
        loop = _find_loop(context.actions, context.config)
        if loop is None:
            return _pass_grade(
                self,
                "No simple or periodic tool-call loop was detected.",
                [
                    (
                        f"Checked {len(context.actions)} tool calls for contiguous periods "
                        f"up to {context.config.loop_max_length}."
                    )
                ],
                metadata={"loop_detected": False},
            )
        actions = context.actions[loop["start_index"] : loop["end_index"]]
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=0.0,
            status="fail",
            severity="high",
            category=self.category,
            message=(
                f"Detected a length-{loop['loop_length']} tool-call loop repeated "
                f"{loop['repeat_count']} times."
            ),
            evidence=[
                f"Contiguous repeated pattern spans steps {_sequence_range(actions)}."
            ],
            affected_steps=[action.step.step_id for action in actions],
            metadata={
                "loop_detected": True,
                "loop_start_step": actions[0].step.step_id,
                "loop_start_sequence": actions[0].step.sequence,
                "loop_length": loop["loop_length"],
                "repeat_count": loop["repeat_count"],
                "pattern": [
                    action.tool_name for action in actions[: int(loop["loop_length"])]
                ],
            },
        )


class ExecutionEfficiencyGrader:
    grader_name = "execution_efficiency"
    grader_version = GRADER_VERSION
    category = "efficiency"

    def grade(self, context: EvaluationContext) -> ProcessGrade:
        metrics = _efficiency_metrics(context)
        reasons: list[str] = []
        repeat_count = int(metrics["repeated_call_count"] or 0)
        tool_count = int(metrics["tool_call_count"] or 0)
        failed_count = int(metrics["failed_tool_call_count"] or 0)
        failed_ratio = failed_count / tool_count if tool_count else 0.0
        if repeat_count:
            reasons.append(f"{repeat_count} unexplained repeated tool call(s)")
        if (
            tool_count >= context.config.failed_tool_ratio_min_calls
            and failed_ratio >= context.config.failed_tool_ratio_threshold
        ):
            reasons.append(f"failed-tool-call ratio is {failed_ratio:.0%}")

        for metric, threshold in context.config.efficiency_thresholds.items():
            value = metrics.get(metric)
            if isinstance(value, (int, float)) and value > threshold:
                reasons.append(
                    f"{metric}={value:g} exceeds configured threshold {threshold:g}"
                )
        for metric, baseline in context.config.baseline_metrics.items():
            value = metrics.get(metric)
            limit = baseline * context.config.baseline_multiplier
            if baseline > 0 and isinstance(value, (int, float)) and value > limit:
                reasons.append(
                    f"{metric}={value:g} exceeds {context.config.baseline_multiplier:g}x "
                    f"baseline ({baseline:g})"
                )

        if not reasons:
            return _pass_grade(
                self,
                "No deterministic inefficiency signal exceeded the configured criteria.",
                [
                    (
                        "No default absolute step/token/cost/latency limit was applied "
                        "without a configured threshold or baseline."
                    )
                ],
                metadata={"metrics": metrics, "signals": []},
            )
        repeat_ratio = repeat_count / max(tool_count, 1)
        penalty = min(0.75, max(repeat_ratio, failed_ratio) + 0.1 * (len(reasons) - 1))
        affected = _unique(
            step_id
            for item in _repeated_call_occurrences(context)
            for step_id in item["step_ids"]
        )
        if failed_ratio >= context.config.failed_tool_ratio_threshold:
            affected = _unique(
                [
                    *affected,
                    *(
                        action.step.step_id
                        for action in context.actions
                        if action.step.success is False
                    ),
                ]
            )
        if not affected:
            affected = [step.step_id for step in context.steps]
        return ProcessGrade(
            grader_name=self.grader_name,
            grader_version=self.grader_version,
            score=round(max(0.0, 1.0 - penalty), 4),
            status="warning",
            severity="medium",
            category=self.category,
            message="Execution contains deterministic inefficiency signals.",
            evidence=reasons,
            affected_steps=affected,
            metadata={"metrics": metrics, "signals": reasons},
        )


DEFAULT_GRADERS: tuple[ProcessGrader, ...] = (
    RepeatedToolCallGrader(),
    RepeatedFailureGrader(),
    FailureRecoveryGrader(),
    FinalVerificationGrader(),
    LoopDetectionGrader(),
    ExecutionEfficiencyGrader(),
)


class ProcessEvaluator:
    """Run independent graders and atomically replace the record's derived result."""

    def __init__(
        self,
        graders: Sequence[ProcessGrader] = DEFAULT_GRADERS,
        config: ProcessEvaluatorConfig | None = None,
        failure_recovery_judge: ModelProvider | None = None,
    ) -> None:
        self.graders = tuple(graders)
        self.config = config or ProcessEvaluatorConfig()
        self.failure_recovery_judge = (
            FailureRecoveryLLMGrader(failure_recovery_judge)
            if failure_recovery_judge is not None
            else None
        )

    def evaluate(self, record: ExecutionRecord) -> ProcessEvaluationResult:
        config = _config_for_record(self.config, record)
        steps = tuple(sorted(record.steps, key=lambda step: step.sequence))
        context = EvaluationContext(
            record=record,
            config=config,
            steps=steps,
            actions=tuple(_action(step) for step in steps if step.type == "tool"),
        )
        grades = [grader.grade(context) for grader in self.graders]
        if self.failure_recovery_judge is not None:
            for index, grade in enumerate(grades):
                if grade.grader_name == "failure_recovery":
                    grades[index] = self.failure_recovery_judge.grade(context, grade)
                    break
        weights = {
            grade.grader_name: max(
                config.grader_weights.get(grade.grader_name, 1.0), 0.0
            )
            for grade in grades
        }
        scored = [
            (grade.score, weights[grade.grader_name])
            for grade in grades
            if grade.score is not None and weights[grade.grader_name] > 0
        ]
        denominator = sum(weight for _, weight in scored)
        overall_score = (
            round(
                sum(float(score) * weight for score, weight in scored) / denominator, 4
            )
            if denominator
            else None
        )
        status = _overall_status(grades)
        issue_count = sum(grade.status in {"warning", "fail"} for grade in grades)
        unavailable_count = sum(grade.status == "insufficient_data" for grade in grades)
        summary = f"{issue_count} process issue(s); {unavailable_count} grader(s) lacked sufficient data."
        result = ProcessEvaluationResult(
            evaluated_at=datetime.now(UTC),
            evaluator_version=EVALUATOR_VERSION,
            overall_score=overall_score,
            status=status,
            summary=summary,
            grades=grades,
            metadata={
                "weights": weights,
                "config": config.model_dump(mode="json"),
                "outcome_success": record.execution.success,
                "harbor_passed": record.evaluation.passed,
                "failure_recovery_judge_configured": (
                    self.failure_recovery_judge is not None
                ),
            },
        )
        record.quality.process_evaluation = result
        return result


def _config_for_record(
    base: ProcessEvaluatorConfig,
    record: ExecutionRecord,
) -> ProcessEvaluatorConfig:
    raw = record.metadata.get("process_evaluation_config")
    if raw is None:
        raw = record.metadata.get("process_evaluation")
    if not isinstance(raw, Mapping):
        return base.model_copy(deep=True)
    merged = base.model_dump()
    for key, value in raw.items():
        if key in ProcessEvaluatorConfig.model_fields:
            if key in {"grader_weights", "efficiency_thresholds", "baseline_metrics"}:
                current = merged.get(key)
                if isinstance(current, dict) and isinstance(value, Mapping):
                    merged[key] = {**current, **dict(value)}
                    continue
            merged[key] = value
    return ProcessEvaluatorConfig.model_validate(merged)


def _action(step: ExecutionStep) -> _Action:
    tool_name = step.metadata.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        tool_name = step.name.removeprefix("Tool Call:").strip()
    return _Action(
        step=step,
        tool_name=tool_name.casefold(),
        arguments=_canonical(step.input),
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return repr(value)


def _same_action(
    left: _Action,
    right: _Action,
    config: ProcessEvaluatorConfig,
) -> bool:
    if left.tool_name != right.tool_name:
        return False
    if left.arguments == right.arguments:
        return True
    return (
        SequenceMatcher(None, left.arguments, right.arguments, autojunk=False).ratio()
        >= config.argument_similarity_threshold
    )


def _argument_similarity(left: _Action, right: _Action) -> float:
    if left.arguments == right.arguments:
        return 1.0
    return SequenceMatcher(
        None, left.arguments, right.arguments, autojunk=False
    ).ratio()


def _repeated_call_occurrences(context: EvaluationContext) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    previous_by_tool: dict[str, tuple[int, _Action]] = {}
    run_length_by_tool: dict[str, int] = {}
    for index, action in enumerate(context.actions):
        previous_item = previous_by_tool.get(action.tool_name)
        previous_by_tool[action.tool_name] = (index, action)
        if previous_item is None:
            run_length_by_tool[action.tool_name] = 1
            continue
        previous_index, previous = previous_item
        if not _same_action(previous, action, context.config):
            run_length_by_tool[action.tool_name] = 1
            continue
        if previous.step.success is not True or action.step.success is not True:
            run_length_by_tool[action.tool_name] = 1
            continue
        if previous.step.output is None or action.step.output is None:
            run_length_by_tool[action.tool_name] = 1
            continue
        if _canonical(previous.step.output) != _canonical(action.step.output):
            run_length_by_tool[action.tool_name] = 1
            continue
        between = context.actions[previous_index + 1 : index]
        if any(
            _is_mutation(item, context.config) and item.step.success is not False
            for item in between
        ):
            run_length_by_tool[action.tool_name] = 1
            continue
        run_length = run_length_by_tool.get(action.tool_name, 1) + 1
        run_length_by_tool[action.tool_name] = run_length
        if run_length < context.config.repeated_call_min_occurrences:
            continue
        similarity = _argument_similarity(previous, action)
        occurrences.append(
            {
                "tool": action.tool_name,
                "step_ids": [previous.step.step_id, action.step.step_id],
                "sequences": [previous.step.sequence, action.step.sequence],
                "argument_similarity": round(similarity, 4),
                "occurrence_count": run_length,
                "evidence": (
                    f"{action.tool_name} repeated at sequences {previous.step.sequence} and "
                    f"{action.step.sequence} ({similarity:.0%} argument similarity and "
                    "identical result) with no mutation between them."
                ),
            }
        )
    return occurrences


def _failure_steps(context: EvaluationContext) -> list[ExecutionStep]:
    failures = [
        step
        for step in context.steps
        if step.type in {"tool", "model"} and step.success is False
    ]
    if context.record.execution.success is False and not failures:
        return [
            step
            for step in context.steps
            if step.type in {"agent", "final"} and step.success is False
        ][:1]
    return failures


def _extract_failure_episodes(context: EvaluationContext) -> list[FailureEpisode]:
    episodes: list[FailureEpisode] = []
    for failure in _failure_steps(context):
        failed_action = _action(failure) if failure.type == "tool" else None
        events: list[_EpisodeEvent] = []
        recovery: ExecutionStep | None = None
        for step in context.steps:
            if step.sequence <= failure.sequence:
                continue
            event = _episode_event(step, failed_action, failure.type, context.config)
            events.append(event)
            if _is_rule_recovery(event, failure.type):
                recovery = step
                break
        episodes.append(
            FailureEpisode(
                failure_step=failure,
                events=tuple(events),
                rule_recovery_step=recovery,
            )
        )
    return episodes


def _episode_event(
    step: ExecutionStep,
    failed_action: _Action | None,
    failure_type: str,
    config: ProcessEvaluatorConfig,
) -> _EpisodeEvent:
    if failure_type == "model":
        related = step.type == "model"
        return _EpisodeEvent(
            step=step,
            related_retry=related,
            same_tool=related,
            same_target=related,
            adjustments=("retried_model_call",) if related else (),
        )
    if failed_action is None or step.type != "tool":
        return _EpisodeEvent(step=step)

    action = _action(step)
    diagnostic = _is_diagnostic(action, config)
    mutation = _is_mutation(action, config)
    same_tool = action.tool_name == failed_action.tool_name
    same_target = _same_target(failed_action, action, config)
    related_retry = same_tool or same_target
    changes: list[str] = []
    if not same_tool:
        changes.append("changed_tool")
    elif action.arguments != failed_action.arguments:
        changes.append("changed_arguments")
    failed_path = _path_value(failed_action.step.input)
    candidate_path = _path_value(action.step.input)
    if (
        failed_path is not None
        and candidate_path is not None
        and failed_path != candidate_path
    ):
        changes.append("changed_execution_path")
    if diagnostic:
        changes.append("queried_environment")
    if mutation:
        changes.append("mutation")
    return _EpisodeEvent(
        step=step,
        diagnostic=diagnostic,
        mutation=mutation,
        related_retry=related_retry,
        same_tool=same_tool,
        same_target=same_target,
        adjustments=tuple(_unique(changes)),
    )


def _is_rule_recovery(event: _EpisodeEvent, failure_type: str) -> bool:
    if event.step.success is not True:
        return False
    if failure_type == "model":
        return event.related_retry
    return (
        event.related_retry
        and event.same_tool
        and event.same_target
        and not event.diagnostic
    )


def _episode_adjustments(episode: FailureEpisode) -> list[str]:
    return _unique(
        adjustment
        for event in episode.events
        if event.diagnostic or event.mutation or event.related_retry
        for adjustment in event.adjustments
    )


def _episode_step_data(step: ExecutionStep) -> dict[str, Any]:
    data: dict[str, Any] = {
        "step_id": step.step_id,
        "sequence": step.sequence,
        "type": step.type,
        "name": step.name,
        "success": step.success,
        "input": step.input,
        "output": step.output,
        "error": step.error,
    }
    if step.type == "tool":
        data["tool_name"] = _action(step).tool_name
    return data


def _episode_metadata(episode: FailureEpisode) -> dict[str, Any]:
    timeline = [
        {
            **_episode_step_data(event.step),
            "roles": [
                role
                for role, enabled in (
                    ("diagnostic", event.diagnostic),
                    ("mutation", event.mutation),
                    ("related_retry", event.related_retry),
                )
                if enabled
            ],
            "same_tool": event.same_tool,
            "same_target": event.same_target,
            "adjustments": list(event.adjustments),
        }
        for event in episode.events
    ]
    return {
        "failure": _episode_step_data(episode.failure_step),
        "timeline": timeline,
        "diagnostic_steps": [
            event.step.step_id for event in episode.events if event.diagnostic
        ],
        "mutation_steps": [
            event.step.step_id for event in episode.events if event.mutation
        ],
        "related_retries": [
            {
                "step_id": event.step.step_id,
                "success": event.step.success,
                "diagnostic_only": event.diagnostic,
                "same_tool": event.same_tool,
                "same_target": event.same_target,
            }
            for event in episode.events
            if event.related_retry
        ],
        "adjustments": [
            {"step_id": event.step.step_id, "changes": list(event.adjustments)}
            for event in episode.events
            if event.adjustments
        ],
        "rule_recovered": episode.rule_recovery_step is not None,
        "rule_recovery_step": (
            episode.rule_recovery_step.step_id if episode.rule_recovery_step else None
        ),
    }


def _episode_assessment(episode: Mapping[str, Any]) -> dict[str, Any]:
    failure = episode["failure"]
    assert isinstance(failure, Mapping)
    recovered = bool(episode["rule_recovered"])
    recovery_step = episode["rule_recovery_step"]
    adjustments = _unique(
        str(change)
        for item in episode["adjustments"]
        if isinstance(item, Mapping)
        for change in item.get("changes", [])
    )
    if recovered:
        evidence = (
            f"Failure at sequence {failure['sequence']} recovered at step "
            f"{recovery_step} through a successful, related non-diagnostic retry."
        )
    else:
        evidence = (
            f"Failure at sequence {failure['sequence']} has no successful "
            "non-diagnostic retry that rules can tie to the original target."
        )
    return {
        "failure_step": failure["step_id"],
        "failure_sequence": failure["sequence"],
        "failure_type": failure["type"],
        "recovered": recovered,
        "recovery_step": recovery_step,
        "adjustments": adjustments,
        "evidence": evidence,
    }


_FAILURE_RECOVERY_JUDGE_SYSTEM_PROMPT = """You are a conservative process-evaluation judge.
Assess each supplied failure episode using only its trace evidence. A successful diagnostic
read/search/find/status/inspect action is not recovery. A successful mutation or unrelated
action is not recovery by itself. Runtime success is not proof that the original failed target
recovered. Decide whether diagnosis was effective, whether the adjustment addressed the failed
target, and whether that same target was demonstrably recovered. Return one JSON object only:
{"episodes":[{"failure_step_id":"...","effective_diagnosis":true,
"adjustment_targets_failure":true,"target_recovered":true,
"recovery_step_id":"... or null","confidence":0.0,"reason":"brief evidence"}]}
Use exactly the supplied failure step IDs. If target_recovered is true, recovery_step_id must be
a later step from that episode that demonstrates recovery. Do not use markdown.
"""


def _judge_failure_episodes(
    provider: ModelProvider,
    context: EvaluationContext,
    episodes: Sequence[FailureEpisode],
) -> tuple[_FailureRecoveryJudgments | None, str | None, bool]:
    payload = {
        "task_name": context.record.identity.task_name,
        "runtime_success": context.record.execution.success,
        "episodes": [_episode_prompt_data(episode) for episode in episodes],
    }
    payload = sanitize(
        payload,
        max_string_length=600,
        max_collection_items=200,
        max_depth=12,
    )
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    limit = context.config.failure_recovery_llm_max_prompt_chars
    prompt_truncated = len(serialized) > limit
    if prompt_truncated:
        serialized = serialized[:limit] + "\n[TRACE PAYLOAD TRUNCATED]"
    messages = [
        Message(role=Role.SYSTEM, content=_FAILURE_RECOVERY_JUDGE_SYSTEM_PROMPT),
        Message(role=Role.USER, content=serialized),
    ]
    try:
        response = collect_response(provider.stream(messages, tools=None))
        raw = _extract_json_object(response.content)
        if raw is None:
            return None, "invalid_json_response", prompt_truncated
        judgments = _FailureRecoveryJudgments.model_validate(raw)
        error = _validate_failure_judgments(judgments, episodes)
        if error:
            return None, error, prompt_truncated
        return judgments, None, prompt_truncated
    except Exception as exc:
        logger.debug("failure-recovery LLM judge failed", exc_info=True)
        return None, f"{type(exc).__name__}", prompt_truncated


def _episode_prompt_data(episode: FailureEpisode) -> dict[str, Any]:
    metadata = _episode_metadata(episode)
    failure = dict(metadata["failure"])
    failure["input"] = _value_preview(failure.get("input"))
    failure["output"] = _value_preview(failure.get("output"))
    timeline: list[dict[str, Any]] = []
    for raw in metadata["timeline"]:
        item = dict(raw)
        item["input"] = _value_preview(item.get("input"))
        item["output"] = _value_preview(item.get("output"))
        timeline.append(item)
    return {**metadata, "failure": failure, "timeline": timeline}


def _value_preview(value: Any, limit: int = 600) -> str | None:
    if value is None:
        return None
    rendered = _canonical(value)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None


def _validate_failure_judgments(
    judgments: _FailureRecoveryJudgments,
    episodes: Sequence[FailureEpisode],
) -> str | None:
    expected = [episode.failure_step.step_id for episode in episodes]
    actual = [item.failure_step_id for item in judgments.episodes]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        return "episode_id_mismatch"
    events_by_failure = {
        episode.failure_step.step_id: {event.step.step_id for event in episode.events}
        for episode in episodes
    }
    for item in judgments.episodes:
        if item.target_recovered and (
            item.recovery_step_id is None
            or item.recovery_step_id not in events_by_failure[item.failure_step_id]
        ):
            return "ungrounded_recovery_step"
        if (
            item.recovery_step_id is not None
            and item.recovery_step_id not in events_by_failure[item.failure_step_id]
        ):
            return "unknown_recovery_step"
    return None


def _path_value(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("path", "file", "workdir", "cwd", "directory"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _target_values(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    targets: set[str] = set()
    for key in (
        "path",
        "file",
        "workdir",
        "cwd",
        "directory",
        "target",
        "url",
        "repository",
    ):
        candidate = value.get(key)
        if candidate is not None:
            targets.add(_canonical(candidate))
    return targets


def _same_target(
    failed: _Action,
    candidate: _Action,
    config: ProcessEvaluatorConfig,
) -> bool:
    failed_targets = _target_values(failed.step.input)
    candidate_targets = _target_values(candidate.step.input)
    if failed_targets or candidate_targets:
        return bool(failed_targets & candidate_targets)
    return _same_action(failed, candidate, config)


def _matches_name(name: str, patterns: Sequence[str]) -> bool:
    normalized = name.casefold().replace("-", "_")
    tokens = set(normalized.split("_"))
    return any(
        (candidate := pattern.casefold().replace("-", "_")) == normalized
        or candidate in tokens
        for pattern in patterns
    )


def _command(action: _Action) -> str:
    value = action.step.input
    if not isinstance(value, Mapping):
        return ""
    command = value.get("command") or value.get("cmd")
    return command if isinstance(command, str) else ""


def _is_diagnostic(action: _Action, config: ProcessEvaluatorConfig) -> bool:
    if _matches_name(action.tool_name, config.environment_query_tool_patterns):
        return True
    command = _command(action)
    return bool(command) and any(
        re.search(pattern, command, flags=re.IGNORECASE)
        for pattern in config.diagnostic_command_patterns
    )


def _is_mutation(action: _Action, config: ProcessEvaluatorConfig) -> bool:
    if _matches_name(action.tool_name, config.mutation_tool_patterns):
        return True
    command = _command(action)
    return bool(command) and any(
        re.search(pattern, command, flags=re.IGNORECASE)
        for pattern in config.mutation_command_patterns
    )


def _is_verification(action: _Action, config: ProcessEvaluatorConfig) -> bool:
    if _matches_name(action.tool_name, config.verification_tool_patterns):
        return True
    command = _command(action)
    return bool(command) and any(
        re.search(pattern, command, flags=re.IGNORECASE)
        for pattern in config.verification_command_patterns
    )


def _find_loop(
    actions: tuple[_Action, ...],
    config: ProcessEvaluatorConfig,
) -> dict[str, int] | None:
    signatures = [(item.tool_name, item.arguments) for item in actions]
    candidates: list[dict[str, int]] = []
    for start in range(len(signatures)):
        max_period = min(
            config.loop_max_length, (len(signatures) - start) // config.loop_min_repeats
        )
        for period in range(1, max_period + 1):
            pattern = signatures[start : start + period]
            repeats = 1
            cursor = start + period
            while signatures[cursor : cursor + period] == pattern:
                repeats += 1
                cursor += period
            if repeats >= config.loop_min_repeats:
                candidates.append(
                    {
                        "start_index": start,
                        "end_index": start + period * repeats,
                        "loop_length": period,
                        "repeat_count": repeats,
                    }
                )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item["end_index"] - item["start_index"],
            item["repeat_count"],
            -item["loop_length"],
            -item["start_index"],
        ),
    )


def _efficiency_metrics(context: EvaluationContext) -> dict[str, int | float | None]:
    usage = context.record.usage
    token_count: int | None = usage.total_tokens
    if token_count is None:
        input_tokens = usage.input_tokens_including_cache
        if input_tokens is None:
            input_parts = [
                usage.input_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            ]
            if any(value is not None for value in input_parts):
                input_tokens = sum(value or 0 for value in input_parts)
        if input_tokens is not None or usage.output_tokens is not None:
            token_count = (input_tokens or 0) + (usage.output_tokens or 0)
    return {
        "step_count": len(context.steps),
        "model_call_count": sum(step.type == "model" for step in context.steps),
        "tool_call_count": len(context.actions),
        "failed_tool_call_count": sum(
            action.step.success is False for action in context.actions
        ),
        "repeated_call_count": len(_repeated_call_occurrences(context)),
        "token_count": token_count,
        "cost": usage.cost,
        "latency_ms": context.record.execution.latency_ms,
    }


def _pass_grade(
    grader: ProcessGrader,
    message: str,
    evidence: list[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> ProcessGrade:
    return ProcessGrade(
        grader_name=grader.grader_name,
        grader_version=grader.grader_version,
        score=1.0,
        status="pass",
        severity="info",
        category=grader.category,
        message=message,
        evidence=evidence,
        metadata=metadata or {},
    )


def _insufficient_grade(
    grader: ProcessGrader,
    message: str,
    evidence: list[str],
    affected_steps: list[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> ProcessGrade:
    return ProcessGrade(
        grader_name=grader.grader_name,
        grader_version=grader.grader_version,
        score=None,
        status="insufficient_data",
        severity="low",
        category=grader.category,
        message=message,
        evidence=evidence,
        affected_steps=affected_steps,
        metadata=metadata or {},
    )


def _overall_status(grades: Sequence[ProcessGrade]) -> ProcessStatus:
    statuses = {grade.status for grade in grades}
    if "fail" in statuses:
        return "fail"
    if "warning" in statuses:
        return "warning"
    if "pass" in statuses:
        return "pass"
    return "insufficient_data"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _sequence_range(actions: Sequence[_Action]) -> str:
    sequences = [action.step.sequence for action in actions]
    if not sequences:
        return "none"
    if len(sequences) == 1:
        return str(sequences[0])
    return f"{sequences[0]}-{sequences[-1]}"


__all__ = [
    "DEFAULT_GRADERS",
    "EVALUATOR_VERSION",
    "EvaluationContext",
    "ExecutionEfficiencyGrader",
    "FailureEpisode",
    "FailureRecoveryGrader",
    "FailureRecoveryLLMGrader",
    "FinalVerificationGrader",
    "LoopDetectionGrader",
    "ProcessEvaluator",
    "ProcessEvaluatorConfig",
    "ProcessGrader",
    "RepeatedFailureGrader",
    "RepeatedToolCallGrader",
]
