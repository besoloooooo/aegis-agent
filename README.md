# Aegis Agent

A lightweight, recoverable, and extensible **Agent Runtime**, built by
extracting, simplifying, and modularizing the core runtime behaviour of
[Hermes](https://github.com/NousResearch/hermes-agent) (© 2025 Nous Research,
MIT — see `THIRD_PARTY_NOTICES.md`).

## Status: Stage 2 — real model provider & streaming tool calls

The runtime drives a full model↔tool loop.  Stage 1 delivered the minimal
vertical slice (fake provider, in-memory sessions); Stage 2 adds a real
OpenAI-compatible provider and streaming with tool-call fragment assembly:

```
user input
  → ModelProvider produces a tool call (streaming, args assembled from fragments)
  → ToolExecutor runs the tool(s)
  → tool result(s) are back-filled into history
  → ModelProvider continues, eventually producing the final answer
```

Still out of scope (later stages): SQLite/Redis, resume/checkpoint, context
compression, skills.

### What's here

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
| Sessions | `aegis_agent.sessions` | `SessionRepository` Protocol + in-memory store |
| Agent loop | `aegis_agent.runtime.AgentRuntime` | guard → context → model → tools → loop; budget, interrupt, timeout, error |
| CLI | `aegis_agent.cli` | interactive REPL, `aegis` command, backend selection |

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

Verified end-to-end against a real endpoint (Qwen via DashScope's
OpenAI-compatible mode): a real turn drove `list_directory`, the result was
back-filled, and the model produced the final answer.

### Dangerous-command guardrail

`run_shell` refuses commands matching a destructive-pattern list (recursive
delete, `mkfs`/`dd`, SQL `DROP`/`DELETE`-without-`WHERE`, fork bomb,
pipe-to-shell, destructive git, service/process kill).  The model cannot bypass
it — only the operator can, via `--allow-dangerous-shell` (CLI) or
`ToolContext(allow_dangerous_shell=True)`.

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
you> exit
```

## Test & lint

```bash
uv run pytest -q
uv run ruff check .
```

Default tests never touch a real paid API (the provider tests inject a fake
client).  One smoke test against a real endpoint is opt-in:

```bash
AEGIS_RUN_INTEGRATION=1 AEGIS_API_KEY=... AEGIS_MODEL=... uv run pytest -m integration
```

## Provenance

See `docs/extraction-plan.md` (the phased plan) and `docs/source-map.md`
(which Aegis modules derive from which Hermes sources, and how).  No Hermes
code is described as wholly original; adapted files carry an attribution
header and retain the Hermes MIT copyright.
