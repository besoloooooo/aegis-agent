"""Behaviour / model / environment system-prompt contributors."""

from __future__ import annotations

import datetime

from aegis_agent.context.prompt_sections import (
    TASK_COMPLETION_GUIDANCE,
    TOOL_USE_ENFORCEMENT_GUIDANCE,
    EnvironmentContributor,
    ModelIdentityContributor,
    TaskCompletionContributor,
    TimestampContributor,
    ToolUseEnforcementContributor,
)
from aegis_agent.models.fake import FakeModelProvider
from aegis_agent.runtime import AgentRuntime
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.builtin import build_default_registry


class _FakeRegistry:
    """A minimal length-only stand-in for the tool registry."""

    def __init__(self, count: int) -> None:
        self._count = count

    def __len__(self) -> int:
        return self._count


class _ModelProvider:
    """A provider exposing a fixed model name."""

    def __init__(self, model: str | None) -> None:
        self._model = model

    @property
    def name(self) -> str:
        return "stub"

    @property
    def model(self) -> str | None:
        return self._model


class TestBehaviourContributors:
    def test_task_completion_rendered_when_tools_present(self):
        c = TaskCompletionContributor(_FakeRegistry(3))
        assert c.render() == TASK_COMPLETION_GUIDANCE

    def test_task_completion_dropped_when_no_tools(self):
        c = TaskCompletionContributor(_FakeRegistry(0))
        assert c.render() is None

    def test_tool_use_enforcement_rendered_when_tools_present(self):
        c = ToolUseEnforcementContributor(_FakeRegistry(1))
        assert c.render() == TOOL_USE_ENFORCEMENT_GUIDANCE

    def test_tool_use_enforcement_dropped_when_no_tools(self):
        c = ToolUseEnforcementContributor(_FakeRegistry(0))
        assert c.render() is None


class TestModelIdentityContributor:
    def test_rendered_when_model_known(self):
        c = ModelIdentityContributor(_ModelProvider("gpt-x"))
        rendered = c.render()
        assert rendered is not None
        assert "gpt-x" in rendered

    def test_dropped_when_model_missing(self):
        c = ModelIdentityContributor(_ModelProvider(None))
        assert c.render() is None

    def test_dropped_for_provider_without_model_attr(self):
        # The fake provider exposes no ``model`` attribute at all.
        c = ModelIdentityContributor(FakeModelProvider())
        assert c.render() is None


class TestEnvironmentContributor:
    def test_reports_supplied_cwd(self):
        c = EnvironmentContributor(cwd="/tmp/aegis-workdir")
        rendered = c.render()
        assert rendered is not None
        assert "Current working directory: /tmp/aegis-workdir" in rendered
        assert "Host:" in rendered
        assert "User home directory:" in rendered

    def test_falls_back_to_process_cwd(self):
        import os

        c = EnvironmentContributor()
        rendered = c.render()
        assert rendered is not None
        assert os.getcwd() in rendered


class TestTimestampContributor:
    def test_renders_todays_date(self):
        rendered = TimestampContributor().render()
        assert rendered is not None
        today = datetime.date.today().strftime("%A, %B %d, %Y")  # noqa: DTZ011
        assert rendered == f"Conversation started: {today}"


class TestIntegrationOrdering:
    """The composed prompt from a defaulted runtime carries the sections in order."""

    def _prompt(self) -> str:
        runtime = AgentRuntime.with_defaults(
            provider=FakeModelProvider(),
            repository=InMemorySessionRepository(),
            enable_skills=False,
            enable_mcp=False,
        )
        return runtime._context.system_prompt

    def test_sections_present_and_ordered(self):
        prompt = self._prompt()
        identity_i = prompt.index("You are Aegis Agent")
        finishing_i = prompt.index("# Finishing the job")
        enforcement_i = prompt.index("# Tool-use enforcement")
        host_i = prompt.index("Host:")
        started_i = prompt.index("Conversation started:")
        assert identity_i < finishing_i < enforcement_i < host_i < started_i

    def test_excluded_subsystems_absent(self):
        prompt = self._prompt()
        # Aegis has no memory / session search / soul / branding — none must leak.
        for term in ("session_search", "SOUL", "Hermes", "USER.md", "persistent memory"):
            assert term not in prompt

    def test_fake_provider_omits_model_identity_line(self):
        prompt = self._prompt()
        assert "You are powered by the model named" not in prompt

    def test_default_registry_triggers_behaviour_blocks(self):
        # Sanity: the default registry is non-empty, so behaviour blocks render.
        assert len(build_default_registry()) > 0
