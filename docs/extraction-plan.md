# Aegis Agent — Hermes 核心链路抽取计划 (Extraction Plan)

> 阶段 0 只读架构分析的产物。本文档基于 `/home/administrator/projects/hermes-agent` 的**真实源码**，
> 所有文件路径、函数名、行号均来自实际代码（行号可能随上游变化，但符号名稳定）。
>
> Hermes 当前版本：`hermes-agent 0.15.1`，许可证 **MIT**（© 2025 Nous Research）。

---

## 1. Hermes 真实调用链（按运行顺序）

下面是从 CLI 启动到最终回复落地的实际执行顺序。每一步给出：文件 → 函数/类 → 主要输入 → 主要输出 → 修改/维护的状态 → 直接依赖 → 下一调用节点。

### 1.0 入口

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 依赖 | 下一节点 |
|---|---|---|---|---|---|---|
| 0 | `pyproject.toml:257` `[project.scripts] hermes = "hermes_cli.main:main"` | argv | — | — | Typer/CLI | `hermes_cli/main.py:main` |
| 1 | `hermes_cli/main.py:main` | argv / flags | — | 解析 `--resume`、model、provider | CLI config | `cli.py:HermesCLI.__init__` |
| 2 | `cli.py:3128 HermesCLI.__init__(resume=…)` | resume id, model config | CLI 实例 | `self.session_id = resume`; `self._resumed = True` (3422-3424) | `hermes_state.SessionDB` | `_preload_resumed_session` / `run()` |
| 3 | `cli.py:5650 _preload_resumed_session` （或 `run()` 5116-5163) | session_id | restored history | 租约 + history | SessionDB.resume | 构建 agent |

### 1.1 用户输入进入运行时

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 依赖 | 下一节点 |
|---|---|---|---|---|---|---|
| 4 | `cli.py` prompt_toolkit 交互循环 (imports 56-71)；`run()` | 用户敲入一行 | `user_input: str` | `self.conversation_history` | prompt_toolkit | `_resolve_turn_agent_config` |
| 5 | `cli.py:5217 self.agent = AIAgent(...)` | model/api_key/base_url/provider/api_mode, max_iterations, toolsets, session_id, session_db, callbacks, ephemeral_system_prompt | AIAgent 实例 | agent 缓存于 `self.agent` | `run_agent.AIAgent` | `run_conversation` |
| 6 | `cli.py:12834 self.agent.run_conversation(user_message=agent_message, conversation_history=self.conversation_history[:-1], stream_callback=…, task_id=self.session_id, persist_user_message=…)` | 用户消息 + 既有历史 | result dict | conversation_history 追加 | run_agent 转发器 | 内部循环 |
| 7 | `run_agent.py:4960 AIAgent.run_conversation` （转发器） | 同上 | 同上 | — | conversation_loop | `run_conversation` 主体 |

### 1.2 Agent Loop（多轮 model↔tool）

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 依赖 | 下一节点 |
|---|---|---|---|---|---|---|
| 8 | `agent/conversation_loop.py:351 run_conversation(agent, user_message, system_message, conversation_history, task_id, stream_callback, persist_user_message)` | user msg + history + system | result dict | `messages`, `agent.iteration_budget=IterationBudget(max)` (485) | 整个 runtime | 主循环 |
| 9 | 主循环 `conversation_loop.py:807 while (api_call_count < max_iterations and iteration_budget.remaining > 0)` | loop state | 每轮 = 1 次模型调用 ± 工具轮 | `api_call_count`, `_turn_exit_reason` | IterationBudget | 守卫 |
| 10 | 循环顶部守卫 (809-845)：租约熔断 `_session_lease_lost` (814-822)、`_interrupt_requested` (825-830)、`api_call_count += 1` (832)、`iteration_budget.consume()` (841) | loop state | continue/break | budget 消耗 | session_lease, IterationBudget | 构建上下文 |
| 11 | 构建 API 上下文 (964-1058)：`api_msg = msg.copy()`，注入 system、剥离 `reasoning`/`_persist_uid`/`finish_reason`、cache_control 断点 | `messages`（源） | `api_messages`（派生副本） | **messages 不变** | system_prompt, context_compressor | 调用模型 |
| 12 | 调用模型 重试循环 `while retry_count < max_retries` (1187)；`agent._interruptible_streaming_api_call(...)` / `_interruptible_api_call(...)` (1354-1361) | api_kwargs | `response` | — | chat_completion_helpers | 解析响应 |
| 13 | 检测工具调用 `if assistant_message.tool_calls:` (3756)；`_repair_tool_call`、`_deduplicate_tool_calls` (3914)、`_build_assistant_message` (3918) | response | assistant_msg | — | tool_dispatch | 执行工具 |
| 14 | `messages.append(assistant_msg)` (3973) 然后 `agent._execute_tool_calls(assistant_message, messages, task_id, api_call_count)` (3988) | tool_calls | tool results | messages 增长 | tool_executor | 追加结果 |
| 15 | 追加结果（执行器内部）：`messages.append(make_tool_result_message(name, content, tc.id))` (`tool_executor.py:668/1199`) | tool result | `role:"tool"` 消息 | messages 增长 | tool_dispatch_helpers | 持久化 |
| 16 | `agent._persist_incremental(messages)` (3995，崩溃耐久 flush) → guardrail 熔断 (3997-4018) → 压缩检查 `_compress_context` (4076-4086) → `agent._session_messages = messages` (4089) | messages | 持久化 | session DB 写入 | hermes_state | 下一轮 `continue` (4092) |

### 1.3 正常结束（模型返回最终文本、无工具调用）

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 下一节点 |
|---|---|---|---|---|---|
| 17 | `conversation_loop.py:4094 else:` “No tool calls — final response”；`final_response = assistant_message.content` (4096)；剥 think、`_build_assistant_message` (4406)、`messages.append(final_msg)` (4423)、`_turn_exit_reason="text_response"` (4425)、`break` (4428) | final text | 退出循环 | messages 追加 | 后处理 |
| 18 | 后处理 (4558-4809)：`completed` (4559)、`_save_trajectory` (4567)、`_cleanup_task_resources` (4570)、`agent._persist_session(messages, conversation_history)` (4576-4577)、`_maybe_write_snapshot` (4582)、结果 dict (4765) | messages | result dict | session DB + 快照 | 返回 CLI |

