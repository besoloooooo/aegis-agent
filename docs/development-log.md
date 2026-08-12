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

1. ~~**持久化**：只有内存会话~~ → 已实现（Stage 12：SQLite 会话存储 +
   消息级幂等落盘 + 快照快速恢复 + SQLite/Redis 会话租约；`--resume` / `--db` /
   `--ephemeral` / `--no-lease` / `--snapshot-every-n`）。无 Redis 真机集成测试。
2. ~~**上下文压缩**：未实现~~ → 已实现（Stage 10 移植三阶段管线，Stage 11 接入
   Agent Loop：`--context-max-tokens` / `--no-compress`，超大工具结果转存
   `~/.aegis/tool-result-cache`）。
3. **Skills**：未加载、未路由。
4. **多 provider**：无 failover / rate-guard / 凭证池（Stage 2 有意砍掉）。
5. **工具执行**：顺序执行，无并发；无 guardrails 链。
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

## Milestone 12 — Stage 12：会话恢复（SQLite 持久化 + 快恢复快照 + 跨进程租约）

### Problem and goal

Aegis 此前只有内存会话：进程退出即丢，无 `--resume`、崩溃丢全部未落盘历史、两个
进程可以同时跑同一会话导致重复模型请求与交叉写历史。本里程碑把用户在 Hermes 上
自行实现的会话恢复功能（三个提交）移植到 Aegis：

- `5a51f55` 消息级增量持久化 + 幂等防重，加固 --resume 崩溃恢复
- `181e078` 快恢复快照——session_snapshots 表 + resume 增量重放
- `03e5adc` 可插拔跨进程会话租约（SQLite/Redis），防多进程重复恢复

### Relevant Hermes behavior and source locations

- `hermes_state.py`（~5000 行中的会话持久化部分）：连接配置三要素
  （`check_same_thread=False` / `timeout=1.0` / `isolation_level=None`）、
  `apply_wal_with_fallback`、`_execute_write`（BEGIN IMMEDIATE + 随机 jitter 重试 +
  每 50 写 TRUNCATE checkpoint）、`append_message`（UNIQUE(session_id,
  client_msg_id) 部分索引 + ON CONFLICT DO NOTHING + 计数同事务累加）、
  `write_snapshot` / `load_latest_snapshot` / `get_messages_after_seq` /
  `resume_conversation`、`session_leases` 表方法。
- `session_lease.py`（573 行自包含文件）：后端接口 + SQLite/Redis 后端 +
  心跳管理器。
- 设计文档：`hermes_state_核心机制.md`（用户自写，本里程碑的主要行为依据）。

### Migration decision: ADAPT（存储层）+ 近乎逐字节 PORT（租约层）

- `hermes_state.py` 是一个 5000 行的耦合大文件，且 Aegis 的存储单元是 `Message`
  dataclass 而非 OpenAI dict——**选择适配性移植**：完整搬走事务机器与恢复算法，
  schema 裁剪到 Aegis 字段。砍掉：FTS5 搜索、标题/归档、rewind/undo、压缩链
  （Aegis 压缩不改写源历史、不分叉会话，故 `resolve_resume_session_id` 的
  parent 链行走逻辑不需要）、token 计费、平台消息 id、codex/多模态附加列。
- `session_lease.py` 本来就是自包含单元——**近乎逐字节 PORT**，仅改 env 变量名
  （`AEGIS_SESSION_LEASE_BACKEND` / `AEGIS_REDIS_URL` / TTL / RENEW）、Redis 键前缀
  （`aegis:session_lease:`）、SQLite 后端改为包装 `SQLiteSessionRepository`。

### Aegis design and data flow

**写路径（每次 `_persist`）**：

```
runtime._persist(message) → SQLiteSessionRepository.append_message
  → _execute_write: BEGIN IMMEDIATE → [事务内现算 seq=MAX(seq)+1
    → INSERT ... ON CONFLICT(session_id, client_msg_id) DO NOTHING
    → 真插入才 UPDATE sessions.message_count（同事务）] → COMMIT（= fsync 落盘）
  → 撞锁：20~150ms 随机 jitter 重试（最多 15 次）；每 50 写一次 TRUNCATE checkpoint
```

**恢复路径（Aegis 架构下天然成立）**：runtime 每次模型调用前都从 repository 读
历史，所以 `--resume <id>` 不需要任何"预加载"——用同一个 SQLite repo 打开已有
session 即恢复。`list_messages` 内部走快路径：

```
resume_messages(session_id)
  → load_latest_snapshot（history_version 匹配 + zlib 解压 + CRC32 校验）
  → 有效：快照 dicts + get_messages_after_seq(last_seq) 尾部 → Messages
  → 任何失效（无快照/版本不符/解压失败/校验失败/JSON 损坏/尾部读取异常）
    → 全量重放（永远正确的兜底）
```

快照生成在 CLI 层：每轮结束后 `maybe_write_snapshot(session_id, every_n=20)`
（per-session 游标控制节奏；快照从**已提交的 DB 行**构建，与全量重放共用同一
解码器 `_row_to_dict`，保证字节一致）。

**租约（CLI 启动时）**：

```
_start_lease: get_lease_backend（sqlite=共用会话库 / redis=AEGIS_REDIS_URL，
              不可达 → 报错退出，绝不静默降级）
  → SessionLeaseManager.acquire(session_id)（失败 = 另一进程持有 → 拒绝启动）
  → 心跳线程每 10s 续期（TTL 30s）
  → 续期失败 → on_lost → 设置 interrupt Event → run_turn 在下一个 guard 停止
    （不再发模型请求、不再写消息，避免双写者）
  → REPL 结束 finally：manager.stop()（停心跳 + 释放）→ repo.close()
```

**CLI 新开关**：`--db`（env `AEGIS_DB_PATH`，默认 `~/.aegis/state.db`）、
`--ephemeral`（内存存储）、`--resume/-r`、`--no-lease`、`--snapshot-every-n`
（默认 20，0 关闭）。

### Key files, classes, and functions

- `sessions/sqlite_store.py`：`SQLiteSessionRepository`（SessionRepository Protocol
  实现）——`_execute_write` / `_apply_wal_with_fallback` / `append_message` /
  `write_snapshot` / `load_latest_snapshot` / `resume_messages` /
  `maybe_write_snapshot` / `bump_history_version` / `try_acquire_session_lease` 等
  租约表方法
- `sessions/lease.py`：`SessionLeaseBackend` / `SQLiteSessionLeaseBackend` /
  `RedisSessionLeaseBackend` / `SessionLeaseManager`（心跳 + on_lost 熔断）/
  `get_lease_backend` / `SessionLeaseUnavailableError`
- `cli.py`：`_build_repository` / `_start_lease` / `_maybe_snapshot`；REPL 传入
  `interrupt=lease_lost`
- `pyproject.toml`：新增 `redis` 可选依赖组

### Reliability invariants, edge cases, and failure handling

- **幂等**：同一 client_msg_id 重复 append → 只落一行、返回既有记录、计数不虚增
  （断言 sessions.message_count == 实际行数）。
- **有序**：seq 在写事务内现算；两个写连接交错 append 20 条 → seq 恰好 0..19 无重号。
- **崩溃耐久**：不 close 直接用第二个连接读 → 已 COMMIT 的行全部可见（WAL）。
- **快照等价**：snapshot+tail == 全量重放（Message 级相等）；blob 损坏 / checksum
  错误 / history_version 不符 → 全部安全降级全量重放且结果正确。
- **恢复后续写不重**：resume 后新消息 seq 接续；把恢复出的消息重新 flush（带原
  client_msg_id）→ 全部幂等跳过。
- **租约互斥**：8 个竞争连接抢同一会话 → 恰好 1 个获胜；TTL 过期可回收；过期持有
  者不能续期/释放/通过 is_owner；心跳保活超过 TTL；on_lost 恰好触发一次；
  switch_session 先拿新再放旧、失败保留旧；Redis 不可达抛错不降级。
- **CLI 级**：`--resume` 恢复并显示消息数；不存在的会话报错退出；租约被占时拒绝
  启动、释放后可再入。

### Tests

- `tests/test_sessions_sqlite.py`（16 个）：协议符合性、幂等、隔离、缺会话异常、
  跨实例持久化、双写者并发、快照等价/损坏/校验/版本失效/keep-N/节奏、恢复续写
  不重、runtime 级 resume 集成（新进程 provider 看到上一轮完整历史）。
- `tests/test_session_lease.py`（19 个）：SQLite 后端全场景 + 管理器心跳/熔断/
  切换 + 后端选择 + Redis 后端（内存 fake client 实现 set NX/PX + Lua 语义；
  中途宕机触发熔断）。
- `tests/test_cli.py`：既有用例改为 hermetic（AEGIS_DB_PATH 指向 tmp）；新增
  `--resume` 恢复、未知会话报错、租约占用拒绝启动 + 释放后可入。
