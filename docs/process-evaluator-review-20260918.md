# Process Evaluation 样例复核与改进方案（2026-09-18）

本报告以实际 Harbor Trial、Aegis ExecutionRecord 和离线 Process Evaluation 为证据。
基础设施失败样例保留，但不能作为任务能力通过率的有效样本。未改动 benchmark 任务、测试或奖励。

## 已确认的对照样例

| execution_id | 任务与真实情况 | 当前过程结果 | 复核意见 |
|---|---|---|---|
| `302189be-9aea-4325-9aba-ec77afd1c89b` | cancel-async-tasks；此前 Harbor 通过，执行了 `python3 test_run.py` | 0.8333 / FAIL，Final Verification 失败 | 自定义 Python 验证未被命令规则识别 |
| `64d31502-a1d6-456e-b9f1-5e7b87f0d853` | chess-best-move；Setup 阶段失败，无有效 Agent 过程 | 1.0 / PASS | 缺数据被误当成无问题 |
| `e365b0fb-e5e5-443b-9814-107c112a1bfa` | regex-log；写入文件后模型调用超时，运行中使用的 WSL 地址后来不可达 | 0.6667 / FAIL；LLM Judge `applied` | 确有中断和未验证，但未区分基础设施责任、Runtime 重试能力与 Agent 恢复机会 |
| `d2ebf469-f9e4-44f1-92d3-bbeab70bef25` | regex-log；Agent 正常结束，24 项自测及多行测试输出通过；独立 Verifier 下载 uv 超时 | 0.8333 / FAIL，Final Verification 失败 | 自测被漏识别；Verifier 超时应表示 Outcome 未知，不能反推 Agent 自测不存在 |
| `bbc25635-6d91-4f6a-b035-467a5c7cffad` | log-summary-date-ranges；Harbor 官方 2 项测试通过，reward=1；Python 内联脚本写 summary.csv 并读回打印 | 1.0 / PASS；Final Verification `required=false` | 评分理由错误：规则漏识别 `python3 -c` 中 `open(..., 'w')` 的实际修改 |
| `acd1dc0e-cdce-410f-848b-5edef2e1c37d` | regex-log；7 轮/6 次工具调用；自测输出通过，官方 1 项测试通过，reward=1 | 0.8333 / FAIL；Final Verification 失败 | seq 9、11 的 Python heredoc 实际做了输入/预期结果比较，仍被漏识别 |
| `d23d64b7-159f-4a73-ada3-01e37384a8a7` | polyglot-c-py；22 轮/21 次工具调用；恢复了 Python SyntaxError；官方测试因多余文件失败，reward=0 | 0.8333 / FAIL；Recovery 为 `rules+llm` PASS，Final Verification 失败 | 恢复判断有真实证据；Final Verification 理由仍错误：执行过数值自测，但未覆盖目录产物约束 |

最终三条 Job 全部完成 Agent 和 Verifier，均无 Harbor 基础设施异常，2 条 reward=1、1 条 reward=0。
日志汇总总耗时 12m01s，polyglot 22m40s，regex 22m58s，包含依赖安装，不可直接当作模型延迟。
三条已导入中央 Store 并完成离线过程评测；Viewer 可按上面的 execution-id 查找。

polyglot 的实际失败是 `/app/polyglot` 包含 `cmain`、`main.py.c`、`test_warn.c`，
而官方测试在运行数值检查前要求目录只包含 `main.py.c`。因此该次 reward=0 不能解释为
已经证实 Fibonacci 计算错误。任务提示提供了会生成 `cmain` 的编译命令，复核时应分别呈现
用户可见要求、官方目录断言、Agent 已做的数值测试和遗漏的清理，而不是笼统声称“未验证”。
LLM Judge 实际定位 SyntaxError 步骤 `660658081e6b44deaadc4e1ff4124469`，
及恢复步骤 `aa516e36a2424b5aac4011d7ab54859c`，`status=applied`、`prompt_truncated=false`。
另外两个最终样例没有失败 episode，Failure Recovery 跳过 LLM，使用规则。

