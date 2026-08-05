"""MCP tool wrapper tests."""

from __future__ import annotations

import json

from aegis_agent.mcp.tools import MCPToolWrapper


class _FakeTool:
    def __init__(self, name, description=None, inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class TestMCPToolWrapper:
    def test_definition_has_mcp_prefix(self):
        tool = _FakeTool("do_thing", "Does a thing")
        wrapper = MCPToolWrapper("myserver", tool)
        assert wrapper.definition.name == "mcp_myserver_do_thing"

    def test_definition_includes_description(self):
        tool = _FakeTool("t", "A helpful tool")
        wrapper = MCPToolWrapper("srv", tool)
        assert wrapper.definition.description == "A helpful tool"

    def test_definition_has_parameters(self):
        tool = _FakeTool("t", "desc", {"type": "object", "properties": {"x": {"type": "string"}}})
        wrapper = MCPToolWrapper("s", tool)
        assert wrapper.definition.parameters["type"] == "object"

    def test_run_returns_tool_result(self):
        # Without an actual MCP server connected, call_tool returns an error JSON.
        wrapper = MCPToolWrapper("nonexistent_srv", _FakeTool("t", "desc"))
        result = wrapper.run({})
        assert result.is_error is True
        parsed = json.loads(result.content)
        assert "error" in parsed
        assert "not connected" in parsed["error"]

    def test_run_never_raises(self):
        wrapper = MCPToolWrapper("bad-server", _FakeTool("t", "desc"))
        # Should not raise
        _result = wrapper.run({"key": "val"})
        _result = wrapper.run({})
        _result = wrapper.run(None)  # type: ignore[arg-type]
        assert True  # sanity
