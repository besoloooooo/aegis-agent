"""MCP prompt guidance tests."""

from __future__ import annotations

from aegis_agent.mcp.guidance import MCPToolsGuidance


class TestMCPToolsGuidance:
    def test_returns_none_when_no_servers(self):
        g = MCPToolsGuidance()
        assert g.render() is None

    def test_returns_text_when_servers_present(self):
        g = MCPToolsGuidance()
        g.set_servers(2)
        result = g.render()
        assert result is not None
        assert "MCP tools" in result
        assert "2 servers" in result

    def test_singular_for_one_server(self):
        g = MCPToolsGuidance()
        g.set_servers(1)
        result = g.render()
        assert result is not None
        assert "1 server" in result
        assert "servers" not in result

    def test_reset_to_zero_returns_none(self):
        g = MCPToolsGuidance()
        g.set_servers(3)
        assert g.render() is not None
        g.set_servers(0)
        assert g.render() is None
