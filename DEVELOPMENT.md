# Aegis 开发记录（Agent Runtime 复用化 + 第一版 Subagent）

本文件记录两个相邻阶段的实现：先把主 Agent 的执行逻辑收敛成一个**可复用的 Agent Runtime**，
再在其上实现**第一版 Subagent**（同步、一次性、fresh context）。详细的逐里程碑记录仍在
`docs/development-log.md`；本文件只聚焦这两步的入口、抽出物、设计理由与调用链。

---

## 阶段一：Agent Runtime 可复用化

### 原来的执行入口在哪里

一次用户请求的完整链路：

```
aegis (CLI)  →  AgentRuntime.with_defaults(...)  →  runtime.run_turn(session_id, text)
                                                        └─ 持久化用户消息
                                                        └─ guard（中断 / 迭代预算）
                                                        └─ context.build(source)  构造派生上下文
                                                        └─ provider.stream(...)   调模型
                                                        └─ 无 tool_calls → 结束；否则 executor.execute → 回填 → 继续
```

- 组装入口：`cli.py:_main` 里的 `AgentRuntime.with_defaults(...)`。
- 循环核心：`runtime.py:AgentRuntime.run_turn`。

### 关键发现：不需要新造一层

阅读后确认：`AgentRuntime` **本身已经是一个依赖注入式的可复用引擎**——`provider` / `registry` /
`executor` / `repository` / `context_builder` 全部通过构造函数注入，从不依赖 Typer、具体 provider
或全局 CLI 状态。真正与"主会话"耦合的只是 `with_defaults`（它做 MCP/skills/memory/project 发现，
是主 Agent 专属的组装根）。

因此本阶段做的是**最小补全**，而不是重构：

- `__init__.py` = **可复用引擎**（未来 Subagent 复用的接缝）；
- `with_defaults(...)` = **Main Agent 的组装根**（Subagent 不该重跑这些发现逻辑）。

### 抽出了什么

在 `runtime.py` 新增 **`AgentConfig`**（`frozen=True` 值对象），聚合"agent 级"标量配置：

| 字段 | 含义 |
|------|------|
| `agent_name` | agent 身份（主 agent 恒为 `"main"`，Subagent 用各自类型名） |
| `max_iterations` | 每轮 model/tool 迭代上限 |

设计要点：**把"身份/调参"与"注入的协作者"分开**。provider / registry / repository 是*依赖*
（常在父子 agent 之间共享），不是配置；`AgentConfig` 只装 agent 之间真正需要各填一份的标量。
这正是"配置后重复实例化同一引擎"的关键。

向后兼容：`__init__` 新增可选 `config` 参数；不传时用旧的 `max_iterations` 关键字合成一个默认
（`agent_name="main"`）配置——现有 20+ 处调用行为完全不变。`with_defaults` 新增 `agent_name="main"`
透传。

### 未来 Subagent 如何复用（当时的预期，已在阶段二兑现）

```
Main Agent ─────┐
                ├──> 同一个 AgentRuntime（不同 AgentConfig + 不同注入依赖）
Subagent ───────┘
```

### 测试

`tests/test_runtime_config.py`：`AgentConfig` 默认值/不可变；`max_iterations` 向后兼容；显式
config 覆盖 kwarg；config 的迭代上限真正约束 `run_turn`；**同一组依赖 + 两个不同 config 建两个
runtime、写入各自 repository 互不串扰**（复用性证明）；`with_defaults(agent_name=...)`。

---

## 阶段二：第一版 Subagent

### 目标与边界

> 让 Main Agent 通过一个 `Agent` 工具启动另一个独立 Agent，由 Subagent **复用同一套 Agent
> Runtime** 完成任务，最终只把结果返回 Main。

本版**只做同步、一次性闭环**：不做 background、fork context、Team、SendMessage、A2A、Coordinator。
生命周期 `CREATED → RUNNING → COMPLETED / FAILED`。

### 调用链

```
User
 ↓
Main Agent Runtime  (run_turn)
 ↓  model 请求调用 Agent(prompt, subagent_type)
AgentTool.run
 ↓
SubagentRunner.run(definition, prompt)
 ↓  用 definition 配置一个新的 AgentRuntime（复用同一 provider，过滤后的 registry，
 ↓  fresh system prompt，独立 InMemory repository）
Subagent AgentRuntime.run_turn   ← 与 Main 完全相同的 model↔tool 循环
 ↓  独立 transcript（私有 session）
SubagentResult(output=...)
 ↓  仅 final_text 作为工具结果回到 Main transcript
Main Agent Runtime 继续
```

