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

## Stage 6 — file-editing tools (write_file / patch / search_files)

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/tools/fuzzy_match.py` | **PORT** | `tools/fuzzy_match.py` → `fuzzy_find_and_replace`, `format_no_match_hint`, `find_closest_lines` | Near-verbatim port of the 9-strategy fuzzy find/replace chain (exact → line-trimmed → whitespace → indentation → escape → trimmed-boundary → unicode → block-anchor → context-aware). Self-contained (only `re`+`difflib`); escape-drift guard, re-indentation, `\t`/`\r` unescape and "did you mean?" hints all retained. Attribution header retained. |
| `src/aegis_agent/tools/fsutil.py` | **ADAPT** | `tools/file_operations.py` → `_detect_line_ending`, `_normalize_line_endings`, `_strip_bom`/`_has_bom`, `_atomic_write`, `_unified_diff`, `_is_write_denied`; `tools/path_security.py` → `has_traversal_component` | Generic file helpers: BOM detect/strip, line-ending detect/normalize, atomic temp+`os.replace` write, unified diff, sensitive-path refusal (generic subset), traversal check. The multi-backend shell `execute()` layer is replaced with direct Python I/O (`pathlib`/`os.replace`); a binary `read_text_raw` is added so CRLF/BOM survive the round-trip (text-mode I/O would translate `\r\n`→`\n`). Attribution header retained. |
| `src/aegis_agent/tools/builtin/write_file.py` | **REWRITE** | `tools/file_tools.py` → `write_file_tool`; `tools/file_operations.py` → `ShellFileOperations.write_file` | Behaviour-equivalent minimal surface: `{path, content}` → `{path, bytes_written, created, dirs_created}` / `{error}`; auto-create parents, full overwrite, atomic write, BOM/CRLF preservation, sensitive-path refusal. Dropped: cross-profile, file-state, lint/LSP, redaction. |
| `src/aegis_agent/tools/builtin/patch.py` | **ADAPT** | `tools/file_operations.py` → `ShellFileOperations.patch_replace` | Replace mode only: read → `fuzzy_find_and_replace` → write → **re-read verify**; unique-match requirement, `replace_all`, empty `new_string`=delete, "did you mean?" no-match hint, CRLF/BOM preserved. V4A multi-file diff mode (`patch_parser.py`/`patch_v4a`) intentionally NOT ported. |
| `src/aegis_agent/tools/builtin/search_files.py` | **REWRITE** | `tools/file_tools.py` → `search_tool`; `tools/file_operations.py` → `ShellFileOperations.search` | `{pattern, target=content\|files, path, file_glob, limit, offset, output_mode, context}` → `{total_count, matches\|files\|counts, truncated}` / `{error}`. Prefers `rg` on PATH, else a pure-`os.walk`/`re`/`fnmatch` fallback with equivalent behaviour; prunes hidden/VCS dirs. Dropped: search loop-breaking, redaction, backend routing. |
| `src/aegis_agent/tools/schemas.py` (updated), `tools/builtin/__init__.py` (updated) | **original** | — | Registered the three new tool definitions and wired them into `build_default_registry()`. |

## Stage 7 — terminal & background-process tools (terminal / process)

`terminal` **replaces** the Stage-1 `run_shell` (removed this stage): it is the
full-featured execution tool (foreground + background launch), and `process`
manages the background processes it spawns.

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/tools/process_registry.py` | **ADAPT** | `tools/process_registry.py` → `ProcessRegistry`, `ProcessSession`, `spawn_local`, `_reader_loop`, `_reconcile_local_exit`, `poll`/`read_log`/`wait`/`kill_process`/`write_stdin`/`submit_stdin`/`close_stdin`/`list_sessions`, `_prune_if_needed` | Local-only port of the in-memory background-process registry: `_running`/`_finished` dicts + lock, per-session rolling 200KB `output_buffer` + daemon reader thread, `subprocess.Popen` + `os.setsid` process group, TTL + LRU pruning, orphaned-pipe reconcile fix, psutil / `taskkill /T /F` tree-kill, ANSI strip. Dropped: sandbox backends (`spawn_via_env`), ptyprocess PTY, watch-pattern rate limiting + global circuit breaker, gateway notification routing, crash-recovery checkpoint file, per-profile HOME isolation, provider-secret env scrubbing. Shell wrapper simplified to `/bin/sh -c` / `cmd /c`. Attribution header retained. |
| `src/aegis_agent/tools/builtin/terminal.py` | **REWRITE** | `tools/terminal_tool.py` → `terminal_tool` | `{command, timeout, workdir, background, pty}` → foreground `{output, exit_code, error}` (timeout → exit_code 124, head/tail truncation, grep/diff exit-code-meaning note, server-command → background hint) or background `{session_id, pid, ...}`. Dangerous-command guardrail retained (operator-only `allow_dangerous_shell`). Dropped: sandbox backends, approval/`force`, watch patterns, notify_on_complete framing. |
| `src/aegis_agent/tools/builtin/process.py` | **REWRITE** | `tools/terminal_tool.py` process actions (delegating to `process_registry`) | Thin wrapper mapping `action ∈ {list, poll, log, wait, kill, write, submit, close}` onto the shared `ProcessRegistry`; unknown id → `{status: "not_found"}`. |
| `src/aegis_agent/tools/builtin/run_shell.py` | **removed** | — | Superseded by `terminal`. Its schema/registration and `RunShellTool` references removed; `tools/schemas.RUN_SHELL` deleted. |
| `src/aegis_agent/models/fake.py`, `tui.py`, `cli.py`, `tools/danger.py`, `tools/registry.py` (docstrings/help) | **original** | — | Updated `run_shell` → `terminal` references (demo shorthand, result renderer, CLI help, guardrail docstrings). |
| `src/aegis_agent/tools/schemas.py`, `tools/builtin/__init__.py` (updated) | **original** | — | `TERMINAL` + `PROCESS` schemas; `build_default_registry()` constructs one shared `ProcessRegistry` injected into both `TerminalTool` and `ProcessTool`. |