### 1.4 模型调用与流式

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 依赖 |
|---|---|---|---|---|---|
| 19 | `agent/chat_completion_helpers.py:527 build_api_kwargs(agent, api_messages)` | agent.model/tools/max_tokens | api_kwargs | — | ProviderProfile hooks |
| 20 | 非流式 `interruptible_api_call`（内 `_call` 184；`create` 在 230）：`request_client.chat.completions.create(**api_kwargs)` | api_kwargs | response | — | OpenAI client |
| 21 | 流式 `interruptible_streaming_api_call`（内 `_call_chat_completions` 1674；`create` 在 1730）：`stream = create(stream=True, stream_options={"include_usage": True})` | api_kwargs | chunk 迭代器 | — | OpenAI client |
| 22 | 流式消费 `for chunk in stream:` (1758)；content delta `content_parts.append(delta.content)` (1804)；reasoning delta (1796-1800)；`finish_reason` (1893)、`usage` (1897) | chunks | content_parts / reasoning_parts | 累积器 | — |
| 23 | **tool_call 片段拼接 (1828-1891)**：name **赋值** (1868)，arguments **拼接** `+=` (1870)，slot 按 remap `idx` (Ollama 索引修复 1835-1846) | tc_delta | tool_calls_acc | tool_calls_acc | — |
| 24 | 重建伪非流式响应 (1900-1959)：`SimpleNamespace` 拼装 `full_content = "".join(content_parts)`，使流式/非流式下游统一 | 累积器 | mock response | — | — |
| 25 | `run_agent.py:3458 _create_request_openai_client` → `agent_runtime_helpers.py:1343 OpenAI(**client_kwargs)`（可注入 Fake） | client_kwargs | OpenAI client | `agent._client_kwargs` (agent_init.py:837) | OpenAI SDK |