### 抽出/新增了什么（`src/aegis_agent/agents/`）

| 文件 | 职责 |
|------|------|
| `definitions.py` | `AgentDefinition`（声明式：身份 / 工具白名单 / 迭代上限 / 是否允许再开 agent）+ 两个内置：`explore`（只读）、`general-purpose`（全量工具，除 Agent 工具）；`READ_ONLY_TOOL_NAMES` 白名单 |
| `runner.py` | `SubagentRunner`：把 definition + prompt 变成一次**配置好的 `AgentRuntime` 运行**，返回 `SubagentResult`；`SubagentStatus`（COMPLETED / FAILED） |
| `agent_tool.py` | `AgentTool`：实现 `Tool` 协议的 `Agent` 工具，参数仅 `prompt` + `subagent_type`，只回传最终结果 |

**核心纪律：没有第二套 loop。** Subagent 就是 `AgentRuntime` 换一个 `AgentConfig`、换一个过滤后的
registry、换一个 fresh `ContextBuilder`、换一个私有 repository 再跑一次 `run_turn`。

### 隔离保证

1. **Fresh context**：Subagent 用全新的空 `InMemorySessionRepository`，看不到 Main 的任何历史；
   dispatch 的 `prompt` 是它唯一输入（所以 Main 必须把背景交代完整）。system prompt 只含
   definition 身份 + 行为分节 + 模型/环境/时间，**不含** skills 索引 / MCP / memory / 用户档案。
2. **独立 transcript**：Subagent 的 model 轮次、工具调用、工具结果全部落在它私有的 repository，
   **绝不写回 Main**；Main transcript 只多一条 `Agent` 工具结果。
3. **一次性**：runtime 与 repository 都是 `SubagentRunner.run` 的局部对象，返回即销毁，无 idle。

### 工具过滤与递归防护

`SubagentRunner._build_sub_registry` 从**父 registry**过滤子集：

- `explore`：白名单 = `READ_ONLY_TOOL_NAMES`（`write_file` / `patch` / `terminal` / `process` 被排除）；
- `general-purpose`：`tool_names=None` → 继承父的全部工具；
- **无论哪种，`Agent` 工具默认被剔除**（`allow_agent_tool=False`）——第一版严格单层，这既是设计
  也是递归防护：Subagent 默认不能再开 Agent。

### 失败处理

Subagent 的 provider/loop 错误 → `run_turn` 返回 `StopReason.ERROR`；被中断 → `INTERRUPTED`。
两者都映射成 `SubagentResult(status=FAILED, error=...)`，再由 `AgentTool` 转成 `is_error=True` 的
**普通工具结果**（绝不抛异常），因此 Main 的循环不会被打断，可以据此恢复或如实报告。

### 接入方式（最小、可开关）

`AgentRuntime.with_defaults` 新增 `enable_subagents=True`：在所有其他工具（builtin + skills + MCP）
注册**之后**再注册 `Agent` 工具，这样 `general-purpose` 子 agent 的工具池能包含它们。
`enable_subagents=False` 时 `Agent` 工具完全缺席，行为与加子 agent 之前逐字节一致。
`startup_info["subagents"]` 计数，TUI banner 显示 `Subagents: N`。

对 `run_turn` 的唯一改动：新增可选 `is_cancelled` 回调参数（与既有 `interrupt` 事件 OR 合并），
让父的 Ctrl+C 能一并停掉子 agent。主路径行为不变。

### 测试（`tests/test_subagent.py`，32 项相关全绿）

- Main 能调用 `Agent` 并拿到结果；
- Subagent 真正跑通共享 runtime 的完整 model↔tool 循环（含一轮工具）；
- `explore` 的 registry 不含任何写/执行工具，且 ⊆ 只读白名单；
- `general-purpose` 保留写工具但**不含** `Agent` 工具（递归防护）；
- Subagent 中间消息不污染 Main transcript（Main 恰好 4 条：user / assistant(agent 调用) /
  tool(agent 结果) / assistant(final)）；