- 全量 `uv run pytest -q`：352 passed / 1 skipped / 1 failed（唯一失败仍是
  Stage 8 遗留的网络用例 test_web_extract_blocks_private_url，与本次无关）。
- `ruff check` / `mypy`（全部改动文件）：零告警 / 零错误。

### Trade-offs, remaining limitations, and TODOs

- **压缩链解析未移植**：Aegis 压缩不分叉会话，源历史始终在单会话内——若未来引入
  改写源历史的机制，需带回 `resolve_resume_session_id` 的链行走逻辑。
- **history_version 暂无 bump 调用方**（Aegis 还没有 /undo 等历史改写）；机制已就绪。
- **租约丢失后的停止是"下一个 guard"粒度**：正在飞行中的模型请求完成后才停，
  可能多写一条 assistant 消息——幂等键保证不产生重复行，但可能与其它进程交错
  一条。Hermes 行为相同（熔断翻转标志位）。
- **Redis 后端只经 fake client 单测** ~~真 Redis 集成测试未移植~~ → **已补**
  （后续跟进）：`tests/lease_worker.py` + `tests/test_session_lease_dualprocess.py`
  移植了双进程子进程测试（单胜者 / 不同会话并行 / 正常退出即刻接管 / kill -9 后
  TTL 接管 / TTL 前拒绝），`tests/test_session_lease_redis_live.py` +
  `tests/docker-compose.redis.yml` 提供真 Redis 集成测试（`integration` marker +
  `AEGIS_TEST_REDIS_URL` 门控，默认不跑）。
- **内存存储 + 租约** ~~时锁落在默认路径的 SQLite 库上~~ → **已修**（后续跟进）：
  `--ephemeral` 且未显式设置 `AEGIS_SESSION_LEASE_BACKEND` 时 CLI 直接跳过租约
  （内存会话无跨进程共享状态，无从冲突），不再触碰默认路径锁库；显式配置后端
  的操作者意图仍被尊重。

### Interview summary

"这个里程碑把我在 Hermes 上做的会话恢复三件套移植到了 Aegis。存储层是适配性移植：
搬走了整套事务机器——WAL、BEGIN IMMEDIATE 进事务就抢锁、撞锁后随机 jitter 重试、
每 50 写 TRUNCATE checkpoint——加上消息级幂等落盘（UNIQUE(session_id,
client_msg_id) 部分索引 + ON CONFLICT DO NOTHING，计数和 INSERT 同事务），但 schema
裁剪到 Aegis 的 Message dataclass，FTS/标题/undo/计费这些上游功能全部砍掉。
恢复在 Aegis 架构下特别干净：runtime 每轮都从 repository 读历史，所以 resume 不需要
预加载——list_messages 内部直接走「最新有效快照 + 尾部增量」快路径，快照用 zlib 压缩
加 CRC32 校验，history_version 不符或任何损坏都安全降级全量重放。租约层是近乎逐字节
移植：SQLite/Redis 双后端、心跳保活、续期失败立即熔断——熔断信号接到 run_turn 的
interrupt event 上，循环在下一个 guard 停止，避免双写者。测试覆盖了 §9 的核心不变式：
幂等、单调有序、会话隔离、快照等价于全量重放、损坏快照安全降级、同会话单一租约主。"

### 对已有 milestone 的改动

- `cli.py`：新增会话存储/租约/快照接线与 CLI 开关（Stage 11 的压缩开关之上）。
- `sessions/__init__.py`：导出 SQLite 存储与租约组件。
- `tests/test_cli.py`：既有用例改为 hermetic DB 路径。
- `docs/source-map.md`：新增 Stage 12 表。

*Hermes 仓库为只读参考，未修改。*

---

## Milestone 11 — Stage 11：压缩管线接入 Agent Loop + 遗留风险清零

### Problem and goal

Milestone 10 把三阶段上下文压缩管线移植进了 `context/` 包，但它还是一个"无人调用的
库"：`run_turn` 仍然每轮把全量派生上下文直接发给模型。本里程碑完成接线并解决
Milestone 10 报告中列出的全部 Remaining risks：

1. **接入 Agent Loop**：每次模型调用前对派生上下文调用 `compress_context`；
2. **跨轮 ContentReplacementState**：由 runtime 按 session 持有并跨轮传入（保提示缓存
   前缀逐字节稳定）；
3. **reasoning_content 不再是 no-op**：`Message`/`ChatResponse` 增加该字段，provider
   捕获、会话持久化、压缩转换器双向携带；
4. **tiktoken 成为正式依赖**：token 估算从字符粗估升级为精确计数；
5. **工具名单补齐 Aegis 内置工具**：`list_directory` 加入 MICRO 可压缩名单；
6. **摘要采样参数落地**：`OpenAICompatibleProvider` 支持 `temperature`，CLI 用
   `temperature=0` + `SUMMARY_MAX_TOKENS` 构造独立的确定性摘要 provider。

### Migration decision: new wiring code (original) + small provider/model-layer extensions

本里程碑几乎全是 Aegis 侧的新接线代码；模型层的 `reasoning_content` 捕获参考了
Hermes 原型中思维链字段的可观察行为（delta.reasoning_content 增量、持久化但不回传
wire）。

### Aegis design and data flow

**运行时接线**（`runtime.py`）：

```
run_turn 循环每次迭代：
  source = repository.list_messages(session_id)     # 原始历史（永不被压缩触碰）
  api_messages = context_builder.build(source)      # 派生视图
  if context_token_budget is not None:
      api_messages = compress_context(
          api_messages, provider, budget,
          storage_dir=compress_storage_dir,          # 默认 ~/.aegis/tool-result-cache
          budget_state=self._budget_state_for(session_id),   # ← 跨轮冻结决定
          summary_provider=self._summary_provider,            # ← 确定性摘要 provider
      )
  provider.stream(api_messages, ...)
```

- `AgentRuntime(..., context_token_budget=None, compress_storage_dir=None,
  summary_provider=None)`：`None` 预算 = 压缩完全关闭（默认，向后兼容；
  `AgentRuntime` 直接构造的既有测试零影响）。
- `_budget_states: dict[session_id, ContentReplacementState]`：**每个会话一个账本**，
  随 runtime 存活；两个会话永不共享替换决定（无跨会话泄漏）。同一条工具结果一旦
  被转存替换，后续每轮从 `state.replacements` 查表重放同一字符串——发给模型的
  上下文前缀逐字节稳定，prompt cache 持续命中。
- 压缩只改派生视图：持久化路径（`_persist`）写回的永远是真实响应消息，摘要/预览
  不会进入会话存储。

**reasoning_content 纵向贯通**：

```
provider 流式 chunk delta.reasoning_content
  → StreamAssembler.feed 产出 ModelEvent.reasoning_delta（新事件 kind）
  → collect_response 折叠进 ChatResponse.reasoning_content
  → run_turn 持久化为 Message.reasoning_content
  → compress 的 message_to_dict / dict_to_message 双向携带
  → 压缩管线的思维链清理（micro_compact Step 3 / 单轮兜底 Step 3.5）由此生效
  → 但 _to_wire_message 刻意不回传 wire（DeepSeek 等 reasoner 会拒绝/忽略历史中的
    reasoning_content；canonical 行为就是不重放思维链）
```

**摘要 provider**（`cli.py`）：主 provider 是 OpenAI 兼容时，
`_build_summary_provider` 用 `OpenAICompatibleProvider.from_env(stream=False,
temperature=0.0, max_tokens=SUMMARY_MAX_TOKENS)` 构造独立摘要 provider（对应原型
`temperature=0.0` 的确定性要求）；构造失败或 fake provider 时回退 None（= 用主
provider），CLI 永不因摘要器而无法启动。

**CLI 配置**：`--context-max-tokens`（env `AEGIS_CONTEXT_MAX_TOKENS`，默认
120_000）、`--no-compress` 全关。

### Key files, classes, and functions

- `runtime.py`：`AgentRuntime` 三个新参数、`_budget_states`、`_budget_state_for`、
  `run_turn` 循环内的压缩调用、assistant 消息持久化携带 reasoning_content
- `context/compress.py`：`compress_context` / `_compress_context` 新增 `budget_state` /
  `summary_provider` 关键字参数；`message_to_dict` / `dict_to_message` 双向携带
  `reasoning_content`
- `models/base.py`：`Message.reasoning_content`、`ChatResponse.reasoning_content`
- `events.py`：`ModelEventKind.REASONING_DELTA` + `collect_response` 折叠
- `models/stream.py`：`StreamAssembler.feed` 捕获 `delta.reasoning_content`
- `models/openai_compat.py`：单发响应捕获 reasoning；`temperature` 构造参数 +
  `from_env` 透传；`_to_wire_message` 不回传 reasoning；顺带修了 Stage 2 遗留的
  `sanitize_surrogates(str | None)` mypy 错误（本文件内的一行类型收窄）
- `models/fake.py`：`FakeReply.reasoning`（测试/演示用）
- `cli.py`：`--context-max-tokens` / `--no-compress`、`_build_summary_provider`、
  `DEFAULT_CONTEXT_MAX_TOKENS = 120_000`
