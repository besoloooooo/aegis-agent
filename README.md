# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by
extracting, simplifying, and modularizing the core runtime behaviour of
[Hermes](https://github.com/NousResearch/hermes-agent) (© 2025 Nous Research,
MIT — see `THIRD_PARTY_NOTICES.md`).

## Status: Stage 5 — core runtime complete with skills + MCP

Five milestones delivered; more planned (see `docs/extraction-plan.md` §7).

| Stage | Theme | Highlights |
|---|---|---|---|
| 1 | Minimal Agent Runtime skeleton | Fake provider, in-memory sessions, 3 builtin tools, Agent Loop |
| 2 | OpenAI-compatible provider & streaming | Real provider, tool-call fragment assembly, dangerous-command guardrail, message sanitization |
| 3 | Live terminal UI | prompt_toolkit input, rich output, pyfiglet banner, kaomoji spinner |
| 4 | Skills subsystem | SKILL.md discovery/loading/routing, `skills_list`/`skill_view` tools, slash commands, dynamic prompt injection |
| 5 | Lightweight MCP client | stdio + HTTP transports, schema normalization, MCPToolWrapper, optional `mcp` SDK |

Still planned: SQLite persistence + checkpoint recovery, context compression, session leases, concurrent tool execution, oversized-tool-result storage, MCP reconnect & circuit breaker.

## What's here

| Area | Module | Notes |
|---|---|---|
| Data structures | `aegis_agent.models.base` | `Message`, `ToolCall`, `ToolResult`, `ToolDefinition`, `ChatResponse` |
| Streaming events | `aegis_agent.events` | `ModelEvent`, `collect_response` (stream → uniform response, cancel-aware) |
| Stream assembly | `aegis_agent.models.stream` | `StreamAssembler` — tool-call name/argument fragment assembly |
| Provider abstraction | `aegis_agent.models.base.ModelProvider` | `Protocol`; runtime depends only on it |
| Fake provider | `aegis_agent.models.fake.FakeModelProvider` | deterministic, scriptable, streaming |
| OpenAI provider | `aegis_agent.models.openai_compat.OpenAICompatibleProvider` | env-configured, streaming + one-shot |
| Tool registry | `aegis_agent.tools.registry.ToolRegistry` | explicit injection, no global singleton |
| Tool executor | `aegis_agent.tools.executor.ToolExecutor` | dispatch + `{"error": ...}` fallback |
| Builtin tools | `read_file`, `list_directory`, `run_shell` | minimal, controlled |
| Dangerous-command guardrail | `aegis_agent.tools.danger` | `run_shell` blocks destructive commands by default; operator-only override |
| Context builder | `aegis_agent.context.builder.ContextBuilder` | derived view; source messages never mutated |
| Dynamic prompt | `aegis_agent.context.system_prompt` | `SystemPromptBuilder` + `PromptContributor` seam |
| Sessions | `aegis_agent.sessions` | `SessionRepository` Protocol + in-memory store |
| Agent loop | `aegis_agent.runtime.AgentRuntime` | guard → context → model → tools → loop; budget, interrupt, timeout, error |
| Skills | `aegis_agent.skills` | SKILL.md discovery, loading, routing, progressive-disclosure tools, prompt index injection |
| MCP client | `aegis_agent.mcp` | stdio + Streamable HTTP, schema adapter, Tool Protocol wrappers, optional `mcp` SDK |
| CLI | `aegis_agent.cli` | interactive REPL, `aegis` command, backend/skills/MCP configuration |

## Model configuration

The provider is configured purely from the environment (no secrets in code):

```bash
export AEGIS_API_KEY=...                 # required for the real provider
export AEGIS_BASE_URL=http://localhost:1234/v1   # optional; any OpenAI-compatible endpoint
export AEGIS_MODEL=gpt-4o-mini           # required for the real provider
```

The CLI picks the backend with `--model-backend auto|fake|openai` (default
`auto`): real provider when `AEGIS_API_KEY` and `AEGIS_MODEL` are set,
otherwise the deterministic fake.

### Dangerous-command guardrail

`run_shell` refuses commands matching a destructive-pattern list (recursive
delete, `mkfs`/`dd`, SQL `DROP`/`DELETE`-without-`WHERE`, fork bomb,
pipe-to-shell, destructive git, service/process kill).  The model cannot bypass
it — only the operator can, via `--allow-dangerous-shell` (CLI) or
`ToolContext(allow_dangerous_shell=True)`.

## Skills

Skills are reusable instruction sets for specific tasks.  A skill is a
directory containing a `SKILL.md` file with YAML frontmatter:

```yaml
---
name: my-skill
description: What this skill does.
---
# Instructions
...
```

Skills are discovered from `~/.aegis/skills/` (or `$AEGIS_SKILLS_DIR`):

```bash
mkdir -p ~/.aegis/skills/my-skill
# write SKILL.md there
```

A compact index is injected into the system prompt.  The model can browse
skills via the `skills_list` tool and load full instructions via `skill_view`.
Users can activate a skill directly with `/skill-name`.

```bash
uv run aegis --skills-dir ~/my-skills   # custom directory
uv run aegis --no-skills                # disable skills
```

## MCP (Model Context Protocol)

Aegis can connect to external MCP servers and use their tools as native tools.
The `mcp` Python SDK is an **optional** dependency:

```bash
uv sync --extra mcp                      # install with MCP support
```

Configure servers in `~/.aegis/config.yaml`:

```yaml
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  remote-api:
    url: "https://example.com/mcp"
    headers:
      Authorization: "Bearer ${MY_TOKEN}"
```

Supported transports: stdio and Streamable HTTP.  MCP tools are registered
alongside builtin and skills tools and are available to the model.  A brief
guidance note is injected into the system prompt.

```bash
uv run aegis --mcp-config ~/my-config.yaml   # custom config path
uv run aegis --no-mcp                         # disable MCP
```

## Install & run

This project uses [uv](https://docs.astral.sh/uv/) exclusively.

```bash
uv sync
uv run aegis            # start the interactive REPL
uv run aegis --model-backend fake     # force the fake provider
uv run aegis --version
uv run python -m aegis_agent
```

In the REPL:

```
you> hello
aegis> Echo: hello
you> list .                  # triggers the list_directory tool
you> read README.md          # triggers the read_file tool
you> run echo hi             # triggers the run_shell tool
you> /skill-name do X        # activates a skill
you> exit
```

## Test & lint

```bash
uv run pytest -q
uv run ruff check .
```

Default tests never touch a real paid API.  One smoke test is opt-in:

```bash
AEGIS_RUN_INTEGRATION=1 AEGIS_API_KEY=... AEGIS_MODEL=... uv run pytest -m integration
```

## Provenance

See `docs/extraction-plan.md` (the phased plan), `docs/development-log.md`
(interview-oriented technical log), and `docs/source-map.md` (which Aegis
modules derive from which Hermes sources, and how).  No Hermes code is
described as wholly original; adapted files carry an attribution header and
retain the Hermes MIT copyright.