- Subagent 失败经 `Agent` 工具冒泡成明确 `is_error` 结果，Main 能继续并收尾；
- `enable_subagents=False` 时 `Agent` 工具缺席、计数为 0；默认存在、计数为 2；
- 反回归：runner 确实构造 `AgentRuntime`（而非平行 loop）、且 config.agent_name 正确；
- 自定义 `AgentDefinition` 可经同一路径工作（可扩展性）。

### 遗留与后续

- 本版是同步、单层、fresh-context。后续可在**同一 runner 接缝**上扩展：background 生命周期、
  fork context（继承父历史）、多层 agent（放开 `allow_agent_tool` + 深度上限）、`.aegis/agents/*.md`
  自定义 agent 加载器（只需产出更多 `AgentDefinition`）、Team / SendMessage / A2A。
- 目前 Subagent 用与 Main 相同的 provider 实例；未来可为子 agent 配置更廉价的模型
  （`AgentConfig`/definition 已是自然落点）。

---

## 阶段三：fresh/fork + foreground/background + 任务生命周期

> 先参考了 Claude Code 的多 Agent 源码（`AgentTool.tsx` / `runAgent.ts` / `forkSubagent.ts` /
> `agentToolUtils.ts` / `LocalAgentTask.tsx` / `forkedAgent.ts`），再把它的设计落到 Aegis 的
> Python 架构上。不照搬 TypeScript 结构或类名。

### 参考了 Claude 的哪些设计

| Claude 机制 | 借鉴到 Aegis 的点 |
|---|---|
| `runAgent` 再跑一遍主 `query()` 循环 | 早已成立的共识：Subagent 复用同一个 `AgentRuntime`，本阶段继续沿用 |
| `LocalAgentTaskState`（`status: running→completed/failed/killed` + abortController） | `SubagentTask` 状态机 + `cancel: threading.Event` |
| `registerAsyncAgent` / `runAsyncAgentLifecycle` 把后台 agent 跑在独立任务里 | `SubagentManager.spawn(background=True)` 跑在 daemon 线程上 |
| `enqueueAgentNotification` 原子置 `notified` 防重复，通知下一轮注入主对话 | `TaskNotification` 队列 + 原子 `notified` 标志；CLI 在**每轮之间** drain 并作为下一输入注入（免轮询） |
| `createChildAbortController`（子随父取消） | 子 `is_cancelled` = 任务 cancel 事件 OR 父 cancel 回调 |
| `buildForkedMessages` / `forkContextMessages`（fork 继承父历史） | `parent_messages` 播种进子 agent 私有 repo（fork 模式） |
| `filterToolsForAgent` + `resolveAgentTools`（白/黑名单 + 默认剔除 Agent 工具） | `SubagentRunner._build_sub_registry`（白名单 + 默认剔除 Agent 工具；嵌套需 `allow_agent_tool`） |
| fork 用占位 tool_result 保字节稳定（prompt cache） | **暂不照搬**：Aegis 直接播种真实历史即可；缓存精化留待后续 |

### Aegis 里怎么实现的

新增 `src/aegis_agent/agents/manager.py`（`SubagentManager`），改造 `runner.py` / `agent_tool.py` /
`definitions.py`，并在 `runtime.py` / `cli.py` / `slash_commands.py` 各加一条接缝：

1. **fresh / fork 两种上下文**
   - fresh：typed agent（`explore`/`general-purpose`），空私有 repo，只收 `prompt`。
   - fork：省略 `subagent_type` 触发，用 `fork_agent_definition()`（`fork=True`，全工具池），
     把父会话历史经 `history_provider=repo.list_messages` 取出、逐条 `_forked_copy`（清
     `client_msg_id`/`seq`，保留 role/content/tool 关联）播种进子的私有 repo，再跑 `run_turn`。
2. **foreground / background**
   - foreground：`manager.spawn(..., background=False)` 在调用线程同步跑完返回 `SubagentResult`。
   - background：`background=True` 起 daemon 线程，立即返回 `SubagentTask`（含 `task_id`）。
3. **独立 transcript**：每个 subagent 一个 `subagent-<type>-<hex>` 私有 session（`InMemory` repo），
   结果里带 `transcript`；Main 只多一条 `Agent` 工具结果（foreground）或一句 handle（background）。