- `context/compress_config.py`：MICRO 名单补 `list_directory`
- `pyproject.toml` / `uv.lock`：`tiktoken` 依赖

### Reliability invariants, edge cases, and failure handling

- **原始历史不变**：测试断言 run_turn 后持久化历史与压缩前逐条相等（仅新增真实的
  user/assistant 消息），摘要标签绝不进入会话存储。
- **跨轮字节稳定**：5×18k 并行工具结果（单条低于一级阈值、合计超二级预算）触发聚合
  级转存；两个连续 turn 发给模型的工具消息逐字节相等，`state.replacements` 有记录、
  转存文件存在。
- **会话隔离**：两个 session 各持一个 state（白盒断言）。
- **默认关闭**：未配置预算时 provider 看到的上下文与接线前完全一致（回归测试）。
- **reasoning 不回传 wire**：spy client 断言 `messages` 载荷里没有
  `reasoning_content` 字段；同时 temperature/max_tokens 正确落入 create() kwargs。
- **摘要 provider 回退**：fake 主 provider → `_build_summary_provider` 返回 None。
- **估算器无关性**：全部压缩测试的预算都按 `_estimate_tokens` 实测值动态计算
  （`_budget_for`），tiktoken 精确计数与字符兜底两种路径下同绿。

### Tests

- `tests/test_runtime_compression.py`（新增 10 个）：默认关闭回归 / run_turn 压缩接线
  （模型看到摘要、历史不变、摘要走 summary_provider）/ 跨轮 state 冻结+字节一致 /
  会话隔离 / reasoning 持久化 / 流式 reasoning 捕获 / wire 不回传+采样参数 /
  单发 reasoning 捕获 / CLI 摘要 provider 回退。
- `tests/test_context_compress.py`：预算全部改为相对估算（tiktoken/兜底两路径同绿）；
  roundtrip 测试覆盖 reasoning_content；新增「历史轮 reasoning 清除、当前轮保留」
  公开 API 测试（18 个用例）。
- 受影响面回归：runtime/stream/openai_provider/fake_provider/context_invariants/cli
  共 80 个用例全绿；全量 `uv run pytest -q`：313 passed / 1 skipped / 1 failed
  （唯一失败仍是 Stage 8 遗留的网络用例 `test_web_extract_blocks_private_url`，
  真实 Tavily 400，与本次无关）。
- `uv run ruff check`（全部改动文件）：零告警；`uv run mypy`（全部改动模块）：
  零错误（含修掉的 Stage 2 遗留那 1 个）。

### Trade-offs, remaining limitations, and TODOs

- **budget state 随 runtime 存活**：进程退出即丢（与内存会话同级）。未来 SQLite 会话
  存储落地时，`ContentReplacementState` 需要随会话持久化/重建——重建是安全的
  （内容寻址转存文件还在，重放会重新做出相同决定），但严格的前缀稳定需要持久化
  `replacements` 映射。
- **转存目录无 GC**：`~/.aegis/tool-result-cache` 只增不减；内容寻址保证不重复写，
  但需要清理策略（后续里程碑）。
- **TUI 不渲染 reasoning**：REASONING_DELTA 事件被 TurnEvent 映射跳过（UI 后续可加
  灰色思维链展示）。
- **默认预算 120k** 是保守通用值；不同模型上下文窗口差异大时由
  `--context-max-tokens` 调整。

### Interview summary

"上个里程碑移植的压缩管线这个里程碑正式接进了 Agent Loop：run_turn 每次模型调用前
对派生上下文跑三阶段压缩，原始历史始终不动。关键设计是 runtime 按 session 持有一本
ContentReplacementState 账——同一条工具结果第 N 轮被转存成什么预览，第 N+1 轮就
逐字节重放什么预览，模型端的前缀缓存因此一直命中；两个会话各持一本账，互不泄漏。
顺手把 Milestone 10 列的遗留风险全清了：Message 和 ChatResponse 加了
reasoning_content 字段，provider 流式捕获、会话持久化、压缩管线双向携带并参与清理，
但刻意不回传给模型（reasoner 类 provider 会拒绝）；tiktoken 成为正式依赖，token
估算从字符粗估变精确；摘要调用有了独立的确定性 provider（temperature=0）；CLI 暴露
--context-max-tokens / --no-compress。所有压缩测试的预算都改成按实测 token 动态计算，
两种估算路径下同绿；跨轮字节稳定有专门的行为测试锁定。"

### 对已有 milestone 的改动

- `models/base.py` / `events.py` / `models/stream.py` / `models/fake.py`：
  reasoning_content 贯通（ChatResponse 新增字段，向后兼容默认值）。
- `models/openai_compat.py`：temperature 参数 + reasoning 捕获/不回传 +
  一处 Stage 2 遗留 mypy 修复。
- `runtime.py` / `cli.py`：压缩接线与新 CLI 开关。
- `context/compress.py` / `compress_config.py`：新参数与名单补充。
- `docs/source-map.md`：新增 Stage 11 表。

*Hermes 仓库为只读参考，未修改。*

---

## Milestone 10 — Stage 10：上下文压缩管线（三阶段会话剪裁与按轮摘要）

### Problem and goal

Aegis 此前没有任何上下文压缩：`ContextBuilder` 每轮把全量历史发给模型，长对话必然顶到
模型上下文上限（见上文「已知限制」第 2 条）。本里程碑把 Hermes 仓库内
`ctx-compress-opt/` 原型中的**会话剪裁与压缩实现**整体移植到 Aegis 并适配——注意是
**移植**（保留算法与行为），不是参考重写。要求的能力：

1. 超大工具结果不硬截断：完整内容转存磁盘，发给模型的只留预览 + 文件路径；
2. 上下文超限时先做一轮本地渐进式清理（去重/摘要化旧工具结果、瘦身参数、删历史
   reasoning），尽量不惊动 LLM；
3. 仍超限时按对话轮次调用 LLM 生成结构化摘要（保留原始 user 问题）；
4. 只剩一轮或摘要压完仍超限时，有逐级激进的单轮兜底；
5. 全流程不修改原始消息历史（source of truth），压缩只作用于派生上下文。

### Relevant Hermes behavior and source locations

源是 `hermes-agent/ctx-compress-opt/` 目录下的四个内聚文件（同一套管线的依赖闭包）：

- `compress.py` —— 主编排：`_compress_context` 三阶段管线 + `_handle_single_round_overflow`
  单轮兜底 + 阶段 C 的轮次切分/摘要序列化/摘要可用性判断；
- `compress_config.py` —— 全部阈值、占位符、标记串、可压缩工具名单的唯一来源；
- `micro_compact.py` —— 阶段 B：阈值触发的渐进式微压缩（本地、无 LLM 调用）；
- `tool_budget.py` —— 阶段 A：两级工具结果预算（单条阈值 + 单轮聚合预算）+ 转存磁盘
  + 预览替换 + 跨轮状态（ContentReplacementState）+ read_file 回读防死循环第三级兜底。

### Migration decision: whole-unit PORT + boundary ADAPT

四个文件是同一行为单元（compress.py 惰性依赖另外两个模块，三者共享 compress_config
的标记串协议，改一个字符即失配），拆分只会增加风险，因此选择**整体移植**：

- `compress_config.py` / `tool_budget.py`：近乎逐字节 PORT（后者本来就是「只依赖标准库、
  可整体拷走」的设计）；
- `micro_compact.py`：PORT，仅把扁平同目录导入改为包内绝对导入；
- `compress.py`：PORT + 边界 ADAPT（见下）。

砍掉的原型死代码（明确记录）：`_handle_single_round_overflow_v1` / `_v2`（旧版兜底，
管线中无任何调用方）、`_truncate_oversized_tools`（已被 tool_budget 路径取代，无调用方）、
`__main__` 自测块。

### Aegis design and data flow

公开入口（新代码）：`compress_context(messages, llm_provider, max_tokens, *, storage_dir=None)`
，输入输出都是 Aegis 的 `Message` dataclass 序列。边界转换器 `message_to_dict` /
`dict_to_message` 是唯一的适配层——**算法核心仍然操作 OpenAI 形状的 dict，与原型逐字节
一致**，这保证了移植行为不漂移。

数据流：

```
list[Message] --message_to_dict--> list[dict]
  → 阶段 A：tool_budget.apply_budget（无条件执行；单条 >20k 字符转存磁盘换预览，
    同批并行结果合计 >80k 再从大到小转存；read_file 读回缓存的第三级硬截断防死循环）
  → 已达标则返回
  → 阶段 B：micro_compact（保护头部 system/运行时标记/已压缩摘要区 + 末尾最近 5 条；
    区间内去重 → 一行信息化摘要旧工具结果 → JSON 感知截断工具参数 → 清历史 reasoning；
    每步后重估，达标即返回）
  → 已达标则返回
  → 阶段 C：_split_into_rounds 按轮切分，最后一轮永不压缩；从最早完整轮次起逐轮
    LLM 摘要（原 user 问题 + "[Context Summary]" assistant 替换整轮），直到达标；
    摘要失败/不可用 → 保留原轮次，绝不用兜底文本替换
  → 仍超限 → _handle_single_round_overflow 单轮兜底（复用阶段 B 工具摘要 → 清历史
    reasoning → 缩参数 → 当前轮 reasoning 去重/头尾截断/清空 → 硬截断工具结果 →
    原子删除最早工具调用组）
--dict_to_message--> list[Message]
```