### 1.5 工具注册与执行

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 依赖 |
|---|---|---|---|---|---|
| 26 | `tools/registry.py:151 ToolRegistry`（单例 544）；`register()` (234-305) 存 `ToolEntry`（77-106）；`discover_builtin_tools()` (57-74) AST 扫描导入副作用注册 | 工具模块 | registry | `_generation` 递增 | 各 tools/*.py |
| 27 | `model_tools.py:264 get_tool_definitions` → `registry.get_definitions(tool_names)` (337-384) → `{"type":"function","function":schema}` | enabled/disabled | schema list | 缓存 | registry |
| 28 | `agent/tool_executor.py:690 execute_tool_calls_sequential` / `180 execute_tool_calls_concurrent`（ThreadPoolExecutor，`_MAX_TOOL_WORKERS=8` 52） | tool_calls | results | guardrails 状态 | registry, guardrails |
| 29 | 每调用：解析 args (`json.loads`) → guardrails `before_call` (301/762) → checkpoint preflight → 派发 | tool_call | result | — | — |
| 30 | `model_tools.py:861 handle_function_call` → `coerce_tool_args` (606) → `registry.dispatch(name, args, task_id)` (390-416) | name+args | result str(JSON) | — | registry |
| 31 | `registry.dispatch`：未知→`{"error":"Unknown tool"}`；async→`_run_async`；异常→`_sanitize_tool_error` (`model_tools.py:586`) 返回 `{"error":...}` | handler | JSON str | — | handlers |
| 32 | 结果→消息：`tool_dispatch_helpers.make_tool_result_message(name, content, tool_call_id)` (320) 含 `role/name/tool_name/content/tool_call_id`；高危工具结果 `_maybe_wrap_untrusted` (372) | result | tool 消息 | — | — |
| 33 | 超大结果三层：`tool_output_limits`（工具内 cap）、`tool_result_storage.maybe_persist_tool_result` (122，写 sandbox 文件 + `<persisted-output>` 预览）、`enforce_turn_budget` (181，聚合 200K) | content | 内联或外置 | sandbox 文件 | env |

### 1.6 System Prompt 与 Context 压缩

| 步骤 | 文件 → 函数 | 输入 | 输出 | 源/派生 | 依赖 |
|---|---|---|---|---|---|
| 34 | `agent/system_prompt.py:347 build_system_prompt(agent, system_message)` → `build_system_prompt_parts` (61)：三层 `stable`(84-280)/`context`(282-298)/`volatile`(300-338)，`\n\n` 连接 | agent + system_message | system 字符串 | 缓存于 `_cached_system_prompt` | memory, skills, context_files |
| 35 | API 消息数组：`conversation_loop.py:964-1058` 逐条 `msg.copy()` 构建 `api_messages` | messages | api_messages | **派生**；messages 为源 | system_prompt |
| 36 | 压缩触发 `context_compressor.py:728 should_compress(prompt_tokens)`：`tokens >= threshold_tokens`（默认 50% context_length，587/625-628）；preflight (610-665) + post-response (4054-4082) | tokens | bool | — | — |
| 37 | 压缩 `context_compressor.py:1827 compress(...)`：Phase1 `_prune_old_tool_results`(754)、Phase2 token 边界 `_find_tail_cut_by_tokens`(1745)、Phase3 `_generate_summary`(1217)、Phase4 `head + summary + tail` | messages | compressed（新列表） | **派生**；输入用 `.copy()` 不变异 | LLM (aux) |
| 38 | 源真相保留 `conversation_compression.py:271 compress_context`：`end_session(old,"compression")`(507) + `create_session(parent_session_id=old)`(517-522)；旧会话存全文，新会话存压缩延续 | messages, system | (compressed, new_system) | **原文入父会话** | hermes_state |

### 1.7 Session / Resume / Lease

| 步骤 | 文件 → 函数 | 输入 | 输出 | 状态 | 依赖 |
|---|---|---|---|---|---|
| 39 | `hermes_state.py:232 SCHEMA_SQL`（`_init_schema` 840 执行）；表：`sessions`(242)、`messages`(287)、`session_snapshots`(319)、`state_meta`(329)、`compression_locks`(334)、`session_leases`(348)、FTS(376-430) | — | SQLite schema | WAL (`apply_wal_with_fallback` 160) | sqlite3 |
| 40 | 仓储 `hermes_state.py:432 SessionDB`；写消息 `append_message` (2213)；单事务 `_execute_write` (639)，`next_seq = MAX(seq)+1` (2293)，INSERT (2304-2332)，同步递增 counters (2348-2358) | session_id, role, content, client_msg_id, … | row id | messages 表 | sqlite3 |
| 41 | 幂等：DB 层 `ON CONFLICT(session_id, client_msg_id) WHERE client_msg_id IS NOT NULL DO NOTHING` (2310-2311)；Python 层 `run_agent.py:1513 _flush_messages_to_session_db` 跳过已带 `_persist_uid` (1556)，否则铸 `uuid4().hex` (1589) 写后盖章 (1605) | messages | 去重写入 | `_persist_uid` | — |
| 42 | 快照写 `hermes_state.py:3026 write_snapshot`：读 active=1 行 → `last_seq=MAX(seq)` → CRC32 + zlib 压缩 → INSERT + 修剪保留 keep=3 (3091-3096) | session_id, history | snapshot row | session_snapshots | — |
| 43 | 快照载 `hermes_state.py:3105 load_latest_snapshot`：无/版本不符(3132)/解压失败(3154)/CRC 不符(3161)/JSON 失败(3169) → 返回 None（=全量回放） | session_id, history_version | {last_seq, messages} 或 None | — | — |
| 44 | 恢复 `hermes_state.py:3198 resume_conversation`：`resolve_resume_session_id`(3212) 取链叶 → 有快照 `get_messages_after_seq(resolved, last_seq)`(3228) 拼 tail，否则全量 `get_messages_as_conversation`(3225)；tail 失败回退全量(3235) | session_id | messages | — | — |
| 45 | 租约 `session_lease.py`：`SessionLeaseBackend`(84) 抽象；`SQLiteSessionLeaseBackend`(132) 走 SessionDB `try_acquire_session_lease`(1274 INSERT OR IGNORE + owner_token 比对）、`renew`(1324)、`release`(1358)、`is_owner`(1378)；`RedisSessionLeaseBackend`(214) `SET NX PX` + Lua(196/205)；`SessionLeaseManager`(377) 心跳线程 | session_id, owner_token | LeaseHandle | session_leases / Redis | sqlite3 / redis |

### 1.8 Skill 加载与注入

| 步骤 | 文件 → 函数 | 输入 | 输出 | 依赖 |
|---|---|---|---|---|
| 46 | 磁盘定义：`SKILL.md` + frontmatter（name/description/platforms/prerequisites/metadata.hermes），目录含 references/templates/scripts/assets；`SKILLS_DIR=~/.hermes/skills`（`tools/skills_tool.py:90`） | SKILL.md | — | yaml |
| 47 | frontmatter 解析 `agent/skill_utils.py:88 parse_frontmatter`（CSafeLoader + 兜底）；(doc: `tools/skills_tool.py:455 _parse_frontmatter`) | 文本 | (frontmatter, body) | yaml |
| 48 | 发现 `agent/skill_utils.py:632 iter_skill_index_files(skills_dir,"SKILL.md")`（os.walk 排序、排除 venv/VCS/cache） | skills_dir | SKILL.md 路径 | os |
| 49 | 注入路由（渐进披露，三路）：① 工具按需 `skills_list`(653)/`skill_view`(828) 注册为工具（1503/1538，toolset="skills"）；② slash 命令 `agent/skill_commands.py`；③ 启动预载 `--skills` `build_preloaded_skills_prompt`(479) 注入 system/preamble | skill name | 工具结果 / 消息 / system 块 | registry |

---

## 2. 功能模块清单

| 模块 | Hermes 负责文件 | 说明 |
|---|---|---|
| **CLI** | `hermes_cli/main.py`, `cli.py` (prompt_toolkit 交互、`AIAgent` 构造、`run_conversation` 调用） | 入口 + REPL + 渲染 |
| **Runtime** | `run_agent.py` (`AIAgent`：构造、client 创建、转发器、持久化转发） | 编排器，聚合所有 |
| **Agent Loop** | `agent/conversation_loop.py` (`run_conversation`, 主循环 807), `agent/iteration_budget.py` (`IterationBudget`), `agent/tool_guardrails.py` (`ToolCallGuardrails`) | 多轮循环 + 预算 + 熔断 |
| **Model Provider** | `providers/base.py` (`ProviderProfile` 声明式）, `providers/__init__.py` （注册/发现）, `agent/chat_completion_helpers.py` （实际调用）, `agent/agent_runtime_helpers.py` (`OpenAI(...)` 1343), `agent/auxiliary_client.py` （辅模型） | 声明 profile + 实际 client 分离 |
| **Tool Registry** | `tools/registry.py` (`ToolRegistry`, `ToolEntry`), `model_tools.py` （发现/定义/派发 façade) | 自注册 + schema 装配 |
| **Tool Executor** | `agent/tool_executor.py` （顺序/并发）, `agent/tool_dispatch_helpers.py` （并行门控、结果消息）, `tools/tool_result_storage.py`, `tools/tool_output_limits.py`, `tools/budget_config.py` | 编排 + 超大结果处理 |
| **Prompt 与 Context** | `agent/system_prompt.py` (system 构建）, `agent/prompt_builder.py`, `agent/context_compressor.py` （压缩）, `agent/conversation_compression.py` （压缩编排+旋转）, `agent/context_engine.py` | prompt 构建 + 压缩 |
| **Session** | `hermes_state.py` (`SessionDB`, `SCHEMA_SQL`, `append_message`, 快照） | SQLite 持久化 |
| **Resume** | `hermes_state.py` (`resume_conversation`, `resolve_resume_session_id`, `load_latest_snapshot`), `cli.py` (`_preload_resumed_session`) | checkpoint+tail 恢复 |
| **Skill** | `agent/skill_utils.py` （解析/发现）, `agent/skill_commands.py` (slash/预载）, `tools/skills_tool.py` (`skills_list`/`skill_view`) | 渐进披露 |
| **Error Handling** | `agent/error_classifier.py`, `conversation_loop.py` 外层 except (4430)、重试计数器， `tool_executor` 异常网， `model_tools._sanitize_tool_error` (586) | 分类 + 重试 + 兜底 |
| **Streaming** | `agent/chat_completion_helpers.py` (`interruptible_streaming_api_call`, chunk 循环 1758, tool_call 拼接 1828-1891, 伪响应重建 1900-1959) | 文本 + 工具参数流式 |

---

## 3. 迁移分类

图例：**PORT**（少量修改即可迁） · **ADAPT**（实现可复用但需解耦） · **REWRITE**（只参考行为重写） · **DROP**（轻量版不需要）。

### 3.1 Model Provider / Streaming

| 项 | 分类 | 原因 |
|---|---|---|
| `providers/base.py` `ProviderProfile` 声明式元数据 | **REWRITE** | 理念好（声明式 profile）但与 Hermes 的 `hermes_cli` 版本 UA、插件发现耦合。Aegis 只需要一个极小的 `ModelProvider` Protocol + 一个 OpenAI-compatible profile。重新设计接口。 |
| `chat_completion_helpers.py` tool_call 片段拼接 (1828-1891) | **ADAPT** | 核心行为（name 赋值 / args 拼接 / Ollama slot 修复 / JSON 修复）是通用且正确的，值得抽取为独立的 `stream_assembler` 模块。需剥掉对 `agent._fire_*` 回调、rate-limit 头、aux 的依赖。 |
| 伪非流式响应重建 (1900-1959) | **ADAPT** | “流式→统一响应对象”是有用的归一化。剥离 `SimpleNamespace` 杂散字段，产出 Aegis 的 `ChatResponse` dataclass。 |
| `interruptible_streaming_api_call` / `interruptible_api_call` 重试/中断脚手架 | **REWRITE** | 与 Hermes 的 failover、rate-guard、credential pool 深度耦合。Aegis 第一版只做单次调用 + 简单重试，参考其中断检查位置。 |
| `auxiliary_client.py` 辅模型路由 | **DROP** | 第一版无压缩/标题/视觉等辅任务。后续压缩阶段再考虑。 |
| 多家 Provider 适配（anthropic_adapter, bedrock, gemini, codex…） | **DROP** | 超出范围（CLAUDE.md §5 排除多 provider）。 |

### 3.2 Agent Loop

| 项 | 分类 | 原因 |
|---|---|---|
| 主循环结构 `conversation_loop.py:807`（守卫→建上下文→调用→检测工具→执行→追加→持久化→continue） | **REWRITE** | 结构是金标准，但函数体 4000+ 行、与 steer/prefill/插件钩子/kanban/voice 耦合。Aegis 重新实现一个 ~200 行的等价循环。 |
| `agent/iteration_budget.py` `IterationBudget` | **PORT** | 60 行、线程安全、零 Hermes 依赖（仅 `threading`）。几乎可直接迁移，改名即可。 |
| `agent/tool_guardrails.py` 重复工具调用熔断 | **ADAPT** | `ToolCallGuardrails`/`before_call` 理念可复用，但需去掉对 Hermes config、kanban 的依赖。作为 loop detection/circuit breaking 的基础。 |
| `_deduplicate_tool_calls` / `_repair_tool_call` | **REWRITE** | 行为简单（去重、修 JSON），但与 Hermes 的修复统计/持久化耦合。参考行为重写。 |
| 中断处理（`_interrupt_requested` 轮询） | **REWRITE** | 模式简单（loop 顶部检查 flag），Aegis 用更轻的 cancel event。 |

### 3.3 Tool Registry / Executor

| 项 | 分类 | 原因 |
|---|---|---|
| `tools/registry.py` `ToolRegistry`/`ToolEntry`/`register`/`get_definitions`/`dispatch` | **ADAPT** | 自注册 + thread-safe + `_generation` 缓存失效都是好设计。需剥掉 AST 自动发现、toolset 解析、check_fn TTL、MCP 桥。 |
| `model_tools.handle_function_call` + `coerce_tool_args` | **ADAPT** | 参数矫正（str→int/bool/list）对容错有用。剥掉插件钩子、ACP 审批。 |
| `agent/tool_dispatch_helpers.py` 并行门控 + `make_tool_result_message` | **ADAPT** | `make_tool_result_message`（320）与 `_maybe_wrap_untrusted`（372）几乎纯函数，去 Hermes 高危工具清单后可用。并行门控 `_should_parallelize_tool_batch` 参考实现。 |
| 超大结果三层（`tool_result_storage` 等） | **ADAPT** | “内联 → 外置 sandbox 文件 + 预览 → 聚合预算”是有价值的 oversized tool-result storage 行为。需去掉对 `env.execute` sandbox 的依赖，改用本地文件存储。 |
| `read_file` (`tools/file_tools.py` `read_file_tool` 692) | **REWRITE** | 行为明确（offset/limit 分页、行号格式、错误 JSON），但与 Hermes 的 `FileOperations`、denylist、secret redaction、file_state 耦合。Aegis 按 §4 最小 schema 重写。 |
| `search_files`（目录列举/内容搜索） | **REWRITE** | 同 read_file。Aegis 拆出独立 `list_directory`（Hermes 无此工具，列举折叠在 search_files 里）+ 可选 search。 |
| `terminal` (`tools/terminal_tool.py` `terminal_tool` 1775) | **REWRITE** | `run_shell` 语义清晰（command/timeout/workdir → output/exit_code），但 Hermes 版有 PTY、后台、watch、active-env 等大量耦合。Aegis 重写最简子进程版。 |
| 其余所有 Hermes 工具 | **DROP** | 超出最小范围。 |

### 3.4 Prompt / Context / Compression

| 项 | 分类 | 原因 |
|---|---|---|
| system prompt 三层（stable/context/volatile）理念 `system_prompt.py:347` | **REWRITE** | 分层利于 prefix cache，值得借鉴，但内容（SOUL.md、memory、平台提示）是 Hermes 特有。Aegis 重做一个简化 builder。 |
| `api_messages` 派生副本模式（`msg.copy()` 剥离内部字段） | **ADAPT** | “源 messages 永不被改、API 视图每轮重建”是核心不变量，必须保留。实现仅几行，但语义关键。 |
| `context_compressor.py` 分阶段压缩（prune→边界→摘要→重组） | **REWRITE** | 分层压缩是 Aegis 目标能力，但 96KB 实现与 Hermes aux client/会话旋转耦合。按阶段重写简化版。 |
| `conversation_compression.compress_context` 原文入父会话的保留策略 | **ADAPT** | “压缩只影响发给模型的 context、原文保留（父会话/独立存储）”是不变量。保留语义，用 Aegis 的 SessionRepository 实现旋转/保留。 |
| 滚动单级摘要 (`_generate_summary` + `_previous_summary`) | **REWRITE** | 行为清晰（增量更新 checkpoint），重写即可。 |
| `trajectory_compressor.py` 离线批处理 | **DROP** | 非运行时路径，是训练/评估工具。 |

### 3.5 Session / Resume / Lease

| 项 | 分类 | 原因 |
|---|---|---|
| `hermes_state.SCHEMA_SQL`（messages/sessions/snapshots/leases 表设计） | **ADAPT** | schema 设计（`seq` 单调、`client_msg_id` 幂等、`active` 软删、snapshot CRC+zlib、`history_version` 校验门）是经过实战的。Aegis 采用精简子集（去掉 FTS、billing、codex、platform 列）。 |
| `SessionDB.append_message` 单事务 + 幂等 `ON CONFLICT DO NOTHING` | **ADAPT** | 幂等写入是核心不变量。剥掉 `_reconcile_columns`、FTS trigger、counters 冗余。 |
| `_flush_messages_to_session_db` 的 `_persist_uid` 铸章模式 | **ADAPT** | “写成功才盖章、失败重试”是 message-level 幂等的关键。语义保留，挂在 Aegis 的 SessionRepository 实现里。 |
| 快照 checkpoint+tail `write_snapshot`/`load_latest_snapshot`/`resume_conversation` | **ADAPT** | “CRC/zlib/history_version 校验、任何损坏回退全量回放”直接对应 Aegis 的 checkpoint+tail recovery 目标。去掉对 SessionDB 内部的具体 SQL，面向接口。 |
| `session_lease.py` `SQLiteSessionLeaseBackend`/`RedisSessionLeaseBackend`/`SessionLeaseManager` | **ADAPT** | 几乎独立（21KB，依赖 sqlite3/redis + SessionDB 方法）。SQLite backend 可少量改造；Redis backend 需保留（Aegis 目标含 Redis lease）。心跳 + 熔断 + `switch_session` 保留。 |
| FTS5 全文/三元组索引 | **DROP** | 第一版不需要会话搜索。 |

### 3.6 Skill

| 项 | 分类 | 原因 |
|---|---|---|
| SKILL.md + frontmatter 定义 + `parse_frontmatter` | **ADAPT** | frontmatter 解析（CSafeLoader + 兜底）是小而通用的。去掉 Hermes 特有键处理。 |
| `iter_skill_index_files` 发现（排除 venv/VCS/cache） | **ADAPT** | 遍历+排除逻辑可复用。 |
| 渐进披露三路（工具 `skills_list`/`skill_view`、slash、预载） | **REWRITE** | 理念保留（元数据常可发现、正文按需/调用注入），但 Hermes 的注入与 telemetry、inline-shell、模板变量耦合。Aegis 第一版只做“轻量 Skill 加载 + 路由到上下文注入”的一路。 |
| inline-shell 展开、模板变量、配置注入 | **DROP** | 第一版不需要。 |

### 3.7 CLI

| 项 | 分类 | 原因 |
|---|---|---|
| `cli.py` 交互 REPL（prompt_toolkit） | **REWRITE** | 752KB、与 voice/banner/渲染/gateway 深度耦合。Aegis 用 Typer 写一个最简交互循环。 |
| `AIAgent` 构造参数组装 (`cli.py:5217`) | **REWRITE** | 参考其“运行时配置→构造”形态，但参数大幅减少。 |
| `--resume` CLI 接线 | **ADAPT** | 行为（resolve→acquire lease→resume_conversation→恢复 cwd）保留，挂到 Aegis CLI。 |

---

## 4. 最小运行范围（Aegis 第一版）

定义 Aegis Agent 第一版的最小垂直链路（端到端可测、不依赖真实付费模型）：

1. **交互式 CLI**（Typer）：读取一行用户输入 → 交给 runtime → 打印最终回复。支持 `--resume <session_id>`。
2. **FakeModelProvider**：实现 `ModelProvider` 接口的确定性假模型，可按脚本产出文本与工具调用（含流式分片），用于核心 Agent Loop 测试。
3. **一个 Agent Loop**：守卫（中断/预算）→ 构建 context（源 messages 的派生副本）→ 调用模型 → 检测 tool_calls → 执行 → 追加 tool 结果 → 循环；正常终止于无工具调用的最终文本；受 max_iterations 约束。
4. **Tool Registry**：注册（名称 → schema + handler）+ 按名查找 + 输出 OpenAI 格式 schema 列表。
5. **Tool Executor**：解析 args → 调用 handler → 异常转 `{"error":...}` → 生成 `role:"tool"` 消息。
6. **`read_file`**：`{path, offset=1, limit=500}` → `{content, total_lines, truncated}` / `{"error"}`。
7. **`list_directory`**：`{path}` → `{entries:[{name, type, size}]}` / `{"error"}`（Aegis 新增，Hermes 无独立工具）。
8. **`run_shell`**：`{command, timeout, workdir}` → `{output, exit_code}` / `{"error"}`。
9. **内存 SessionRepository**：`InMemorySessionRepository` 实现 `SessionRepository` 接口（append_message 幂等、list、snapshot），供测试与第一版运行；SQLite 实现在后续阶段。
10. **Fake Model 端到端测试**：完整跑通 输入→模型→工具→多轮→最终回复→持久化 的确定性测试。

第一版**不做**：真实 OpenAI provider、SQLite/Redis、压缩、超大结果外置、租约、Skill、并发工具执行、guardrails。（这些在后续阶段，见 §7。）

---

## 5. 目标目录结构

```
src/aegis_agent/
├── __init__.py                 # 包标识 + 版本；不放业务逻辑
├── __main__.py                 # `python -m aegis_agent` → cli.app()
│
├── cli/
│   ├── __init__.py
│   └── app.py                  # Typer app；交互 REPL；--resume；渲染
│
├── runtime/
│   ├── __init__.py
│   ├── agent.py                # AegisAgent：聚合 provider/tools/session/context，
│   │                           #   对外暴露 run_turn(user_message) -> TurnResult
│   └── loop.py                 # Agent Loop：多轮 model↔tool 循环
│
├── models/
│   ├── __init__.py
│   ├── base.py                 # ModelProvider Protocol；ChatMessage/ChatResponse/
│   │                         #   ToolCall/ToolDefinition 数据结构
│   ├── fake.py                 # FakeModelProvider（确定性、可脚本化、可流式）
│   ├── openai_compat.py        # OpenAI-compatible provider（后续阶段）
│   └── stream.py               # 流式增量累积 + tool_call 片段拼接（name 赋值/args 拼接）
│
├── tools/
│   ├── __init__.py
│   ├── base.py                 # Tool Protocol；ToolResult；ToolContext
│   ├── registry.py             # ToolRegistry：register/get/definitions
│   ├── executor.py             # ToolExecutor：dispatch + 异常兜底 + tool 消息构建
│   └── builtin/
│       ├── __init__.py
│       ├── read_file.py
│       ├── list_directory.py
│       └── run_shell.py
│
├── context/
│   ├── __init__.py
│   ├── builder.py              # ContextBuilder：源 messages → 派生 api_messages（注入 system、剥离内部字段）
│   ├── system_prompt.py        # system prompt 分层构建
│   └── compression.py          # 层级压缩（后续阶段；只影响发给模型的 context）
│
├── sessions/
│   ├── __init__.py
│   ├── base.py                 # SessionRepository / LeaseBackend Protocol；Message/Session/Snapshot 结构
│   ├── memory.py               # InMemorySessionRepository（第一版）
│   ├── sqlite.py               # SQLite 实现（后续阶段）
│   ├── snapshot.py             # checkpoint + tail 恢复；损坏回退全量回放（后续）
│   └── lease.py                # SQLite/Redis 租约（后续阶段）
│
├── skills/
│   ├── __init__.py
│   ├── loader.py               # SKILL.md frontmatter 解析 + 发现（后续阶段）
│   └── router.py               # Skill 路由 → 上下文注入（后续阶段）
│
└── errors.py                   # 统一错误类型 + 分类
```

### 模块职责与公开接口

| 模块 | 职责 | 公开接口（Protocol/函数） |
|---|---|---|
| `cli` | 终端交互、参数解析、渲染 | Typer `app` |
| `runtime` | 编排一个 turn、驱动 loop | `AegisAgent.run_turn(user_message) -> TurnResult` |
| `runtime.loop` | model↔tool 多轮循环 | `run_loop(ctx) -> LoopResult` |
| `models.base` | 模型抽象与数据结构 | `ModelProvider`, `ChatMessage`, `ChatResponse`, `ToolCall`, `ToolDefinition` |
| `models.stream` | 流式增量累积、tool_call 拼接 | `StreamAssembler.feed(delta) -> ...` |
| `tools.base` | 工具抽象 | `Tool`, `ToolResult`, `ToolContext` |
| `tools.registry` | 工具注册与 schema | `ToolRegistry.register/get/definitions` |
| `tools.executor` | 工具派发与兜底 | `ToolExecutor.execute(tool_calls) -> list[ToolResultMessage]` |
| `context.builder` | 构建发给模型的派生 context | `ContextBuilder.build(messages) -> api_messages` |
| `context.system_prompt` | system prompt 构建 | `build_system_prompt(...) -> str` |
| `sessions.base` | 会话持久化抽象 | `SessionRepository`, `LeaseBackend`, `Message`, `Snapshot` |
| `sessions.memory` | 内存仓储 | `InMemorySessionRepository` |
| `skills.loader` | Skill 加载 | `parse_frontmatter`, `discover` |

### 依赖方向（允许的箭头）

```
cli  →  runtime  →  models, tools, context, sessions, skills
runtime.loop  →  models.base, tools.executor, context.builder, sessions.base
tools.executor  →  tools.registry, tools.base
tools.builtin   →  tools.base
context.builder →  models.base (仅数据结构), context.system_prompt
sessions.sqlite/memory →  sessions.base
skills.router   →  skills.loader, context.builder
models.*        →  models.base
```

### 不允许的反向依赖（硬性规则）

- `runtime` / `runtime.loop` **不得** import `cli`（无 Typer/渲染依赖）。
- `runtime` / `tools` / `context` **不得** import `sessions.sqlite` 或任何具体 SQL/Redis 命令 —— 只依赖 `sessions.base` 接口。
- `runtime` **不得** import `models.openai_compat` —— 只依赖 `models.base.ModelProvider`。
- `models` / `tools` / `context` / `sessions` / `skills` 之间**禁止循环 import**；下层（base/数据结构）不依赖上层。
- 任何模块**不得**读写全局 CLI 状态或单例（除显式注入的 registry 实例外）。

---

## 6. 依赖闭包

### 6.1 必须保留的第三方依赖（Aegis 第一版）

| 依赖 | 用途 | 对应 Hermes |
|---|---|---|
| `typer` | CLI（交互 + `--resume`） | Hermes 用 prompt_toolkit + Typer 混合；Aegis 简化用 Typer |
| `rich` | 终端渲染 | 同 |
| `pydantic` | 数据结构 / schema 校验 | 同 |
| `pyyaml` | SKILL frontmatter（后续） | 同 |

> 已在 `aegis-agent/pyproject.toml`：`pydantic`, `rich`, `typer`。后续需 `uv add pyyaml`。

### 6.2 可用标准库替代的依赖

| Hermes 依赖 | Aegis 替代 | 说明 |
|---|---|---|
| `prompt_toolkit` | 标准 `input()` / Typer 内建 | 第一版无需复杂 TUI |
| `requests` / `httpx`（provider HTTP） | `openai` SDK 自带 httpx；或 stdlib `urllib` | 第一版 Fake provider 无需 HTTP；OpenAI provider 阶段用 `openai` SDK |
| `tenacity` | 手写重试循环 | 第一版重试逻辑简单 |
| `python-dotenv` | `os.environ` | 环境变量读取 |
| `croniter`, `Markdown`, `PyJWT`, `psutil`, `pathspec`, `fastapi`, `uvicorn` | — | 全部 DROP（超范围） |

### 6.3 Hermes 特有依赖（不迁移）

`mautrix`/`python-olm`(matrix)、`python-telegram-bot`、`discord.py`、`slack-bolt`、`dingtalk-stream`、`lark-oapi`、`mcp`、`boto3`、`azure-identity`、`faster-whisper`、`edge-tts`、`fal-client`、`firecrawl-py`、`exa-py` 等 —— 全部属排除范围（CLAUDE.md §5）。

### 6.4 可删除依赖（Aegis 第一版不需要）

- 所有 messaging/voice/browser/cron/computer-use 依赖。
- `openai` SDK 在第一版（Fake provider）不需要；引入 OpenAI provider 阶段再 `uv add openai`。

### 6.5 全局变量 / 单例（Hermes 中的，Aegis 须避免或显式注入）

| Hermes 全局/单例 | 位置 | Aegis 处理 |
|---|---|---|
| `tools/registry.py:544 registry = ToolRegistry()` | 模块级单例 | Aegis 用**显式注入**的 `ToolRegistry` 实例，不用模块级单例 |
| `providers/__init__.py` `_REGISTRY`/`_ALIASES`/`_discovered` | 模块级 | Aegis 不需要 provider 插件发现 |
| `run_agent.OpenAI`（可被测试 patch 的模块级名） | `agent_runtime_helpers.py:1343` | Aegis 通过构造函数注入 provider，不靠 patch |
| `cli.py` 大量 `CLI_CONFIG`、全局回调（`set_sudo_password_callback` 等） | 模块级 | Aegis 全部通过构造参数传入 |
| `agent._session_messages` / `agent._client_kwargs` 等实例可变状态 | `run_agent.py` | Aegis 收敛进明确的 `RuntimeState`/`TurnState` 数据类 |

### 6.6 循环依赖（Hermes 中的，Aegis 须规避）

- `providers/base.py` → `hermes_cli.__version__`（lazy import 注释 “avoid layer cycle”）。
- `tool_dispatch_helpers._is_mcp_tool_parallel_safe` → `tools.mcp_tool`（lazy import 注释 “avoid circular dependencies”）。
- `run_agent.py` ↔ `agent/conversation_loop.py` ↔ `agent/tool_executor.py`（大量 `_ra()` / `agent.*` 回调穿透，是最重的耦合）。
- `model_tools.py` ↔ `tools/registry.py` ↔ 各 `tools/*.py`（发现/注册环）。

Aegis 通过**依赖注入 + 单向分层**（§5 依赖方向）根除这些环。

### 6.7 最难解耦的五个位置

1. **`agent/conversation_loop.py:run_conversation`（4000+ 行）** —— 与 steer、prefill、插件钩子、kanban、voice、guardrails、压缩、持久化、快照全部内联耦合。解耦策略：只抽取“循环骨架 + 终止/预算/中断”，其余能力作为可选钩子后置注入。
2. **`run_agent.py:AIAgent`** —— 聚合了 client 创建、header 注入、持久化转发、快照、clarify/reasoning 回调、provider failover。解耦策略：拆成 `AegisAgent`（编排）+ 注入的 `ModelProvider`/`SessionRepository`/`ToolExecutor`。
3. **`agent/chat_completion_helpers.py`** —— 120KB，把传输、重试、中断、流式拼接、failover、rate-limit 头、codex/anthropic 分发揉在一起。解耦策略：只抽出 `stream assembler` 纯函数（tool_call 拼接），传输层重写。
4. **`hermes_state.py:SessionDB`（226KB）** —— schema 演进、FTS、counters、快照、租约、压缩锁全在一个类。解耦策略：Aegis 的 `SessionRepository` 接口 + 精简 SQLite 实现（去 FTS/billing/codex）。
5. **`model_tools.py` + `tools/registry.py` + `tools/*.py`** —— 自注册 + AST 发现 + toolset 解析 + Tool Search 桥三层耦合。解耦策略：显式注册（无 AST 扫描）、无 toolset 概念（第一版固定工具集）、无 Tool Search。

---

## 7. 未完成计划（文档顺序不代表完成顺序）

> 验收命令统一为 `uv run pytest -q` 与 `uv run ruff check .`（在 aegis-agent 目录）。

### ✅ 已完成

| 阶段 | 内容 | 对应 milestone |
|---|---|---|
| 阶段 1 | 最小垂直链路（Fake + 内存会话 + Agent Loop + builtin tools） | Stage 1 |
| 阶段 3 | OpenAI-compatible provider + 流式 + 消息净化 | Stage 2 |
| — | 交互式终端 UI（prompt_toolkit + rich + 流式输出） | Stage 3（新增） |
| 阶段 7 | Skills 子系统（SKILL.md 发现/加载/路由、`skills_list`/`skill_view`、`/skill-name` 斜杠命令、SystemPromptBuilder 动态注入） | Stage 4（超出原计划） |
| — | 轻量 MCP 客户端（stdio + Streamable HTTP、schema adapter、MCPToolWrapper、PromptContributor） | Stage 5（新增） |
| 阶段 4 | 危险命令护栏（`run_shell` 销毁性命令拦截） | Stage 2（部分，未全部完成） |

### ❌ 未完成

#### SQLite 会话持久化 + checkpoint/tail + resume

- **范围**：`SessionRepository` 的 SQLite 实现（messages/sessions/snapshots 精简 schema）、`append_message` 幂等（`ON CONFLICT DO NOTHING`）、`seq` 单调、快照写/载（CRC+zlib+history_version）、`resume`（checkpoint+tail，损坏回退全量回放）、CLI `--resume` 真正接线。
- **明确不做**：FTS、租约、压缩旋转、Redis。
- **验收测试**：① checkpoint 恢复 == 全量回放；② 损坏 checkpoint 安全回退；③ 单调 `seq`；④ 无跨会话历史；⑤ 重启后 `--resume` 恢复历史。

#### 工具增强：并发执行 + 超大结果外置

- **范围**：并发工具执行（ThreadPoolExecutor）、超大结果三层（工具内 cap → 外置存储 + 预览 → 聚合预算）、`_maybe_wrap_untrusted`。
- **已做**：危险命令护栏（`tools/danger.py`）、参数 JSON 修复（`sanitize.py:repair_tool_call_arguments`）、异常→`{"error":...}` 结果（`ToolExecutor`）。
- **未做**：并发执行、`ToolGuardrails`（重复工具/无进展熔断）、超大结果外置存储 + 预览、聚合预算。

#### 层级上下文压缩

- **范围**：`context/compression.py` 分阶段压缩（prune 旧 tool 结果 → token 边界 → 滚动摘要 → head+summary+tail）、token 阈值触发、**原文保留**、压缩只影响发给模型的 context。
- **验收测试**：① 超过阈值触发压缩；② 原文 messages 不被修改；③ 压缩后恢复 == 全量回放；④ 摘要失败时不变更。

#### 会话租约（SQLite + Redis）

- **范围**：`LeaseBackend` Protocol、SQLite 租约（INSERT OR IGNORE + owner_token）、Redis 租约（SET NX PX + Lua）、`SessionLeaseManager` 心跳 + 熔断 + `switch_session`。
- **验收测试**：① 同一 session 仅一个 owner；② TTL 过期可回收；③ 心跳失败触发熔断并停止写入；④ Redis 不可用不静默回退 SQLite。

#### 可靠性与并发测试（不完全——核心不变量测试已有，非核心未补）

- **范围**：不变量测试套件（CLAUDE.md §9）：单消息幂等、会话内单调、无重复模型请求、无重复工具结果、无跨会话历史、单一租约 owner、checkpoint 恢复 == 全量回放、损坏 checkpoint 安全回退、压缩后原文不变。
- **已做**：幂等、单调、会话隔离、context 不改原文、provider 不污染 runtime（守卫测试）。
- **未做**：租约/checkpoint/压缩相关——需等对应功能实现后才能测。

#### MCP 增强（新增需求）

- **范围**：重连机制（server 断开后自动恢复）、断路器（连续失败降级）、`tools/list_changed` 动态刷新。
- **当前**：连接成功时工具固定，server 断开后调用直接报错。

---

## 8. 许可证与源码归属

### 8.1 Hermes 许可证

- **MIT License**，© 2025 Nous Research（`hermes-agent/LICENSE`）。
- `pyproject.toml:22` `license = "MIT"`，`license-files = ["LICENSE"]`。
- MIT 允许：使用、复制、修改、合并、发布、分发、再许可、销售 —— **附带条件**：所有副本或“实质部分”须保留版权声明与许可声明。

### 8.2 各类内容的处理

| 内容 | 处理 |
|---|---|
| **可直接适配（PORT/ADAPT）** —— `iteration_budget.py`、`session_lease.py` 骨架、`tool_dispatch_helpers.make_tool_result_message`/`_maybe_wrap_untrusted`、stream tool_call 拼接逻辑、SessionDB schema 设计、`parse_frontmatter` | 允许适配，但**必须在文件头保留 Hermes 版权与 MIT 声明**，并在 `docs/source-map.md` 记录来源。 |
| **建议重新实现（REWRITE）** —— Agent Loop 骨架、CLI、各内置工具（read_file/list_directory/run_shell)、压缩、system prompt builder | 只参考**行为**，在 Aegis 中重写。重写代码属原创，但行为等价处仍在 `docs/source-map.md` 记录“参考自 Hermes 哪个文件/函数”。**不得**把适配的 Hermes 代码描述为完全原创。 |
| **不迁移（DROP）** | 无需声明。 |

### 8.3 需要保留声明的文件

凡是 **PORT** 或 **ADAPT**（实质派生）的 Aegis 源文件，文件头加：

```python
# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
```

并在对应位置保留原版权行。

### 8.4 LICENSE 如何处理

- Aegis 自身采用 MIT（与 Hermes 一致，简化归属）。
- Aegis 根目录 `LICENSE`：Aegis 的版权行。
- **另设** `THIRD_PARTY_NOTICES.md`：完整收录 Hermes 的 MIT 文本与 “© 2025 Nous Research”。

### 8.5 THIRD_PARTY_NOTICES.md 如何处理

内容应包括：
- Hermes (hermes-agent)：MIT，© 2025 Nous Research，完整许可文本。
- 说明哪些 Aegis 模块实质派生自 Hermes（引用 `docs/source-map.md`）。
- 其余第三方依赖（typer/rich/pydantic/pyyaml 等）按各自许可列名（可在后续补全其声明）。

### 8.6 docs/source-map.md 应记录的信息

对每个 PORT/ADAPT/REWRITE 项，记录一行映射：

| Aegis 文件 | 关系（PORT/ADAPT/REWRITE） | Hermes 来源文件 → 符号 | 备注 |
|---|---|---|---|
| `runtime/loop.py` | REWRITE | `agent/conversation_loop.py:run_conversation` (351, loop 807) | 仅参考循环骨架与终止/预算/中断行为 |
| `models/stream.py` | ADAPT | `agent/chat_completion_helpers.py` (1828-1891 tool_call 拼接； 1900-1959 伪响应） | 抽取 name 赋值/args 拼接/Ollama slot/JSON 修复 |
| `runtime/…/iteration_budget.py` | PORT | `agent/iteration_budget.py:IterationBudget` (17) | 线程安全 consume/refund |
| `tools/executor.py` | ADAPT | `agent/tool_executor.py` + `tools/registry.py:dispatch` (390) + `model_tools.handle_function_call` (861) | 异常→`{"error":...}` |
| `tools/…/messages.py` | ADAPT | `agent/tool_dispatch_helpers.py:make_tool_result_message` (320), `_maybe_wrap_untrusted` (372) | tool 结果消息 + 不可信包裹 |
| `sessions/sqlite.py` | ADAPT | `hermes_state.py:SCHEMA_SQL` (232), `append_message` (2213), 幂等 (2310) | 精简 schema；`seq`/`client_msg_id`/`active` |
| `sessions/snapshot.py` | ADAPT | `hermes_state.py:write_snapshot` (3026), `load_latest_snapshot` (3105), `resume_conversation` (3198) | CRC+zlib+history_version；损坏回退 |
| `sessions/lease.py` | ADAPT | `session_lease.py:SessionLeaseBackend` (84), SQLite (132), Redis (214), Manager (377) | 心跳 + 熔断 + switch_session |
| `skills/loader.py` | ADAPT | `agent/skill_utils.py:parse_frontmatter` (88), `iter_skill_index_files` (632) | frontmatter + 发现 |
| `context/builder.py` | ADAPT | `agent/conversation_loop.py` (964-1058 派生 api_messages) | 源 messages 不被修改 |
| `context/compression.py` | REWRITE | `agent/context_compressor.py:compress` (1827) + `conversation_compression.compress_context` (271) | 阶段化压缩；原文保留语义 |
| `tools/builtin/read_file.py` | REWRITE | `tools/file_tools.py:read_file_tool` (692) | 行为等价，重写 |
| `tools/builtin/list_directory.py` | REWRITE | （Hermes 无独立工具；参考 `search_files` target=files) | Aegis 新增 |
| `tools/builtin/run_shell.py` | REWRITE | `tools/terminal_tool.py:terminal_tool` (1775) | 行为等价，重写 |

---

## 附：分析依据

- 本计划基于对 Hermes 真实源码的只读分析（未修改 Hermes 任何文件）。
- 关键事实直接来自源码：`pyproject.toml`（入口/依赖/许可）、`LICENSE`、`providers/base.py`、`providers/__init__.py`、`agent/tool_dispatch_helpers.py`、`agent/iteration_budget.py`、`agent/tool_result_classification.py`，以及对 `cli.py`、`run_agent.py`、`agent/conversation_loop.py`、`agent/chat_completion_helpers.py`、`agent/system_prompt.py`、`agent/context_compressor.py`、`agent/conversation_compression.py`、`hermes_state.py`、`session_lease.py`、`tools/registry.py`、`model_tools.py`、`agent/tool_executor.py`、`tools/file_tools.py`、`tools/terminal_tool.py`、`agent/skill_utils.py`、`agent/skill_commands.py`、`tools/skills_tool.py` 的定向读取。