4. **完成通知（免轮询）**：后台子 agent 完成时 manager 入队一条 `TaskNotification`（`notified`
   原子置位，绝不重复）。`AgentRuntime.drain_subagent_notifications()` 是接缝；CLI `_repl` 在每轮
   之后 `_collect_subagent_notifications` → 把通知文本**作为下一轮输入自动注入**，模型据此继续。
5. **任务状态管理**：`SubagentTask.status ∈ {running, completed, failed, killed}`；`/agents` 列出。
   `kill(task_id)` 立即置 KILLED（对齐 Claude 在 running 任务上直接转态，而非等循环收尾），
   `_execute` 不会把已 KILLED 的任务改回 COMPLETED。
6. **tool / permission 隔离**：子 registry 从父 registry 过滤；`explore` 只读白名单；`Agent` 工具
   默认剔除；`allow_dangerous_shell` / `cwd` 经 ToolContext 传给子 agent。
7. **并发 / 递归 / turn 限制**：并发上限 `DEFAULT_MAX_CONCURRENT=8`（超限返回 FAILED 结果而非
   抛异常）；嵌套深度 `DEFAULT_MAX_DEPTH=1`（depth 用 `threading.local` 沿线程谱系追踪，超限拒绝）；
   turn 上限沿用各 definition 的 `max_iterations`（fork 默认 25）。

### 对前两版实现做的调整及原因

- **`AgentTool` 构造签名变了**：原来 `AgentTool(runner, agents)`，现在 `AgentTool(manager, allow_fork=,
  history_provider=)`。原因：fork 需要访问父会话历史（`history_provider`），background/通知/限额
  需要一个集中的任务管理者（manager），runner 自己不该持有这些。旧的前台/fresh 路径行为不变。
- **`SubagentRunner.run` 增加 `parent_messages` 参数并返回带 `transcript` 的 `SubagentResult`**：
  支撑 fork 播种和"独立 transcript 可被父读取/通知引用"。
- **`AgentRuntime.__init__` 增加 `subagent_manager`**（类型标 `object`，避免模块级循环 import）：
  runtime 只在轮间 drain 通知队列，自己不 spawn（spawn 是 Agent 工具的职责）。
- **kill 语义对齐 Claude**：`_execute` 里"已 KILLED 不被覆盖"。

### 与 Claude 的主要差异

- **无 async 事件循环**：Aegis 主循环是同步的，background 用 daemon 线程而非 task/promise。
- **通知走"下一轮输入注入"而非独立消息队列 + sidechain UI**：更贴合 Aegis 的 REPL 单会话模型。
- **fork 不做字节级 prompt-cache 精化**（占位 tool_result / 共享渲染 system prompt 暂未实现）。
- **权限模型更简**：Claude 有 permissionMode/bubble/plan 等；Aegis 目前只有
  `allow_dangerous_shell` 一档 operator 开关。
- **进度追踪/摘要（ProgressTracker、background summarization）未实现**：只保留
  iterations/tool_calls 轻量遥测。

### 下一阶段如何扩展到长期 teammate 和 Agent 通信

- **长期 teammate**：把一次性 `SubagentTask` 升级为可 idle/唤醒的常驻任务——`SubagentManager`
  已持有 task 表和线程，只需给 task 加"挂起等消息"状态 + 一个唤醒入口（参考 Claude 的
  InProcessBackend + 每轮读邮箱）。
- **Agent 通信**：在 manager 上加 SendMessage 等价物——按 task_id 路由消息，运行中的塞进
  `pending_messages`、在子 agent 工具轮间隙注入；idle 的可唤醒续跑（子 transcript 已在，可复用
  `parent_messages` 播种机制做 resume）。文件邮箱（跨进程）可后置。
- 这些都建立在**同一个 `SubagentManager` + `AgentRuntime`** 接缝上，无需新的执行循环。

### 测试（`tests/test_subagent_v2.py` + 既有 `test_subagent.py`）

