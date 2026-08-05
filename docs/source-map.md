# Source Map — Aegis Agent ↔ Hermes

This file records the provenance of Aegis Agent code relative to the Hermes
(`hermes-agent`, © 2025 Nous Research, MIT) reference sources, per CLAUDE.md
§7 and the extraction plan §8.6.

Relationship legend:

- **PORT** — copied with little or no change; retains Hermes copyright.
- **ADAPT** — derived from Hermes but decoupled/simplified; retains Hermes
  copyright and an attribution header.
- **REWRITE** — written fresh in Aegis, referencing only Hermes' *observable
  behaviour*; original Aegis code (no Hermes copyright), but the behavioural
  source is noted here for traceability.

Only files with a Hermes relationship are listed.  All other Aegis files are
original work with no Hermes derivation.

## Stage 1 — minimal Agent Runtime skeleton

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/runtime.py` (`IterationBudget`) | **PORT** | `agent/iteration_budget.py` → `IterationBudget` (line 17) | Thread-safe consume/refund counter; only `threading` dep. Attribution header retained. |
| `src/aegis_agent/runtime.py` (`AgentRuntime.run_turn`) | **REWRITE** | `agent/conversation_loop.py` → `run_conversation` (line 351; main loop ~807) | Follows the loop skeleton (guard → build context → call model → detect tool calls → execute → append results → loop) and the terminate-on-final-answer / max-iterations behaviour, decoupled from steering/plugins/persistence. |
| `src/aegis_agent/events.py` (`collect_response`) | **ADAPT** | `agent/chat_completion_helpers.py` (stream→pseudo-response rebuild, ~1900-1959) | "Fold streamed events into one uniform response" normalisation, reduced to a pure function. |
| `src/aegis_agent/context/builder.py` | **ADAPT** | `agent/conversation_loop.py` (per-call `api_messages` build, ~964-1058) | "Derived copy each call; source messages never mutated; strip internal fields; prepend system prompt". Attribution header retained. |
| `src/aegis_agent/tools/executor.py` | **ADAPT** | `agent/tool_executor.py`; `model_tools.handle_function_call`; `tools/registry.py:dispatch`; `agent/tool_dispatch_helpers.make_tool_result_message` (line 320) | Exception → `{"error": ...}` result; unknown tool → error; build `role=tool` message with name + tool_call_id. Attribution header retained. |
| `src/aegis_agent/tools/builtin/read_file.py` | **REWRITE** | `tools/file_tools.py` → `read_file_tool` (line 692) | Behaviour-equivalent minimal surface: `{path, offset, limit}` → `{content, total_lines, truncated}` / `{"error"}`; `LINE_NUM|CONTENT`. No dedup/redaction/guards. |
| `src/aegis_agent/tools/builtin/list_directory.py` | **REWRITE** | (no direct Hermes equivalent; closest is `search_files target=files`) | Aegis-specific: `{path}` → `{entries:[{name,type,size}]}` / `{"error"}`. |
| `src/aegis_agent/tools/builtin/run_shell.py` | **REWRITE** | `tools/terminal_tool.py` → `terminal_tool` (line 1775) | Behaviour-equivalent minimal surface: `{command, timeout, workdir}` → `{output, exit_code}` / `{"error"}`; timeout + 50k-char output cap. No PTY/background/watch. |
| `src/aegis_agent/sessions/memory_store.py` | **REWRITE** | `hermes_state.py` → `SessionDB.append_message` (line 2213), idempotency (~2310) | In-memory analogue of "one persisted logical message per client message ID" + monotonic `seq`. No SQLite this stage. |
| `src/aegis_agent/models/base.py`, `models/fake.py`, `tools/registry.py`, `tools/schemas.py`, `sessions/models.py`, `sessions/repository.py`, `exceptions.py`, `cli.py`, `__main__.py` | **original** | — | New abstractions / no Hermes derivation. |

## Stage 2 — OpenAI-compatible provider & streaming tool calls

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/models/stream.py` (`StreamAssembler`, `assemble_stream`) | **ADAPT** | `agent/chat_completion_helpers.py` (streamed tool-call assembly, ~1828-1891) | name **assigned** (not concatenated — providers resend full names), arguments **concatenated** across fragments, multi-slot by index, empty-`choices` usage chunks ignored. Decoupled into a client-independent pure module. Attribution header retained. |
| `src/aegis_agent/models/openai_compat.py` (`OpenAICompatibleProvider`) | **REWRITE** | `agent/chat_completion_helpers.py` (`interruptible_api_call`/`interruptible_streaming_api_call`) | OpenAI-compatible transport (`client.chat.completions.create`, streaming + one-shot), env-based config (`AEGIS_API_KEY`/`AEGIS_BASE_URL`/`AEGIS_MODEL`), error→`ModelProviderError`/`ModelTimeoutError` normalisation. Failover/rate-guard/credential-pool dropped. |
| `src/aegis_agent/events.py` (`collect_response` `is_cancelled`) | **ADAPT** | `agent/chat_completion_helpers.py` (interrupt-aware streaming consumption) | Added a per-event cancel poll so an interrupt aborts a stream mid-flight and discards the partial response. |
| `src/aegis_agent/runtime.py` (`run_turn` cancel/error wiring) | **REWRITE** | `agent/conversation_loop.py` (interrupt check + outer error handling) | Loop now routes `OperationCancelled` → `INTERRUPTED`, `ModelProviderError`/`ModelTimeoutError` → `ERROR`; still depends only on the `ModelProvider` Protocol. |
| `src/aegis_agent/exceptions.py` (`ModelTimeoutError`, `OperationCancelled`) | **original** | — | New error types; `ModelTimeoutError` subclasses `ModelProviderError` for uniform handling. |
| `src/aegis_agent/cli.py` (`_select_provider`) | **original** | — | CLI-side backend selection (fake vs OpenAI-compatible) from flag + env. Runtime stays provider-agnostic. |
| `src/aegis_agent/tools/danger.py` (`DANGEROUS_PATTERNS`, `detect_dangerous_command`) | **ADAPT** | `tools/approval.py` → `DANGEROUS_PATTERNS` (line 367), `detect_dangerous_command` (line 543) | Generic destructive-pattern subset (recursive delete, mkfs/dd, SQL DROP/DELETE-no-WHERE/TRUNCATE, fork bomb, pipe-to-shell, git destructive, service/process kill). Hermes-specific entries (config/env paths, gateway/docker lifecycle, sudo-askpass) omitted. Attribution header retained. |
| `src/aegis_agent/tools/builtin/run_shell.py` (guardrail) | **ADAPT** | `tools/terminal_tool.py` (dangerous-command check, `force` internal flag, ~1976-2018) | `run_shell` now blocks dangerous commands by default; operator-only `ToolContext.allow_dangerous_shell` enables them (never the model), mirroring Hermes' internal `force`. |
| `src/aegis_agent/models/sanitize.py` (`sanitize_surrogates`, `repair_tool_call_arguments`) | **ADAPT** | `agent/message_sanitization.py` → `_SURROGATE_RE`/`_sanitize_surrogates` (lines 24-39), `_repair_tool_call_arguments`/`_escape_invalid_chars_in_json_strings` (lines 143-279) | Full-range (U+D800-DFFF) surrogate scrub → U+FFFD (fixes DashScope/Qwen contaminated-history crash); malformed tool-call arg JSON repair (Python `None`, trailing commas, unclosed structures, literal control chars) with `"{}"` last resort. Attribution header retained. |
| `src/aegis_agent/models/openai_compat.py` (`_to_wire_message`) | **ADAPT** | `agent/message_sanitization.py` → `_sanitize_messages_surrogates` (line 75) | All wire-bound strings scrubbed of surrogates before serialisation so contaminated history can be replayed. |
| `src/aegis_agent/models/stream.py` (`StreamAssembler.finish`) | **ADAPT** | `agent/chat_completion_helpers.py` (`_repair_tool_call_arguments` at finalisation, ~1921) | Assembled tool-call arguments pass the repair pass before emission. |
| `src/aegis_agent/tools/executor.py` (`_parse_arguments`) | **ADAPT** | `agent/message_sanitization.py` → `_repair_tool_call_arguments` (line 185) | Executor repairs malformed argument JSON before decoding instead of silently substituting `{}`. |
| `src/aegis_agent/env.py` | **original** | — | Minimal `.env` loader (no dependency); used by CLI + `OpenAICompatibleProvider.from_env`. |

