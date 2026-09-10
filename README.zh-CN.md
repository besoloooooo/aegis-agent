# Aegis Agent

一个轻量、可恢复、可扩展的 **Agent 运行时**，通过提取、简化并演进 [Hermes](https://github.com/NousResearch/hermes-agent) 与 Claude Code 参考实现中的实用运行时行为构建而成。

> 专为需要会话恢复、上下文压缩、记忆、工具调用、子代理、团队、代理间消息传递以及可扩展运行时组件的长时运行代理而设计。

## 🏁 已交付里程碑

从最小骨架到完整运行时，共十九个里程碑：

**核心运行时**
1. 最小 Agent 运行时 —— 假提供者、内存会话、Agent 循环，
   每轮对话默认最多 50 次迭代，可配置
2. OpenAI 兼容与原生 Anthropic 提供者及流式工具调用
3. 实时终端 UI

**工具**
4. Skills 子系统 —— `SKILL.md` 发现 / 加载 / 路由
5. 轻量 MCP 客户端 —— stdio + Streamable HTTP
6. 文件编辑工具 —— write_file / patch / search_files
7. 终端与后台进程工具
8. Web 工具 —— 带 SSRF 防护的 web_search / web_extract
9. 技能管理 —— `skill_manage`

**可靠性与会话**
10. 上下文压缩流水线 —— 卸载 → 本地微压缩 → LLM 摘要
11. 压缩接入 agent 循环 + reasoning_content
12. SQLite 持久化 + 快照快速恢复 + 跨进程租约

**提示 / 记忆 / 搜索**
13. 动态系统提示词段
14. 个人长期记忆（Auto Memory）—— `USER.md` + `MEMORY.md` 索引
15. 记忆召回 + 后台提取
16. 会话历史搜索 —— FTS5 `session_search`
17. 项目级长期记忆 —— `--project [PATH]`

**交互式 UX**
18. 斜杠命令套件 —— `/save` `/new` `/history` `/undo` `/retry` `/title` …，
    以及全屏 TTY 布局：可滚动聊天记录、固定底部输入框、Markdown 回复、
    高亮输入 token，`Ctrl+End` 一键跳回实时末尾。

**多代理编排**
19. 多代理编排 —— `Agent`、`team_create`、`send_message`、`/agents`

**代理质量基础**
- 质量阶段 0 —— 可选 Langfuse 端到端追踪，覆盖 Agent 运行、模型调用、工具调用、子代理与最终结果
- 质量阶段 1–2 —— Harbor 自定义代理集成，以及一个与提供者无关的 `ExecutionRecord`，
  将运行时步骤、用量、Harbor 验证器结果与产物关联起来，并对代理转发采用容器安全的显式启用策略
- 质量阶段 3 —— 规则优先、可解释的 Process Evaluation，包含可选的失败恢复 LLM Judge、
  可选的实时/历史普通对话 Record、CLI 批量评测，以及可定位到 Trace Step 的 Viewer Issue
- 本地 Trace Viewer，支持一键同步 Harbor、按游标分页读取 Langfuse Root、分栏展示
  日常对话/评测/全部运行，并明确区分失败 Run 与错误 Observation

---

## 🏗 项目结构

```text
src/aegis_agent/
├── cli.py          # Typer CLI / REPL 入口
├── tui.py          # 终端 UI（prompt_toolkit + rich）
├── slash_commands.py  # 交互式 /command 注册表与调度器
├── runtime.py      # AgentRuntime —— agent 循环
├── events.py       # 模型事件流
├── agents/         # 子代理、团队、代理间消息传递
├── observability/  # fail-open 追踪 API、脱敏、Langfuse 适配器
├── quality/        # ExecutionRecord、Process Evaluation、本地存储、Harbor 适配器
├── integrations/   # 可选的对外编排器适配器（Harbor）
├── models/         # 提供者无关协议、fake / OpenAI / Anthropic 适配器
├── tools/          # 工具注册表、执行器、内置工具
├── context/        # 上下文构建 + 压缩
├── sessions/       # 会话仓库（内存 / SQLite）+ 租约
├── memory/         # Auto Memory（长期记忆，个人 + 项目范围）
├── skills/         # SKILL.md 加载 / 路由
└── mcp/            # MCP 客户端
```

原始对话消息被完整保留。上下文压缩只修改发送给模型的派生视图。

---

## 🚀 快速开始

前置条件：[uv](https://docs.astral.sh/uv/) —— 它会自动安装 Python 3.11 工具链。

```bash
# 1. 安装 uv（一次性）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 同步依赖 —— uv 读取 .python-version 并准备 Python 3.11 及所有依赖
uv sync

# 3. 运行
uv run aegis
```

配置一个 OpenAI 兼容模型：

```bash
export AEGIS_API_KEY=...
export AEGIS_BASE_URL=http://localhost:1234/v1
export AEGIS_MODEL=gpt-4o-mini

uv run aegis
```

或配置原生 Anthropic Messages API 提供者：

```bash
export ANTHROPIC_API_KEY=...
export ANTHROPIC_MODEL=claude-sonnet-4-6
# export ANTHROPIC_BASE_URL=https://api.anthropic.com  # 可选覆盖

uv run aegis --model-backend anthropic
```

`--model-backend auto` 保持 OpenAI 兼容配置的优先级；当同时设置了
`ANTHROPIC_API_KEY` 与 `ANTHROPIC_MODEL` 时则选择 Anthropic。
使用 `--model-backend fake` 强制使用确定性假提供者。

没有模型配置时，Aegis 也可以用其确定性的假提供者运行。

---

## 💾 会话恢复

消息持久化到 SQLite 存储。存储是**有作用域的**：个人作用域使用
`~/.aegis/state.db`，而项目作用域将其会话存放在 `~/.aegis/projects/<project-id>/state.db`
中、与记忆放在一起（与 Claude Code 一致，因此会话始终归属于创建它的作用域）。

```text
~/.aegis/state.db                          # 个人作用域
~/.aegis/projects/<project-id>/state.db    # 项目作用域
```

Aegis 使用：

* SQLite WAL 持久化
* 幂等的消息写入
* 周期性快照
* SQLite / Redis 会话租约
* 快照 + 尾部回放用于恢复

恢复之前的会话 —— 传入启动时相同的 `--project`，因为项目会话存放在项目自己的存储中、
对个人作用域不可见：

```bash
uv run aegis --resume my-session
uv run aegis --project /path/to/repo --resume my-session
```

不持久化运行：

```bash
uv run aegis --ephemeral
```

---

## ⌨️ 斜杠命令

交互式 REPL 支持 `/commands`（在 REPL 内输入 `/help`）：

```text
/new [name]    开始新会话（别名：/reset）
/clear         清屏 + 开始新会话
/history       显示对话（工具消息折叠）
/save          将调试快照（本地 / 线上 / 系统提示词）
               转储到 ./aegis-chat-logs/（别名：/chatlog）
/retry         重发最后一条消息
/undo [N]      回退 N 个用户轮次（记录软删除、保留在磁盘上以备审计）
               并预填输入框供编辑
/title [name]  设置或显示会话标题
/sessions      列出已记录的会话
/agents        列出子代理任务及其状态
/exit          退出（别名：/quit）
```

未匹配到任何命令的 `/token` 会依次落入技能路由，再原样交给模型。
在交互式 TTY 中，输入框会高亮已知斜杠命令、带引号字符串、路径类 token、
提及与标签，并在输入时显示一个小的命令提示。

---

## 🖥 交互式 TTY 渲染

实时终端 UI 使用 Rich 和 prompt_toolkit。在交互式 TTY 中，当前助手段落会在
token 流式输出时以 Rich Markdown 重新渲染，然后在工具边界或轮次结束时提交到历史。
因此标题、列表、强调、行内代码、围栏代码块与表格在生成期间和之后都保持终端样式。
工具调用仍以紧凑的状态行出现在响应段落之间。

交互式 TTY 会话使用 prompt_toolkit 全屏布局：聊天历史位于可滚动输出面板中，
输入框固定在底部。鼠标滚轮事件路由到历史面板，PageUp/PageDown 从键盘滚动，
而输入行保持可见。每个滚轮事件移动一行历史，以获得精确的原生终端滚动效果。
随时按 `Ctrl+End` 直接跳到最底部并恢复跟随实时输出。裁剪面板有意不显示滚动条，
因为局部切片无法准确代表或拖拽完整历史。只有用户已经位于底部时新输出才跟随尾部，
因此流式输出不会把手动滚动过的历史视图拉走。
输入框将 prompt_toolkit 历史、光标编辑与 token 高亮保持在一条干净的输入行中。
用户消息、助手 Markdown、工具状态与错误都共享历史面板。非 TTY 输入/输出
保持更简单的纯流式路径，因此管道、日志与测试保持稳定。

历史面板使用**视口裁剪**来维持滚动性能，即使是很长的对话也如此。
只渲染可见行加上缓冲区域。独立的绝对历史位置被转换为裁剪切片的局部滚动位置，
因此手动滚动保持稳定，而实时输出持续延长对话。

---

## 多代理编排

Aegis 内置编排工具，无需第二个 agent 循环即可拆分工作。
子代理与队友复用 `AgentRuntime`，使用各自独立的会话仓库、工具集与记录。

### `Agent`

`Agent` 工具为自包含任务启动一次性（one-shot）子代理：

```text
Agent
- prompt: 给子代理的任务指令
- subagent_type: explore | general-purpose  （可选）
- run_in_background: true | false
```

类型化子代理默认是**全新的**：它们接收请求的任务和各自的私有记录，
而不是主会话历史。内置的 `explore` 代理是只读的，用于大范围代码/文档检查。
`general-purpose` 拥有正常的内置工具集，但默认不获得 `Agent`，
以避免递归扇出。

如果省略 `subagent_type`，Aegis 会创建从主会话历史播种的 fork 子代理。
这对评审、反例或针对当前上下文的第二种意见很有用。
在所有模式下，子代理的中间轮次保持私有；主会话只接收最终结果
或后台完成通知。

使用 `run_in_background: true` 时，`Agent` 立即返回一个任务 id。
后台任务在守护线程上运行，Aegis 会在 REPL 轮次之间注入其完成通知，
因此模型无需轮询。启动面板会显示当前运行的子代理数量
（`Subagents: N running`）；REPL 会在轮次之间刷新该数字，
因此后台任务完成后它会回落到 0。

### `team_create` 与 `send_message`

`team_create` 创建带命名、长生命周期的队友的进程内团队。每个队友有
稳定的名字、自己的会话仓库，以及针对当前 Aegis 进程的持续上下文。
处理完一条消息后它进入空闲，新团队消息到达时再被唤醒。

```text
team_create
- description: 这个团队是做什么的
- members:
  - name: researcher
    agent_type: explore
    task: 初始任务
```

`send_message` 在活动团队内路由消息：

```text
send_message
- recipient: teammate-name | team-lead | *
- message: 要投递的文本
```

团队负责人可以给队友发消息，队友之间或给负责人发消息，
`*` 广播给其他队友。投递是团队作用域的：队友不能跨团队边界发送消息。

在 REPL 中使用 `/agents` 检查子代理任务 id、类型、状态、后台标志和描述。
它是任务自省，而不是完整的团队名册或团队管理 UI。

当前限制：团队与队友邮箱仅在进程内；团队状态与队友记录在进程重启后
不持久化；`/agents` 尚未列出完整的团队成员；嵌套子代理创建被有意限制，
以防止失控递归。

---

## 🔭 Langfuse 可观测性（质量阶段 0）

Langfuse 追踪是可选、fail-open 的旁路通道。安装 extra 并提供两份凭据即可启用：

```bash
uv sync --extra observability

export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://cloud.langfuse.com  # 可选

uv run aegis
```

每次 `AgentRuntime.run_turn` 创建一个顶层 trace。模型调用、集中式工具执行、
子代理运行与最终结果作为嵌套 observation 出现：

```text
Aegis Run
├── Model Call
├── Tool Call: list_directory
├── Tool Call: Agent
│   └── Subagent Run: explore
│       ├── Model Call
│       ├── Tool Call: read_file
│       └── Final Result
├── Model Call
└── Final Result
```

如果 SDK 缺失、凭据不完整或 Langfuse 上报失败，Aegis 会自动使用
no-op 后端并保留原始运行时结果与错误处理。Trace 载荷会针对常见凭据字段
与密钥模式进行递归脱敏；长字符串截断为 20,000 字符，原始长度作为元数据保留。

OpenAI 兼容与 Anthropic 响应会把提供者报告的输入、输出、缓存读取、
缓存写入 token 用量传播到每个 Langfuse Model Call。OpenAI 流式收集其
最终的 usage-only chunk。Anthropic 流式把 `message_start` 的输入/缓存桶
与最终 `message_delta` 的输出 token 合并，保留分开的缓存读取与缓存创建。
Anthropic 的 API 不返回总 token 或直接成本，因此 Aegis 让这些字段保持未设置，
而不是估算它们。直接返回总量或成本的兼容网关会透传；Aegis 没有价格表。

---

## 🧪 Harbor 离线评测（质量阶段 1–2）

Aegis 提供非交互式 runner 和一个 Harbor 自定义已安装代理。
Harbor 仍然负责任务、环境、试验（trials）、重试/并发、验证器与奖励；
Harbor 仓库本身无需修改。

直接运行一个 Aegis 任务（除非显式使用 `--model-backend fake`，否则
使用所选的真实提供者）：

```bash
uv run aegis run \
  --instruction "检查项目并完成任务" \
  --model-backend openai \
  --cwd /path/to/worktree
```

从兄弟 Harbor 检出目录运行 Aegis（Harbor 需要 Docker 可用）：

```bash
cd ../harbor
PYTHONPATH=../aegis-agent/src uv run harbor run \
  -p /path/to/task-or-dataset \
  --agent aegis_agent.integrations.harbor:Aegis \
  --model openai/qwen-model \
  --ae AEGIS_API_KEY="$AEGIS_API_KEY" \
  --ae AEGIS_BASE_URL="$AEGIS_BASE_URL" \
  --ae LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY" \
  --ae LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY" \
  --ae LANGFUSE_BASE_URL="$LANGFUSE_BASE_URL"
```

宿主机代理变量不会被隐式复制进任务容器。`127.0.0.1:10808` 之类的回环代理在
容器中会指向容器自身，并导致模型 `APIConnectionError`。如果提供者确实需要代理，
应先使用容器可达的代理地址，再通过 `--ae HTTP_PROXY=...` / `--ae HTTPS_PROXY=...`
显式传入。

适配器会在 Harbor 任务环境中安装当前的 Aegis wheel，并在 `/app` 中运行工具。
Harbor 的试验 UUID 成为 Aegis 的 `execution_id`；Langfuse trace id 由它
确定性派生，因此无需时间戳匹配。运行时记录写入试验的 `agent/` 日志之下。
Harbor 完成其验证器后，终态化单个试验或整个任务：

```bash
uv run aegis quality import-harbor ../harbor/jobs/<job-directory>
```

最终记录存储在 `~/.aegis/quality/executions`（可用 `AEGIS_EXECUTION_RECORDS_DIR`
覆盖）并作为 `execution-record.json` 放在每个 Harbor `result.json` 旁边。
运行时成功与验证器通过/失败是分开的；缺失的用量、成本或验证器字段保持
`null`，而不是被猜测。Langfuse 保持可选且 fail-open。

打开本地 Trace Viewer：

```bash
uv run aegis quality view
```

点击 **Sync Harbor** 即可从 `~/harbor/jobs` 导入所有新增或发生变化的已完成 Trial；已经同步且没有
变化的记录会直接跳过。使用 `--harbor-jobs-dir PATH` 或 `AEGIS_HARBOR_JOBS_DIR` 可指定其他位置。
需要只导入某一个 Result 或 Job 时，仍可使用 `quality import-harbor` 命令。

查看器打开 `http://127.0.0.1:8765`，立即显示本地 ExecutionRecord，并通过 SDK 的 v4
Observations API 在后台按游标分页加载 Langfuse 根 Observation（最多 1,000 条；达到安全上限时
页面会明确提示）及每个 Trace 的详情。即使外部 Execution ID 被重复使用、一个 Trace 中出现多个
Root，每个 Root Observation 仍使用自己的 ID 独立展示。顶部按运行类型分成三个视图，避免日常
聊天和基准评测混在一起：

- **Conversations**：按 Session → Turn 组织交互式对话；
- **Evaluations**：按 Job → Trial 组织已导入的 Harbor 评测，直接展示任务标识、Reward、Verifier
  Pass/Fail、Runtime Error 和 Usage；
- **All Runs**：合并的运维视图，也包含普通非交互式 `aegis run` 任务。

选择任一运行后继续进入 Model/Tool/Final 详情；各层按需展示调用次数、耗时、Input/Output Token、
Cache Read/Write、缓存命中率、Cost 和错误状态。缓存命中率定义为
Cache Read ÷ Provider 报告的总输入 Token。对阿里云百炼 OpenAI 兼容接口的隐式缓存，就是
`prompt_tokens_details.cached_tokens / prompt_tokens`；当 Cache Write 未返回时，Viewer 用等价的
`prompt_tokens` 原值或 `total_tokens - output_tokens` 得到精确分母。真实的零会显示为零（例如
`$0.00`），
Provider 没有返回的 Usage 或 Cost 则保持未知（`—`）；部分组成项未知的混合汇总会用 `≥` 标成
下界。同一个 Turn 内连续 Model Call 还会显示相邻 Input Token 的增长量。

选择一个 Turn 后，页面先展示最后一次模型调用实际收到的完整消息上下文（`system`、`user`、
`assistant`、`tool`）和最终模型输出，再展示 Agent/Model/Tool/Final 执行树。Model 和 Tool 节点会
直接显示关键字段；右栏优先展示状态、Usage、Request 和 Response，Raw Payload 放在最后的折叠区。
错误 Observation 会被明显标红，并向 Turn 与 Session 汇总。汇总卡片将失败 Run 与错误
Observation 数分开，避免把一次包含多个错误节点的失败显示成多个失败 Run。Python 响应边界会再次统一脱敏，
因此 Langfuse SDK 后加的 Metadata 或历史本地字段中的凭据也不会进入浏览器。历史 Message 本身没有
独立时间戳，因此时间只展示在真实执行节点上，不做猜测。共享 `session_id` 的 Turn 会显示在一个
可折叠的 Session 组中，组内保持时间顺序并聚合状态。Langfuse 凭据保留在 Python 进程中，绝不会
发送到浏览器 JavaScript。Trace GET 接口保持只读；唯一的变更接口是用户显式点击触发的 Harbor
Sync POST，它只访问配置好的 Jobs 目录，并使用每次启动随机生成的同源令牌保护。服务不开放 CORS，
默认绑定回环地址；需要时可用 `--no-open`、`--port`、`--records-dir` 或
`--harbor-jobs-dir`。

## 🔎 过程评测（质量阶段 3）

Process Evaluation 只消费已有 `ExecutionRecord`，不强依赖 Langfuse 或 Harbor，也不改变
Agent Loop。默认只运行确定性规则，不调用模型。可以评测一个 JSON Record/Execution ID，或批量
评测本地 Store：

```bash
uv run aegis quality evaluate <execution-id-or-record.json>
uv run aegis quality evaluate --all
uv run aegis quality evaluate --all --json
uv run aegis quality evaluate <record> --failure-recovery-judge openai
```

普通交互对话保持显式启用：`--record-conversations` 为每个 Turn 写一个独立的
`run_kind=conversation` Record；`--process-evaluate-conversations` 还会在 Turn 完成后立即评测。
同一对话的 Turn 共享 `session_id`，但各自使用不同的 Execution/Trace ID：

```bash
uv run aegis --record-conversations
uv run aegis --process-evaluate-conversations
```

已有 SQLite 会话可以事后重建。命令按用户 Turn 生成确定性 ID，因此重复执行只覆盖同一批 Record，
不会制造重复数据：

```bash
uv run aegis quality record-session <session-id>
uv run aegis quality record-session <session-id> --evaluate
```

重建 Record 保留已持久化的消息、工具名/参数/结果、关联 ID 和可用时间戳，并标记
`reconstructed_from_session`。历史中没有保存的 Provider/Model、Usage、Cost、精确请求上下文和调用耗时
保持未知，不做推测。

命令会安全替换派生字段 `quality.process_evaluation`，但保持 `schema_version: "1.0"`、原始 Step、
Runtime Outcome 和 Harbor Verifier 数据不变。每次结果保存评测时间、Evaluator 版本、实际权重、
Overall Score/Status，以及 6 个带版本、可解释的 Grade：

- 重复工具调用：参数相同或高度相似、结果相同，且中间没有成功的状态修改；
- 重复失败：同一未调整的工具操作达到可配置的连续失败阈值；
- 失败恢复：为每个 Tool/Model/Runtime Failure 提取完整 Episode，包含诊断、工具/参数/路径变化、
  Mutation、相关重试及结果；成功的 read/search/status 和无关成功动作本身绝不算 Recovery；
- 最终验证：检测到重要状态修改后，查找成功且与任务匹配的验证工具或命令；
- 循环检测：识别连续 `A×N` 和周期性 `(A,B)×N` 工具模式；
- 执行效率：记录 Step/Model/Tool/Failure/Repeat/Token/Cost/Latency；只有显式配置阈值或
  Baseline 时才使用绝对/相对上限。

可选的 `FailureRecoveryLLMGrader` 只精炼已有 `failure_recovery` Grade：仅在至少存在一个 Failure
Episode 时发起一次 Side Query，判断诊断是否有效、调整是否针对原失败、原失败目标是否真正恢复。
通过 `--failure-recovery-judge`（或 `AEGIS_FAILURE_RECOVERY_JUDGE`）选择 `auto`、`openai` 或
`anthropic`。没有模型配置、Provider 异常、JSON 非法、Episode ID 不匹配或 Recovery Step 无法落到
原 Trace 时，Score/Status 保持规则结果；其余 5 个 Grader 始终为确定性规则。

长期 Quality 偏好可写入 `~/.aegis/config.yaml`（或通过 `--config` / `--mcp-config` 指定的文件）：

```yaml
quality:
  conversations:
    record: false
    evaluate: false
  failure_recovery_judge:
    enabled: true
    provider: openai       # auto、openai 或 anthropic
    model: <judge-model>
    base_url: null         # 可选兼容 Endpoint
```

API Key 不写入 YAML。OpenAI-compatible Judge 读取 `AEGIS_API_KEY`，未在上面覆盖时读取
`AEGIS_MODEL` / `AEGIS_BASE_URL`；Anthropic Judge 读取 `ANTHROPIC_API_KEY`，并可读取
`ANTHROPIC_MODEL` / `ANTHROPIC_BASE_URL`。显式 CLI 参数临时覆盖配置的 Provider；
`enabled: false` 会让所有 Process Evaluation 保持纯规则模式。

每个非 Pass Grade 都包含 Severity、Evidence 和可定位的 `step_id`。如果字段不足以可靠判断，
结果使用 `insufficient_data`，不伪造结论。任务可通过
`record.metadata.process_evaluation_config` 覆盖验证规则、相似度/循环阈值、效率阈值或 Baseline，
以及 Grader 权重；`failure_recovery_llm_enabled=false` 可按 Record 禁用 Judge。Process FAIL 目前
只是分析结果，不是 Quality Gate，因此 CLI 只会在读取、评测或保存发生操作错误时返回非零。

Viewer 将 Harbor **Outcome** 与 **Process** 状态分开展示，因此允许 `Outcome PASS` 同时
`Process FAIL`。Run 卡片显示 Process Score/Status；ExecutionRecord 详情显示每个 Issue 的 Grader、
Severity、Message 和 Affected Steps，点击 Issue 会尽量定位第一个受影响的本地 Step。Harbor 重导入
会保留已有 Process Result，并且永远不修改原始 `result.json`。

### 触发与记录生命周期

CLI 会先加载项目目录的 `.env`，再加载 `~/.aegis/.env`。只有同时存在
`LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY` 时才启用 Langfuse；
`LANGFUSE_BASE_URL` 用于选择 Cloud 区域或自托管 Backend。SDK 缺失、凭证不完整、初始化错误
或上传失败时，都会自动降级为 No-op Trace。

| 操作 | 触发条件 | 结果 |
|---|---|---|
| `uv run aegis --session <id>` | 每次用户提交消息并进入 `AgentRuntime.run_turn()` | 写入普通 Session 历史；启用 Langfuse 时，每个 Turn 再产生一个标记为 `conversation` 的云端 Trace。只有显式使用 `--record-conversations`、`--process-evaluate-conversations` 或对应持久配置时才生成 ExecutionRecord。 |
| `uv run aegis quality record-session <session-id>` | 显式重建已有 SQLite 会话 | 为每个已持久化的用户 Turn 写入一个确定、幂等的 `conversation` ExecutionRecord；`--evaluate` 同时运行过程评测。缺失的历史遥测保持未知，并在 Record Metadata 中说明。 |
| `uv run aegis run ...` | 一个非交互式任务 | 始终写入标记为 `task` 的本地 ExecutionRecord；启用 Langfuse 时同时发送 Trace。缺少 `execution_id` 时生成 UUID；`session_id` 默认等于它；`trace_id` 根据它确定性生成。`--run-kind evaluation` 保留给评测适配器。 |
| `uv run harbor run ... --agent aegis_agent.integrations.harbor:Aegis` | 一个或多个 Harbor Trial | Harbor 启动每个 Docker 任务环境、安装 Aegis，将 Trial UUID 作为 `execution_id`，并把运行标记为 `evaluation`。Aegis 写入 Runtime Record，然后 Harbor 运行 Verifier 并写入 `result.json`。 |
| `uv run aegis quality import-harbor <result-or-job>` | 显式执行 Verifier 后导入 | 把 Harbor Identity、Verifier Reward、异常、汇总 Usage 和 Artifact 合并到最终 ExecutionRecord。参数可以是单个 `result.json` 或整个 Job 目录。 |
| `uv run aegis quality evaluate <record>` / `--all` | 显式离线过程评测 | 运行 5 个确定性 Grader 和规则优先的 Failure Recovery；显式配置的 Judge 只精炼 Failure Episode，失败时安全保留规则结果。原子更新 `quality.process_evaluation`；Outcome/Verifier 字段与原始 Step 保持不变。 |
| `uv run aegis quality view` | 启动本地查看页 | 先读取本地 JSON，再按游标读取可选 Langfuse Root，达到安全上限时明确提示；**Sync Harbor** 从配置的 Jobs 目录增量导入新增或变化的已完成 TrialResult，不修改 Langfuse Trace。单元测试会强制把 Langfuse 凭据设为空，防止开发者 `.env` 把 pytest Trace 上传到云端；该隔离只阻止后续上传，不会自动修改已有的 Langfuse 历史。 |

Langfuse 采用异步上传。CLI 正常退出时会调用 Runtime `shutdown()`，Flush 等待中的
Observation。Harbor 最终合并是独立的显式步骤：没有执行 `import-harbor` 或点击
**Sync Harbor** 时，Trial 中仍有 Aegis Runtime Record 和 Harbor 原始 `result.json`，但中央
Record 尚未加入 Verifier 输出。

### 记录的数据

Langfuse 的一个 `Aegis Run` 记录用户任务、Session/Agent/版本 Metadata、最终输出、成功或错误、
Stop Reason、迭代/Tool 数量和耗时。子节点记录：

- Model Call：Provider、模型、输入消息、输出、Finish Reason、Tool Call、错误、耗时、可靠的
  输入/输出/总 Token、独立 Cache Read/Write Token，以及上游直接返回的 Cost。
- Tool Call：工具名称、脱敏后的参数和结果、成功或错误及耗时。
- Subagent Run：类型、任务、Parent Agent、结果、错误、耗时，以及内部嵌套的
  Model/Tool/Final Tree。
- Final Result：输出、成功或错误和 Stop Reason。

本地 `ExecutionRecord` Schema 还包含稳定的 `run_kind`（`conversation`、`task` 或
`evaluation`）、Execution/Task/Trial/Job/Session/Trace Identity、Agent 配置、Execution 时间与状态、
按父子关系排序的 Step Tree、Usage 计数桶、Harbor Verifier
Result/Reward/Pass 状态和 Artifact/日志路径。Runtime 成功与 Verifier Pass/Fail 始终分开。
Provider 或 Harbor 没有提供的字段保持 `null`。

所有 Langfuse Payload 和本地 Step Payload 都通过同一个 Sanitizer。常见凭证字段和密钥模式会
替换为 `[REDACTED]`。字符串最多 20,000 字符，集合最多 100 项，递归最多 8 层；发生截断时会
保留原始大小 Metadata。

### 存储位置

| 数据 | 默认位置 | 覆盖方式或说明 |
|---|---|---|
| 交互式 Session 历史 | `~/.aegis/state.db` | `--db` 可覆盖；`--ephemeral` 禁用持久化；Project Scope 使用项目数据目录。 |
| Langfuse Trace | `LANGFUSE_BASE_URL` 对应的 Project | Aegis 不维护第二份本地 Langfuse 数据库。 |
| 单次执行中央 Record | `~/.aegis/quality/executions/<execution_id>.json` | 使用 `--records-dir` 或 `AEGIS_EXECUTION_RECORDS_DIR` 覆盖。 |
| 单次执行显式副本 | `--record-path` / `AEGIS_EXECUTION_RECORD_PATH` | 与中央 Record 同步写入。 |
| Harbor Runtime Record | `<trial-dir>/agent/execution-record.runtime.json` | 在 Verifier 输出产生前写入。 |
| Harbor Agent 日志 | `<trial-dir>/agent/aegis.txt` | 保留 Agent stdout/stderr。 |
| Harbor 原始结果 | `<trial-dir>/result.json` | Harbor 在 Verifier 完成后写入。 |
| Harbor 最终 Record | `<trial-dir>/execution-record.json` | 由 `quality import-harbor` 写入。 |
| 导入后的中央副本 | `~/.aegis/quality/executions/<trial-uuid>.json` | 包含合并后的 Runtime 与 Verifier 结果。 |
| Trace Viewer | 不创建独立存储 | 读取本地 Record 和 Langfuse v4 Observation；显式 Harbor Sync 会把 Final Record 写入既有中央 Store 和 TrialResult 旁。 |

查看页使用 `session_id` 对 Conversation 分组，使用 `job_id` 对 Harbor Evaluation 分组；同一个运行的
本地与云端副本仍通过精确 `trace_id` 关联，不依赖时间戳。云端成功状态优先读取 Aegis 显式 Success
Metadata；旧 Trace 则根据 Level、
`stop_reason`、结束时间和 Output 兼容推导。`CLOUD` 表示只有 Langfuse 的 Trace，`LOCAL` 表示
ExecutionRecord，本地条目上的 `LF` 表示找到了相同的 Langfuse Trace。搜索命中任一 Turn 时会
保留完整 Session，避免隐藏追问上下文。

---

## 📦 上下文压缩

长时会话在超过配置的上下文预算时，会在模型调用前被压缩。

流水线包含三个阶段：

```text
超大工具结果卸载
          ↓
     本地微压缩
          ↓
    轮次级 LLM 摘要
```

大型工具输出被移动到：

```text
~/.aegis/tool-result-cache/
```

原始会话历史永远不会被修改。

配置上下文预算：

```bash
uv run aegis --context-max-tokens 80000
```

或：

```bash
export AEGIS_CONTEXT_MAX_TOKENS=80000
```

---

## 🧠 记忆

Aegis 将**长期记忆**与**原始会话历史**分离。

```text
~/.aegis/
├── state.db                    # 个人会话存储
├── USER.md                     # 全局用户画像（两个作用域共用）
├── memory/                     # 个人作用域
│   ├── MEMORY.md
│   └── *.md
└── projects/
    └── <project-id>/           # 项目作用域（按项目隔离）
        ├── state.db            # 项目会话存储
        └── memory/
            ├── MEMORY.md
            └── *.md
```

长期记忆支持：

* 记忆索引注入
* 基于相关性的召回 —— **默认开启**
* 轮次后记忆提取 —— **默认开启**
* **个人作用域**（默认）与**项目作用域** —— `USER.md` 是全局的，记忆按作用域隔离

用以下命令关闭任意一个动态通道：

```bash
uv run aegis --no-memory-recall
uv run aegis --no-memory-extract
```

使用 `--project` 开启项目作用域记忆（裸 `--project` 使用当前目录）：

```bash
uv run aegis --project /path/to/repo
```

---

## 🧩 配置

持久化设置存放在 `~/.aegis/config.yaml`（与 `mcp_servers` 同一文件；
完整键参考见 `config.example.yaml`）。只需填写你想覆盖的键。
优先级：**CLI 标志 > 配置文件 > 内置默认值**。

```yaml
# ~/.aegis/config.yaml
memory:
  recall: true
  extract: true
context:
  max_tokens: 120000
iterations:
  max: 50
session:
  snapshot_every_n: 20
mcp_servers: { ... }
```

内置上限为**每轮对话 50 次模型/工具迭代**，不是 50 次独立工具调用。
可通过上面的 `iterations.max` 持久设置，或单次启动时使用
`uv run aegis --max-iterations 100`（简写 `-n 100`）覆盖。
修改后需重启 Aegis 并恢复会话才能生效；已经运行的会话保留启动时的上限。
子代理显式配置的上限不变。

可从文件配置：memory（`enabled` / `recall` / `extract` / `project`）、
context（`compress` / `max_tokens`）、iterations（`max`）、
session（`db_path` / `snapshot_every_n` / `lease`）、skills（`enabled` / `dir`）、
mcp（`enabled`）、shell（`allow_dangerous`）、model（`backend`）。

---

## 🔍 会话历史搜索

Aegis 可以不调用 LLM，直接从 SQLite 搜索历史对话。

搜索层使用：

```text
SQLite FTS5
   +
BM25 排序
   +
CJK 三元组匹配
```

`session_search` 工具支持：

* 搜索历史消息
* 浏览最近的会话
* 读取完整会话
* 检查某个点附近的消息

---

## 🛠 内置工具

Aegis 目前包含：

```text
read_file
list_directory
write_file
patch
search_files

terminal
process

web_search
web_extract

session_search

Agent
team_create
send_message

skills_list
skill_view
skill_manage
```

更多工具可以通过 MCP 暴露。

`terminal` 前台超时返回 `exit_code: 124`，并保留进程被杀死前捕获的
stdout/stderr，因此代理可以用替代命令恢复，而不是丢失部分诊断信息。

---

## 🧩 Skills 与 MCP

### Skills

Aegis 支持基于 `SKILL.md` 的扩展，具备：

* 发现
* 加载
* 路由
* 斜杠命令
* 渐进式披露（progressive disclosure）
* 动态提示词注入

### MCP

MCP 客户端支持：

```text
stdio
Streamable HTTP
```

并带 schema 规范化与运行时工具包装。单个 MCP 工具调用仍遵守服务器配置的
`timeout`；慢速上游操作会作为 MCP 错误结果返回给模型，而不是让 agent 循环崩溃。

---

## ⚙️ 实用命令

```bash
uv run aegis

uv run aegis --resume my-session
uv run aegis --db ./custom.db
uv run aegis --ephemeral

uv run aegis --no-lease
uv run aegis --no-compress
uv run aegis --no-memory

# recall/extract 默认开启；按如下方式关闭：
uv run aegis --no-memory-recall
uv run aegis --no-memory-extract

uv run aegis --project /path/to/repo
uv run aegis --project

uv run aegis --version
```

在 REPL 内使用 `/agents` 检查子代理任务。

可选依赖：

```bash
uv sync --extra web
uv sync --extra redis
```

---

## 🧪 开发

```bash
uv run pytest -q
uv run ruff check .
```

默认测试不需要付费模型 API。

---

## 🗺 路线图

计划中的改进包括：

* 并发工具执行
* 防护型熔断器（guardrail circuit breaker）
* MCP 重连与熔断器
* 剩余历史版本化集成

---

## 📚 文档

更多实现细节见：

```text
docs/extraction-plan.md
docs/development-log.md
docs/source-map.md
```

* `extraction-plan.md` —— 运行时提取与开发计划
* `development-log.md` —— 实现说明与工程决策
* `source-map.md` —— Aegis 模块与其 Hermes / Claude Code 参考来源的对应关系

---

## 📄 来源说明（Provenance）

Aegis 在文档注明处从 Hermes 与 Claude Code 参考来源提取、改编并重新实现了
选定的运行时行为。Claude Code 作为记忆与多代理/团队特性的行为与架构参考，
见 `docs/source-map.md` 中的说明。

改编的源文件在需要处保留了署名。详见：

```text
THIRD_PARTY_NOTICES.md
docs/source-map.md
```

获取详细来源与许可信息。

Hermes © 2025 Nous Research，以 MIT 许可发布。
