"""Tests for the first-version Subagent closed loop.

Covers the acceptance points for this milestone:

* the Main Agent can call the ``Agent`` tool and get a result back;
* the subagent genuinely reuses the shared :class:`AgentRuntime`;
* ``explore`` has no write/mutating tools;
* the subagent's intermediate messages never pollute the Main transcript;
* a subagent failure surfaces to the Main Agent as a clear error result;
* the Main Agent's own behaviour is unchanged when subagents are disabled.
"""

from __future__ import annotations

import json

from aegis_agent.agents.agent_tool import AgentTool
from aegis_agent.agents.definitions import (
    AGENT_TOOL_NAME,
    READ_ONLY_TOOL_NAMES,
    AgentDefinition,
    builtin_agents,
)
from aegis_agent.agents.manager import SubagentManager
from aegis_agent.agents.runner import SubagentRunner, SubagentStatus
from aegis_agent.models.base import Role
from aegis_agent.models.fake import FakeModelProvider, FakeReply
from aegis_agent.runtime import AgentRuntime, StopReason
from aegis_agent.sessions.memory_store import InMemorySessionRepository
from aegis_agent.tools.builtin import build_default_registry

# ---------------------------------------------------------------------------
# Runner-level tests (the reuse seam)
# ---------------------------------------------------------------------------


def _runner(provider):
    registry = build_default_registry()
    return SubagentRunner(provider, registry), registry


def _agent_tool(provider, agents=None, *, allow_fork=False, parent=None, history=None):
    """Build an AgentTool wired through a SubagentManager (the new constructor)."""
    parent = parent if parent is not None else build_default_registry()
    runner = SubagentRunner(provider, parent)
    manager = SubagentManager(runner, agents if agents is not None else builtin_agents())
    return AgentTool(manager, allow_fork=allow_fork, history_provider=history)


def test_runner_returns_final_output():
    provider = FakeModelProvider(script=[FakeReply(text="analysis complete")])
    runner, _ = _runner(provider)
    result = runner.run(builtin_agents()["explore"], "analyse the memory module")

    assert result.status is SubagentStatus.COMPLETED
    assert result.output == "analysis complete"
    assert result.agent_type == "explore"


def test_runner_runs_a_tool_then_reports():
    """A subagent drives the full model↔tool loop of the shared runtime."""
    script = [
        FakeReply.tool("list_directory", {"path": "."}, call_id="c1"),
        FakeReply(text="found 3 files"),
    ]
    provider = FakeModelProvider(script=script)
    runner, _ = _runner(provider)
    result = runner.run(builtin_agents()["explore"], "list the cwd")

    assert result.status is SubagentStatus.COMPLETED
    assert result.output == "found 3 files"
    assert result.tool_calls == 1
    assert result.iterations == 2


def test_explore_has_no_write_tools():
    """The explore subagent's registry must exclude mutating tools."""
    provider = FakeModelProvider(script=[FakeReply(text="ok")])
    runner, parent_registry = _runner(provider)
    sub_registry = runner._build_sub_registry(builtin_agents()["explore"])
    names = set(sub_registry.names())

    assert "write_file" not in names
    assert "patch" not in names
    assert "terminal" not in names
    assert "process" not in names
    # only names on the read-only whitelist survive
    assert names <= READ_ONLY_TOOL_NAMES
    # sanity: the parent DID have the write tools to begin with
    assert "write_file" in set(parent_registry.names())


def test_general_purpose_keeps_write_tools_but_not_agent_tool():
    provider = FakeModelProvider(script=[FakeReply(text="ok")])
    # Parent registry with an Agent tool present, to prove it's stripped.
    parent = build_default_registry()
    runner = SubagentRunner(provider, parent)
    agents = builtin_agents()
    parent.register(_agent_tool(provider, agents, parent=parent))

    sub_registry = runner._build_sub_registry(agents["general-purpose"])
    names = set(sub_registry.names())
    assert "write_file" in names
    assert "terminal" in names
    assert AGENT_TOOL_NAME not in names  # recursion guard


def test_failed_subagent_reports_error():
    class BoomProvider:
        name = "boom"

        def stream(self, messages, tools=None):
            from aegis_agent.exceptions import ModelProviderError

            raise ModelProviderError("provider exploded")

    runner, _ = _runner(BoomProvider())
    result = runner.run(builtin_agents()["explore"], "do something")
    assert result.status is SubagentStatus.FAILED
    assert result.error is not None


# ---------------------------------------------------------------------------
# End-to-end via the Main Agent + Agent tool
# ---------------------------------------------------------------------------


def test_main_agent_invokes_agent_tool_and_gets_result():
    """Full closed loop: Main model calls Agent(...), subagent runs, Main
    continues with the returned result."""
    repo = InMemorySessionRepository()
    # One shared FIFO script consumed in ACTUAL call order across both agents:
    #   1) Main requests the Agent tool
    #   2) subagent (runs during the tool call) produces its final answer
    #   3) Main produces its final answer using the returned result
    script = [
        FakeReply.tool(
            AGENT_TOOL_NAME,
            {"prompt": "analyse the code", "subagent_type": "explore"},
            call_id="a1",
        ),
        FakeReply(text="subagent analysis: all good"),
        FakeReply(text="Main saw the subagent result."),
    ]
    provider = FakeModelProvider(script=script)

    runtime = AgentRuntime.with_defaults(provider=provider, repository=repo)
    result = runtime.run_turn("main-session", "please analyse")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_text == "Main saw the subagent result."

    # The Main transcript contains the Agent tool call + its result, but NOT
    # any of the subagent's internal messages.
    main_msgs = repo.list_messages("main-session")
    tool_msgs = [m for m in main_msgs if m.role is Role.TOOL]
    assert len(tool_msgs) == 1
    agent_result = json.loads(tool_msgs[0].content)
    assert agent_result["result"] == "subagent analysis: all good"
    assert agent_result["subagent_type"] == "explore"


