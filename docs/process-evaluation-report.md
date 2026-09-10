# Aegis Agent — Process Evaluation 实现细节报告

（阶段 3，代码位于 `src/aegis_agent/quality/process.py` 与 `src/aegis_agent/quality/models.py`）

---

## 一、数据模型层（models.py）

### 1.1 核心类型

| 模型 | 作用 |
|---|---|
| `ExecutionRecord` | 稳定输入契约，`schema_version="1.0"`，包含 identity / agent / execution / usage / steps / evaluation / **quality** / artifacts / metadata |
| `QualitySummary` | 派生结构容器，仅持 `process_evaluation`，独立于 Harbor Outcome 评测写入 |
| `ProcessGrade` | 单个 grader 的可解释结果：`score (0–1 或 None)`、`status`、`severity`、`category`、`message`、`evidence[]`、`affected_steps[]`、`metadata{}` |
| `ProcessEvaluationResult` | 聚合结果：`evaluated_at`、`evaluator_version`、`overall_score`、`overall status`、`summary`、`grades[]`、`metadata`（含实际生效权重与配置快照） |

### 1.2 关键类型别名

- `ProcessStatus = "pass" | "warning" | "fail" | "insufficient_data"`
- `ProcessSeverity = "info" | "low" | "medium" | "high" | "critical"`
- `RunKind = "conversation" | "task" | "evaluation"`

### 1.3 步骤模型

`ExecutionStep` 使用 `success: bool | None` 三态——`None` 表示"未知/未完成"，这是所有 grader 处理数据缺失的基础。`type` 限定为 `agent / model / tool / final / span` 五种。

**派生结构原则**：`quality.process_evaluation` 完全独立可重建，覆盖写入不触碰原始 steps。

---

## 二、配置层（ProcessEvaluatorConfig）

Pydantic 可版本化配置，全部阈值可被 record metadata 覆盖：

```
argument_similarity_threshold = 0.96      # 参数相似度判定阈值
repeated_call_min_occurrences = 2         # 重复调用告警下限
repeated_failure_min_occurrences = 3       # 未调整重复失败判定阈值
loop_min_repeats = 3
loop_max_length = 4                        # 循环周期最大长度
failed_tool_ratio_threshold = 0.5
failed_tool_ratio_min_calls = 4
baseline_multiplier = 2.0                  # Baseline 超标倍数
requires_verification: bool | None = None  # None = 自动启发式
```

**三类模式识别表**（均支持 `casefold` + `-`→`_` 归一化后的 token 匹配）：

- `mutation_tool_patterns`：write/edit/patch/delete/remove/move/copy/create/apply
- `verification_tool_patterns`：test/pytest/unittest/lint/ruff/mypy/build/check/verify
- `environment_query_tool_patterns`：read/list/search/find/status/inspect

**命令正则**：识别 `pytest/ruff/mypy/tox/nox`、`npm test`、`cargo test`、`git diff/status` 等验证命令；`rm/mv/cp/mkdir/touch/install`、`sed -i`、`git apply/commit/merge/rebase` 等变更命令。

### Metadata 覆盖机制（`_config_for_record`）

读取顺序：`metadata["process_evaluation_config"]` → `metadata["process_evaluation"]`（兼容旧键名）。合并策略：

- 标量字段直接替换
- **字典字段（`grader_weights` / `efficiency_thresholds` / `baseline_metrics`）执行 shallow merge**，而非整体替换——用户只需覆盖想改的键
- 白名单过滤：只接受 `ProcessEvaluatorConfig.model_fields` 中已定义的键，防止拼写错误静默生效

---

## 三、六个 Grader 逐一分析

### 3.1 RepeatedToolCallGrader（冗余检测）

**核心算法** `_repeated_call_occurrences`（单次线性扫描，按 tool_name 维护 `previous_by_tool` 与 `run_length_by_tool`）：

判定"重复且无意义"需同时满足 5 个条件：

1. 相同 tool_name
2. 参数 canonical JSON 相等，或 `SequenceMatcher.ratio() ≥ 0.96`
3. **两次调用均 `success is True`**（失败的归 repeated_failure 管）
4. **两次均有 output 且 canonical 化后相等**（结果相同才告警，排除"读文件两次但内容变了"的合理重读）
5. **两次之间无成功的 mutation 动作**（中间改了状态，重查就是合理的）