## Stage 3 — live terminal UI & streaming output

The streaming *plumbing* (provider → `ModelEvent` → `collect_response`) was
already in place from Stage 2; the gap was that the runtime folded the whole
stream into a final `ChatResponse` and the CLI only printed the assembled
text after the turn ended.  This stage surfaces the in-flight stream to the
terminal via an observer seam, and adds a presentation layer adapted from
Hermes' UX.

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/events.py` (`collect_response` `on_event`) | **ADAPT** | `agent/chat_completion_helpers.py` (stream consumption) | Added a per-event `on_event` forwarder so a caller can observe the stream while it is still folded into one `ChatResponse`. Folding behaviour unchanged. |
| `src/aegis_agent/runtime.py` (`TurnEvent`, `run_turn` `on_event`) | **REWRITE** | `agent/conversation_loop.py` (`_vprint`/`_buffer_vprint`/`_safe_print` live feedback) | A runtime-level `TurnEvent` stream (TEXT_DELTA / TOOL_CALL / TOOL_RESULT / TURN_END / ERROR) emitted alongside the existing loop. The runtime stays free of any UI dependency; it only calls an optional `on_event` callback. |
| `src/aegis_agent/tui.py` (`_ThinkingRenderable`) | **ADAPT** | `agent/display.py` → `KawaiiSpinner` (lines 559-783) | Same kawaii data (braille frames + faces + "thinking verbs") but driven by a rich `Live` refresh loop via a `__rich__` renderable, instead of a daemon thread writing ``\r``. Skin engine + `patch_stdout` dropped. Attribution header retained. |
| `src/aegis_agent/tui.py` (`Tui`, banner, `_render_tool_result`) | **REWRITE** | `hermes_cli/banner.py` (welcome banner), `agent/display.py` (tool preview lines), `cli.py` (prompt_toolkit `PromptSession` input) | Own ASCII block-letter "AEGIS" logo + teal palette (Hermes uses gold caduceus) so the two are visually distinct. Input uses a single prompt_toolkit `PromptSession` (full line editing: ←/→ cursor, Ctrl-A/E, ↑/↓ history) with a non-TTY `input()` fallback; no full-screen `Application`/`HSplit`/completion widget. Output via `rich` `Console` (panels, styled text). |

## Notes

- Hermes files that were **inspected but not carried over** this stage
  (compression, SQLite store, leases, skills, multi-provider failover,
  auxiliary client) are scheduled for later stages and tracked in
  `docs/extraction-plan.md` §3 and §7.
- No Hermes file was modified.  All Aegis code lives under
  `/home/administrator/projects/aegis-agent`.

## Stage 4 — skills subsystem & dynamic prompt injection

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/context/system_prompt.py` | **ADAPT** | `agent/system_prompt.py` → `build_system_prompt_parts` (line 61) | Ordered-section assembly with empty-section omission; reduced from 3-tier (stable/context/volatile) to a flat `PromptContributor` list — the minimal seam for subsystem injection. |
| `src/aegis_agent/context/builder.py` (updated) | **ADAPT** | — | `ContextBuilder` now accepts a `SystemPromptBuilder` (backward-compatible: plain `str` wraps into one). `build()` calls `prompt_builder.build()` per turn, making the prompt dynamic. |
| `src/aegis_agent/skills/models.py` | **REWRITE** | `agent/skill_utils.py` → `SkillInfo` (skill metadata struct) | Lightweight dataclasses: `Skill` (full parse + frontmatter + body + directory) and `SkillMeta` (name/description/category for the tier-1 index). |
| `src/aegis_agent/skills/frontmatter.py` | **ADAPT** | `agent/skill_utils.py` → `parse_frontmatter` (line 88) | YAML `---` fence split + `yaml.safe_load`; malformed YAML → naive `key: value` scan fallback. Dropped the Hermes `CSafeLoader` variant. |
| `src/aegis_agent/skills/loader.py` | **ADAPT** | `agent/skill_utils.py` → `iter_skill_index_files` (line 632), `skill_matches_platform` (line 128) | Walks a user dir for `SKILL.md`; enforces name/description length caps; platform gating (macos→darwin, windows→win32); prunes excluded dirs; name-collision dedupe. Dropped: bundled skills, external-dir config, plugin namespaces, mtime-cached per-dir indices. |
| `src/aegis_agent/skills/router.py` | **ADAPT** | `agent/skill_commands.py` → `resolve_skill_command_key` (line 413), `build_skill_invocation_message` (line 432), `_build_skill_message` (line 160) | Slug normalisation + resolution + activation-message wrapper (activation note + body + directory + supporting-files listing + trailing instruction). Dropped: template-var substitution, inline-shell expansion, config resolution, platform-keyed command cache. |
| `src/aegis_agent/skills/prompt.py` | **ADAPT** | `agent/prompt_builder.py` → `build_skills_system_prompt` (line 1053) | Compact `<available_skills>` index grouped by category; progressive-disclosure instruction to call `skill_view`. Dropped: two-layer prompt-snapshot cache, conditional fallback/requires visibility rules. |
| `src/aegis_agent/skills/tools.py` | **ADAPT** | `tools/skills_tool.py` → `skills_list` (line 653), `skill_view` (line 828) | Two progressive-disclosure tools implementing Aegis's `Tool` Protocol: `skills_list` returns the tier-1 index as JSON; `skill_view` returns a skill's full body or a supporting file (path-traversal guarded). Dropped: prompt-injection scanner, credential/setup checks, collision reporting across dirs, plugin-namespace handling, usage telemetry. |
| `src/aegis_agent/runtime.py` (updated) | **REWRITE** | — | `with_defaults` now discovers skills, registers skill tools, builds a `SystemPromptBuilder` with `SkillsIndexContributor`, and exposes a `SkillRouter` for CLI slash routing. |
| `src/aegis_agent/cli.py` (updated) | **original** | — | `--skills-dir` / `--no-skills` flags; `/skill-name` slash routing in the REPL (`_maybe_route_skill`). |
| `src/aegis_agent/context/__init__.py` (updated) | **original** | — | Re-exports `SystemPromptBuilder`, `PromptContributor`, `DEFAULT_IDENTITY`. |