def test_subagent_transcript_does_not_pollute_main():
    """The subagent runs a tool internally; none of that appears in Main."""
    repo = InMemorySessionRepository()
    # Interleaved in real call order: Main's Agent call, then the subagent's
    # internal tool round + final answer, then Main's final answer.
    script = [
        FakeReply.tool(
            AGENT_TOOL_NAME,
            {"prompt": "list files and summarise", "subagent_type": "explore"},
            call_id="a1",
        ),
        FakeReply.tool("list_directory", {"path": "."}, call_id="s1"),
        FakeReply(text="there are files"),
        FakeReply(text="done"),
    ]
    provider = FakeModelProvider(script=script)
    runtime = AgentRuntime.with_defaults(provider=provider, repository=repo)
    runtime.run_turn("main", "go")

    main_msgs = repo.list_messages("main")
    roles = [m.role for m in main_msgs]
    # user, assistant(agent call), tool(agent result), assistant(final) — exactly.
    assert roles == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    # The subagent's list_directory result is NOT in the main transcript.
    tool_names = [m.name for m in main_msgs if m.role is Role.TOOL]
    assert tool_names == [AGENT_TOOL_NAME]


def test_agent_tool_unknown_type_is_error():
    provider = FakeModelProvider(script=[FakeReply(text="unused")])
    tool = _agent_tool(provider)
    result = tool.run({"prompt": "x", "subagent_type": "nope"})
    assert result.is_error
    assert "unknown subagent_type" in json.loads(result.content)["error"].lower()


def test_agent_tool_missing_prompt_is_error():
    provider = FakeModelProvider()
    tool = _agent_tool(provider)
    result = tool.run({"subagent_type": "explore"})
    assert result.is_error


def test_subagent_failure_surfaces_through_agent_tool():
    """A subagent that errors becomes an is_error tool result the Main sees."""
    repo = InMemorySessionRepository()

    class OneGoodThenBoom:
        """Main's first call requests the Agent tool; the subagent's call raises."""

        name = "mixed"

        def __init__(self):
            self.calls = 0

        def stream(self, messages, tools=None):
            self.calls += 1
            from aegis_agent.events import ModelEvent
            from aegis_agent.exceptions import ModelProviderError
            from aegis_agent.models.base import ToolCall

            if self.calls == 1:
                yield ModelEvent.tool(
                    ToolCall(
                        id="a1",
                        name=AGENT_TOOL_NAME,
                        arguments=json.dumps(
                            {"prompt": "do it", "subagent_type": "explore"}
                        ),
                    )
                )
                yield ModelEvent.done("tool_calls")
            elif self.calls == 2:
                # This is the subagent's model call — fail it.
                raise ModelProviderError("subagent model down")
            else:
                yield ModelEvent.text_delta("Main handled the failure.")
                yield ModelEvent.done("stop")

    provider = OneGoodThenBoom()
    runtime = AgentRuntime.with_defaults(provider=provider, repository=repo)
    result = runtime.run_turn("main", "go")

    assert result.stop_reason is StopReason.FINAL_ANSWER
    assert result.final_text == "Main handled the failure."
    tool_msg = next(m for m in repo.list_messages("main") if m.role is Role.TOOL)
    payload = json.loads(tool_msg.content)
    assert "error" in payload
    assert "failed" in payload["error"].lower()


def test_enable_subagents_false_removes_agent_tool():
    """Disabling subagents restores the exact pre-subagent tool set."""
    provider = FakeModelProvider(script=[FakeReply(text="hi")])
    runtime = AgentRuntime.with_defaults(
        provider=provider,
        repository=InMemorySessionRepository(),
        enable_subagents=False,
    )
    assert AGENT_TOOL_NAME not in runtime._registry.names()
    assert runtime.startup_info.get("subagents", 0) == 0


def test_agent_tool_present_by_default():
    provider = FakeModelProvider(script=[FakeReply(text="hi")])
    runtime = AgentRuntime.with_defaults(
        provider=provider, repository=InMemorySessionRepository()
    )
    assert AGENT_TOOL_NAME in runtime._registry.names()
    assert runtime.startup_info.get("subagents") == 2


def test_subagent_reuses_agentruntime_type():
    """Guard against a parallel loop: the runner must build an AgentRuntime."""
    import aegis_agent.agents.runner as runner_mod

    built = {}
    real_init = AgentRuntime.__init__

    def spy_init(self, *args, **kwargs):
        built["config"] = kwargs.get("config")
        return real_init(self, *args, **kwargs)

    runner_mod.AgentRuntime.__init__ = spy_init
    try:
        provider = FakeModelProvider(script=[FakeReply(text="done")])
        runner = SubagentRunner(provider, build_default_registry())
        runner.run(builtin_agents()["explore"], "task")
    finally:
        runner_mod.AgentRuntime.__init__ = real_init

    assert built["config"] is not None
    assert built["config"].agent_name == "explore"


def test_custom_agent_definition_is_extensible():
    """A user-defined AgentDefinition works through the same runner path."""
    provider = FakeModelProvider(script=[FakeReply(text="custom done")])
    custom = AgentDefinition(
        name="reviewer",
        description="reviews things",
        system_prompt="You review code.",
        tool_names=frozenset({"read_file"}),
        max_iterations=5,
    )
    tool = _agent_tool(provider, {"reviewer": custom})
    assert "reviewer" in tool.definition.parameters["properties"]["subagent_type"]["enum"]
    result = tool.run({"prompt": "review", "subagent_type": "reviewer"})
    assert not result.is_error
    assert json.loads(result.content)["result"] == "custom done"