对 `_canonical` 使用 `sort_keys=True` + 紧凑分隔符，保证 dict 键序不影响比较；不可 JSON 化的值回退到 `repr()`。

**评分**：`score = 1 - min(重复组数/总动作数, 1)`，status=warning，severity=medium。每个重复对都记录 `argument_similarity` 具体数值和序列号区间，evidence 可直接定位。

### 3.2 RepeatedFailureGrader（无策略调整的重复失败）

**流式 run 检测**：顺序遍历失败动作（`success is False`），用 `_same_action` 判断是否"未调整"——工具名或参数变化即重置 run（换工具/改参数都算策略调整）。连续达到 3 次未调整失败才成 run，超过继续追加。

**严格判定**：一旦成 run 直接 `score=0.0, status=fail, severity=high`。metadata 记录每个 run 的 `attempt_count` 和具体 step_id/sequence 列表。

### 3.3 FailureRecoveryGrader（失败恢复质量）

**候选失败集**：`tool`/`model` 类型的失败步骤；若整体执行失败但无步骤级失败，取最后一个失败的 `agent`/`final` 步骤兜底。

**恢复评估** `_assess_recovery`：

- tool 失败：扫描后续动作，识别 4 类策略调整信号——`changed_tool`、`changed_arguments`、`changed_execution_path`（比较 path/file/workdir/cwd/directory 字段）、`queried_environment`；第一个成功的后续动作即 recovery
- model 失败：后续首个成功的 model 调用即恢复，标记 `retried_model_call`
- **最终兜底**：若无显式恢复但整体 `execution.success is True`，取 final 步骤，标记 `final_only=True`（不虚报策略调整）

**评分公式**：`score = 0.75 × recovered_ratio + 0.25 × adjustment_ratio`——恢复是主体，策略调整是加分项。

**状态推导**（三档）：

| 条件 | status / severity |
|---|---|
| 有失败未恢复 | fail / high |
| 全恢复但部分无调整 | warning / low |
| 全部调整后恢复 | pass / info |

### 3.4 FinalVerificationGrader（最终验证）

**配置优先级**：`requires_verification=False` 直接 pass（显式关闭）；`True` 强制要求；`None`（默认）走启发式。

**启发式逻辑**：

1. 找出所有 mutation 候选，区分成功/成功未知两组
2. `required = 强制开启 or 存在成功的 mutation`——没有实际改状态就不要求验证
3. **数据不足优先**：mutation 步骤的 success 全是 `None` → `insufficient_data`，不臆断
4. 取最后一个成功 mutation 的 sequence，向后找验证动作：
   - 有成功验证 → `score=1.0, pass`，记录验证步骤 ID
   - 有验证但失败 → `fail / high`
   - 完全没验证 → `fail / medium`（区别于"验证失败"的严重度）

### 3.5 LoopDetectionGrader（周期循环检测）

**算法** `_find_loop`：对每个起点、每个周期长度 1–4，检查签名字符串 `(tool_name, canonical_args)` 是否连续重复 ≥3 次。这是 O(n²·L) 的暴力匹配，但对典型记录长度（几十到几百步）完全可接受。

**择优**：多个候选循环时按 `(覆盖跨度, 重复次数, -周期长度, -起始位置)` 取最大——优先报最长、最明显的循环。只认**连续**周期，避免 A…A…A 中间夹其他动作的误报。

**零容忍**：确认循环即 `score=0, fail, high`，metadata 记录 pattern（周期内工具名序列）、起止 sequence。

### 3.6 ExecutionEfficiencyGrader（执行效率）

**指标收集** `_efficiency_metrics`：step_count / model_call_count / tool_call_count / failed_tool_call_count / repeated_call_count / token_count / cost / latency_ms。

Token 计算有优雅的三级回退：`total_tokens` → `input_tokens_including_cache + output` → `input + cache_read + cache_write + output`，兼容 Harbor 的合并缓存字段。

**触发条件**（全部需显式配置，避免武断绝对值）：

