# Aegis Agent

一个轻量、可恢复、可扩展的 **Agent 运行时**，通过提取、简化并演进
[Hermes](https://github.com/NousResearch/hermes-agent) 与 Claude Code 参考实现中的
实用运行时行为构建而成。

Aegis 面向需要会话恢复、上下文压缩、记忆、工具调用、子代理、团队和可扩展运行时
组件的长时运行代理。

## 已包含能力

- **核心运行时：** Agent Loop、fake/OpenAI 兼容/原生 Anthropic 提供者、流式工具调用、
  上下文压缩和实时终端 UI。
- **工具与扩展：** 文件与 Shell 工具、带 SSRF 防护的 Web 工具、`SKILL.md` 技能、
  MCP（stdio 与 Streamable HTTP）以及会话搜索。
- **会话与记忆：** SQLite 持久化、快照、租约、个人/项目记忆、异步召回、提取和会话恢复；
  召回正文通过缓存友好的临时上下文注入，不会持久化，也不会形成孤立的工具消息。
- **编排：** 一次性/后台子代理、进程内团队和代理间消息传递。
- **质量能力：** 可选 Langfuse 追踪、Harbor 集成、`ExecutionRecord`、过程评测和本地 Trace Viewer。

详细实现历史与设计决策见
[`docs/development-log.md`](docs/development-log.md)。

## 项目结构

```text
src/aegis_agent/
├── cli.py          # CLI / REPL 入口
├── runtime.py      # AgentRuntime 与 Agent Loop
├── models/         # 提供者协议与模型适配器
├── tools/          # 工具注册表、执行器与内置工具
├── context/        # 上下文构建与压缩
├── sessions/       # 会话仓库与租约
├── memory/         # 个人与项目记忆
├── skills/         # SKILL.md 加载与路由
├── agents/         # 子代理、团队与消息传递
├── observability/  # 可选追踪与脱敏
├── quality/        # Record、评测、存储与 Viewer
├── integrations/   # Harbor 适配器
└── mcp/            # MCP 客户端
```

原始对话消息会被保留；上下文压缩只修改发送给模型的派生视图。

## 快速开始

前置条件：[uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run aegis
```

没有模型配置时，Aegis 使用确定性的 fake provider。配置 OpenAI 兼容 Endpoint：

```bash
export AEGIS_API_KEY=...
export AEGIS_BASE_URL=http://localhost:1234/v1
export AEGIS_MODEL=gpt-4o-mini
uv run aegis
```

配置原生 Anthropic provider：

```bash
export ANTHROPIC_API_KEY=...
export ANTHROPIC_MODEL=claude-sonnet-4-6
uv run aegis --model-backend anthropic
```

使用 `--model-backend fake` 强制使用确定性 provider。`auto` 优先使用 OpenAI 兼容配置，
否则在 Anthropic 配置完整时使用 Anthropic。

## 会话与项目

交互式会话存储在 SQLite 中。个人会话使用 `~/.aegis/state.db`；项目会话使用
`~/.aegis/projects/<project-id>/` 下独立的存储。

```bash
uv run aegis --resume my-session
uv run aegis --project /path/to/repo --resume my-session
uv run aegis --ephemeral       # 不持久化会话
```

使用 `--project` 将记忆和会话限定到当前项目；裸 `--project` 使用当前目录。

## 交互式命令

在 REPL 中输入 `/help` 查看完整列表。常用命令包括：

```text
/new [name]    开始会话（别名：/reset）
/clear         清屏并开始会话
/history       查看对话
/save          写入本地调试快照（别名：/chatlog）
/retry         重发最后一条用户消息
/undo [N]      撤销用户轮次，同时保留审计历史
/title [name]  设置或显示会话标题
/sessions      列出已记录的会话
/agents        查看子代理任务
/exit          退出（别名：/quit）
```

交互式 UI 支持 Markdown 回复、历史滚动和斜杠命令补全；非 TTY 输入/输出仍适合管道和日志。

## Harbor 与质量评测

直接运行一个非交互式任务：

```bash
uv run aegis run \
  --instruction "检查项目并完成任务" \
  --model-backend openai \
  --cwd /path/to/worktree
```

从兄弟 Harbor 仓库运行评测（需要 Docker）：

```bash
cd ../harbor
PYTHONPATH=../aegis-agent/src uv run harbor run \
  -p /path/to/task-or-dataset \
  --agent aegis_agent.integrations.harbor:Aegis \
  --model openai/qwen-model
```

Harbor 验证完成后导入一个结果或 Job：

```bash
uv run aegis quality import-harbor ../harbor/jobs/<job-directory>
```

过程评测离线消费 `ExecutionRecord`，不会改变 Agent Loop：

```bash
uv run aegis quality evaluate <execution-id-or-record.json>
uv run aegis quality evaluate --all
uv run aegis quality evaluate --all --json
```

评测会检查证据完整性；证据不足时保持 `insufficient_data`，不会强行判定成功或失败。
Execution Efficiency 在同一 `task_id` 累积至少 5 个成功 execution 并生成 median baseline 前显示
`NOT_SCORED`，不会影响总体 PASS/FAIL。可选的 Judge 可以复核含糊的恢复和验证证据。
请将凭据放在环境变量中，不要写入 YAML。完整评分行为见
[开发报告](docs/development-log.md) 和 [复核报告](docs/process-evaluator-review-20260918.md)。

## Trace Viewer 与可观测性

启用可选的 Langfuse 追踪：

```bash
uv sync --extra observability
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
uv run aegis
```

追踪是可选的 fail-open 旁路。启动本地 Viewer：

```bash
uv run aegis quality view
```

Viewer 读取本地 Record，可同步已完成的 Harbor Trial，并将普通对话、评测和全部运行
分开显示。默认绑定回环地址；需要时使用 `--no-open`、`--port`、`--records-dir` 或
`--harbor-jobs-dir`。Run 状态反映最终执行结果；已恢复的中间步骤错误仍会在对应
Observation 和独立的错误 Observation 计数中保留。

## 配置

持久化设置位于 `~/.aegis/config.yaml`，优先级为
**CLI 参数 > 配置文件 > 内置默认值**。完整键参考见 [`config.example.yaml`](config.example.yaml)。

```yaml
memory:
  recall: true
  extract: true
context:
  max_tokens: 120000
iterations:
  max: 50
session:
  snapshot_every_n: 20
```

常用选项包括 `--max-iterations`、`--context-max-tokens`、`--no-compress`、`--no-memory`、
`--no-memory-recall`、`--no-memory-extract`、`--no-lease` 和 `--project`。

可选依赖组：

```bash
uv sync --extra web
uv sync --extra redis
uv sync --extra observability
```

## 开发

```bash
uv run pytest -q
uv run ruff check .
```

默认测试使用确定性 provider，不需要付费模型 API。

## 文档与来源

- [`docs/development-log.md`](docs/development-log.md) —— 实现历史与工程决策
- [`docs/source-map.md`](docs/source-map.md) —— Aegis 与 Hermes、Claude Code 参考实现的对应关系
- [`docs/extraction-plan.md`](docs/extraction-plan.md) —— 提取与开发计划
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) —— 第三方署名与许可证

Aegis 在文档注明处独立实现或改编 Hermes 与 Claude Code 的选定运行时行为，
不包含它们无关的产品功能。