关键适配点（相对原型）：

| 原型依赖 | Aegis 适配 |
|---|---|
| `configs.config` / `utils.log_utils` | 标准库 `logging.getLogger(__name__)` |
| `await llm_provider.chat(messages, model=..., temperature=0.0, max_tokens=...)` | 同步 `ModelProvider.stream()` + `collect_response()`；模型名/采样参数由 provider 自持（Protocol 不含这些参数） |
| `ROOT_PATH/tool-budget-cache` 硬编码 | `storage_dir` 注入，默认 `~/.aegis/tool-result-cache` |
| 可选的 `agent.redact` | 砍掉，仅保留正则脱敏兜底（gh token / Bearer / sk-） |
| `from compress_config import ...` 扁平导入 | 包内绝对导入 |
| `messages[1]` 直接取下标 | 补了 `len > 1` 与 content 为 None 的守卫（原型在极端短列表下会 IndexError） |

### Key files, classes, and functions

- `context/compress.py`：`compress_context`（公开入口）、`_compress_context`（三阶段管线）、
  `_handle_single_round_overflow`、`_split_into_rounds`、`_is_complete_round`、
  `_serialize_round_for_summary`、`_summarize_round`、`_estimate_tokens`（tiktoken 惰性
  导入 + 字符/2.5 兜底）、`message_to_dict` / `dict_to_message` / `estimate_tokens`
- `context/compress_config.py`：全部阈值/标记串/工具名单（`CONTEXT_SUMMARY_TAG`、
  `PERSISTED_OUTPUT_TAG`、`KEEP_RECENT_MESSAGES=5`、两份 COMPACTABLE_TOOLS 名单等）
- `context/micro_compact.py`：`micro_compact`、`_clearable_ranges`、`_deduplicate_tool_results`、
  `_summarize_old_tool_results`（一行信息化摘要）、`_truncate_tool_call_args`（JSON 感知）
- `context/tool_budget.py`：`apply_budget`、`maybe_persist_large_tool_result`、
  `enforce_tool_result_budget`、`ContentReplacementState`、`is_readback_of_persisted`、
  `hard_truncate_readback`
- `context/__init__.py`：导出 `compress_context` / `estimate_tokens` / `message_to_dict` /
  `dict_to_message`

### Reliability invariants, edge cases, and failure handling

- **原始消息绝不被修改**：公开入口先转 dict 副本再压缩；单轮兜底内部 `copy.deepcopy`；
  测试用深拷贝快照逐一断言输入不变。
- **工具调用协议合法**：删除只按「assistant tool_calls + 对应 tool 结果」整组原子删除；
  测试断言结果集中 tool 消息的 `tool_call_id` 与 assistant 发起的调用严格相等（无孤儿）。
- **宁可超预算也不丢内容**：转存失败原样返回；摘要失败/为空/命中拒答前缀 → 保留原轮次；
  每个阶段都包在 try/except 中，压缩自身失败绝不中断 Agent 主循环。
- **转存防死循环**：`read_file` 读回我们自己转存的缓存文件时不再次转存（第三级就地硬截断），
  测试锁定该行为。
- **已压缩摘要区受保护**：头部保护识别连续的 `(user, "[Context Summary]" assistant)` 对；
  二次压缩不重复摘要、不丢弃、不重复搬入（有回归测试）。
- **最后一轮永不压缩**；不完整轮次（结尾不是无 tool_calls 的非空 assistant）原样保留。
- **思维链字段**：`reasoning_content` 的清理逻辑完整移植；Aegis 的 `Message` 目前不携带该
  字段，相关步骤在 Aegis 消息上天然是 no-op（保留以与上游行为一致，未来 provider 支持
  思维链时自动生效）。

### Tests

`tests/test_context_compress.py`（17 个用例，全部确定性，fake provider，无需真实 API）：

- token 估算（全字段计数 / 公开入口接受 Message）；
- Message↔dict 边界：roundtrip、tool 字段、非字符串 content 容错；
- 轮次切分与完整性判断（5 条规则）；
- 阶段 A：25k 字符工具结果转存 tmp_path、预览含路径、磁盘文件字节一致、输入不变；
  read_file 读回缓存 → 不再转存而是就地硬截断（防死循环）；
- 阶段 B：重复工具结果去重（旧的换回指占位、保护尾部不动）、旧结果信息化一行摘要
  （`[terminal] ran \`npm test\` -> exit 0, ...`）、输入不变；
- 阶段 C：5 轮超限对话压缩到达标、摘要数==provider 调用数、原 user 问题保留、最后一轮
  完整保留、system 在头部；摘要提供者抛异常 / 返回拒答文本 → 原轮次保留、无摘要注入；
  已有摘要区的二次压缩保护；
- 单轮兜底：硬截断超大工具结果（结构保留、达标）；最后手段整组删除工具调用组
  （协议完整性断言）。

结果：`pytest tests/test_context_compress.py` 17/17 通过；全量 `uv run pytest -q`
303 passed / 1 skipped / 1 failed——唯一失败是 `test_web_tools.py::test_web_extract_blocks_private_url`
（Stage 8 未提交工作区里的网络相关用例，真实调用 Tavily 返回 400，与本里程碑代码无
任何共享路径，移植前后的失败与本次改动无关）。`uv run ruff check` 新增文件零告警
（移植代码中刻意的「catch-all 降级」按项目惯例标注 `# noqa: BLE001 — 理由`）；
`uv run mypy src/aegis_agent/context/` 零错误。

### Source relationship

四个压缩模块均为 **PORT**（`micro_compact.py` 与 `compress.py` 含适配），保留 Hermes
署名头；`compress.py` 中的 `message_to_dict` / `dict_to_message` / `compress_context` /
`estimate_tokens` 边界层为 **original** 新代码。详见 `docs/source-map.md` Stage 10 表。

### Trade-offs, remaining limitations, and TODOs

- **尚未接入 Agent Loop**：`runtime.py` 的 `run_turn` 目前仍直接发 `ContextBuilder` 的
  全量派生视图；把 `compress_context` 挂到每次模型调用前（含 max_tokens 配置项与
  storage_dir 约定）是下一个里程碑。
- **跨轮缓存稳定的 state 未接入**：`tool_budget.apply_budget` 支持跨轮
  `ContentReplacementState`（保证提示缓存前缀逐字节稳定），当前公开入口每次新建一次性
  state；接入 Loop 时应由会话级持有者传入。
- **两份可压缩工具名单**（MICRO / FALLBACK）按原型原样保留，含 Aegis 不存在的工具名
  （浏览器/视觉/高德 MCP）——只是字符串，无副作用；是否裁剪留给后续统一决策。
- **tiktoken 为可选增强**：未加入依赖；缺失时走字符/2.5 粗估（原型同款兜底）。
- 摘要调用的 temperature/max_tokens 由 provider 配置决定（Aegis 的 ModelProvider
  Protocol 不含采样参数），原型中的 `temperature=0.0` 约束需在 provider 层落实。

### Interview summary

"这个里程碑把 Hermes `ctx-compress-opt` 原型的上下文压缩管线整体移植进了 Aegis。
它是一个三阶段级联：阶段 A 用 tool_budget 把超大工具结果转存磁盘、只给模型看预览；
阶段 B 用 micro_compact 做本地渐进清理（去重、一行信息化摘要旧工具结果、JSON 感知
截断参数、清历史思维链），往往这一步就压回阈值、省掉 LLM 调用；阶段 C 才按轮调用
LLM 生成结构化摘要，最早的完整轮次先压，最后一轮和已有摘要区受保护；还有单轮兜底
处理'只剩一轮也超限'的极端情况，最后手段是原子删除整个工具调用组、绝不留孤儿。
移植策略是保留算法核心逐字节不变——它操作 OpenAI dict——然后只在外面包了一层
Aegis `Message` 的边界转换器，这样行为不漂移、还能直接复用原型成熟的降级语义：
摘要失败就保留原轮次，转存失败就原样发送，任何一步出错都不中断主循环。
适配点主要是四处：同步 ModelProvider Protocol 替代原来的 async chat 接口、
stdlib logging 替代项目日志器、storage_dir 依赖注入替代硬编码路径、包内绝对导入。
测试用 fake provider 全覆盖三个阶段和兜底，并锁定核心不变式——原始消息永不被修改。"

### 对已有 milestone 的改动

- `src/aegis_agent/context/__init__.py`：新增压缩管线的公开导出（不影响既有导出）。
- `docs/source-map.md`：新增 Stage 10 表。