- 内生信号：重复调用数 > 0；失败率 ≥ 0.5 且调用 ≥ 4 次
- `efficiency_thresholds`：绝对阈值（仅用户配置才生效）
- `baseline_metrics`：超过 `baseline × 2.0` 才告警

**无 baseline 时的克制**：`_pass_grade` 的 evidence 明确写 "No default absolute step/token/cost/latency limit was applied without a configured threshold or baseline"——设计上拒绝拍脑袋的"步数超过 N 就是低效"。

**评分**：`penalty = min(0.75, max(repeat_ratio, failed_ratio) + 0.1 × (信号数-1))`，`score = 1 - penalty`，warning 级别（不 fail）。

---

## 四、聚合层（ProcessEvaluator）

### 4.1 执行流程

```
evaluate(record)
  → _config_for_record：base config + metadata 覆盖合并
  → steps 按 sequence 排序
  → 构建 EvaluationContext（record / config / steps / 仅 type=="tool" 的 actions）
  → 顺序执行 6 个 grader
  → 加权聚合 + 状态推导
  → 原子写回 record.quality.process_evaluation
```

### 4.2 加权聚合

- 权重取 `max(config.grader_weights.get(name, 1.0), 0.0)`——**权重为 0 等效于禁用该 grader**（不参与分母）
- 只对 `score is not None` 且权重 > 0 的 grade 加权平均；`insufficient_data` 不拖累总分
- 所有默认权重为 1.0，总分 = 简单平均

### 4.3 状态推导

优先级：**fail > warning > pass > insufficient_data**——任一 grader fail 即整体 fail。summary 一行报告"N 个 process 问题；M 个 grader 数据不足"。

### 4.4 可追溯性

result.metadata 快照了三样东西：

- 实际生效的 `weights`（而非配置里的原始意图）
- 完整 `config`（`model_dump(mode="json")`，评分时用的真实阈值）
- `outcome_success` 与 `harbor_passed`（过程分与结果分的对照锚点）

---

## 五、工程细节与设计取舍

### 5.1 版本化

`EVALUATOR_VERSION = "1.0.0"` 与 `GRADER_VERSION = "1.0.0"` 分离——单 grader 升级不必升 evaluator。未来 grader 可各自携带版本号。

### 5.2 扩展接缝

`ProcessGrader` 是 `Protocol`（结构化类型），暴露 `grader_name / grader_version / category / grade()` 四个成员。`ProcessEvaluator(graders=...)` 可注入自定义 grader 序列，为后续 attribution 类 grader 留了口子。

### 5.3 确定性保证

- 全部比较基于 canonical JSON + `SequenceMatcher`（`autojunk=False` 避免长字符串启发式截断）
- `_matches_name` 做大小写和 `-`/`_` 归一化，token 级匹配
- 不依赖时间、随机数、网络或 LLM 判断——同一 record 多次评测结果完全一致（除 `evaluated_at` 时间戳）

### 5.4 已知局限

| 局限 | 说明 |
|---|---|
| 字符相似度非语义 | `"path=/a/file.txt"` 与 `"path=/b/file.txt"` 相似度极高，但路径不同可能正是策略调整——由 `changed_execution_path` 信号部分缓解 |
| 循环检测仅连续周期 | 非连续的 A…B…A…B 不识别，换取低误报 |
| 工具语义靠模式表 | 自定义工具的 mutation/verification 语义需通过 metadata 配置 patterns |
| final_only 恢复不加分 | 靠最终成功兜底的恢复不计策略调整，可能压低 adjustment_ratio |

### 5.5 与 Outcome 评测的关系

完全解耦：`overall_score` 不读取 `evaluation.passed`；两者只在 metadata 中并列展示供人工对照。一个过程分高但结果失败的 trial（如验证步骤被误判）与结果成功但过程混乱的 trial 都能被独立暴露。

---

## 六、一句话总结

这套 Process Evaluation 是一个**纯确定性、可版本化、证据驱动**的离线评分框架：6 个独立 grader 各自产出带 step 级定位的结构化判定，通过可配置权重聚合为一个可解释的总分，且在数据不足时明确降级（`insufficient_data`）而非臆断——所有阈值可按任务粒度覆盖，无 baseline 时拒绝使用武断的绝对效率标准。