## Stage 8 — web tools (web_search / web_extract)

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/tools/web/url_safety.py` | **ADAPT** | `tools/url_safety.py` → `is_safe_url`, `_is_blocked_ip`, `_ALWAYS_BLOCKED_IPS`/`_ALWAYS_BLOCKED_NETWORKS`, `_BLOCKED_HOSTNAMES`, `_CGNAT_NETWORK` | The SSRF gate, verbatim in substance: http/https scheme allowlist, cloud-metadata + link-local always-blocked floor (with IPv4-mapped IPv6 variants), private/loopback/reserved/multicast/CGNAT blocking, fail-closed on DNS error. Dropped: the `security.allow_private_urls` config/env toggle + cache, the QQ trusted-host allowlist, the async wrapper. Aegis always enforces private-IP blocking; synchronous only. Attribution header retained. |
| `src/aegis_agent/tools/web/backends.py` | **original** | — | New, dependency-light backend seam (Hermes dispatches through a plugin registry of paid SDKs). Search: DuckDuckGo via `ddgs` by default (no key), Tavily/Exa via direct `httpx` REST when `TAVILY_API_KEY`/`EXA_API_KEY` is set. Extract: `httpx` GET + `trafilatura` HTML→markdown, base64-image stripping, tag-strip fallback. Monkeypatchable module-level functions. |
| `src/aegis_agent/tools/builtin/web_search.py` | **REWRITE** | `tools/web_tools.py` → `web_search_tool` | `{query, limit=5}` → `{results: [{title, url, description, position}], count, backend}` / `{error}`. Never raises. |
| `src/aegis_agent/tools/builtin/web_extract.py` | **REWRITE** | `tools/web_tools.py` → `web_extract_tool` | `{urls: [...]}` (≤5) → `{results: [{url, title, content, error}], count}`. Per-URL SSRF gate before fetch; per-URL errors inline. Hermes' optional LLM summarisation not ported. |
| `pyproject.toml` | **original** | — | Added `httpx` (core dep) and a `web` optional extra (`ddgs`, `trafilatura`), mirroring the existing `mcp` extra pattern. |

## Stage 9 — skill management tool (skill_manage)

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/skills/install.py` | **ADAPT** | `tools/skills_hub.py` → `HubLockFile`, `record_install`/`record_uninstall`/`list_installed`, `install_from_quarantine`, `uninstall_skill`, `bundle_content_hash`, `_validate_skill_name`, `_resolve_lock_install_path`, `_is_path_redirect` | Core install/uninstall/update primitives. Lock file (`<skills_dir>/.aegis-lock.json`) provenance model, two-layer symlink/junction rejection, `rmtree` guard (must be within skills_dir AND must not be skills_dir root), `_dir_hash` (sorted-relpath + content SHA-256). Install source: local directory copy, or URL fetch (single SKILL.md). Update = re-fetch source + hash-compare + reinstall. Dropped: quarantine, scan verdict/trust level, multi-source hub routing, audit log, website policy, SSRF redirect chaining, provenance signing, telemetry. Attribution header retained. |
| `src/aegis_agent/skills/manage_tool.py` | **REWRITE** | `tools/skill_manager_tool.py` → `skill_manage` (hub actions) | `{action ∈ install\|uninstall\|update\|list, source?, name?, force?}` → `{success, ...}` / `{success:false, error}`. Delegates to `skills/install.py`; calls `loader.discover(force=True)` after mutations so the index refreshes. Registered in `runtime.with_defaults` (enable_skills branch) alongside `skills_list` / `skill_view`. |
| `src/aegis_agent/runtime.py` (updated) | **original** | — | Registered `SkillManageTool` in the `enable_skills` branch. |