*Hermes 仓库为只读参考，未修改。*

---

## Milestone 9 — Stage 9：技能管理工具（skill_manage）

### Problem and goal

迁入最后一个工具 **`skill_manage`**——技能的安装/卸载/更新/列表。Aegis 已有
`SkillLoader`（发现 `SKILL.md`）和 `skills_list`/`skill_view` 两个展示工具，但缺少
变写能力。Hermes 的安装/卸载/更新逻辑藏在一个庞大的 `skills_hub.py`（多注册表源 + 隔离 +
安全扫描 + 审计日志）和一个 `skill_manager_tool.py`（作者工具，非安装工具）里，并且安装
工具本身不作为 agent tool 暴露——它是 CLI 命令面。Aegis 把它做成一个标准的 `Tool` Protocol
工具，对接现有 `SkillLoader`。

### Relevant Hermes behavior

- `tools/skills_hub.py`：`install_from_quarantine`（移入 skills dir 前先隔离+扫描）、
  `uninstall_skill`（仅卸锁记录中的，锁条目路径受多层防护）、`bundle_content_hash`（排序
  relpath + 内容 SHA-256）、`_resolve_lock_install_path`（逐分量拒绝符号链接、resolve 后
  拒绝逃逸与直指 SKILLS_DIR 根）、`HubLockFile`（`{version:1, installed:{name:{source,
  content_hash, install_path}}}`）
- `tools/skill_manager_tool.py`：作者端 `skill_manage`（create/edit/patch/delete/write_file/
  remove_file 六动作，是**写本地 SKILL.md 的作者工具**，非 install/uninstall）

### Migration decision: 混合（ADAPT + REWRITE）

- `skills/install.py` → **ADAPT**：保留锁文件模型、路径双层安全防护、内容哈希、"只卸锁记录的"
  守卫、rmtree 前重名校验。**丢弃**隔离阶段、扫描/信任度/标识符、多注册表源路由、审计日志、
  网站策略、SSRF 重定向链、出处签名。Aegis install 源仅两个：本地目录复制，或直接 URL 下载
  单个 SKILL.md（httpx，M3 已添加）。更新 = 重新获取 + 哈希比对 + force 重装。
- `skills/manage_tool.py` → **REWRITE**（实现 `Tool` Protocol，动作 `install/uninstall/update/list`）。
- 注册在 `runtime.with_defaults` 的 `enable_skills` 分支中，与 `SkillsListTool`/`SkillViewTool` 并列。

### Aegis design and data flow

- `skill_manage` `{action, source?, name?, force?}`：`install` 把 source（本地目录或 URL）复制/
  下载到 `skills_dir/<name>/`，锁文件记录，`loader.discover(force=True)` 刷新索引。
  `uninstall` 查锁→校验路径→rmtree→去锁→重扫。`update` 重新获取源→算哈希→同则 up_to_date、
  异则 force reinstall。`list` 返回锁条目。
- 锁文件 `<skills_dir>/.aegis-lock.json`：`{version:1, installed:{name:{source, install_path, content_hash}}}`。
- 路径安全（Hermes 双层防护）：`_valid_name`（禁 `..`/`/`/超长）；`_resolve_install_path`：
  ① 逐分量拒绝 `is_symlink()`/`is_junction()`；② `resolve()` 后 `is_relative_to(skills_dir)` +
  `!= skills_dir`（防 `rmtree` 致灾）。

### Key files, classes, and functions

- `skills/install.py`：`SkillLock`、`install_skill`、`uninstall_skill`、`update_skill`、`list_installed`、
  `_valid_name`、`_resolve_install_path`、`_is_redirect`、`_dir_hash`、`_read_skill_name`
- `skills/manage_tool.py`：`SKILL_MANAGE`（schema）、`SkillManageTool`
- `runtime.py`：注册于 `with_defaults` 的 `enable_skills` 分支

### Reliability invariants, edge cases, and failure handling

- **路径安全双层**：源树中符号链接被拒绝；安装目标的分量间符号链接被拒绝；解析后不在
  skills_dir 内的路径被拒绝；解析后等于 skills_dir 根的被拒绝（防全员删除）。
- **锁门**：只有经 install 记录在锁中的技能才可卸载/更新；手放的 builtin 技能不被误删。
- **force 门**：已存在同名技能时默认拒绝（`force=True` 才覆盖）。
- **加载器刷新**：每次 install/uninstall/update 后调用 `loader.discover(force=True)`，工具
  立即可见变化。
- **永不抛异常**：校验失败、fetch 失败、文件操作失败全部转为 `{success:false, error}` 结果。

### Tests

- `tests/test_skill_manage.py`（12）：本地目录安装/重复拒绝/force 覆写/卸载/卸载未安装/
  update 无源/update up_to_date/update 检测改动/list 列表/缺 source/URL 安装（monkeypatch
  httpx 假响应）/非法 action。

### Source relationship

`skills/install.py` 为 **ADAPT**；`skills/manage_tool.py` 为 **REWRITE**。详见 `docs/source-map.md` Stage 9。

### Trade-offs, remaining limitations, and TODOs

- 仅支持本地目录与直接 URL；无 Git/GitHub 多文件下载、无 registry 概念。
- 无隔离/安全扫描阶段（Hermes 的 `skills_guard` 未迁）——信任操作者。
- Update 对本地目录源重新计算源目录哈希后对比，未做 diff 或增量更新。
- 不能更改已安装技能的 source 重定向（要改只能先卸后装）。

### Interview summary

"这个里程碑给 Aegis 加上了技能管理——`skill_manage` 工具支持 install/uninstall/update/list。
它背后是一个从 Hermes `skills_hub.py` 裁剪出来的轻量 install 模块：一个锁文件记录每个技能的
源、安装路径、内容哈希；安装路径被双层安全防护（拒绝符号链接、必须留在 skills_dir 内且不等于
根）；卸载必须锁中有记录；更新 = 重新获取源 + 哈希比对 + force 重装。安装源支持本地目录
（含 SKILL.md）和直接 URL 下载单文件 SKILL.md。每次变更后调用 `loader.discover(force=True)`
刷新索引，`skills_list`/`skill_view` 立即可见变化。Hermes 的隔离/安全扫描/多注册表/审计日志
都砍掉了——Aegis 保持了它的轻量定位。"

### 对已有 milestone 的改动

- `docs/source-map.md`：新增 Stage 9 表。

*Hermes 仓库为只读参考，未修改。*

---

## Milestone 8 — Stage 8：Web 工具（web_search / web_extract）

### Problem and goal

迁入 **Web** 两件套：`web_search`（网页搜索）与 `web_extract`（网页正文抓取）。Hermes 的
实现深度绑定付费 API/自建后端（Firecrawl/Tavily/Exa/Parallel）与插件注册表，默认无 key 不可用。
按用户决策采用"**免费默认 + 可选付费后端**"策略：搜索默认走 `ddgs`（DuckDuckGo，无需 key），
抓取默认 `httpx` + `trafilatura`（HTML→markdown）；检测到 `TAVILY_API_KEY`/`EXA_API_KEY`
时用 httpx 直调其 REST 端点升级。同时把 Hermes 的 SSRF 防护门完整保留下来。

### Relevant Hermes behavior

- `tools/web_tools.py`（1212 行）：`web_search_tool`/`web_extract_tool` 经 `agent.web_search_registry`
  插件注册表分发到 7 个后端；extract 另有可选 LLM 摘要、secret-in-URL 拦截、SSRF 门、base64 图片剔除。
- `tools/url_safety.py`（305 行）：`is_safe_url` SSRF 门——http/https scheme 白名单、
  云元数据/链路本地 always-blocked 硬底（含 IPv4-mapped IPv6 变体）、私网/环回/保留/组播/CGNAT
  拦截、DNS 失败默认拒绝。

### Migration decision: 混合（ADAPT + REWRITE + 原创 backends）

- `tools/web/url_safety.py` → **ADAPT**：SSRF 门实质保留。丢弃 `security.allow_private_urls`
  config/env 开关及缓存、QQ 白名单、async 包装。Aegis **始终**强制私网拦截（无 opt-out），仅同步版。
- `tools/web/backends.py` → **原创**：Hermes 用付费 SDK 插件注册表；Aegis 用一个轻量、可 monkeypatch
  的后端缝。搜索：默认 `ddgs`，有 key 则 httpx 直调 Tavily/Exa REST（不引厂商 SDK）。抓取：httpx +
  trafilatura。模块级函数便于测试时替换。
- `builtin/web_search.py`、`builtin/web_extract.py` → **REWRITE**（对齐行为、实现 `Tool` Protocol）。
- **依赖**：`httpx` 进核心依赖；`ddgs`/`trafilatura` 进可选 `web` extra（仿 `mcp` extra）。未装 extra 时
  工具返回明确 `{error}`（含安装提示），不崩溃。Hermes 的 LLM 摘要不迁。

### Aegis design and data flow

