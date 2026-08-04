"""Tool schemas (JSON Schema) for Aegis Agent's builtin tools.

These are the *minimal* schemas defined in ``docs/extraction-plan.md`` §4.
They are expressed as :class:`~aegis_agent.models.base.ToolDefinition` objects
(imported from the models layer) so the registry can advertise them to the
model without this module depending on the registry — keeping the dependency
direction one-way (tools → models.base, never the reverse).
"""

from __future__ import annotations

from aegis_agent.models.base import ToolDefinition

READ_FILE = ToolDefinition(
    name="read_file",
    description=(
        "Read a text file with line numbers and pagination. Output format is "
        "'LINE_NUM|CONTENT' (1-indexed). Use offset/limit for large files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)."},
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed).",
                "default": 1,
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read.",
                "default": 500,
                "maximum": 2000,
            },
        },
        "required": ["path"],
    },
)

LIST_DIRECTORY = ToolDefinition(
    name="list_directory",
    description=(
        "List the entries of a directory. Returns each entry's name, type "
        "(file/dir/other) and size in bytes (size is null for directories)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: current working directory).", "default": "."},
        },
        "required": [],
    },
)

RUN_SHELL = ToolDefinition(
    name="run_shell",
    description=(
        "Run a shell command and capture its combined output and exit code. "
        "Controlled: a timeout is always enforced and output is capped."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute."},
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait before killing the command.",
                "default": 30,
                "minimum": 1,
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the command (default: session cwd).",
            },
        },
        "required": ["command"],
    },
)

#: All builtin tool definitions, in registration order.
BUILTIN_DEFINITIONS = (READ_FILE, LIST_DIRECTORY, RUN_SHELL)


__all__ = ["BUILTIN_DEFINITIONS", "LIST_DIRECTORY", "READ_FILE", "RUN_SHELL"]