- fresh 子 agent transcript 只含自己的一轮（不继承）；fork 子 agent transcript 含父历史且顺序正确；
- 经 Agent 工具省略 type 走 fork；fork 缺 history_provider / 被禁用时报清晰错误；
- background 立即返回 task handle、完成后入队通知且 drain 后即空（免轮询、不重复）；
- 后台失败也入队通知（带 error）；runtime 暴露 drain 接缝；
- 任务状态转 completed；并发超限 / 深度超限返回 FAILED；kill 置 KILLED 且幂等；
- 后台子 agent 的内部工具轮不污染 Main transcript（线程路由 provider 保证确定性）；
- `/agents` 在有/无/禁用三种状态下输出正确；`enable_subagents=False` 时 manager 缺席、drain 为空。

---

## 阶段四：Persistent Team / Teammate + Agent 间通信

> 参考了 Claude Code 的 `TeamCreateTool/`、`SendMessageTool/SendMessageTool.ts`、
> `utils/teammateMailbox.ts`、`utils/teammate.ts`、`utils/swarm/inProcessRunner.ts`、
> `utils/swarm/teamHelpers.ts`、`tasks/InProcessTeammateTask/`，把设计落到 Aegis 的
> Python 线程模型上，不照搬 TS 结构。

### 先修正的上一阶段边界问题

1. **kill 不真正取消**：后台直接 spawn（`parent_context=None`）时 `task.cancel` 事件从不传入
   `run_turn`，kill 只改状态。修复：`SubagentRunner.run` 新增 `cancel_event` 参数，与父
   `is_cancelled` OR 合并后真正中断在飞的模型调用/工具执行；`SubagentManager._execute` 把
   `task.cancel` 传下去。
2. **递归深度用 `threading.local` 不稳**：后台线程独立，谱系断裂。修复：改为**显式谱系**——
   `SubagentTask` 增加 `agent_id` / `parent_agent_id` / `depth`，`spawn()` 改收
   `parent_agent_id` / `parent_depth`，跨线程安全。
3. **通知只在下一轮输入才看到**：本阶段为 teammate 做了真正的**事件唤醒**（见下）。
4. **provider 共享写死**：`SubagentRunner.run` 新增 `provider` 覆盖参数，单个 agent 可用不同
   模型；runtime 不再把"所有 agent 永远共享同一 provider"写死。

### 参考了 Claude 的哪些设计

| Claude 机制 | 借鉴到 Aegis 的点 |
|---|---|
| `runInProcessTeammate` 的 `while` 循环：跑一轮→idle→等下一条 | `PersistentTeammate._loop`：RUNNING→IDLE→阻塞等消息→RUNNING |
| `allMessages` 累积 + 每轮作 `forkContextMessages` 传入 → 连续上下文 | teammate 持有**一个**常驻 repo+session，每轮复用同一 session → 连续 transcript |
| 文件邮箱（read/unread + 锁），500ms 轮询 | `AgentTransport` 抽象 + `InProcessTransport`（queue + `threading.Event` **事件唤醒，不轮询**） |
| `SendMessage` 路由 `to`（名字 / `*` 广播），team 边界 = 收件人须在 members | `TeamManager.send_message`（team 边界校验 + `*` 广播） |
| idle 通知自动发给 lead | teammate 转 IDLE 时经 `_make_idle_hook` 给 lead 邮箱投一条 |
| 生命周期 CREATED→RUNNING→IDLE→…→completed/failed/killed；shutdown 协议 | `TeammateStatus`（CREATED/RUNNING/IDLE/FAILED/STOPPED）；`stop`/SHUTDOWN 消息（v1 简化，无审批） |

### Aegis 的 Team 生命周期

```
team_create
  → TeamManager.create_team            (lead = team-lead = Main Agent)
  → spawn_teammate(name, type)         (稳定身份 name/agent_id/session_id)
       PersistentTeammate._loop:
         CREATED → RUNNING(收到消息跑一轮) → IDLE(事件阻塞等下一条)
                 → RUNNING → IDLE → …
                 → SHUTDOWN消息 / stop() → STOPPED
                 → 某轮失败 → FAILED（不退出循环，可再被唤醒）
  → stop_team → 所有 teammate STOPPED，资源释放
```

### AgentTransport 如何设计

- 抽象 `AgentTransport`（Protocol）：`send` / `receive` / `has_pending` / `close_recipient`，
  Team 层只依赖它，不碰具体 queue/file。
- 消息结构 `AgentMessage`：`message_id` / `sender` / `recipient` / `type`（message/task/shutdown）/
  `content` / `created_at`。
