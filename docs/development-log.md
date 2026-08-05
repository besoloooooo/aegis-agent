# Aegis Agent Development Log

> 面试复习用技术文档。内容以仓库**实际实现**为准（源码、测试、git 历史、`docs/source-map.md`）。
> 区分「已实现」与「计划实现」，不虚构功能、测试结果或性能数据。
> 代码名称保留英文，叙述用中文。
>
> 当前版本：`0.1.0`（`src/aegis_agent/__init__.py`）
> 最近提交：`b1c6ca0` / `e75c0e6`（master）

---

## Project overview

Aegis Agent 是一个**轻量、可恢复、可扩展的 Agent Runtime**，通过从
[Hermes](https://github.com/NousResearch/hermes-agent)（© 2025 Nous Research，MIT）
的核心运行时行为中**抽取、简化、模块化**而来。

定位（见 `CLAUDE.md` §2）：

- 仓库：`aegis-agent`，Python 包：`aegis_agent`，CLI 命令：`aegis`
- 只保留 Hermes 的**核心链路**：交互式 CLI、Agent Runtime、Agent Loop、模型 provider 抽象、
  OpenAI-compatible provider、fake provider、工具注册/执行/内置工具、上下文构建、会话存储。
- **明确排除**（`CLAUDE.md` §5）：Telegram/Discord、桌面宠物、Web UI、语音、浏览器自动化、
  定时任务、Hermes 全部工具与 provider、安装脚本与品牌 UI 等。

### 当前进度

三个里程碑已完成：

| 阶段 | 主题 | 状态 |
|---|---|---|
| Stage 1 | minimal Agent Runtime skeleton（fake provider + 内存会话 + 内置工具 + Agent Loop） | 已完成 |
| Stage 2 | OpenAI-compatible provider + 流式工具调用片段组装 + 消息净化 + 危险命令护栏 | 已完成 |
| Stage 3 | 实时终端 UI + 流式输出（prompt_toolkit 输入 + rich 输出 + ANSI Shadow banner + 颜文字 spinner） | 已完成 |
| Stage 4 | Skills 子系统 + 动态系统提示注入（SKILL.md 发现/加载/路由、`skills_list`/`skill_view` 渐进式工具、`/skill-name` 斜杠命令、`SystemPromptBuilder` + `PromptContributor` 注入缝） | 已完成 |
| Stage 5 | 轻量 MCP 客户端（stdio + Streamable HTTP、schema adapter 三阶段 pipeline、`MCPToolWrapper` 注册进 ToolRegistry、`MCPToolsGuidance` 提示注入、可选 `mcp` SDK 依赖） | 已完成 |

> 注：`README.md` 顶部仍标注 "Stage 2"，未随 Stage 3 更新；以本文与 `docs/source-map.md` 为准。

### 计划实现（尚未做）

SQLite/Redis 会话存储、租约（lease）、resume/checkpoint 恢复、上下文压缩、
多 provider failover/限流、并发工具执行、超大工具结果外置存储。
均记录于 `docs/extraction-plan.md` 与 `docs/source-map.md` 的 Notes 节，源码中**未实现**。

---

## Current architecture

### 模块划分与依赖方向

`CLAUDE.md` §6 要求模块分离，且 **Agent Loop 不得直接依赖** Typer、SQLite SQL、Redis 命令、
具体 provider、全局 CLI 状态。实际依赖方向是严格单向：

```
cli → runtime → models / tools / context / sessions
                 （全部通过 Protocol/接口，不依赖具体实现）
```

模块（`src/aegis_agent/`）：

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `models` | 核心数据结构、`ModelProvider` Protocol、fake/openai provider、流式组装、消息净化 | `base.py`, `fake.py`, `openai_compat.py`, `stream.py`, `sanitize.py` |
| `tools` | `Tool`/`ToolRegistry`/`ToolExecutor`、内置工具、危险命令检测、JSON Schema | `registry.py`, `executor.py`, `schemas.py`, `danger.py`, `builtin/*.py` |
| `context` | 派生上下文构建（系统提示 + 历史副本，不改原文）、动态系统提示组装 | `builder.py`, `system_prompt.py` |
| `skills` | 技能发现、加载、路由、渐进式工具（`skills_list`/`skill_view`）、提示索引注入 | `models.py`, `frontmatter.py`, `loader.py`, `router.py`, `prompt.py`, `tools.py` |
| `sessions` | `SessionRepository` Protocol + 内存实现 | `repository.py`, `memory_store.py`, `models.py` |
| `runtime` | Agent Runtime + Agent Loop + 迭代预算 + `TurnEvent` | `runtime.py` |
| `events` | `ModelEvent` + `collect_response`（流→统一响应） | `events.py` |
| `cli` / `tui` | Typer 入口 + prompt_toolkit/rich 终端 UI | `cli.py`, `tui.py` |
| `exceptions`, `env` | 统一异常、无依赖 `.env` loader | `exceptions.py`, `env.py` |

### 核心调用流程（一轮 `run_turn`）

入口：`aegis` → `cli.py:main` → Typer `_main` → `_select_provider` → `AgentRuntime.with_defaults`
→ `_repl`（`tui.Tui` 渲染）。

一轮用户输入的数据流（`runtime.py:AgentRuntime.run_turn`）：

1. **持久化用户消息**：`_persist(session_id, Message(role=USER, content=...))`，由
   `SessionRepository.append_message` 赋单调 `seq`、按 `client_msg_id` 幂等。
2. **循环守卫**：先查 `interrupt` 事件（协作式中断），再 `IterationBudget.consume()` 查迭代预算。
3. **构建派生上下文**：`source = repository.list_messages(session_id)` →
   `api_messages = self._context.build(source)`（`ContextBuilder.build` 前置系统提示 + 清洗副本，
   **不改原始消息**）。
4. **调用模型（流式）**：`collect_response(self._provider.stream(api_messages, tools=...), is_cancelled=..., on_event=_emit)`。
   provider 产出 `ModelEvent`（`TEXT_DELTA`/`TOOL_CALL`/`DONE`/`ERROR`）；`collect_response`
   边折叠边经 `on_event` 把每个事件转成 `TurnEvent` 转发给 UI。
5. **持久化助手消息**：`Message(role=ASSISTANT, content=response.content, tool_calls=response.tool_calls)`。
6. **分支**：
   - 无 `tool_calls` → 终态 `FINAL_ANSWER`，`break`。
   - 有 `tool_calls` → `ToolExecutor.execute(...)`，逐个 `to_messages` 持久化 `role=TOOL` 结果，
     每个 `ToolResult` 经 `on_event` 发 `TOOL_RESULT` 事件；继续循环回到第 2 步。
7. **TURN_END**：循环退出后发一个 `TurnEvent.turn_end(stop_reason)`；返回 `TurnResult`。

工具定义走**旁路参数** `tools=self._registry.definitions()`，不进消息列表。

### 关键接口（Protocol）

均 `@runtime_checkable`，runtime 只依赖接口：

- `ModelProvider`（`models/base.py`）：`name` + `stream(messages, tools) -> Iterator[ModelEvent]`。
- `SessionRepository`（`sessions/repository.py`）：`create_session` / `get_session` /
  `append_message`（幂等于 `client_msg_id`、赋单调 `seq`）/ `list_messages` / `message_count`。
- `Tool`（`tools/registry.py`）：`definition` + `run(arguments, context) -> ToolResult`。
- `ContextBuilder`（`context/builder.py`）：普通类，`build(messages) -> list[Message]`。

---

## Existing milestones

### Milestone 1 — Stage 1：minimal Agent Runtime skeleton

**Problem and goal**
目标是一个可端到端跑通的"垂直切片"：用户输入 → fake 模型产出工具调用 → 执行工具 →
回填结果 → 模型给最终答案，全程不碰网络与付费 API。为后续真 provider 与持久化打底。

**Implemented behavior**
- Agent Loop：guard → context → model → tools → loop，终态 `FINAL_ANSWER` / `MAX_ITERATIONS`。
- `FakeModelProvider`：脚本队列 + 规则兜底；可逐字符分块产出 `TEXT_DELTA` 以走通流式路径。
- 内置工具：`read_file`（带行号/分页）、`list_directory`、`run_shell`（带超时与输出上限）。
- `ToolRegistry` 显式注入（非全局单例）；`ToolExecutor` 把任何工具异常转成 `{"error":...}` 结果。
- 内存会话：幂等、单调 `seq`、会话隔离。
- 上下文构建：派生副本，清掉 `client_msg_id`/`seq`，前置静态系统提示。
- CLI REPL（Typer），`aegis` 命令。

**Design and data flow**
- 单向依赖 `cli → runtime → {models, tools, context, sessions}`；runtime 只认 Protocol。
- 原始消息为 source of truth；`ContextBuilder.build` 只读不改（`_derive` 复制并清内部字段）。
- `IterationBudget` 线程安全 consume/refund，控制循环上限。

**Key implementation**
- `runtime.py`：`AgentRuntime`、`AgentRuntime.with_defaults`、`run_turn`、`IterationBudget`、`StopReason`、`TurnResult`。
- `models/base.py`：`Message`/`ToolCall`/`ToolResult`/`ToolDefinition`/`ChatResponse`/`Role`。
- `models/fake.py`：`FakeModelProvider`、`FakeReply`。
- `tools/registry.py`：`Tool` Protocol、`ToolContext`、`ToolRegistry`。
- `tools/executor.py`：`ToolExecutor.execute` / `execute_one` / `to_messages`。
- `tools/builtin/*.py`：三个内置工具。
- `sessions/memory_store.py`：`InMemorySessionRepository`（线程安全）。
- `context/builder.py`：`ContextBuilder.build` / `_derive`、`DEFAULT_SYSTEM_PROMPT`。

**Source relationship**
- `IterationBudget`：**PORT** 自 Hermes `agent/iteration_budget.py`。
- `run_turn`：**REWRITE**，参照 Hermes `agent/conversation_loop.py:run_conversation` 的循环骨架，
  去掉 steering/plugins/persistence。
- `executor.py`：**ADAPT** 自 Hermes `agent/tool_executor.py` 等（异常→`{"error":...}`、未知工具→错误、
  组装 `role=tool` 消息）。
- `read_file` / `run_shell` / `list_directory`：**REWRITE**（行为等价最小面）。
- `memory_store.py`：**REWRITE**（内存版的幂等 + 单调 `seq`，对标 Hermes `SessionDB.append_message`）。
- 其余（`base.py`、`fake.py`、`registry.py`、`schemas.py`、`cli.py` 等）：**original**。
- 详见 `docs/source-map.md` Stage 1 表。

**Tests and evidence**
`tests/test_runtime.py`（11 项，含纯文本回答、单工具调用→最终答案、工具结果回填历史、
下一轮可见、未知工具错误、工具内部异常被捕获、max-iterations 停止、消息顺序与 `seq`、
会话隔离、context 不改原文）；`tests/test_sessions.py`（6 项幂等/单调/隔离/未知会话）；
`tests/test_fake_provider.py`（9 项脚本/分块/规则兜底/错误事件）；`tests/test_tools.py`（16 项内置工具）。

**Design trade-offs**
- 显式依赖注入取代 Hermes 的全局单例 + AST 发现，避免隐藏全局态、利于测试隔离。
- 内存会话进程退出即丢，跨进程 resume 留待后续 SQLite 阶段。
- 工具顺序执行（Stage 1 范围）；并发执行、guardrails、超大结果外置均不做。

**Interview summary**
"Aegis 的 Agent Loop 是从 Hermes 抽出来的最小可运行切片：runtime 只依赖 `ModelProvider`、
`SessionRepository`、`ToolExecutor`、`ContextBuilder` 四个 Protocol，循环结构是 guard →
build 派生上下文 → 调模型 → 检测工具调用 → 执行回填 → 继续循环。关键设计是**原始消息为
source of truth**，发给模型的上下文是每轮重建的派生副本，为后续压缩/恢复留好位置；
工具异常统一转成 `{"error":...}` 结果，绝不击穿循环。"

---

### Milestone 2 — Stage 2：OpenAI-compatible provider & streaming tool calls

**Problem and goal**
接入真实 OpenAI-compatible 端点，并解决流式响应里**工具调用参数分片到达**的重组问题，
以及上游模型历史污染（脏 UTF-16 代理项、畸形 JSON 参数）导致后续轮次崩溃的问题；
同时给 `run_shell` 加危险命令护栏。

**Implemented behavior**
- `OpenAICompatibleProvider`：环境变量配置（`AEGIS_API_KEY`/`AEGIS_BASE_URL`/`AEGIS_MODEL`），
  支持真流式（`stream=True`）与 one-shot 两种模式，均归一化为 `ModelEvent` 流。
  错误归一为 `ModelProviderError` / `ModelTimeoutError`。
- `StreamAssembler` / `assemble_stream`：把 OpenAI chunk 序列折叠成事件——
  文本 delta 即时转发；工具调用片段按 `index` 槽位累积，`name` **赋值**（不拼接，因部分
  provider 每片重发全名），`arguments` **拼接**；流结束时发完整 `TOOL_CALL` + `DONE`。
- 消息净化（`sanitize.py`）：`sanitize_surrogates` 把孤立代理项（U+D800–DFFF，DashScope/Qwen
  会产生）替换为 U+FFFD，避免污染历史回传时崩溃；`repair_tool_call_arguments` 修复畸形
  参数 JSON（Python `None`、尾逗号、未闭合、字面控制字符），最后兜底 `"{}"`。
  在 `_to_wire_message`（wire 前）与 executor 解析前、`StreamAssembler.finish` 处都走净化。
- 危险命令护栏（`danger.py`）：`detect_dangerous_command` 用 `(regex, 说明)` 列表首匹配；
  `run_shell` 默认拦截（递归删除、`mkfs`/`dd`、SQL `DROP`/无 `WHERE` 的 `DELETE`/`TRUNCATE`、
  fork bomb、pipe-to-shell、破坏性 git、杀进程/服务）。开关 `ToolContext.allow_dangerous_shell`
  是**操作员-only**，不是工具参数，模型无法自行开启。
- 中断/错误接线：`OperationCancelled`（流式中途取消，丢弃半成品）→ `INTERRUPTED`；
  `ModelProviderError`/`ModelTimeoutError` → `ERROR`。

**Design and data flow**
- provider 产出 `ModelEvent`，`collect_response` 折叠成 `ChatResponse`，下游循环只处理统一形状——
  流式与非流式路径同形（对标 Hermes 把流式 chunk 重建为伪非流式响应）。
- 流式中途可中断：`collect_response(is_cancelled=...)` 在每个事件前轮询，置位即抛
  `OperationCancelled`，半成品响应被丢弃而非持久化。
- `client` 可注入（`OpenAICompatibleProvider(client=...)`），测试用 `FakeOpenAIClient` 离线驱动。

**Key implementation**
- `models/openai_compat.py`：`OpenAICompatibleProvider.from_env` / `stream` / `_stream_call` /
  `_oneshot_call` / `_to_wire_message` / `_events_from_response` / `_wrap_error`。
- `models/stream.py`：`StreamAssembler` / `assemble_stream`（`_ToolCallAccumulator`）。
- `models/sanitize.py`：`sanitize_surrogates` / `repair_tool_call_arguments`。
- `tools/danger.py`：`DANGEROUS_PATTERNS` / `detect_dangerous_command`。
- `tools/builtin/run_shell.py`：护栏接入（默认拦截，operator override）。
- `tools/executor.py:_parse_arguments`：解析前先 `repair_tool_call_arguments`。

**Source relationship**
- `stream.py`：**ADAPT** 自 Hermes `agent/chat_completion_helpers.py`（流式工具调用组装，
  ~1828–1891）。
- `openai_compat.py`：**REWRITE**，参照 Hermes `interruptible_api_call` /
  `interruptible_streaming_api_call`（env 配置 + `create(stream=True)` + 错误归一），去掉
  failover/rate-guard/credential-pool。
- `collect_response is_cancelled`：**ADAPT**（中断感知流式消费）。
- `sanitize.py`：**ADAPT** 自 Hermes `agent/message_sanitization.py`（代理项清洗 + 参数 JSON 修复）。
- `danger.py`：**ADAPT** 自 Hermes `tools/approval.py`（通用破坏性命令子集，去掉 Hermes 专有条目）。
- 详见 `docs/source-map.md` Stage 2 表。

**Tests and evidence**
`tests/test_runtime_streaming.py`（7 项：model error/timeout 优雅停止、cancel 前置、
cancel 中途丢弃半成品、多工具调用全部执行、OpenAI provider 经 fake client 跑通完整 loop、
runtime 不 import 具体 provider 的守卫测试）；`tests/test_openai_provider.py`（~17 项：
流式/非流式文本与工具调用、传输错误/超时归一、`from_env` 校验、wire 消息形状、
代理项清洗各路径；外加 1 个 `@pytest.mark.integration` 的真端点冒烟测试，默认 skip）；
`tests/test_stream.py`（流式组装）；`tests/test_sanitize.py`；`tests/test_tools.py` 危险命令项
（默认拦截、git reset --hard 拦截、安全命令放行、operator override、模型无法经参数开启、子集检测）。

**Design trade-offs**
- 选流式为**主契约**（连 fake 也产 `ModelEvent`），让真流式与 one-shot 走同一条组装路径，
  简化下游。代价：非流式 provider 也要包一层事件。
- 净化放在"最后一公里"（wire 前、解析前、流结束时），既保证落库原文不变，又避免脏数据
  在历史回传时炸掉——这是 Hermes 在 DashScope/Qwen 上踩过的坑。
- 危险护栏的开关不放工具参数里，从根上杜绝模型自行绕过。

**Interview summary**
"Stage 2 把真 provider 接进来，难点是流式工具调用：参数 JSON 是分片到达的，name 要赋值
不能拼接、arguments 要拼接、多调用按 index 槽位区分；我在 `StreamAssembler` 里把这些规则
独立成一个不依赖 HTTP 客户端的纯模块，便于单测。另一个坑是某些 provider 会在历史里留
孤立 UTF-16 代理项，回传时崩溃，所以 wire 前统一清洗、畸形参数 JSON 在执行前修复。
`run_shell` 的危险命令护栏开关是操作员-only，不暴露给模型。"

---

### Milestone 3 — Stage 3：live terminal UI & streaming output

**Problem and goal**
此前 CLI 用裸 `input()` + `echo`：只在**整轮结束后**打印 `final_text`，终端看不到实时输出，
打错字也无法左右移光标。目标：把已在流动的 `ModelEvent` 接到终端上，做出 Hermes 风格
但外观差异化的交互式 TUI。

**Implemented behavior**
- **流式输出缝**：`collect_response` 新增 `on_event` 前向回调，每个事件折叠前转发；
  `run_turn(on_event=...)` 把模型事件包成 `TurnEvent`（`TEXT_DELTA`/`TOOL_CALL`/
  `TOOL_RESULT`/`TURN_END`/`ERROR`）回调给 UI，并在工具执行后发 `TOOL_RESULT`、循环退出后发
  `TURN_END`。runtime 仍拿到完整 `ChatResponse`，持久化/循环逻辑不变——纯加观察者缝。
- **prompt_toolkit 输入**：`Tui` 用 `PromptSession` + `FileHistory(~/.aegis/history)`，
  支持 ←/→ 移动光标、Ctrl-A/E、↑/↓ 历史。非 TTY（测试/管道）自动回退 `input()`。
- **rich 输出**：`rich.Console` + `Theme`（青色调，与 Hermes 金色区分）；
  banner 用 `pyfiglet` 渲染 `AEGIS-AGENT`（`ansi_shadow` 字体，即圆角 `█`+`╗╔╚═` 风格）；
  工具结果用 `Panel` 带框（错误红框）；流式文本逐字内联打印（`markup=False` 防模型输出
  里的 `[` 被当 rich 标记）。
- **颜文字 spinner**：`_ThinkingRenderable` 由 `rich.Live` 刷新驱动（非守护线程），
  braille 帧 + kawaii 颜文字 + thinking 动词；节奏：帧 0.15s、颜文字 1.2s、动词 2.4s。
  非 TTY 跳过 spinner，保证捕获输出干净。

**Design and data flow**
- `Tui.begin_turn()` 起一个 `Live` spinner（仅 TTY）；`on_event` 收到首个 `TEXT_DELTA` 即
  `live.stop()` 然后内联打印；`TOOL_CALL` 停 spinner 起工具行；`TOOL_RESULT` 渲染 `Panel` 后
  重起 spinner（预判下一轮模型调用）；`TURN_END` 收尾。
- runtime 完全不知道 UI（单向 `cli/tui → runtime`）；TUI 不知道 Typer/具体 provider。

**Key implementation**
- `events.py`：`collect_response(..., on_event=...)`。
- `runtime.py`：`TurnEvent` / `TurnEventKind` / `TurnEvent.from_model_event`（`DONE` 返回 `None`
  以免发出虚假的提前 `TURN_END`）/ `run_turn` 的 `_emit`。
- `tui.py`：`Tui`、`_ThinkingRenderable`、`_build_logo`（pyfiglet）、`_banner_renderable`、
  `_render_event` 状态机、`_build_prompt_session`（prompt_toolkit）。
- `cli.py`：`_main` → `Tui().banner(...)` → `_repl`；`_select_provider` 对 fake 用
  `FakeModelProvider(chunk_text=True)` 让流式在 demo 里可见。

**Source relationship**
- `collect_response on_event`：**ADAPT**（Hermes `chat_completion_helpers` 流消费 + 前向回调）。
- `TurnEvent` / `run_turn on_event`：**REWRITE**，对标 Hermes `conversation_loop` 的
  `_vprint`/`_buffer_vprint`/`_safe_print` 实时反馈，runtime 不依赖任何 UI。
- `_ThinkingRenderable`：**ADAPT** 自 Hermes `agent/display.py:KawaiiSpinner`（braille 帧 +
  颜文字 + 动词），改为 `Live` 刷新驱动，去掉 skin engine 与 `patch_stdout`。
- banner / `Tui` / 工具面板：**REWRITE**，参照 Hermes `hermes_cli/banner.py` 的"启动横幅"
  概念，自绘 `AEGIS-AGENT` + 青色配色；输入用单个 `PromptSession`（非 Hermes 的全屏
  `Application`/`HSplit`/补全组件）。
- 详见 `docs/source-map.md` Stage 3 表。

**Tests and evidence**
`tests/test_tui.py`（3 项）：`on_event` 顺序断言（TEXT_DELTA×2 → TOOL_CALL → TOOL_RESULT →
TURN_END，且 `DONE` 不产生虚假 TURN_END）、CLI 流式输出（`list_directory` 工具名出现在
捕获输出）、CLI 纯 echo 流式（`Echo: hello aegis` 逐字拼接后仍可匹配）。
既有 CLI 测试（`test_cli.py`）在新 TUI 下仍通过（非 TTY 回退 `input()` 路径）。

**Design trade-offs**
- 不移植 Hermes 的 prompt_toolkit 全屏 `Application` + skin_engine（大而耦合），改为
  "prompt_toolkit 仅做输入 + rich 做输出"的轻组合，解决"光标移动 + 流式 + 工具面板"诉求。
- 颜文字 spinner 放弃守护线程 `\r` 动画，改用 `Live` 刷新，避免与 rich 输出抢行。
- `DONE` 事件映射成 `None`（不转发），由 runtime 统一发最终 `TURN_END`，避免双发。

**Interview summary**
"流式管道其实早就端到端通了（provider 产 `ModelEvent` → `collect_response` 折叠），
缺的是把事件接到终端。我加了个 `on_event` 观察者缝：`collect_response` 折叠前转发每个事件，
`run_turn` 把它们包成 `TurnEvent` 回调给 UI，runtime 逻辑零改动。UI 用 prompt_toolkit 做输入
（←/→ 光标、↑/↓ 历史），rich 做输出（`pyfiglet` 的 `ansi_shadow` banner、`Panel` 工具结果、
`Live` 驱动的颜文字 spinner）。关键决策是 `DONE` 事件不转发，由 runtime 统一发 `TURN_END`，
避免 UI 收到两次结束信号。"

---

## Test status

### 运行方式

```bash
uv run pytest -q          # 默认全离线，不碰付费 API
uv run ruff check .
uv run mypy src           # 可选
```

### 实际结果（本次整理时复核）

- `uv run ruff check .` → **All checks passed**。
- `uv run pytest -q` → **221 passed, 1 skipped**（skip 为 `test_openai_provider.py`
  的 `@pytest.mark.integration` 真端点冒烟测试，需 `AEGIS_RUN_INTEGRATION=1` + `AEGIS_*` 配置）。

### 测试策略与覆盖

- **确定性 fake**：核心 Agent Loop 用 `FakeModelProvider`（脚本队列 + 规则兜底）和
  `FakeOpenAIClient`（`tests/fakes.py`，模拟 OpenAI SDK 的 chunk/completion 形状），全程离线。
- **不变量测试**（`CLAUDE.md` §9）：一个 `client_msg_id` 只持久化一条逻辑消息、单调 `seq`、
  无重复模型请求、无跨会话历史、checkpoint recovery 等于全量重放（后者属未实现的 SQLite 阶段，
  当前仅内存版）、context 不改原文（`test_context_builder_does_not_mutate_source`）。
- **守卫测试**：`test_loop_does_not_import_concrete_provider` 读 `runtime.py` 源码断言不含
  `openai_compat` / `OpenAICompatibleProvider`，强制保持 provider 无关。
- 测试文件分布：`test_sessions`(6) + `test_runtime`(11) + `test_fake_provider`(9) +
  `test_tools`(16) + `test_runtime_streaming`(7) + `test_openai_provider`(~17, 含 1 skip) +
  `test_stream` + `test_sanitize` + `test_cli`(4) + `test_tui`(3) + `test_env` +
  `test_skills_frontmatter`(9) + `test_skills_loader`(18) + `test_skills_prompt`(10) +
  `test_skills_tools`(12) + `test_skills_router`(9) + `test_context_invariants`(6)。

### 边界情况已覆盖

工具异常转 `{"error":...}`、未知工具、畸形参数 JSON 修复、孤立代理项清洗、流式中途取消丢弃
半成品、max-iterations 停止、危险命令默认拦截且模型不可绕过、`on_event` 事件顺序。

### 真实端点

`test_real_endpoint_smoke` 是 opt-in 集成测试，本次整理**未运行**（无 key）。
`README.md` 记载此前曾用 DashScope 的 Qwen OpenAI-compatible 模式验证过一轮真实
`list_directory`→回填→最终答案；该结果为历史记录，非本次复核产出。

---

## Source relationship

完整对照见 [`docs/source-map.md`](source-map.md)。关系图例：

- **PORT**：几乎照搬，保留 Hermes 版权。
- **ADAPT**：派生但解耦/简化，保留版权 + 署名头。
- **REWRITE**：参考 Hermes 的**可观测行为**重写，Aegis 原创（无 Hermes 版权），但记录行为来源。
- **original**：Aegis 原创，无 Hermes 派生。

### 汇总

| Aegis 模块 | 关系 | Hermes 来源 |
|---|---|---|
| `runtime.IterationBudget` | PORT | `agent/iteration_budget.py` |
| `runtime.AgentRuntime.run_turn` | REWRITE | `agent/conversation_loop.py:run_conversation` |
| `runtime.TurnEvent` / `on_event` | REWRITE | `conversation_loop` 的 `_vprint`/`_buffer_vprint` 实时反馈 |
| `events.collect_response`（含 `is_cancelled`/`on_event`） | ADAPT | `agent/chat_completion_helpers.py`（流→统一响应） |
| `models.stream.StreamAssembler` | ADAPT | `agent/chat_completion_helpers.py`（流式工具调用组装） |
| `models.openai_compat` | REWRITE | `interruptible_api_call`/`interruptible_streaming_api_call` |
| `models.sanitize` | ADAPT | `agent/message_sanitization.py` |
| `tools.executor` | ADAPT | `agent/tool_executor.py` 等 |
| `tools.danger` | ADAPT | `tools/approval.py` |
| `context.builder` | ADAPT | `conversation_loop.py`（每轮 `api_messages` 构建） |
| `sessions.memory_store` | REWRITE | `hermes_state.py:SessionDB.append_message` |
| `tools/builtin/*` | REWRITE | `tools/file_tools.py`、`tools/terminal_tool.py` 等（行为等价最小面） |
| `tui._ThinkingRenderable` | ADAPT | `agent/display.py:KawaiiSpinner` |
| `tui` banner / `Tui` | REWRITE | `hermes_cli/banner.py`、`cli.py`（prompt_toolkit 输入） |
| `skills.*` | ADAPT | `agent/skill_utils.py`、`agent/skill_commands.py`、`tools/skills_tool.py`、`agent/prompt_builder.py` |
| `context.system_prompt` | ADAPT | `agent/system_prompt.py:build_system_prompt_parts` |
| `cli._select_provider`、`exceptions`、`env` 等 | original | — |

适配/派生文件均带 Hermes 署名头（`# Portions adapted from Hermes ...`）；
`THIRD_PARTY_NOTICES.md` 收录 Hermes MIT 全文与运行时依赖许可（openai/typer/rich/pydantic/
pyfiglet MIT，prompt_toolkit BSD-3，wcwidth MIT）。

---

## Current limitations and TODOs

### 已知限制（实现层面）

1. **持久化**：只有内存会话，进程退出即丢；无 SQLite/Redis、无 resume/checkpoint、无租约。
2. **上下文压缩**：未实现；`ContextBuilder` 每轮发全量历史，长对话会顶到模型上下文上限。
3. **Skills**：未加载、未路由。
4. **多 provider**：无 failover / rate-guard / 凭证池（Stage 2 有意砍掉）。
5. **工具执行**：顺序执行，无并发；无 guardrails 链；无超大结果外置存储。
6. **TUI**：逐字符流式在**管道/重定向**下因 stdout 缓冲看不出渐进（真 TTY 才可见）；
   banner 在 <120 列的窄终端会被 figlet 折行或由终端自行折行。
7. **真实端点**：本会话未跑真 OpenAI e2e（无 key），仅 fake + 单元测试。

### 已知缺陷 / 风险

- `uv run mypy src` 报 1 个**既有**错误：`src/aegis_agent/models/openai_compat.py:182`
  `sanitize_surrogates(message.tool_call_id)` 传了 `str | None`（`tool_call_id` 可空）。
  属 Stage 2 遗留，与本次 Stage 3 无关，未在本任务范围修复。
- `README.md` 顶部状态标注 "Stage 2"，未随 Stage 3 更新（文档滞后，非代码缺陷）。
- `aegis-agent.png` 仅作项目品牌图（README/文档），终端不可靠显示 PNG，故 CLI 用 ASCII banner。

### 计划的后续里程碑（未实现）

- SQLite 会话存储 + checkpoint/tail recovery + 恢复时 corrupted checkpoint 安全回退到全量重放。
- SQLite/Redis 会话租约（单 owner）。
- 分层上下文压缩（只改派生视图，原文不动）。
- 超大工具结果外置存储 + 预览。
- 循环检测与熔断。
- Skill 加载与路由。
- 真 OpenAI provider 的端到端冒烟（opt-in integration）常态化。

---

## Interview review index

按"一句话能复述"组织，快速复习用。

1. **项目定位**：从 Hermes 抽核心链路做的轻量 Agent Runtime，只保留 CLI + Loop + provider 抽象 +
   工具 + 上下文 + 会话；明确砍掉所有消息集成与品牌 UI。
2. **架构**：单向依赖 `cli → runtime → {models, tools, context, sessions}`；runtime 只依赖四个
   Protocol（`ModelProvider`/`SessionRepository`/`Tool`/`ContextBuilder`），不碰 Typer/SQL/Redis/具体 provider。
3. **Agent Loop**：guard（interrupt + IterationBudget）→ build 派生上下文 → `provider.stream`
   → `collect_response` → 持久化助手消息 → 有工具则执行回填再循环，无工具则 `FINAL_ANSWER`。
4. **原始消息为 source of truth**：发给模型的上下文是每轮重建的副本，清掉 `client_msg_id`/`seq`；
   压缩/恢复未来只改派生视图，原文不动。
5. **流式契约**：provider 产 `ModelEvent`，`collect_response` 折叠成 `ChatResponse`，流式与非流式同形；
   `StreamAssembler` 处理工具调用分片（name 赋值、arguments 拼接、按 index 槽位）。
6. **健壮性**：wire 前清洗孤立代理项、执行前修复畸形参数 JSON（解决 DashScope/Qwen 历史污染崩溃）；
   工具异常统一转 `{"error":...}` 不击穿循环；流式中途取消丢弃半成品。
7. **安全**：`run_shell` 危险命令默认拦截，开关是操作员-only（非工具参数），模型无法绕过。
8. **会话不变量**：幂等于 `client_msg_id`、单调 `seq`、会话隔离；内存版线程安全。
9. **流式 UI**：`on_event` 观察者缝把 `TurnEvent` 回调给 UI，runtime 零改动；prompt_toolkit 输入
   （光标移动/历史）+ rich 输出 + `pyfiglet` banner + `Live` 颜文字 spinner；`DONE` 不转发以免双发 `TURN_END`。
10. **测试**：105 passed / 1 skipped，全离线确定性 fake；守卫测试强制 runtime 不 import 具体 provider；
    integration 真端点 opt-in。
11. **与 Hermes 的关系**：PORT / ADAPT / REWRITE / original 四档，ADAPT 文件带署名头，
    `docs/source-map.md` 逐项可查，不把派生代码当完全原创。
12. **Skills 与动态 prompt**：`SystemPromptBuilder` + `PromptContributor` 缝 → `ContextBuilder` 每轮
    实时渲染系统提示；技能用 progressive disclosure（紧凑索引进 prompt、`skills_list`/`skill_view`
    工具按需取完整内容）；`/skill-name` 斜杠命令经 `SkillRouter.invocation_message` 注入；
    所有技能工具实现现有 `Tool` Protocol 插入 `ToolRegistry`，不搬 Hermes 的全局单例模式；
    原始会话消息全程不碰（source-of-truth 不变式）。

---

*本文档由 `CLAUDE.md` 规定的开发流程产出，仅修改 `docs/development-log.md`，未改源码与测试。
Hermes 仓库为只读参考，未修改。*

---

## Milestone 5 — Stage 5：轻量 MCP 客户端

### Problem and goal

Milestone A 建立了 `SystemPromptBuilder` + `PromptContributor` 和 `Tool` Protocol
两条扩展缝。现在用这两条缝接入外部 MCP（Model Context Protocol）服务器，
让 Aegis 能把任何 MCP 服务器的工具当作原生工具使用。

这是 `CLAUDE.md` §5 "除非明确要求才做"的功能——用户显式要求。范围是轻量级：
**stdio + Streamable HTTP 传输，无可选功能**（无 SSE/OAuth/sampling/断路器）。

### Relevant Hermes behavior

Hermes 的 MCP 实现是一整个 `tools/mcp_tool.py` 模块（~3900 行）。核心架构：
- 一个后台 daemon 线程 event loop，所有 MCP 会话跑在上面
- 跨线程协程调度：`run_coroutine_threadsafe` + 100ms 轮询 `Future`
- 连接：`StdioServerParameters` + `stdio_client`（stdio），`streamable_http_client` + `httpx.AsyncClient`（HTTP）
- Schema 适配：`_normalize_mcp_input_schema` 三阶段 pipeline（local refs / nullable union / object shape repair）+ `strip_nullable_unions`
- 工具注册：`registry.register(schema=..., handler=..., toolset=..., check_fn=...)` 直接用 Hermes 全局单例
- 高级功能：OAuth 2.1 PKCE、SSE、断路器、`tools/list_changed` 动态刷新、sampling

### Migration decision: ADAPT

选择 **ADAPT**：保留 Hermes 的两块最干净复用逻辑（schema 适配器 + 连接/调度），
但适配到 Aegis 的显式 DI + Protocol 架构（MCP 工具包装成 `Tool` Protocol 对象，
而不是直接调 `registry.register` 全局单例），并大幅砍掉高级功能。

### Aegis design and data flow

**依赖**：`mcp` SDK 是 `pyproject.toml` 的可选依赖（`[project.optional-dependencies] mcp`）。
运行时 guarded import：SDK 没装时 `is_available()` 返回 `False`，MCP 功能静默跳过。

**配置** (`mcp/config.py`)：
- `load_mcp_config(path) -> dict[str, dict]` — 读 `~/.aegis/config.yaml` 的 `mcp_servers:` 键
- 递归 `${ENV_VAR}` 插值，merge 默认值（timeout=120, connect_timeout=60, enabled=True）

**Schema 适配** (`mcp/schema_adapter.py`)：
- `sanitize_mcp_name_component` — `[^A-Za-z0-9_]` → `_`
- `normalize_mcp_input_schema` — 三阶段：`_rewrite_local_refs`（definitions→$defs）
  → `_strip_nullable_union`（anyOf [{T}, {null}] → {T, nullable:true}）
  → `_repair_object_shape`（补 type/poperties/修剪 required）
- `convert_mcp_tool(server_name, mcp_tool) -> dict` — 产出 `{name: "mcp_{s}_{t}", description, parameters}`
- `strip_nullable_unions` 从 Hermes `tools/schema_sanitizer.py` **内联**（~50行），避免跨模块依赖

**连接** (`mcp/client.py`)：
- 模块级状态：一个 daemon 线程 + asyncio event loop + `dict[str, _MD]` 服务器表 + `threading.Lock`
- `_ensure_loop()` + `_run_on_loop(coro, timeout)`：跨线程协程调度
- `connect_stdio_server` / `connect_http_server`：建 session → initialize → list_tools
- `call_tool(server_name, tool_name, args, timeout) -> str`：调 `session.call_tool`，收集 text blocks，返回 JSON
- 凭证清洗：`_CREDENTIAL_PATTERNS` 正则 scrub 所有 error text
- `disconnect_all()`：优雅关闭

**工具包装** (`mcp/tools.py`)：
- `MCPToolWrapper` 实现 `Tool` Protocol：存 `definition: ToolDefinition` + 服务器名 + 工具名 + timeout
- `run(arguments, ctx)` → `call_tool` → `ToolResult`
- 永远不抛异常（MCP 调用错误 → `is_error=True` 结果）

**提示注入** (`mcp/guidance.py`)：
- `MCPToolsGuidance` 实现 `PromptContributor`：有服务器连接时 render "MCP tools from N servers are available"

**接线**：
- `with_defaults(enable_mcp=True, mcp_config_path=None)`：读配置 → 连服务器 → 转换 schema →
  `MCPToolWrapper` → `registry.register(wrapper)` → `prompt_builder.add(MCPToolsGuidance)`
- CLI `--mcp-config` / `--no-mcp` flags

### Key files, classes, and functions

- `mcp/config.py`：`load_mcp_config`, `_interpolate_env_vars`, `DEFAULT_MCP_SERVER_CONFIG`
- `mcp/schema_adapter.py`：`sanitize_mcp_name_component`, `normalize_mcp_input_schema`, `convert_mcp_tool`, `_rewrite_local_refs`, `_strip_nullable_union`, `_repair_object_shape`
- `mcp/client.py`：`connect_server`, `call_tool`, `get_server_tools`, `disconnect_all`, `_ensure_loop`, `_run_on_loop`
- `mcp/tools.py`：`MCPToolWrapper(definition, run)`, `build_wrappers`
- `mcp/guidance.py`：`MCPToolsGuidance(render)`

### Reliability invariants, edge cases, and failure handling

- SDK 没装 → `is_available()` = False，MCP 功能静默跳过
- 配置文件缺失 → `load_mcp_config` 返回 `{}`，不抛异常
- 单个服务器连接失败 → 记日志，跳过去，不阻止 Aegis 启动
- MCP 工具调用超时 → `TimeoutError` → `{"error": "MCP call failed: ..."}`
- MCP 服务器崩溃 → `session.call_tool` 抛异常 → 被 `call_tool` 的 except 捕获，返回 error JSON
- MCP 工具返回值含凭证 → `_sanitize_error` regex scrub `[REDACTED]` 代替
- `MCPToolWrapper.run(None)` → `dict(None)` 不抛 → 防御 `if arguments is None: arguments = {}`

### Tests

- `test_mcp_schema_adapter`（20）：名称清洗(3) / 空 schema / 合法 schema /
  definitions→$defs / $ref 重写 / nullable union collapse(4) / object repair(5) / 工具转换(4)
- `test_mcp_config`（8）：文件缺失/无键/非dict/servers加载/非dict条目/skip/默认值merge/ENV插值/未匹配ENV保留/数组插值
- `test_mcp_tools`（5）：前缀/描述/parameters/run error返回/run 不抛
- `test_mcp_guidance`（4）：无服务器 → None / 2 服务器 / 1 服务器单数 / reset

### Source relationship

Schema adapter + config loader + client 为 **ADAPT**；
tools (wrapper) 为 **REWRITE**（Aegis 特有 pipeline）；guidance 为 **original**。
见 `docs/source-map.md` Stage 5 表。

### Trade-offs, remaining limitations, and TODOs

- **只支持 stdio + Streamable HTTP**（无 SSE，无 OAuth）——极轻量，但限制可连接的服务器类型
- **无重连**——connect失败即跳过，server之后断开不自动恢复
- **无断路器**——连续失败的 server 不会自动降级
- **无 dynamic tool refresh**——连接后 tools 列表固定，不支持 `list_changed` 通知
- **无 sampling**——不支持服务器发起 LLM 请求
- **无 utility tools**——不注册 `list_resources` / `read_resource` / `list_prompts` / `get_prompt`

### Interview summary

"Stage 5 做了 MCP 客户端，范围精打细算到最小可用面：stdio + Streamable HTTP 两个传输，
schema adapter 从 Hermes 搬了关键的三阶段归一化 pipeline（`definitions→$defs` /
nullable union 折叠 / object 形状修复），保证同一个 MCP 工具的 inputSchema 在 OpenAI、
Anthropic、Gemini 上都能通过验证。

每个发现的 MCP 工具包装成 Aegis 的 `Tool` Protocol —— 一个 `MCPToolWrapper` 存着
`ToolDefinition` 和 `call_tool` 调度逻辑，这样它可以和内置工具、技能工具一样注册进
`ToolRegistry`，模型无差别调用。

后台是一个 daemon 线程 event loop，`asyncio.run_coroutine_threadsafe` + Future 轮询
做跨线程阻塞调用。显式砍掉了 SSE、OAuth、sampling、断路器等 Hermes 级功能，
保持 Aegis 的轻量身份。"

### 对已有 milestone 的改动

- `docs/development-log.md`：更新测试计数（105→221）、进度表加 Stage 5、模块表加 mcp、溯源表加 mcp 条目、"计划实现"移除 MCP、面试索引加第 13 条
- `docs/source-map.md`：新增 Stage 5 表

*本 milestone 由用户显式要求，不在原始 §4 范围内（§5 "unless explicitly requested"）。*

---

## Milestone 4 — Stage 4：Skills 子系统 & 动态系统提示注入

### Problem and goal

此前 `ContextBuilder` 只用**一条静态字符串**作为系统提示——没有子系统的扩展点。
需要建立一个技能（Skills）子系统，支持：
1. 按 `SKILL.md`（YAML frontmatter + markdown body）格式发现和加载技能；
2. 用 progressive disclosure 模式（紧凑索引进系统提示，完整内容按需取）；
3. 模型通过 `skills_list`/`skill_view` 工具获取技能，用户可以 `/skill-name` 调用。

同时需要先把"静态字符串系统提示"升级为"可组合的动态构建器"，
作为技能（和未来 MCP 等）的注入缝。

### Relevant Hermes behavior

Hermes skills 是 agentskills.io / Anthropic Claude Skills 兼容格式：
目录含 `SKILL.md`（YAML frontmatter: `name`/`description` + markdown body），
可选 `references/`、`templates/`、`scripts/`。

三个激活路径：
- 系统提示里的**紧凑索引**（`<available_skills>`，按 category 分组）→ 模型按需调用 `skill_view`
- 显式 `/skill-name` slash 命令 → `build_skill_invocation_message` 注入
- CLI preload `--skills`。

关键源文件：`agent/skill_utils.py`（解析 + 发现）、`tools/skills_tool.py`
（`skills_list`/`skill_view`）、`agent/skill_commands.py`（路由 + 调用消息）、
`agent/prompt_builder.py:build_skills_system_prompt`（索引注入）、
`agent/system_prompt.py:build_system_prompt_parts`（分层系统提示组装）。

### Migration decision: ADAPT port

技能子系统是 self-contained 的，且与当前 Aegis 架构兼容——
`skills_list`/`skill_view` 按现有 `Tool` Protocol 实现就能插入 `ToolRegistry`。
选择 **ADAPT**（不是 PORT 全局单例、不是全 REWRITE）：
- 保留 Hermes 的前端格式（SKILL.md）、progressive disclosure 设计、slug 路由、
  调用消息格式、紧凑索引格式。
- 适配 Aegis 的显式 DI + Protocol 架构（不使用 Hermes 的全局单例 + AST 发现）。
- 砍掉：prompt injection 扫描器、credential/setup 检查、Curator 遥测、
  plugin 命名空间、prompt-snapshot 磁盘缓存、template-var 替换、
  inline-shell 展开、config 解析、platform-keyed 命令缓存。

同时，系统提示的动态性通过**新的** `SystemPromptBuilder` + `PromptContributor` 缝实现——
这不是 Hermes 的直接移植，而是引用其"分层组装"思想的最小化实现。

### Aegis design and data flow

**动态系统提示** (`context/system_prompt.py`)：
- `PromptContributor` Protocol：`render() -> str | None`
- `SystemPromptBuilder`：identity 头 + 有序 contributor 列表 → `build()` 每次渲染。
  空/None contributor 被移除，所以"无技能"的 prompt 与原来的静态默认 prompt 字节相同。
- 原有的 `DEFAULT_SYSTEM_PROMPT` 保持身份为 `DEFAULT_IDENTITY`。

**技能数据结构** (`skills/models.py`)：
- `Skill`：fully-parsed（frontmatter dict + body + 目录路径）
- `SkillMeta`：name/description/category（紧凑索引用）

**技能发现** (`skills/loader.py`)：
- `SkillLoader(dirs)`（默认 `~/.aegis/skills` 或 `$AEGIS_SKILLS_DIR`）
- `discover()` 有缓存（`force=True` 重扫）；按 name dedupe（先到的赢）。
- 验证：name/description 必填、长度上限（64 / 1024）、平台门控（macos→darwin 映射）。
- Category 从 parent 文件夹名派生（只当 parent 非 search root 时）。

**路由** (`skills/router.py`)：
- `SkillRouter` Protocol（CLAUDE.md §6 要求）
- `DefaultSkillRouter`：slug 归一化（`/My_Skill` → `my-skill`），先精确名匹配再 slug 匹配。
- `invocation_message(skill, instruction)`：`[The "X" skill was invoked ...]` +
  body + `[Skill directory: ...]` + supporting files 列表 + 用户指令。

**渐进式工具** (`skills/tools.py`)：
- `SkillsListTool` (`skills_list`)：返回 `{"skills": [...], "count": N}`，可按 category 过滤。
- `SkillViewTool` (`skill_view`)：`{name, file_path?}` → 完整 body 或指定引用文件（路径遍历守卫）。

**提示注入** (`skills/prompt.py`)：
- `SkillsIndexContributor(loader)` 实现 `PromptContributor`：render `<available_skills>` 块，
  按 category 分组，加上调用 `skill_view` 的指令。无技能时返回 None。

**接线**：
- `AgentRuntime.with_defaults(enable_skills=True, skills_dir=...)` 在 `build_default_registry` 后
  发现并注册技能工具，构造 `SystemPromptBuilder` + `SkillsIndexContributor`，
  传入 `ContextBuilder`，暴露 `SkillRouter`。
- CLI `--skills-dir` / `--no-skills`；`/skill-name instruction` 行经 `_maybe_route_skill` 解析
  → `invocation_message` 代替原始输入传给 `run_turn`。

**关键不变式**：
- 原始消息 unchanged：技能索引只进入 `ContextBuilder` 的**派生视图**，原始会话历史不变。
- `ContextBuilder` 的 `system_prompt` 属性仍是 `str`（从 builder 实时渲染）。

### Key files, classes, and functions

- `context/system_prompt.py`：`PromptContributor` (Protocol), `SystemPromptBuilder.build/add`, `DEFAULT_IDENTITY`
- `context/builder.py`：`ContextBuilder(..., system_prompt: str | SystemPromptBuilder | None)`，backward-compat
- `skills/models.py`：`Skill(name, description, category, directory, skill_md_path, frontmatter, body)`, `SkillMeta`
- `skills/frontmatter.py`：`parse_frontmatter(content) -> (dict, body)`
- `skills/loader.py`：`SkillLoader(discover/get/metas)`, `default_skills_dirs()`, `_matches_platform`, `MAX_NAME_LENGTH=64`, `MAX_DESCRIPTION_LENGTH=1024`
- `skills/router.py`：`SkillRouter` Protocol, `DefaultSkillRouter`, `normalize_skill_key`
- `skills/prompt.py`：`SkillsIndexContributor.render() -> str | None`
- `skills/tools.py`：`SkillsListTool`, `SkillViewTool`, `SKILLS_LIST`/`SKILL_VIEW` schemas
- `runtime.py`：`with_defaults(enable_skills, skills_dir)`, `skill_router` property
- `cli.py`：`_maybe_route_skill`, `--skills-dir`/`--no-skills`

### Reliability invariants, edge cases, and failure handling

- 缺失目录 → 空列表，不抛异常。
- 一个技能格式错误 → 跳过，不影响其他。
- 平台不匹配 → 跳过（debug 日志）。
- `skill_view` 路径遍历：`../etc/passwd` → error result（路径跑出技能目录被拒绝）。
- `skill_view` 文件不存在/技能不存在/cwd 外 → error result（永不抛异常）。
- Name 空/无 → 跳过；collision → 先赢后报警告。
- 工具注册后即使零技能存在，索引 contributor 返回 None，prompt 不变。

### Tests

- `tests/test_skills_frontmatter.py`（9）：正当/缺 fence/YAML 错误回退/非 mapping/CRLF/列表/tags/markdown body
- `tests/test_skills_loader.py`（18）：发现、category 派生、metas、空 dir、name/description 必填/截断、
  名称碰撞、get_by_name、缓存/force、unreadable skip、排除 dir（.git）、platform gate 4 个、
  默认 dir（env/no env）
- `tests/test_skills_prompt.py`（10）：builder 行为（6：identity-only/custom/none drop/empty drop/
  multiple join/strip/empty identity）、skills index（4：render/when none/group by category/general fallback）
- `tests/test_skills_tools.py`（12）：skills_list（5：返回所有/过滤/null category/case-insensitive/空）、
  skill_view（7：body/unknown/missing name/引用文件/绝对路径拒绝/traversal 拒绝/文件缺失/error永不抛）
- `tests/test_skills_router.py`（9）：slug 归一化（5）、resolve（3）、invocation message（3：
  activation note/supporting files/instruction append）
- `tests/test_context_invariants.py`（6）：source unchanged 3路径（string/builder/skills）、
  backward-compat string/None/空
- `tests/test_cli.py` 既有用例通过（非 TTY fallback 路径）

### Source relationship

所有技能模块都是 **ADAPT** 自 Hermes，保留了 Hermes MIT 署名头。
`SystemPromptBuilder` + `PromptContributor` 是**参考 Hermes 分层思想的新实现**。
`cli.py` / `runtime.py` 的接线是 **original**。
详见 `docs/source-map.md` Stage 4 表。

### Trade-offs, remaining limitations, and TODOs

- **Skills 只从 user dir 加载**（不 bundled，不 external dir config）→ 极简。
- **Prompt 缓存**：Hermes 有 disk snapshot 缓存避免每次都 rebuild prompt index；
  Aegis 无，每次都 render（代价低，因索引很小）。
- **Prompt 注入扫描**：Hermes 有 `_INJECTION_PATTERNS` 检查 skill 内容是否含不安全注入；
  Aegis 已砍（轻量面）。
- **Plugin/命名空间**：不支持 `plugin:skill` 限定名（Hermes 有）。
- **Reload**：无 `/reload-skills` 命令；只有 force=True 的 API（CLI 不暴露）。
- **MCP 技能**：下一阶段（Milestone B）是轻量 MCP client，复用的缝已就绪——
  `PromptContributor` 用于工具使用指导，`Tool` Protocol 用于 MCP 工具注册。

### Interview summary

"Stage 4 实现了 Hermes 的 Skills 子系统，但适配到 Aegis 的显式 DI 架构里。
核心设计是 **progressive disclosure**：技能的全部 body 不放进 prompt，只在系统提示里注入
一个紧凑的 `<available_skills>` 索引（名字+描述，按category分组），模型看到相关技能后调用
`skill_view` 工具获取完整指令——这叫 tier 1→tier 2 的两级展开，Hermes 也是这样做的。

用户可以通过 `/skill-name instruction` 显式调用，`_maybe_route_skill` 把斜杠命令解析成
`SkillRouter.invocation_message` 注入到当前轮次的用户输入里。

整个技能的 discover→register→index→view 流程都是通过现有的 `Tool` Protocol 和显式构造的
`ToolRegistry`，Hermes 那种全局单例 + AST 发现模式没搬过来。

另外，为了同时支持技能索引注入和后来的 MCP 工具指导，我先把 `ContextBuilder` 从"一条静态字
符串"升级成了 `SystemPromptBuilder` + `PromptContributor` 缝：`ContextBuilder` 构造时接受
一个 `SystemPromptBuilder`（或普通 str 保持向后兼容），每轮 `build()` 实时调用
`prompt_builder.build()` 渲染系统提示——所以技能索引会随加载的技能集合变化，但原始的会话
消息列表完全不碰，source-of-truth 不变式不改。"