## Stage 10 — context compression pipeline (three-phase trimming & per-round summary)

Whole-unit port of the cohesive four-file prototype in `hermes-agent/ctx-compress-opt/`
(the dependency closure of `compress.py`).  The algorithm core still operates on
OpenAI-shaped dicts, byte-faithful to the prototype; the only new code is the
`Message`↔dict boundary layer and the public entry point.  Dropped prototype dead
code (documented in the module header): `_handle_single_round_overflow_v1`/`_v2`,
`_truncate_oversized_tools` (never called), and the `__main__` self-test block.

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/context/compress_config.py` | **PORT** | `ctx-compress-opt/compress_config.py` (entire file) | Near-verbatim: every threshold / placeholder / marker string / compactable-tool allowlist. Tool names absent from Aegis (browser/vision/amap MCP) intentionally retained for upstream parity. Attribution header retained. |
| `src/aegis_agent/context/tool_budget.py` | **PORT** | `ctx-compress-opt/tool_budget.py` (entire file) | Phase A: two-level tool-result budget (per-result + per-turn aggregate), persist-to-disk + preview replacement, cross-turn `ContentReplacementState`, read_file read-back loop guard + third-level hard truncate. Stdlib-only by design. Attribution header retained. |
| `src/aegis_agent/context/micro_compact.py` | **PORT** | `ctx-compress-opt/micro_compact.py` (entire file) | Phase B: threshold-triggered progressive micro-compaction (dedup → informative one-line summaries → JSON-aware arg truncation → history reasoning clear). Only the import style adapted (flat same-dir → absolute package imports). Attribution header retained. |
| `src/aegis_agent/context/compress.py` | **PORT + ADAPT** | `ctx-compress-opt/compress.py` → `_compress_context`, `_handle_single_round_overflow`, `_split_into_rounds`, `_is_complete_round`, `_serialize_round_for_summary`, `_summarize_round`, `_estimate_tokens` | Three-phase pipeline + single-round overflow fallback. Adaptations: stdlib `logging` (was `configs.config`/`utils.log_utils`); synchronous `ModelProvider.stream()` + `collect_response` for summaries (was async `llm_provider.chat(..., model, temperature, max_tokens)` — sampling params now provider-owned); injectable `storage_dir` defaulting to `~/.aegis/tool-result-cache` (was hard-coded `ROOT_PATH/tool-budget-cache`); regex-only redaction (dropped optional `agent.redact`); absolute package imports; `len>1`/None guards around the runtime-context-marker probe (prototype could `IndexError`). Attribution header retained. |
| `src/aegis_agent/context/compress.py` (`message_to_dict`, `dict_to_message`, `compress_context`, `estimate_tokens`) | **original** | — | Aegis boundary layer: `Message`↔OpenAI-dict converters + public entry point. Keeps the ported dict-based core untouched and enforces the "source messages never mutated" invariant. |
| `src/aegis_agent/context/__init__.py` (updated) | **original** | — | Exports `compress_context` / `estimate_tokens` / `message_to_dict` / `dict_to_message`. |
| `tests/test_context_compress.py` | **original** | — | 17 deterministic tests over all three phases + single-round fallback + boundary adapters; fake/exploding providers only. |

## Stage 11 — wire compression into the Agent Loop + reasoning_content plumbing

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/runtime.py` (compression wiring) | **REWRITE** | `agent/conversation_loop.py` (per-call `api_messages` build feeding the model) | `run_turn` compresses the *derived* context before each model call when `context_token_budget` is set; per-session `ContentReplacementState` held in `_budget_states` (frozen cross-turn replacement decisions → byte-stable prompt prefix); source history untouched. |
| `src/aegis_agent/context/compress.py` (`budget_state` / `summary_provider` params) | **original** | — | Threads the cross-turn budget state and an optional deterministic summary provider through the ported pipeline (prototype used one-shot state and the main LLM client). |
| `src/aegis_agent/models/base.py` (`Message.reasoning_content`, `ChatResponse.reasoning_content`) | **ADAPT** | `ctx-compress-opt/compress.py` (`reasoning_content` dict field handling) | The compression pipeline accounts for/clears chain-of-thought; the field is now carried by Aegis messages end-to-end so those steps are live. Persisted, but never echoed onto the wire. |
| `src/aegis_agent/events.py` (`REASONING_DELTA`), `models/stream.py` (capture), `models/openai_compat.py` (one-shot capture + `temperature` param), `models/fake.py` (`FakeReply.reasoning`) | **ADAPT** | Hermes reasoner providers' `delta.reasoning_content` streaming behaviour | Reasoning deltas folded into `ChatResponse.reasoning_content`; `OpenAICompatibleProvider` gains `temperature`/`max_tokens` pinning (used to build the deterministic summary provider). Also fixed the pre-existing `sanitize_surrogates(str \| None)` mypy error in `_to_wire_message`. |
| `src/aegis_agent/cli.py` (`--context-max-tokens` / `--no-compress`, `_build_summary_provider`) | **original** | — | CLI-side budget config (env `AEGIS_CONTEXT_MAX_TOKENS`, default 120_000) and deterministic summary provider construction (`temperature=0.0`, `max_tokens=SUMMARY_MAX_TOKENS`, non-streaming). |
| `src/aegis_agent/context/compress_config.py` (`list_directory` in MICRO list) | **original** | — | Aegis builtin added to the compactable set; upstream-only names retained for parity. |
| `tests/test_runtime_compression.py` | **original** | — | 10 tests: loop wiring, per-session state stability/isolation, reasoning capture/persistence/wire-stripping, summary-provider seam. |