- `web_search` `{query, limit=5(1..100)}` → `{results:[{title,url,description,position}], count, backend}` / `{error}`。
  `backends.web_search` 按 env 选后端：Tavily→Exa→ddgs。
- `web_extract` `{urls:[...]}（≤5）` → `{results:[{url,title,content,error}], count}`。每个 URL 先过
  `is_safe_url` SSRF 门（私网/元数据/非 http(s) 直接 Blocked，**不发任何请求**），再 httpx 抓取 +
  trafilatura 转 markdown，剔 base64 图、截断 20k 字符。单 URL 失败内联报告，永不抛异常。
- SSRF 门：scheme 白名单 → 元数据 hostname/IP 硬底 → 解析每个 A/AAAA 记录查私网/环回/保留/组播/CGNAT，
  DNS 失败默认拒绝。

### Key files, classes, and functions

- `tools/web/url_safety.py`：`is_safe_url`、`_is_blocked_ip`、always-blocked 常量集
- `tools/web/backends.py`：`web_search`、`web_extract`、`search_backend_name`、`_search_ddgs/_tavily/_exa`、
  `_strip_html`、`_strip_base64_images`
- `builtin/web_search.py`：`WebSearchTool`；`builtin/web_extract.py`：`WebExtractTool`

### Reliability invariants, edge cases, and failure handling

- **SSRF 防护**：`http://169.254.169.254`、`metadata.google.internal`、私网/环回/CGNAT、非 http(s)
  scheme 全部拒绝；DNS 失败 fail-closed。有专门无网络单元测试（monkeypatch `getaddrinfo`）。
- **永不抛异常**：后端异常/缺包/单 URL 失败都转为 `{error}` 结果。
- **优雅降级**：未装 `ddgs`/`trafilatura` 时给安装提示；trafilatura 不可用时退回标签剥离。
- **上下文保护**：base64 图片剔除、单页 20k 字符截断。

### Tests

- `tests/test_web_safety.py`（9，**无网络**——monkeypatch `getaddrinfo`）：非法 scheme、元数据字面 IP、
  元数据 hostname、环回/私网/CGNAT、解析到元数据的域名、公网放行、DNS 失败 fail-closed、空/畸形 URL。
- `tests/test_web_tools.py`（7，后端 monkeypatch，**无网络**）：search 返回/缺 query/后端错误；
  extract 内容/多 URL 部分失败/≤5 上限/缺 urls/真实后端下私网 URL 被 SSRF 门拦截。

### Source relationship

`url_safety.py` 为 **ADAPT**（保留 Hermes MIT 归属头）；`backends.py` 为原创；
`web_search.py`/`web_extract.py` 为 **REWRITE**。详见 `docs/source-map.md` Stage 8。

### Trade-offs, remaining limitations, and TODOs

- 免费默认（ddgs）稳定性取决于 DuckDuckGo，可能限流/变动；生产建议配 Tavily/Exa key。
- Hermes 的 LLM 摘要、secret-in-URL 正则、重定向逐跳 SSRF 校验未迁（重定向由 httpx `follow_redirects`
  一次跟进，目标不再逐跳复查——DNS rebinding/redirect 绕过属已知 SSRF 残余风险，与 Hermes 文档一致）。
- 未做网站 blocklist 策略（`website_policy.py`）。

### Interview summary

"这个里程碑把 Web 搜索与抓取迁进 Aegis，但**没有**照搬 Hermes 的付费 SDK 插件注册表——那是它默认
无 key 不可用的根源。我换成一个轻量、可 monkeypatch 的后端缝：搜索默认用 `ddgs`（DuckDuckGo，零配置
开箱即用），检测到 Tavily/Exa 的 key 就用 httpx 直调其 REST 升级；抓取用 httpx + trafilatura 把 HTML
转成 markdown。真正原样保住的是 Hermes 的 **SSRF 防护门**：scheme 白名单、云元数据/链路本地硬底、
私网/环回/保留/组播/CGNAT 拦截、DNS 失败默认拒绝——每个 URL 在发任何请求前先过这道门。测试完全不打
真实网络：SSRF 门用 monkeypatch `getaddrinfo` 喂各种 IP 做单元测试，工具层则 monkeypatch 后端函数，
另加一个用真实后端验证私网 URL 被门拦截的用例。"

### 对已有 milestone 的改动

- `pyproject.toml`：核心依赖加 `httpx`；新增可选 `web` extra（`ddgs`、`trafilatura`）。
- `docs/source-map.md`：新增 Stage 8 表。

*Hermes 仓库为只读参考，未修改。*

---

## Milestone 7 — Stage 7：终端与后台进程工具（terminal / process）

### Problem and goal

M1 迁入了文件三件套。本里程碑迁入**终端/进程**两件套：功能更全的 `terminal`（前台一次性执行 +
后台启动），以及配套的后台进程管理工具 `process`。按用户决策，`terminal` **取代** Stage-1 的
极简 `run_shell`（后者移除），避免两个语义重叠的执行工具。目标是在 Aegis 显式 DI 架构下复刻
Hermes 后台进程管理的核心能力（输出缓冲、状态轮询、阻塞等待、树杀、stdin 交互），而非一个
只会 `subprocess.run` 的版本。

### Relevant Hermes behavior

- `tools/terminal_tool.py`（2282 行）：`terminal_tool` 支持前台（跑完即返回、timeout→124、
  输出头尾截断、grep/diff 退出码解释、长驻服务→后台提示）与后台（`background=true` 立即返回
  `session_id`）。但深度耦合 Hermes 的 sandbox 后端（local/docker/singularity/modal/ssh）、
  审批/force 护栏、watch_patterns、gateway 通知路由。
- `tools/process_registry.py`（1432 行）：`ProcessRegistry` 单例 + `ProcessSession`（id、滚动 200KB
  `output_buffer`、daemon reader 线程、`_running`/`_finished` dict + 锁、TTL+LRU 修剪）、
  八个生命周期动作、`_reconcile_local_exit`（孤儿管道挂起修复）、psutil/`taskkill /T /F` 树杀。

### Migration decision: 混合（ADAPT + REWRITE + 删除 run_shell）

- `tools/process_registry.py` → **ADAPT**（本地子集）：保留注册表模型、reader 线程、spawn_local、
  八个动作、修剪、孤儿管道修复、树杀、ANSI 剥离。**丢弃** sandbox 后端（`spawn_via_env`）、
  ptyprocess PTY、watch_patterns 限流 + 全局熔断、gateway 通知路由、崩溃恢复 checkpoint 文件、
  per-profile HOME 隔离、provider-secret env 清洗。shell 包装从 `[shell, -lic, "set +m; cmd"]`
  简化为 `/bin/sh -c` / `cmd /c`。
- `builtin/terminal.py`、`builtin/process.py` → **REWRITE**（对齐行为、实现 `Tool` Protocol）。
- `builtin/run_shell.py` → **删除**（被 terminal 取代）。

### Aegis design and data flow

- `terminal` `{command, timeout=60(≤600), workdir, background=false, pty=false}`：
  前台 → `{output, exit_code, error}`（error=null 表成功；timeout→exit_code 124；输出 40%/60%
  头尾截断；grep/diff 的 exit 1 给 "非错误" 注解；`&`/server 类命令给后台化提示）。
  后台 → `{session_id, pid, output, exit_code:0, error:null}`，进程进入共享 registry。
  **沿用危险命令护栏**（`detect_dangerous_command` + operator-only `allow_dangerous_shell`）。
- `process` `{action, session_id?, data?, timeout?, offset, limit}`，action ∈
  `list/poll/log/wait/kill/write/submit/close`，薄封装 registry 的同名方法；未知 id → `{status:"not_found"}`。
- 注册：`build_default_registry()` 构造**单个** `ProcessRegistry`，同时注入 `TerminalTool` 与
  `ProcessTool`——两者共享同一份后台进程状态。

### Key files, classes, and functions

- `tools/process_registry.py`：`ProcessRegistry`、`ProcessSession`、`spawn_local`、`_reader_loop`、
  `_reconcile_local_exit`、`poll/read_log/wait/kill_process/write_stdin/submit_stdin/close_stdin/list_sessions`、
  `_prune_if_needed`、`_kill_popen_tree`、`_strip_ansi`
- `builtin/terminal.py`：`TerminalTool`、`_truncate_output`、`_exit_code_meaning`、`_SERVER_HINTS`
- `builtin/process.py`：`ProcessTool`

### Reliability invariants, edge cases, and failure handling

- **后台进程不泄漏**：reader 线程 `finally` 里 `wait()` 收割子进程；spawn 后置步骤失败会先杀孤儿再抛。
- **孤儿管道挂起修复**：直接子进程已退出但后代仍持有 stdout 管道时，`_reconcile_local_exit`
  非阻塞 drain 并把 session 标记为 exited，避免 poll 永远 "running"。
