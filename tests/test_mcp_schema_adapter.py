"""MCP schema adapter tests: name sanitization, normalization pipeline, tool conversion."""

from __future__ import annotations

from aegis_agent.mcp.schema_adapter import (
    convert_mcp_tool,
    normalize_mcp_input_schema,
    sanitize_mcp_name_component,
)


class TestSanitizeNameComponent:
    def test_replaces_non_alnum_with_underscore(self):
        assert sanitize_mcp_name_component("my-tool") == "my_tool"

    def test_replaces_spaces(self):
        assert sanitize_mcp_name_component("my tool") == "my_tool"

    def test_preserves_valid_chars(self):
        assert sanitize_mcp_name_component("hello_World123") == "hello_World123"

    def test_empty_returns_empty(self):
        assert sanitize_mcp_name_component("") == ""


class TestNormalizeInputSchema:
    def test_empty_schema_returns_object_shape(self):
        result = normalize_mcp_input_schema(None)
        assert result == {"type": "object", "properties": {}}

    def test_empty_dict_returns_object_shape(self):
        result = normalize_mcp_input_schema({})
        assert result == {"type": "object", "properties": {}}

    def test_preserves_valid_schema(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        result = normalize_mcp_input_schema(schema)
        assert result["type"] == "object"
        assert result["properties"]["x"]["type"] == "string"

    # -- Stage 1: $ref rewriting ----------------------------------------

    def test_rewrites_definitions_key_to_defs(self):
        schema = {
            "type": "object",
            "definitions": {"Foo": {"type": "string"}},
            "properties": {},
        }
        result = normalize_mcp_input_schema(schema)
        assert "$defs" in result
        assert "definitions" not in result
        assert result["$defs"]["Foo"]["type"] == "string"

    def test_rewrites_ref_prefix(self):
        schema = {
            "type": "object",
            "properties": {"x": {"$ref": "#/definitions/Foo"}},
            "definitions": {"Foo": {"type": "integer"}},
        }
        result = normalize_mcp_input_schema(schema)
        assert result["properties"]["x"]["$ref"] == "#/$defs/Foo"

    # -- Stage 2: nullable union collapse ---------------------------------

    def test_collapses_nullable_union(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
        }
        result = normalize_mcp_input_schema(schema)
        assert result["properties"]["name"]["type"] == "string"
        assert result["properties"]["name"]["nullable"] is True

    def test_preserves_meaningful_union(self):
        """A union of two non-null types should not be collapsed."""
        schema = {
            "type": "object",
            "properties": {
                "value": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
        }
        result = normalize_mcp_input_schema(schema)
        props = result["properties"]["value"]
        assert "anyOf" in props
        assert len(props["anyOf"]) == 2

    def test_collapses_oneof_nullable(self):
        schema = {
            "type": "object",
            "properties": {
                "x": {"oneOf": [{"type": "number"}, {"type": "null"}]},
            },
        }
        result = normalize_mcp_input_schema(schema)
        assert result["properties"]["x"]["type"] == "number"

    def test_carries_metadata_to_collapsed_branch(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "The name",
                    "default": "world",
                },
            },
        }
        result = normalize_mcp_input_schema(schema)
        assert result["properties"]["name"]["description"] == "The name"
        assert result["properties"]["name"]["default"] == "world"

    # -- Stage 3: object shape repair ------------------------------------

    def test_injects_missing_type_object(self):
        schema = {"properties": {"x": {"type": "string"}}}
        result = normalize_mcp_input_schema(schema)
        assert result["type"] == "object"

    def test_injects_empty_properties(self):
        schema = {"type": "object"}
        result = normalize_mcp_input_schema(schema)
        assert result["properties"] == {}

    def test_prunes_dangling_required(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a", "b", "c"],
        }
        result = normalize_mcp_input_schema(schema)
        assert result["required"] == ["a"]

    def test_drops_required_when_all_dangling(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["x", "y"],
        }
        result = normalize_mcp_input_schema(schema)
        assert "required" not in result

    def test_repairs_deeply_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "properties": {"inner": {"type": "string"}},
                    "required": ["inner", "ghost"],
                },
            },
        }
        result = normalize_mcp_input_schema(schema)
        inner = result["properties"]["nested"]
        assert inner["type"] == "object"
        assert inner["required"] == ["inner"]


class DummyTool:
    def __init__(self, name, description=None, inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class TestConvertMCPTool:
    def test_prefixes_with_mcp_server_tool(self):
        tool = DummyTool("do_thing", "Does a thing")
        schema = convert_mcp_tool("my-server", tool)
        assert schema["name"] == "mcp_my_server_do_thing"

    def test_sanitizes_special_chars(self):
        tool = DummyTool("run-thing", "Description")
        schema = convert_mcp_tool("my-server", tool)
        assert schema["name"] == "mcp_my_server_run_thing"

    def test_fallback_description(self):
        tool = DummyTool("tool", None)
        schema = convert_mcp_tool("srv", tool)
        assert "server srv" in schema["description"].lower()

    def test_includes_parameters(self):
        tool = DummyTool("t", "desc", {"type": "object", "properties": {"x": {"type": "string"}}})
        schema = convert_mcp_tool("s", tool)
        assert schema["parameters"]["type"] == "object"