## Stage 12 — session recovery (SQLite store + snapshot fast-resume + cross-process leases)

Ports the user's own Hermes session-recovery commits (`5a51f55` message-level
idempotent persistence, `181e078` session_snapshots fast resume, `03e5adc`
pluggable leases).  Design reference: `hermes_state_核心机制.md`.

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/sessions/sqlite_store.py` | **ADAPT** | `hermes_state.py` → connection setup, `apply_wal_with_fallback`, `_execute_write`, `_try_wal_checkpoint`, `append_message` (idempotency), `write_snapshot`/`load_latest_snapshot`/`get_messages_after_seq`/`resume_conversation`, `get_history_version`/`bump_history_version`, `try_acquire_session_lease`/`renew_session_lease`/`release_session_lease`/`is_session_lease_owner`/`get_session_lease_info` | Transaction machinery and recovery algorithms ported 1:1; schema trimmed to the Aegis `Message` fields (dropped FTS5, titles/archives, rewind, token billing, platform/codex columns). Implements the `SessionRepository` Protocol and stores `Message` dataclasses instead of OpenAI dicts. `parent_session_id` lineage walking NOT ported (Aegis compression never forks sessions). `history_version` kept for future history-rewrite invalidation. `list_messages` internally uses the snapshot+tail fast path. Attribution header retained. |
| `src/aegis_agent/sessions/lease.py` | **PORT** | `session_lease.py` (entire file, 573 lines) | Near-verbatim: backend interface, SQLite/Redis backends, heartbeat manager with `on_lost` circuit breaker, backend factory. Adapted: `AEGIS_*` env vars, `aegis:session_lease:` Redis key prefix, SQLite backend wraps `SQLiteSessionRepository`. Attribution header retained. |
| `src/aegis_agent/cli.py` (updated) | **original** | — | `--db`/`--ephemeral`/`--resume`/`--no-lease`/`--snapshot-every-n` flags; `_build_repository`, `_start_lease` (lease-loss → `run_turn` interrupt event), `_maybe_snapshot` after each turn; resume display line. |
| `src/aegis_agent/sessions/__init__.py` (updated) | **original** | — | Exports the SQLite store and lease components. |
| `pyproject.toml` | **original** | — | New `redis` optional extra (lease backend only). |
| `tests/test_sessions_sqlite.py`, `tests/test_session_lease.py` | **ADAPT** | `tests/run_agent/test_idempotent_persistence.py`, `tests/hermes_state/test_session_snapshots.py`, `tests/test_session_lease.py` (behavioural reference) | Same invariants re-expressed against the Aegis Protocol/`Message` surface: idempotency, monotonic seq, isolation, crash durability, snapshot==full-replay + corruption fallbacks, resume-continue no-dup, single lease winner incl. 8-way race, TTL takeover, stale-owner rejection, heartbeat/on_lost/switch, Redis via in-memory fake client. Dual-process subprocess tests (hermes `tests/lease_worker.py`) replaced by multi-connection in-process contenders. |

## Stage 13 — dynamic system-prompt sections (identity / behaviour / model / environment)

Fills in the actual prompt content behind the existing `SystemPromptBuilder` +
`PromptContributor` seam (Milestone A).  Hermes composes its prompt in three
tiers (stable / context / volatile) via `build_system_prompt_parts`; Aegis
reproduces the subset of sections it has subsystems for and drops the rest
(memory, `session_search`, USER.md, SOUL.md, context files, kanban,
computer-use, platform hints, Nous branding + docs URL).

| Aegis file | Relationship | Hermes source → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/context/system_prompt.py` (`DEFAULT_IDENTITY`) | **ADAPT** | `agent/prompt_builder.py` → `DEFAULT_AGENT_IDENTITY` | De-branded: same helpful/direct/uncertainty-admitting/targeted persona, Nous branding and docs-site pointer removed. Symbol name unchanged so existing imports/tests are unaffected. |
| `src/aegis_agent/context/prompt_sections.py` (`TaskCompletionContributor`, `ToolUseEnforcementContributor` + their text) | **ADAPT** | `agent/prompt_builder.py` → `TASK_COMPLETION_GUIDANCE`, `TOOL_USE_ENFORCEMENT_GUIDANCE` | Text adapted (de-branded). Rendered only when the tool registry is non-empty. Unlike Hermes these are NOT model-family-gated — the model-substring matching table (`TOOL_USE_ENFORCEMENT_MODELS`) is out of scope. |
| `src/aegis_agent/context/prompt_sections.py` (`ModelIdentityContributor`) | **ADAPT** | `agent/system_prompt.py` → alibaba model-name line | Renders "You are powered by the model named …" only when the provider exposes a truthy `model` (read via `getattr`); fake provider → nothing. |
| `src/aegis_agent/context/prompt_sections.py` (`EnvironmentContributor`, `_is_wsl`, `_WSL_ENVIRONMENT_HINT`) | **ADAPT** | `agent/prompt_builder.py` → `build_environment_hints` (local branch), `WSL_ENVIRONMENT_HINT`; `hermes_constants.py` → `is_wsl` | Host OS line (WSL/Windows/macOS/Linux) + home + cwd (the ToolContext cwd) + WSL filesystem hint. Remote-backend branch dropped (Aegis has no docker/ssh/modal terminals). |
| `src/aegis_agent/context/prompt_sections.py` (`TimestampContributor`) | **ADAPT** | `agent/system_prompt.py` → volatile timestamp line | Date-only "Conversation started: …" for prompt-cache byte-stability (Hermes PR #20451 rationale). Session-id/model/provider sub-lines dropped. |
| `src/aegis_agent/runtime.py` (`with_defaults` wiring) | **original** | — | Registers the five contributors on `prompt_builder` in Hermes' section order (identity → task-completion → tool-use → skills → mcp → model-identity → environment → timestamp). No change to `run_turn` or the source-of-truth invariant. |
| `src/aegis_agent/context/__init__.py` (updated) | **original** | — | Re-exports the new contributors. |
| `tests/test_prompt_sections.py` | **original** | — | 14 tests: per-contributor render/drop conditions + composed-prompt ordering and exclusion of unsupported-subsystem terms (`memory`, `session_search`, `SOUL`, `Hermes`). |

## Stage 14 — personal long-term memory (Stage 1: storage format + MEMORY.md index injection + behaviour prompt)

Reproduces the *file-system + prompt-driven* half of Claude Code's Auto Memory
(design reference: `Claude-Code/docs/08-memory.md`, i.e. `src/memdir/*`).  Only
the **personal (user-level) scope** and the three read-side pieces are built;
relevance recall (`findRelevantMemories`), background extraction
(`extractMemories`), embeddings and project/team memory are explicitly deferred
to later stages.  Source here is Claude Code (not Hermes), so this is a
behavioural re-implementation, not a code port.

| Aegis file | Relationship | Claude Code reference → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/memory/paths.py` | **ADAPT** | `src/memdir/paths.ts` → `getAutoMemPath` / dir layout | Personal-scope only: `<home>/USER.md` + `<home>/memory/{MEMORY.md,*.md}`. Home = `$AEGIS_HOME` → `~/.aegis`; `$AEGIS_MEMORY_DIR` overrides the memory dir directly. Dropped: settings-driven override chain, git-root canonicalisation, project/team dirs, the SECURITY `~/.ssh` guard (no untrusted project-settings source in Aegis yet). |
| `src/aegis_agent/memory/types.py` | **ADAPT** | `src/memdir/memoryTypes.ts` → the four kinds | `MemoryType` enum = `user`/`feedback`/`project`/`reference`, verbatim taxonomy so project scope can reuse it. `parse()` is tolerant (unknown/missing → `None`). The full eval-tuned behaviour prose lives in `prompt.py` instead. |
| `src/aegis_agent/memory/store.py` | **ADAPT** | `src/memdir/memdir.ts` → `truncateEntrypointContent`; `memoryScan.ts` frontmatter read | `truncate_entrypoint_content` = 200-line + 25 KB dual cap with a truncation notice (UTF-8 byte-safe). `load_user_profile`/`load_memory_index` read+truncate (missing/empty → `None`, never raises). `parse_memory_file` reuses the skills frontmatter parser → `MemoryEntry` (exposed for a later recall stage; nothing auto-loads bodies this milestone). |
| `src/aegis_agent/memory/prompt.py` | **ADAPT** | `src/memdir/memdir.ts` → `loadMemoryPrompt`; `memoryTypes.ts` behaviour text; `utils/claudemd.ts` (:979) index injection | Three `PromptContributor`s: `MemoryBehaviorContributor` (static rules — what memory is, per-kind what-to-store, what-NOT-to-store, stale-history/verify-before-use, current-instruction-wins/ignore semantics), `UserProfileContributor` (USER.md, distinct header), `MemoryIndexContributor` (MEMORY.md, labelled "index, not the bodies"). Re-rendered each build; missing files → `None`. |
| `src/aegis_agent/memory/__init__.py` | **original** | — | Package doc + re-exports; states the deferred scope explicitly. |
| `src/aegis_agent/runtime.py` (`with_defaults` wiring) | **original** | — | `enable_memory`/`memory_home` params; adds the three memory contributors after the MCP note and before model/env/timestamp; `startup_info["memory"]` = 1 when USER.md or MEMORY.md loaded. No change to `run_turn` or the source-of-truth invariant. |
| `src/aegis_agent/cli.py`, `src/aegis_agent/tui.py` (updated) | **original** | — | `--no-memory` flag; startup panel shows `Memory: on/none`. |
| `tests/test_memory.py` | **original** | — | 27 tests: path resolution/overrides, four-kind parse, frontmatter parse, line/byte truncation, present/absent injection, USER.md vs MEMORY.md distinct semantics, runtime wiring + `--no-memory`, and session-persistence-unaffected. |
| `tests/test_prompt_sections.py` (updated) | **original** | — | Exclusion test narrowed to `session_search`/`SOUL`/`Hermes` (memory is now a real subsystem); added `test_memory_section_present`. |

## Stage 15 — memory recall + background extraction (Stage 2/3, personal scope)

Reproduces Claude Code Auto Memory's two *dynamic* channels — relevance recall
(`findRelevantMemories`) and background extraction (`extractMemories`) — still
personal scope only, no embeddings / project / team / autoDream.  Both reuse
Aegis's existing `ModelProvider` Protocol as the side-query transport (no new
provider), and both are strictly best-effort.

| Aegis file | Relationship | Claude Code reference → symbol | Notes |
|---|---|---|---|
| `src/aegis_agent/memory/scan.py` | **ADAPT** | `src/memdir/memoryScan.ts:scanMemoryFiles` / `formatMemoryManifest` | Metadata-only scan (filename/name/description/type/mtime), reads just the leading ~40 lines (never bodies), excludes `MEMORY.md`, caps at 200 newest-first. A bad/missing file is skipped, never fatal. |
| `src/aegis_agent/memory/retriever.py` | **ADAPT** | `src/memdir/findRelevantMemories.ts`; `utils/attachments.ts:getRelevantMemoryAttachments`/`collectSurfacedMemories` | Manifest → side query (JSON `{"files":[…]}` ≤5, unsure→skip) → keep only filenames present in the manifest (rejects invention/`../`) → read selected bodies under 4 KB/file + 12 KB/total caps → `render_recall_block` (`## Relevant memories`, per-file `file=`/`type=` tags). |
| `src/aegis_agent/memory/sidequery.py` | **original** | — | Shared one-shot helper: `provider.stream()` folded via `collect_response`, then tolerant JSON-object extraction (bare / fenced / embedded). Any failure → `None`. |
| `src/aegis_agent/memory/prompt.py` (`RelevantMemoriesContributor`) | **ADAPT** | `utils/attachments.ts` attachment injection (behaviour) | Stateful contributor; `set_block`/`clear` per turn. Injection is via the system-prompt rebuild (Aegis has no attachment message), so `run_turn`'s loop body is unchanged and source history is untouched. |
| `src/aegis_agent/memory/extractor.py` | **ADAPT** | `services/extractMemories/extractMemories.ts` + `prompts.ts` | Cursor over `client_msg_id` (unknown cursor → last-12 fallback, never "nothing" or "everything"); side query returns `{"actions":[{action,filename,type,name,description,content}]}`; `_coerce_action` validates filename + personal-only type (rejects `project`/unknown) + non-empty content. `apply_actions` routes every write through the path-safe store and rebuilds the index once. No forked sub-agent (structured action → store, no Write tool granted to the extractor). |
| `src/aegis_agent/memory/store.py` (write side) | **ADAPT** | `extractMemories` write path + index sync; `FileWriteTool.ts` mtime staleness check | `render_memory_file` (frontmatter + body), `is_valid_memory_filename` (bare `*.md`, no `..`/sep/`MEMORY.md`/dotfile), `write_memory_file` (atomic + resolved-path-inside-dir check + mtime staleness check — refuse overwrite when the file changed since this process last read it), `rebuild_index` (derive `MEMORY.md` from files: sorted, deduped, idempotent; removes empty index). `record_read`/`get_last_read` maintain the process-local read-state cache. |
| `src/aegis_agent/memory/manager.py` | **ADAPT** | `query.ts:301/1599` (recall prefetch/collect), `stopHooks.ts:149` (post-final-reply) | `before_turn` *starts* a background recall (pool thread → `Future`, non-blocking); `collect_recall` is the non-blocking collect point (skip if not done → Claude's "didn't make it in time"); `after_turn` enqueues extraction onto a single serial worker thread (fire-and-forget, mirrors Claude's stash queue — no file lock, serialised writes); `drain` waits for in-flight work. Emits `memory.recall`/`memory.extract` `MemoryEvent`s. |
| `src/aegis_agent/runtime.py` (wiring) | **original** | — | `__init__` gains `memory_manager`; `run_turn` calls `before_turn` after persisting the user message, a non-blocking `collect_recall` before each `build(context)` inside the loop (so recall lands after a tool round, not on the first), and `after_turn` after a `FINAL_ANSWER`; `shutdown()` drains background work. `with_defaults` builds the manager when `enable_memory_recall`/`enable_memory_extract` are set. `startup_info` gains `memory_recall`/`memory_extract`. |
| `src/aegis_agent/cli.py` (updated) | **original** | — | `--memory-recall` / `--memory-extract` opt-in flags; `runtime.shutdown()` in the exit path drains in-flight recall/extract. |
| `tests/test_memory_recall.py` | **original** | — | 17 tests: empty/missing dir no-op, frontmatter scan, bad-file tolerance, 200-cap, 0..5 selection, invalid/`../` rejection, render block, context injection, history-not-modified, already-surfaced dedup, main-agent-write→skip. |
| `tests/test_memory_extract.py` | **original** | — | 14 tests: cursor new-messages/fallback, noop, create, update-over-duplicate, project-type rejection, unsafe-filename rejection, index rebuild + idempotency, no-outside-root write, extractor-failure isolation, mutex skip, filename validation. |