前三个阶段性 Job：`aegis-process-eval-sample-20260918`、
`aegis-process-eval-proxy-check-20260918`、`aegis-process-eval-stable-proxy-20260918`，
位于 `/home/nacha/harbor/jobs`。原始 `result.json`、`agent/execution-record.runtime.json`、
`verifier/test-stdout.txt` 保留为来源证据。中央评测结果位于
`/home/nacha/.aegis/quality/executions/<execution-id>.json` 的 `quality.process_evaluation`。

## 主要不足

1. **证据可用性没有前置门槛。** `ProcessEvaluator.evaluate` 在空步骤上仍运行六个 grader。
   “没观察到重复/循环/修改”分别给 PASS，最终产生 1.0。缺步骤、真实无工具对话、完整评测运行
   是三个不同状态，不能都按“没有坏行为”处理。
2. **最终验证以名字和命令正则代替语义。** `_is_verification` 识别 pytest 等命令，却漏掉
   自定义 Python、heredoc 测试和直接比较实际/预期结果。反过来，`git status`、`git diff` 被列为
   验证，可能无法证明任务功能正确；命令正则还可能命中被打印或写入文件的文本。
3. **变更与验证没有关联到同一个产物。** 当前只要求“最后一次识别到的修改之后存在成功验证”。
   未验证同一文件/验收条件，也不能可靠识别同一 shell 调用内“写文件→测试”的先后关系。
   日志汇总新样例还证明内联 Python 写文件可完全绕过 mutation 规则；即使最终任务正确，
   `required=false` 仍是错误解释，不能把总分恰好通过当作 grader 正确。
4. **失败恢复归责过粗。** 模型请求失败会被视为待恢复 episode；若 Runtime 直接终止，Agent
   根本没收到错误反馈，LLM 仍会因没有诊断/恢复扣分。应记录谁可以控制重试、是否存在恢复机会。
5. **效率 PASS 的含义容易被高估。** regex-log 超时记录有约 30.9k tokens、433 秒 Runtime，
   Efficiency 仍是 1.0，因为没有阈值或同任务基线。这不证明高效，只表示没有配置可判定的信号。
6. **LLM 参与情况不够直观。** 默认启用目前只作用于 Failure Recovery。有失败才调用；
   Final Verification 等仍是规则。一次实际 `rules+llm` 评测约 11 秒，响应快慢不能判断是否调用。

这些样例不是随机抽样，数量也不足以估算误报率或各模型能力排名；这里给出具体缺陷证据和回归目标。
当前默认六项等权平均，0.8333 通常表示五项 1.0、一项 0.0，而总状态只要出现 FAIL 就可能为 FAIL。
这个分数不是任务正确概率，也不是经过标注集校准的质量置信度。

## 建议的改进顺序与验收标准

### P0：完整性和故障阶段

在评分前生成独立的证据状态：`complete`、`partial`、`missing`，并记录
`failure_phase = setup | agent | verifier`。Setup 失败且无 Agent 步骤时，总分应为 null，
状态为 `insufficient_data`；完整的无工具回答不因没有工具而被误标。
Verifier 中断应保留 Runtime 成功证据，Outcome 为未知，并单独展示基础设施错误。
不要用 Harbor reward 直接覆盖过程评分。

验收：五条原始 Setup 失败记录不再出现 1.0 PASS；空 Trace、完整无工具回答、
Agent 中断、Verifier 中断四类均有可追溯状态；原始记录不被改写。

### P1：基于证据的 Final Verification

提取“修改了什么→执行了什么检查→检查了哪个产物→输出是否满足预期”的证据链。
明确的测试框架命令先用规则；自定义脚本、heredoc 和其他含糊场景调用 LLM Judge，
要求返回真实 step id、产物、输入/输出证据、置信度和判断理由。
“未识别到验证”在证据不足时返回 unknown/insufficient_data，不直接声称没有验证。