- 第一版 `InProcessTransport`：每个 recipient 一个 deque 收件箱 + 一个 `threading.Event`。
  `receive` 用 `event.wait()` 阻塞——idle teammate 不耗 token、不空转；消息到达即被唤醒。
- 未来可扩展：`FileMailboxTransport`（Claude 的磁盘 JSON 邮箱，跨进程）、`A2ATransport`（远程，未做）。

### idle / wakeup 如何实现

teammate 的 `_loop` 在 `_transport.receive(address, timeout=0.5)` 上阻塞；`send` 时 set 对应
recipient 的 event 把它唤醒，跑一轮后回到 IDLE 继续等。`stop()` 既 set `_stop` 又
`close_recipient` 唤醒阻塞的 receive 使其尽快退出。**没有模型轮询 inbox**。

### transcript / context 如何保持

每个 teammate 持有**一个** `InMemorySessionRepository` + **一个** session（`team-<tid>-<name>`）。
每轮 `SubagentRunner.run(repository=…, session_id=…)` 复用同一 store，模型每轮都看到完整历史；
不被 lead/Main 吸收。关系信息齐全：`team_id` / `agent_id` / `agent_name` / `lead_id` / `session_id`。
lead（Main）没有自己的线程，其收件箱由 runtime/CLI 在轮间 `drain_team_messages()` 注入下一输入。

### 对前面实现做的调整

- `SubagentRunner`：新增 `cancel_event` / `repository` / `session_id` / `provider` / `extra_tools`
  参数（兼容既有 subagent 用法，默认值不变）；新增 `add_extra_tool`。
- `SubagentTask`：加 `agent_id` / `parent_agent_id`；`spawn` 改收显式 `parent_depth`。
- `AgentRuntime`：新增 `team_manager` 字段/属性 + `drain_team_messages()`；`with_defaults`
  建 `InProcessTransport` + `TeamManager`，给 lead 注册 `team_create` / `send_message`。
- CLI `_repl`：轮间 drain  seam 合并 subagent 通知与 team lead 消息（`_collect_agent_notifications`）。

### 与 Claude 的主要差异

- **无 async**：Claude 用 promise/event loop；Aegis 用 daemon 线程 + `threading.Event`。
- **不轮询**：Claude 500ms 轮文件邮箱；Aegis 用事件驱动唤醒（低开销阻塞）。
- **shutdown 无审批**：Claude 的 shutdown_request/response 需队友批准；v1 直接 stop。
- **无 task-list / plan-approval / 权限桥**：v1 范围控制，未做。
- **通信在同进程**：跨进程 FileMailbox / A2A 留作扩展。

### 测试（`tests/test_team.py`，21 项）

transport 收发/超时/close 唤醒；建队+成员、稳定身份（name/agent_id/session_id、按名/id 解析）、
重名拒绝；task→IDLE（不销毁）、唤醒后续上下文（context 变大、transcript 跨轮）；idle 通知到 lead；
lead→teammate、teammate→teammate（经 send_message 工具）、跨 team/未知收件人边界、`*` 广播；
stop 终止（join 后 STOPPED）、多 teammate 并行、teammate transcript 与 Main 隔离；
`team_create`/`send_message` 工具（建队+spawn、lead 发消息、无 team 报错）；runtime 暴露
team_manager + 工具注册、`enable_subagents=False` 时全部缺席；teammate 单轮失败不拖垮 team
（FAILED 后可再唤醒回 IDLE）。

### 当前限制

单 lead-team；shutdown 无审批；无 task-list 协调；无跨进程/远程通信；teammate 默认与 lead 共享
provider（已留 provider 覆盖接口）。

### 后续如何扩展到 A2A 和 Coordinator

- **A2A**：实现 `A2ATransport`（同一 `AgentTransport` 接口，底层走网络/UDS），Team 层无需改动；
  `AgentMessage` 已带 `created_at`/`message_id`，可直接序列化传输。
- **Coordinator**：Main 退化为只持有 Agent/SendMessage/TaskStop 的指挥者，全部执行外包给
  teammate——复用现有 `TeamManager` + `PersistentTeammate`，只需一个"角色裁剪"的配置把 Main 的
  工具集裁到编排三件套（参考 Claude `coordinatorMode.ts`）。