- **树杀**：POSIX 用进程组 `killpg`（spawn 时 `os.setsid`），Windows 用 `taskkill /T /F`。
- **输出有界**：每 session 滚动 200KB 缓冲；前台输出 50KB 头尾截断。
- **危险命令默认拦截**，模型无法通过参数自开（无 force/allow 参数）。
- **工具永不抛异常**：一切失败返回 `{error}` / `{status:"error"}` 结果。

### Tests

- `tests/test_terminal.py`（12）：前台输出/退出码、非零退出、timeout(124)、缺 command、workdir、
  后台返回 session_id 且入 registry、危险命令默认拦截/git reset --hard/安全命令不拦/operator override/
  模型参数不可开。
- `tests/test_process.py`（8）：list 可见、poll+wait（exit 0 + 输出）、log 分页、kill（killed→already_exited）、
  stdin write/submit/close（cat 回显）、not_found（7 动作）、缺 session_id、非法 action。
- `tests/test_tools.py` 精简为 read_file/list_directory（run_shell 用例迁入 test_terminal.py）。

### Source relationship

`process_registry.py` 为 **ADAPT**；`terminal.py`、`process.py` 为 **REWRITE**；`run_shell.py` 删除。
PORT/ADAPT 文件保留 Hermes MIT 归属头。详见 `docs/source-map.md` Stage 7。

### Trade-offs, remaining limitations, and TODOs

- PTY：`pty=true` 目前退化为普通 pipe 并附注（未引入 ptyprocess 依赖）；后续如需真交互式 TUI 可加。
- 后台进程**无崩溃恢复**（Hermes 有 checkpoint 文件）——Aegis 重启后丢失，符合轻量定位。
- `notify_on_complete` 仅记录标志，无 gateway/chat 通知（Hermes 的通知路由不迁）。
- wait 的 `interrupted` 语义（用户发新消息打断）未迁——Aegis 无对应 gateway 概念。

### Interview summary

"这个里程碑把 Hermes 的后台进程管理迁进 Aegis，并用功能更全的 `terminal` 取代了极简 `run_shell`。
`terminal` 前台跑完即返回（timeout→124、输出头尾截断、grep/diff 退出码解释），`background=true`
则立即返回 `session_id`。配套的 `process` 工具驱动一个**本地版** `ProcessRegistry`——每进程一个
滚动 200KB 输出缓冲 + daemon reader 线程，支持 list/poll/log/wait/kill/write/submit/close 八个动作。
两个工具共享同一个 registry 实例（在 `build_default_registry` 里构造注入），后台进程因此可被发现、
轮询、阻塞等待、树杀（POSIX 进程组 / Windows taskkill）、以及向 stdin 写入/送 EOF。我保留了 Hermes
一个关键修复：直接子进程退出但后代持有管道时，reconcile 逻辑非阻塞 drain 并标记退出，避免状态永远
卡在 running。Hermes 的 sandbox 后端、PTY、watch 限流熔断、gateway 通知路由、checkpoint 持久化
都显式砍掉，保持 Aegis 的轻量定位。"

### 对已有 milestone 的改动

- 删除 `builtin/run_shell.py` 及其 schema（`terminal` 取代）。
- `models/fake.py`、`tui.py`、`cli.py`、`tools/danger.py`、`tools/registry.py`：`run_shell` → `terminal` 引用更新。
- `docs/source-map.md`：新增 Stage 7 表。

*Hermes 仓库为只读参考，未修改。*

---

## Milestone 6 — Stage 6：文件编辑工具（write_file / patch / search_files）

### Problem and goal

Aegis 此前只有 3 个极简内置工具（`read_file` / `list_directory` / `run_shell`）。用户要求从
Hermes 迁入一批"尽量不做阉割版"的工具。这是其中第一个里程碑——**文件操作三件套**：
`write_file`（写文件）、`patch`（精确/模糊替换）、`search_files`（文件内容/名搜索）。
目标是在 Aegis 的显式 DI + `Tool` Protocol 架构下，复刻 Hermes 编辑器的核心健壮性
（原子写、BOM/行尾保留、模糊匹配、写后校验），而不是抄一个只会 `open().write()` 的简陋版。

### Relevant Hermes behavior

- `tools/fuzzy_match.py`（747 行）：`fuzzy_find_and_replace` 的 9 策略匹配链
  （exact → line-trimmed → whitespace → indentation → escape → trimmed-boundary →
  unicode → block-anchor → context-aware），外加 escape-drift 检测、替换区重缩进、
  `\t`/`\r` 智能反转义、`find_closest_lines` 的 "did you mean?" 提示。**仅依赖 `re`+`difflib`，零 Hermes 耦合。**
- `tools/file_operations.py`（1973 行）：`ShellFileOperations.write_file` / `patch_replace` / `search`，
  以及通用小工具 `_detect_line_ending` / `_normalize_line_endings` / `_strip_bom` /
  `_atomic_write` / `_unified_diff` / `_is_write_denied`。核心行为：自动建父目录、
  整体覆盖、temp+`mv` 原子写、保留 BOM 与 CRLF、敏感路径拒写、patch 写后重读校验、
  无匹配时给 "did you mean?" 提示、search 优先 ripgrep 回退 grep。
- `tools/path_security.py`：`has_traversal_component` / `validate_within_dir`。

### Migration decision: 混合（PORT + ADAPT + REWRITE）

- `tools/fuzzy_match.py` → **PORT**（几乎原样）：它是自包含纯函数模块，依赖闭包全是 stdlib，
  是最小完整迁移单元，拆开会徒增工作。只做了 typing import 的整理。
- `tools/fsutil.py` → **ADAPT**：把 Hermes 散在 `file_operations.py` 里的通用 helper 收敛成一个
  无后端耦合的模块。**关键改动**：Hermes 一切读写都走 pluggable 终端后端 `execute()`（docker/ssh/modal），
  此处换成直接 Python I/O（`pathlib`/`os.replace`）；并新增 `read_text_raw`（二进制读取）——
  因为 Python 文本模式 `read_text`/`write` 会做 universal-newline 转换（`\r\n`→`\n`），
  会把 CRLF/BOM 信息在读写往返中抹掉，必须绕过。
- `builtin/write_file.py`、`builtin/search_files.py` → **REWRITE**（对齐行为、按 Aegis `Tool` 协议重写）。
- `builtin/patch.py` → **ADAPT**（复用 fuzzy_match；对齐 `patch_replace`）。
- **显式丢弃** Hermes 的 cross-profile 镜像、file_state/staleness 跟踪、连搜熔断、
  lint/LSP 层、secret redaction、sandbox 后端路由。**V4A 多文件补丁模式不迁**（用户确认只做 replace 模式）。

### Aegis design and data flow

- `tools/fsutil.py`：`resolve_path`（cwd 感知 + `~` 展开）、`is_write_denied`（`/etc`、`/boot`、
  `.ssh` 等通用敏感路径拒绝）、`detect_line_ending`/`normalize_line_endings`、`strip_bom`/`has_bom`、
  `atomic_write`（同目录 temp + `os.replace`，二进制写保证 CRLF 不被翻译）、`read_text_raw`、
  `unified_diff`、`has_traversal_component`。
- `builtin/write_file.py`：`{path, content}` → `{path, bytes_written, created, dirs_created}` / `{error}`。
  存在文件时先读原文探测 BOM 与行尾，写回时保留。
- `builtin/patch.py`：`{path, old_string, new_string, replace_all}` →
  `{success, path, replaced, strategy, diff}` / `{success:false, error}`。多匹配且未 `replace_all` 报错；
  无匹配追加 "did you mean?" 提示；**写后重读校验**内容确实落盘（防静默失败）。
- `builtin/search_files.py`：`target=content` 用正则搜内容（`rg` 优先，纯 Python `os.walk`+`re` 回退，
  跳过二进制与隐藏/VCS 目录）；`target=files` 按 glob 找文件名（`rg --files --sortr=modified`，
  回退 `fnmatch`，新→旧）。`output_mode ∈ content/files_only/count`，`limit`/`offset` 分页。
- 三者在 `tools/schemas.py` 定义 schema，经 `build_default_registry()` 注册进 `ToolRegistry`。

### Key files, classes, and functions

- `tools/fuzzy_match.py`：`fuzzy_find_and_replace`、`format_no_match_hint`、`find_closest_lines`、9 个 `_strategy_*`
- `tools/fsutil.py`：`atomic_write`、`read_text_raw`、`detect_line_ending`、`normalize_line_endings`、
  `is_write_denied`、`resolve_path`、`unified_diff`
- `builtin/write_file.py`：`WriteFileTool`
- `builtin/patch.py`：`PatchTool`
- `builtin/search_files.py`：`SearchFilesTool`、`_walk_files`、`_parse_rg_content`

### Reliability invariants, edge cases, and failure handling

- **原子写**：temp 文件 + `os.replace`，崩溃不留半截文件；任何失败清理 temp。
- **CRLF/BOM 保留**：二进制读写往返，CRLF 文件改完仍是 CRLF（有测试断言字节级保留）。
- **patch 写后校验**：写回后重读比对，落盘不符即报错。
- **敏感路径拒写**：`/etc`、`/boot`、`.ssh/credentials` 等拒绝写入。
- **无匹配兜底**：patch 找不到 old_string 时给最相近行提示，帮助模型自纠。
- **工具永不抛异常**：一切失败（文件不存在/是目录/无匹配/非法正则）都返回 `{error}` 结果。