对自定义脚本必须同时看检查逻辑与实际输出。仅命名为 `test_*.py`、仅打印 PASS、
或退出码为 0 都不能单独证明通过。当前 regex-log 自测脚本失败分支只打印 FAIL，
不会以非零状态退出，因此本次输出通过可作为证据，但脚本退出状态本身不够可靠。

验收：两个已知自定义 Python 样例识别到验证；`echo pytest`、仅写测试未运行、
对无关文件测试、输出 FAIL 但退出 0、测试后又修改产物均不能通过；
LLM 不可用或引用不存在步骤时保留不确定状态，不伪造规则确认。
新增 polyglot 样例要求能表示“有功能验证，但未覆盖目录产物约束”，避免把部分覆盖变成
“完全没有验证”或“全部要求已验证”。日志汇总样例要求识别内联 Python 写文件。

### P2：恢复机会与 LLM 调用可见性

Failure Episode 增加错误来源、是否暴露给 Agent、后续是否有模型行动机会。
模型调用失败导致 Runtime 直接终止，记录为基础设施/Runtime 恢复问题；
Agent 收到工具失败后继续重复错误动作才适合评估其恢复策略。
CLI 输出每个 grader 的 `rules / rules+llm / fallback / skipped` 和原因，
记录 Judge 模型、耗时、用量、提示词版本与截断情况。

验收：有无恢复机会的两条相似轨迹评分不同且理由可定位；
默认启用、无失败跳过、无模型配置、Judge 超时四条路径的 CLI 行为可区分。

### P3：校准与回归集

先用本次样例建立人工复核的正/负/未知标签，再逐步增加成功、真实功能失败、
合理重试、循环、部分验证等轨迹。重复同一 record 检查 Judge 稳定性；
统计各 grader 的误报/漏报，不只看平均分。效率以同任务、同模型/预算条件下的分布为基线，
无基线时展示实际用量与“未校准”，避免把默认 PASS 解释为效率优良。

本轮实现的是网络贯通和可配置模型超时；上述评分改进仍是待实施方案，
未为了得到更好分数而修改 grader 或样例结果。

## 本次环境准备与复现约束

最终三条样例使用 `aegis-process-<task>-fixed-v2-20260918` Job，任务路径分别明确指向
`regex-log`、`log-summary-date-ranges`、`polyglot-c-py`，使用相同 qwen3.7-plus 模型。
Agent/Verifier 显式代理为 `http://host.docker.internal:18080`，模型超时 300 秒，
Setup multiplier=4，最大 Harbor 重试数 0；pip/uv 使用 wrapper 中配置的镜像。

该 18080 端口是本次测试的临时转发，不是自动部署的持久代理服务。今后复现应让
`AEGIS_HARBOR_PROXY` 指向实际可达、可用的代理监听端口。
验收完成后已停止转发；没有留下运行中的测试容器，也没有修改用户持久代理配置。

预检发现 `https://astral.sh/uv/0.7.13/install.sh` 重定向到 releases.astral.sh 后返回 403，
而 GitHub 官方同版本发布地址能返回 200。因此在三条任务的运行容器中预装官方 uv 0.7.13
二进制到 `/usr/local/bin`。下载位置为 GitHub `astral-sh/uv` 的 `0.7.13` Release；
`uv-x86_64-unknown-linux-gnu.tar.gz` 与同地址 `.sha256` 校验一致：
`909278eb197c5ed0e9b5f16317d1255270d1f9ea4196e7179ce934d48c4c2545`。
这个准备步骤没有改动 `/app` 中的任务产物、benchmark 测试、Verifier 判据或 reward。
样例属于有此依赖预装的环境，不能宣称未经准备的原始镜像已经稳定可复现。
后续日志汇总 Trial 的原始 Verifier 安装脚本又成功下载 uv，说明预检 403 不是持续复现的
全站不可用，可能与请求路径、客户端或瞬时网络状态有关；不把它归因为确定的永久封锁。

后续批量运行宜把这类固定依赖预装到可版本化的基础镜像，并保存镜像摘要、代理/镜像配置
与实际软件版本；避免每个 Trial 重新下载依赖消耗大部分时间。