## Stage 5 — lightweight MCP client

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/mcp/schema_adapter.py` | **ADAPT** | `tools/mcp_tool.py` → `_normalize_mcp_input_schema` (line 3001), `_convert_mcp_schema` (line 3120), `sanitize_mcp_name_component` (line 3109); `tools/schema_sanitizer.py` → `strip_nullable_unions` (line 131) | Three-stage pipeline (definitions→$defs + nullable-union collapse + object-shape repair) inlined into one module. `strip_nullable_unions` inlined (~50 lines) to avoid depending on Hermes' `schema_sanitizer`. |
| `src/aegis_agent/mcp/config.py` | **ADAPT** | `tools/mcp_tool.py` → `_load_mcp_config` (line 2537), `_interpolate_env_vars` (line 2524) | YAML config (`mcp_servers:` key, `${ENV}` interpolation, defaults merge). Dropped: Hermes-specific config backend (`hermes_cli.config`), dotenv side-load. |
| `src/aegis_agent/mcp/client.py` | **ADAPT** | `tools/mcp_tool.py` → `_ensure_mcp_loop` (line 2442), `_run_on_mcp_loop` (line 2458), `_connect_stdio` / `_connect_http`, `_make_tool_handler` (line 2590) | Background daemon-thread event loop; cross-thread coroutine scheduling; stdio + Streamable HTTP connect; `call_tool` with text-block collection + credential-stripped errors. Dropped: interrupt-aware polling, OAuth recovery, session-expiry retry, circuit breaker, reconnect backoff, SSE transport, content-type preflight, image-block caching, MCP notification handler, sampling. |
| `src/aegis_agent/mcp/tools.py` | **REWRITE** | — | `MCPToolWrapper` implementing Aegis's `Tool` Protocol; `build_wrappers` factory. New code for Aegis's register-then-run pipeline (Hermes registers handler functions directly via the singleton registry). |
| `src/aegis_agent/mcp/guidance.py` | **original** | — | `MCPToolsGuidance` implementing Milestone A's `PromptContributor` Protocol. |
| `src/aegis_agent/runtime.py` (updated) | **REWRITE** | — | `with_defaults` now discovers and connects MCP servers, registers their tools, and adds the MCP guidance contributor. |
| `src/aegis_agent/cli.py` (updated) | **original** | — | `--mcp-config` / `--no-mcp` flags. |