### Tests

- `tests/test_write_file.py`（7）：新建/覆盖/建父目录/CRLF 保留/敏感路径拒绝/缺字段/拒绝目录。
- `tests/test_patch.py`（10）：精确替换/模糊缩进匹配/空串删除/多匹配需 replace_all/replace_all/
  无匹配给提示/文件缺失/缺字段/CRLF 保留。
- `tests/test_search_files.py`（12）：内容匹配/glob 过滤/无匹配/files_only/count/非法正则/
  分页/文件名 glob/裸模式/路径缺失/排除隐藏与 VCS。

### Source relationship

`fuzzy_match.py` 为 **PORT**；`fsutil.py`、`patch.py` 为 **ADAPT**；`write_file.py`、`search_files.py`
为 **REWRITE**（行为等价）。PORT/ADAPT 文件保留 Hermes MIT 归属头。详见 `docs/source-map.md` Stage 6。

### Trade-offs, remaining limitations, and TODOs

- patch 只做 replace 模式，不含 V4A 多文件补丁（用户确认）。
- search 的纯 Python 回退有 20000 文件扫描上限与每行 500 字符截断；`rg` 可用时更快且尊重 .gitignore。
- 未做 Hermes 的 lint/LSP 诊断、secret redaction、跨进程 file-state 跟踪。

### Interview summary

"这个里程碑把 Hermes 编辑器三件套的**健壮性内核**迁进了 Aegis 的 `Tool` Protocol 架构。
最关键的是 `fuzzy_match.py` 几乎原样整体迁移——它是零依赖纯函数，9 策略匹配链能容忍 LLM
生成代码常见的空白/缩进/转义漂移。配套地我把 Hermes 散落的原子写、BOM/CRLF 保留、统一 diff、
敏感路径守卫收敛成一个无后端耦合的 `fsutil` 模块——这里有个坑：Python 文本模式 I/O 会做
universal-newline 转换，会把 CRLF 在读写往返中抹成 LF，所以我用二进制 `read_text_raw` +
二进制 `atomic_write` 保住字节级不变式，并用测试断言。patch 写后还会重读校验，杜绝静默失败。
全部三个工具实现现有 `Tool` Protocol，经显式 `build_default_registry()` 注册，不依赖 Hermes 的
全局单例或终端后端抽象。"

### 对已有 milestone 的改动

- `docs/source-map.md`：新增 Stage 6 表。
- `tools/schemas.py`、`tools/builtin/__init__.py`：注册三个新工具。

*Hermes 仓库为只读参考，未修改。*

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

## Milestone 5 — 动态系统提示词的实际内容（identity / behaviour / model / environment）

### Problem and goal

Milestone 4 把 `ContextBuilder` 升级成了 `SystemPromptBuilder` + `PromptContributor`
的缝，但真正的系统提示词内容一直是残缺的：`DEFAULT_IDENTITY` 只有一句话
（"You are Aegis Agent, a helpful assistant…"），除了技能索引和 MCP 提示，没有任何
行为准则、模型身份、运行环境信息。目标是参照 Hermes 的动态组合方式，把 Aegis
**真正拥有的能力对应的那几段** 补齐，其余（长期记忆、session_search、USER.md、SOUL.md、
context files、kanban、computer-use、平台提示、Nous 品牌）坚决不写进去。

### Relevant Hermes behavior and source

Hermes 在 `agent/system_prompt.py:build_system_prompt_parts` 里把提示词分成三层
（stable / context / volatile），每层用"条件 append + 丢空串 + `\n\n` join"的方式组装：

- stable：`DEFAULT_AGENT_IDENTITY` → `TASK_COMPLETION_GUIDANCE` →
  `TOOL_USE_ENFORCEMENT_GUIDANCE`（按模型家族门控）→ 技能索引 → 模型身份行 →
  `build_environment_hints`（Host/home/cwd + `WSL_ENVIRONMENT_HINT`）→ 平台提示；
- volatile：memory、USER profile、时间戳（date-only，PR #20451 为了 prompt cache 稳定）。

Aegis 的 `SystemPromptBuilder` 本质就是这套"有序 contributor + 丢空 + join"的泛化版。

### Migration decision: ADAPT

文本块（finishing the job / tool-use enforcement / WSL hint / 模型身份行）从 Hermes
**改写去品牌**后照搬语义；identity 是 REWRITE（去掉 Nous 和 docs URL）；`_is_wsl`
从 `hermes_constants.is_wsl` 适配（读 `/proc/version` 找 microsoft 标记，进程内缓存）。
组装 wiring 是 Aegis 原创。刻意的简化：tool-use enforcement **不按模型家族门控**
（Hermes 有个 `TOOL_USE_ENFORCEMENT_MODELS` 子串匹配表），Aegis 只要注册了工具就注入；
`build_environment_hints` 的 remote-backend 分支（docker/ssh/modal）不搬——Aegis 没有
远程终端后端。

### Aegis design and data flow

新增 `src/aegis_agent/context/prompt_sections.py`，五个 `PromptContributor`：

- `TaskCompletionContributor(registry)` / `ToolUseEnforcementContributor(registry)`
  —— 注册表非空才渲染（无工具时这两段的失效模式不存在，直接丢）；
- `ModelIdentityContributor(provider)` —— `getattr(provider, "model", None)` 为真才渲染，
  所以 fake provider 不产出这一段；
- `EnvironmentContributor(cwd)` —— Host 行（WSL/Windows/macOS/Linux）+ home + cwd
  （传入的是 `ToolContext.cwd`，即工具真正解析相对路径的目录）+ WSL 时追加文件系统提示；
- `TimestampContributor()` —— date-only 的 "Conversation started: …"。

`AgentRuntime.with_defaults` 按 Hermes 的段落顺序把它们挂到 `prompt_builder` 上：
identity → task-completion → tool-use →（技能索引）→（MCP 提示）→ 模型身份 → 环境 → 时间戳。
每个 contributor 都持有活的依赖（registry / provider），每轮 `build()` 重新渲染，所以提示
词跟随当前状态变化；但 `run_turn` 和"原始消息不可变"不变式一个字没动——这些只影响派生视图。

### Key files

- `src/aegis_agent/context/prompt_sections.py`（新）—— 五个 contributor + 文本常量 + `_is_wsl`；
- `src/aegis_agent/context/system_prompt.py` —— 重写 `DEFAULT_IDENTITY`；
- `src/aegis_agent/runtime.py` —— `with_defaults` 里的 wiring；
- `src/aegis_agent/context/__init__.py` —— re-export；
- `tests/test_prompt_sections.py`（新）—— 14 个测试。

### Reliability invariants, edge cases, and failure handling

- 无工具 → 行为两段消失；fake provider → 无模型身份行；无技能 → 无索引段（已有测试覆盖）；
- 组合顺序有测试断言（identity < finishing < enforcement < host < started）；
- 排他性测试：组合出的提示词里不得出现 `session_search` / `SOUL` / `Hermes` /
  `USER.md` / `persistent memory` 等 Aegis 不支持的子系统词；
- date-only 时间戳保证系统提示词一天内字节稳定（prompt-cache 友好）。

### Tests

`uv run pytest -q tests/test_prompt_sections.py`（14 passed）；
回归 `test_skills_prompt` / `test_context_invariants`（32 passed）、
`test_runtime` / `test_cli` / `test_tui`（21 passed）。
`uv run ruff check`（改动文件全过）、`uv run mypy src`（改动文件无新增错误）。

### Trade-offs, remaining limitations, and TODOs

- tool-use enforcement 不按模型家族门控，是有意简化；若将来接入更多真实 provider，
  可以把 Hermes 的 `TOOL_USE_ENFORCEMENT_MODELS` 匹配表补上；
- 没有 context-files / memory / user-profile 段，符合当前里程碑边界；
- 仓库里 `mcp/client.py`、`cli.py` 有先前遗留的 ruff/mypy 告警，与本次改动无关，未触碰。

### Interview summary

"这一步是把之前搭好的 `SystemPromptBuilder` 缝真正填满内容。我先读了 Hermes 怎么动态
拼系统提示词——它分 stable/context/volatile 三层，每层条件 append、丢空串、`\n\n` join。
我照着它的段落顺序，只把 Aegis 真有对应能力的几段做成 `PromptContributor`：行为准则
（finishing the job + tool-use enforcement，注册表非空才出）、模型身份（provider 有 model
才出）、运行环境（Host/home/cwd + WSL 提示）、date-only 时间戳。关键是**克制**：长期记忆、
session_search、SOUL、context files 这些 Aegis 还没有的东西，我特意不写进去，还加了排他性
测试守住这条线。整个改动只塑形派生的系统消息，原始会话历史和不变式完全不碰。"
