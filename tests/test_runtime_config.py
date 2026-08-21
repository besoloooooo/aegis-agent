"""Tests for the reusable Agent Runtime abstraction (``AgentConfig``).

These lock in the refactor that turned ``AgentRuntime`` into a configurable,
re-instantiable engine: the Main Agent is just one runtime built with the
default (``"main"``) identity, and a future Subagent would be a second runtime
built from the *same* injected dependencies with a different ``AgentConfig``.

The behavioural chain (persist → build context → call model → run tools → loop)
is covered by ``test_runtime.py``; here we only assert the configuration seam
and that reuse does not leak state between agents.
"""

from __future__ import annotations

from aegis_agent.context.builder import ContextBuilder
from aegis_agent.models.base import Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import (
    DEFAULT_MAX_ITERATIONS,
    MAIN_AGENT_NAME,
    AgentConfig,
    AgentRuntime,
    StopReason,
)
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.executor import ToolExecutor
from aegis_agent.tools.registry import ToolRegistry


def test_agentconfig_defaults_to_main_identity():
    cfg = AgentConfig()
    assert cfg.agent_name == MAIN_AGENT_NAME == "main"
    assert cfg.max_iterations == DEFAULT_MAX_ITERATIONS


def test_agentconfig_is_immutable():
    cfg = AgentConfig(agent_name="worker", max_iterations=3)
    import dataclasses

    try:
        cfg.agent_name = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("AgentConfig should be frozen")


def test_default_runtime_carries_main_identity(make_runtime):
    runtime, _ = make_runtime(script=[FakeReply(text="hi")])
    assert runtime.agent_name == "main"
    assert runtime.config.agent_name == "main"


def test_max_iterations_kwarg_still_honoured():
    """Backward compat: the pre-existing ``max_iterations`` keyword still works
    when no explicit AgentConfig is passed to the constructor."""
    registry = ToolRegistry()
    runtime = AgentRuntime(
        provider=FakeModelProvider(),
        registry=registry,
        executor=ToolExecutor(registry),
        repository=InMemorySessionRepository(),
        max_iterations=7,
    )
    assert runtime.max_iterations == 7
    assert runtime.agent_name == "main"


def test_explicit_config_wins_over_max_iterations_kwarg():
    registry = ToolRegistry()
    runtime = AgentRuntime(
        provider=FakeModelProvider(),
        registry=registry,
        executor=ToolExecutor(registry),
        repository=InMemorySessionRepository(),
        max_iterations=99,  # ignored: config is the source of truth
        config=AgentConfig(agent_name="worker", max_iterations=2),
    )
    assert runtime.max_iterations == 2
    assert runtime.agent_name == "worker"


def test_config_max_iterations_bounds_the_loop():
    """The config's cap actually drives the run_turn iteration budget."""
    registry = ToolRegistry()
    provider = FakeModelProvider(
        script=[FakeReply.tool("nope", {}, call_id=f"c{i}") for i in range(20)]
    )
    runtime = AgentRuntime(
        provider=provider,
        registry=registry,
        executor=ToolExecutor(registry),
        repository=InMemorySessionRepository(),
        config=AgentConfig(agent_name="worker", max_iterations=3),
    )
    result = runtime.run_turn("s1", "loop")
    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert result.iterations == 3


def test_same_dependencies_two_runtimes_are_isolated():
    """Reuse proof: one shared set of collaborators (registry, executor,
    context builder) drives two independently-configured runtimes writing to
    *separate* repositories — mirroring how a Main Agent and a future Subagent
    would share the engine yet keep their own histories."""
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    context = ContextBuilder("shared-identity")

    main_repo = InMemorySessionRepository()
    sub_repo = InMemorySessionRepository()

    main = AgentRuntime(
        provider=FakeModelProvider(script=[FakeReply(text="main answer")]),
        registry=registry,
        executor=executor,
        repository=main_repo,
        context_builder=context,
        config=AgentConfig(agent_name="main"),
    )
    sub = AgentRuntime(
        provider=FakeModelProvider(script=[FakeReply(text="sub answer")]),
        registry=registry,
        executor=executor,
        repository=sub_repo,
        context_builder=context,
        config=AgentConfig(agent_name="worker", max_iterations=5),
    )

    main_result = main.run_turn("s", "hi from main")
    sub_result = sub.run_turn("s", "hi from sub")

    assert main.agent_name == "main"
    assert sub.agent_name == "worker"
    assert main_result.final_text == "main answer"
    assert sub_result.final_text == "sub answer"

    # Same session id, different repositories → no cross-agent history bleed.
    main_msgs = main_repo.list_messages("s")
    sub_msgs = sub_repo.list_messages("s")
    assert [m.role for m in main_msgs] == [Role.USER, Role.ASSISTANT]
    assert main_msgs[0].content == "hi from main"
    assert sub_msgs[0].content == "hi from sub"


def test_with_defaults_accepts_agent_name():
    runtime = AgentRuntime.with_defaults(
        provider=FakeModelProvider(),
        repository=InMemorySessionRepository(),
        agent_name="worker",
        max_iterations=4,
    )
    assert runtime.agent_name == "worker"
    assert runtime.max_iterations == 4
