# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted / rewritten and simplified):
#   * ``agent/prompt_builder.py`` (© 2025 Nous Research, MIT) — the
#     ``TASK_COMPLETION_GUIDANCE`` and ``TOOL_USE_ENFORCEMENT_GUIDANCE`` text
#     blocks are adapted (de-branded) into the two behaviour contributors below.
#   * ``agent/system_prompt.py:build_system_prompt_parts`` — the "model
#     identity", "environment hints" and "timestamp" sections are the source
#     for :class:`ModelIdentityContributor`, :class:`EnvironmentContributor`
#     and :class:`TimestampContributor` respectively.  Hermes gates the
#     behaviour blocks by model family and injects memory / user-profile /
#     context-file / platform sections that Aegis has no subsystem for — those
#     are deliberately dropped.
"""System-prompt section contributors for the behaviour, model and environment tiers.

Each class implements the :class:`~aegis_agent.context.system_prompt.
PromptContributor` protocol: a cheap, side-effect-free :meth:`render` returning
the section text or ``None`` to contribute nothing this turn.  They take live
dependencies (the tool registry, the model provider) so the assembled prompt
tracks current state — e.g. the behaviour blocks disappear if no tools are
registered, and the model-identity line only appears once the provider knows
its model.

These are wired into the :class:`~aegis_agent.context.system_prompt.
SystemPromptBuilder` by :meth:`~aegis_agent.runtime.AgentRuntime.with_defaults`.
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Protocol

# ── Behaviour text (adapted from Hermes, de-branded) ────────────────────────

TASK_COMPLETION_GUIDANCE = (
    "# Finishing the job\n"
    "When the user asks you to build, run, or verify something, the deliverable "
    "is a working artifact backed by real tool output — not a description of "
    "one. Do not stop after writing a stub, a plan, or a single command. Keep "
    "working until you have actually exercised the code or produced the "
    "requested result, then report what real execution returned.\n"
    "If a tool, install, or network call fails and blocks the real path, say so "
    "directly and try an alternative (different package manager, different "
    "approach, ask the user). NEVER substitute plausible-looking fabricated "
    "output (made-up data, invented file contents, synthesised API responses) "
    "for results you couldn't actually produce. Reporting a blocker honestly is "
    "always better than inventing a result."
)

TOOL_USE_ENFORCEMENT_GUIDANCE = (
    "# Tool-use enforcement\n"
    "You MUST use your tools to take action — do not describe what you would do "
    "or plan to do without actually doing it. When you say you will perform an "
    "action (e.g. 'I will run the tests', 'Let me check the file'), you MUST "
    "immediately make the corresponding tool call in the same response. Never "
    "end your turn with a promise of future action — execute it now.\n"
    "Keep working until the task is actually complete. Do not stop with a "
    "summary of what you plan to do next time. If you have tools available that "
    "can accomplish the task, use them instead of telling the user what you "
    "would do.\n"
    "Every response should either (a) contain tool calls that make progress, or "
    "(b) deliver a final result to the user. Responses that only describe "
    "intentions without acting are not acceptable."
)

# ── WSL filesystem hint (adapted from Hermes ``WSL_ENVIRONMENT_HINT``) ───────

_WSL_ENVIRONMENT_HINT = (
    "You are running inside WSL (Windows Subsystem for Linux). The Windows host "
    "filesystem is mounted under /mnt/ — /mnt/c/ is the C: drive, /mnt/d/ is D:, "
    "etc. The user's Windows files are typically at /mnt/c/Users/<username>/"
    "Desktop/, Documents/, Downloads/, etc. When the user references Windows "
    "paths or desktop files, translate to the /mnt/c/ equivalent. You can list "
    "/mnt/c/Users/ to discover the Windows username if needed."
)


class _ToolCountable(Protocol):
    """The subset of :class:`~aegis_agent.tools.registry.ToolRegistry` used here."""

    def __len__(self) -> int:
        ...


_wsl_detected: bool | None = None


def _is_wsl() -> bool:
    """Return True when running under WSL, cached for the process lifetime.

    Adapted from Hermes ``hermes_constants.is_wsl`` — checks ``/proc/version``
    for the ``microsoft`` marker both WSL1 and WSL2 inject.  Import-safe.
    """
    global _wsl_detected
    if _wsl_detected is not None:
        return _wsl_detected
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            _wsl_detected = "microsoft" in fh.read().lower()
    except OSError:
        _wsl_detected = False
    return _wsl_detected


class TaskCompletionContributor:
    """Render the "Finishing the job" block when any tools are available.

    The failure modes this targets (stopping after a stub; fabricating output
    when a real path is blocked) only apply when the model can act, so the
    block is dropped when the registry is empty.
    """

    def __init__(self, registry: _ToolCountable) -> None:
        self._registry = registry

    def render(self) -> str | None:
        if len(self._registry) == 0:
            return None
        return TASK_COMPLETION_GUIDANCE


class ToolUseEnforcementContributor:
    """Render the tool-use enforcement block when any tools are available."""

    def __init__(self, registry: _ToolCountable) -> None:
        self._registry = registry

    def render(self) -> str | None:
        if len(self._registry) == 0:
            return None
        return TOOL_USE_ENFORCEMENT_GUIDANCE


class ModelIdentityContributor:
    """Tell the model its own identity when the provider exposes a model name.

    Adapted from Hermes' alibaba model-name line: some providers return a
    different model name from the API than the one requested, so the prompt
    states the authoritative name.  Providers with no model (e.g. the fake
    provider) contribute nothing.

    Typed against ``object`` because the model name is read structurally via
    ``getattr`` — any provider works, whether or not it declares ``model``.
    """

    def __init__(self, provider: object) -> None:
        self._provider = provider

    def render(self) -> str | None:
        model = getattr(self._provider, "model", None)
        if not model:
            return None
        return (
            f"You are powered by the model named {model}. When asked what model "
            f"you are, answer based on this information, not on any model name "
            f"returned by the API."
        )


class EnvironmentContributor:
    """Describe the execution environment: host OS, home, cwd, and WSL note.

    Adapted from Hermes ``build_environment_hints`` (local-backend branch only —
    Aegis has no remote terminal backends).  ``cwd`` should be the directory the
    tools actually resolve relative paths against (the ToolContext cwd) so the
    prompt names where file operations land.
    """

    def __init__(self, cwd: str | None = None) -> None:
        self._cwd = cwd

    def render(self) -> str | None:
        lines: list[str] = []
        if _is_wsl():
            lines.append("Host: WSL (Windows Subsystem for Linux)")
        elif sys.platform == "win32":
            import platform

            lines.append(f"Host: Windows ({platform.release()})")
        elif sys.platform == "darwin":
            import platform

            mac_ver = platform.mac_ver()[0]
            lines.append(f"Host: macOS ({mac_ver or platform.release()})")
        else:
            import platform

            lines.append(f"Host: {platform.system()} ({platform.release()})")

        lines.append(f"User home directory: {os.path.expanduser('~')}")
        cwd = self._cwd if self._cwd is not None else os.getcwd()
        lines.append(f"Current working directory: {cwd}")

        section = "\n".join(lines)
        if _is_wsl():
            section = f"{section}\n\n{_WSL_ENVIRONMENT_HINT}"
        return section


class TimestampContributor:
    """Render a date-only "Conversation started" line.

    Adapted from Hermes' volatile timestamp line.  Date-only (not minute
    precision) keeps the system prompt byte-stable across a day so the upstream
    prompt cache stays warm; the model can query exact wall-clock time via
    tools when it actually needs it.
    """

    def render(self) -> str | None:
        # Local calendar date on purpose — the user's "today", not UTC.  Naive
        # is fine here: only the date is rendered, never a tz-sensitive instant.
        today = datetime.date.today()  # noqa: DTZ011
        return f"Conversation started: {today.strftime('%A, %B %d, %Y')}"


__all__ = [
    "TASK_COMPLETION_GUIDANCE",
    "TOOL_USE_ENFORCEMENT_GUIDANCE",
    "EnvironmentContributor",
    "ModelIdentityContributor",
    "TaskCompletionContributor",
    "TimestampContributor",
    "ToolUseEnforcementContributor",
]
