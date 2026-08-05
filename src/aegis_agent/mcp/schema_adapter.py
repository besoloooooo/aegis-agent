# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (adapted and inlined):
#   * ``tools/mcp_tool.py`` (© 2025 Nous Research, MIT) —
#     ``_normalize_mcp_input_schema`` (line 3001) with its three-stage pipeline
#     (``_rewrite_local_refs`` / ``_strip_nullable_union`` /
#     ``_repair_object_shape``), ``_convert_mcp_schema`` (line 3120), and
#     ``sanitize_mcp_name_component`` (line 3109).
#   * ``tools/schema_sanitizer.py:strip_nullable_unions`` (line 131, MIT) —
#     collapsed-union logic, inlined here to avoid a dependency on Hermes'
#     schema_sanitizer module.
"""Convert MCP tool schemas to Aegis-compatible tool definitions.

MCP servers emit plain JSON Schema (often JSON Schema draft-07 with
``definitions`` and ``anyOf`` nullable unions).  Different LLM providers reject
different shapes: Kimi/Moonshot rejects ``#/definitions/...`` refs; Anthropic
rejects the ``{"type": "null"}`` branch in ``anyOf`` unions; Google Gemini
rejects dangling ``required`` entries.  The three-stage normalization pipeline
in :func:`normalize_mcp_input_schema` produces a schema that passes validation
on all three in one pass.
"""

from __future__ import annotations

import re
from typing import Any

# Matches any character outside [A-Za-z0-9_], which are the only characters
# safe for OpenAI function-calling tool names.
_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def sanitize_mcp_name_component(value: str) -> str:
    """Replace unsafe characters with ``_`` for use in a tool name prefix."""
    return _NAME_SAFE_RE.sub("_", str(value or ""))


# ---------------------------------------------------------------------------
# Three-stage normalization pipeline
# ---------------------------------------------------------------------------


def normalize_mcp_input_schema(schema: dict | None) -> dict:
    """Normalize an MCP ``inputSchema`` for LLM tool-calling compatibility.

    Returns a well-formed ``{"type": "object", "properties": {...}}`` dict
    suitable for use as the ``parameters`` value in an OpenAI tool definition.
    An empty or missing schema returns the minimal object shape.
    """
    if not schema or not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = _rewrite_local_refs(schema)
    normalized = _strip_nullable_union(normalized)
    normalized = _repair_object_shape(normalized)

    # Top-level guard: must be a dict with type=object and a properties key.
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}
    return dict(normalized)


def convert_mcp_tool(server_name: str, tool: Any) -> dict:
    """Convert one MCP tool listing into a registry-ready schema dict.

    ``tool`` is expected to have ``.name``, ``.description``, and
    ``.inputSchema`` attributes (the ``mcp.types.Tool`` shape).

    Returns ``{"name": "mcp_<server>_<tool>", "description": ..., "parameters": {...}}``.
    """
    safe_server = sanitize_mcp_name_component(server_name)
    safe_tool = sanitize_mcp_name_component(tool.name)
    prefixed = f"mcp_{safe_server}_{safe_tool}"
    description = (
        tool.description
        if isinstance(getattr(tool, "description", None), str) and tool.description
        else f"MCP tool {tool.name} from server {server_name}"
    )
    return {
        "name": prefixed,
        "description": description,
        "parameters": normalize_mcp_input_schema(getattr(tool, "inputSchema", None)),
    }


# ---------------------------------------------------------------------------
# Stage 1: rewrite JSON Schema draft-07 ``definitions`` → ``$defs``
# ---------------------------------------------------------------------------


def _rewrite_local_refs(node: Any) -> Any:
    """Convert ``#/definitions/X`` refs and the ``definitions`` key to
    ``#/$defs/X`` / ``$defs`` for Kimi / Moonshot compatibility."""
    if isinstance(node, dict):
        normalized: dict[str, Any] = {}
        for key, value in node.items():
            out_key = "$defs" if key == "definitions" else key
            normalized[out_key] = _rewrite_local_refs(value)
        ref = normalized.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            normalized["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
        return normalized
    if isinstance(node, list):
        return [_rewrite_local_refs(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# Stage 2: collapse ``anyOf [{T}, {null}]`` nullable unions
# ---------------------------------------------------------------------------


def _strip_nullable_union(schema: Any) -> Any:
    """Collapse ``anyOf`` / ``oneOf`` nullable unions to the non-null branch.

    MCP / Pydantic optional fields commonly arrive as::

        {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}

    Anthropic rejects the null branch.  The non-null variant is kept with a
    ``nullable: true`` hint; optionality is otherwise represented by the parent
    object's ``required`` array.
    """
    if isinstance(schema, list):
        return [_strip_nullable_union(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    stripped = {k: _strip_nullable_union(v) for k, v in schema.items()}
    for key in ("anyOf", "oneOf"):
        variants = stripped.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [
            item for item in variants
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            replacement.setdefault("nullable", True)
            for meta_key in ("title", "description", "default", "examples"):
                if meta_key in stripped and meta_key not in replacement:
                    replacement[meta_key] = stripped[meta_key]
            return _strip_nullable_union(replacement)
    return stripped


# ---------------------------------------------------------------------------
# Stage 3: repair object-shaped nodes
# ---------------------------------------------------------------------------


def _repair_object_shape(node: Any) -> Any:
    """Fill missing ``type: "object"``, inject empty ``properties``, prune
    dangling ``required`` entries (Google Gemini rejects them)."""
    if isinstance(node, list):
        return [_repair_object_shape(item) for item in node]
    if not isinstance(node, dict):
        return node

    repaired = {k: _repair_object_shape(v) for k, v in node.items()}

    # Coerce missing / null type when the shape is clearly an object.
    if not repaired.get("type") and ("properties" in repaired or "required" in repaired):
        repaired["type"] = "object"

    if repaired.get("type") == "object":
        # Ensure properties exists so required can reference it safely.
        if "properties" not in repaired or not isinstance(repaired.get("properties"), dict):
            repaired["properties"] = {}

        # Prune required entries that don't exist in properties.
        required = repaired.get("required")
        if isinstance(required, list):
            props = repaired.get("properties") or {}
            valid = [r for r in required if isinstance(r, str) and r in props]
            if len(valid) != len(required):
                if valid:
                    repaired["required"] = valid
                else:
                    repaired.pop("required", None)

    return repaired


__all__ = [
    "convert_mcp_tool",
    "normalize_mcp_input_schema",
    "sanitize_mcp_name_component",
]
