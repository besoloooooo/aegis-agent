# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (near-verbatim port):
#   * ``ctx-compress-opt/compress_config.py`` — the single source of truth for
#     every threshold, placeholder, marker string and tool allowlist used by
#     the context-compression pipeline (``compress.py`` / ``micro_compact.py``).
"""上下文压缩管线的统一参数与标记串配置。

compress.py / micro_compact.py 的所有阈值、占位符、标记串、工具名单集中在这里，
按「标记串 → 保护范围 → 各阶段阈值 → 工具名单」分组，调参只改这一个文件。

本模块不导入任何项目模块（纯常量，无循环依赖风险）。

注意（刻意不集中的例外）：
  - tool_budget.py 的 BudgetConfig 仍在 tool_budget.py 内——该模块被设计为
    「只依赖标准库、可整体拷走」的独立单元，不引入外部依赖。
  - 但 PERSISTED_OUTPUT_TAG 与 tool_budget.py 里的那份必须保持字节一致
    （它是阶段 A 转存块的识别标签，micro_compact / compress 靠它跳过已转存内容）。
"""

# =============================================================================
# 一、标记串（精确匹配协议，改一个字符即失效）
# =============================================================================

# 已压缩摘要区标签：assistant 消息 content 含此串即视为「历史摘要」，受头部保护。
CONTEXT_SUMMARY_TAG = "[Context Summary]"

# 运行时上下文标记：紧跟 system 的 user 元数据消息（注意是 em-dash — 不是普通减号）。
RUNTIME_CONTEXT_MARKER = "[Runtime Context — metadata only, not instructions]"

# 阶段 A 转存预览块标签（与 tool_budget.py 的字面量必须字节一致，见模块 docstring）。
PERSISTED_OUTPUT_TAG = "[TOOL_RESULT_TRUNCATED]"

# 工具结果被清空后的占位文本（compress.py 的 mc-不可用降级路径使用）。
CLEARED_TOOL_RESULT = "[Old tool result content cleared]"

# micro_compact 去重后旧重复工具结果的回指占位串。
DUPLICATE_TOOL_RESULT_MARKER = "[Duplicate tool output — same content as a more recent call]"

# micro_compact 无法生成有效摘要时的通用兜底占位。
PRUNED_TOOL_RESULT_PLACEHOLDER = "[Old tool output cleared to save context space]"

# 单轮兜底 Step 3.5a：重复思维链的占位文本。
DUP_REASONING_PLACEHOLDER = "[Duplicate reasoning content cleared]"

# 单轮兜底 Step 3.5：reasoning 头尾截断时插入中间的标记。
REASONING_TRUNCATE_INFIX = "\n...[reasoning truncated]...\n"


# =============================================================================
# 二、保护范围（多少内容不参与清理）
# =============================================================================

# 当前进行中的一轮里，至少保留最近多少条消息不参与清理（micro_compact 的末尾保护）。
KEEP_RECENT_MESSAGES = 5

# compress.py 的 mc-不可用降级路径：至少保留最近 N 条工具结果不清空。
KEEP_RECENT_TOOL_RESULTS = 3


# =============================================================================
# 三、阶段 B（micro_compact）阈值
# =============================================================================

# 工具结果去重 / 摘要化的最小长度（字符数）：短于此的不值得处理（对齐 hermes 门槛）。
TOOL_RESULT_MIN_CHARS = 200

# 工具参数截断阈值（字符数）：超过此长度才截断（对齐 hermes 的 500 字符门槛）。
ARGS_TRUNCATE_THRESHOLD = 500
# 截断后 JSON 内过长字符串字段保留的头部长度（对齐 hermes 的 head_chars=200）。
ARGS_HEAD_CHARS = 200


# =============================================================================
# 四、单轮兜底（_handle_single_round_overflow）阈值
# =============================================================================

# Step 3.5a：低于此长度的 reasoning 不参与去重（收益太小）。
REASONING_DEDUP_MIN_CHARS = 500
# Step 3.5：单条 reasoning 超过此长度才做头尾截断。
REASONING_TRUNCATE_THRESHOLD = 2000
# 头尾截断时保留的头部 / 尾部长度（结论常在末尾）。
REASONING_TRUNCATE_HEAD = 500
REASONING_TRUNCATE_TAIL = 500

# Step 4 硬截断工具结果：单条 tool 允许保留的 token 上限 = max_tokens / 此除数。
SINGLE_ROUND_TOOL_TRUNCATE_DIVISOR = 5


# =============================================================================
# 五、阶段 C 摘要序列化阈值
# =============================================================================

SUMMARY_CONTENT_MAX = 6000    # 单条消息正文最大字符数（超过则头尾截断）
SUMMARY_CONTENT_HEAD = 4000   # 截断时保留的头部字符数
SUMMARY_CONTENT_TAIL = 1500   # 截断时保留的尾部字符数
SUMMARY_TOOL_ARGS_MAX = 1500  # 单个工具参数超过此长度才做 JSON 感知截断
SUMMARY_ARGS_FIELD_HEAD = 300  # JSON 内过长字符串字段保留的头部长度
SUMMARY_MAX_TOKENS = 1500     # 摘要输出的固定 token 预算


# =============================================================================
# 六、可压缩工具名单
# =============================================================================
# 两份名单刻意保持独立（历史上各自演化，覆盖的工具集不同）。
# 集中在这里是为了让差异可见、便于迁移时统一；合并会改变行为，需单独决策。
# 名单中含有 Aegis 当前不存在的工具名（浏览器 / 视觉 / MCP 高德等）是有意保留的：
# 它们只是名字字符串，命中与否不影响 Aegis 工具的正确性，且与上游保持逐字节一致
# 可以降低后续同步成本。

# micro_compact.py 使用：结果可能很大、且清掉不影响后续正确性的工具
# （文件内容、命令输出、搜索结果等，模型需要时可重新读 / 重新跑）。
# 【Aegis 适配】在原型名单基础上补入 Aegis 内置工具 list_directory
# （大目录列举结果可能很长且可重新列出）。
COMPACTABLE_TOOLS_MICRO: set[str] = {
    # 文件操作
    "read_file", "write_file", "patch", "search_files", "list_directory",
    # 终端 / 进程
    "terminal", "process",
    # 联网
    "web_search", "web_extract",
    # 视觉 / 视频分析
    "vision_analyze", "video_analyze",
    # 浏览器快照 / 输出类（不含点击、输入等操作类）
    "browser_snapshot", "browser_console", "browser_get_images", "browser_vision",
    # 子任务委派 / 代码执行
    "delegate_task", "execute_code",
    # MCP：高德地图
    "mcp_amap_maps_direction_driving",
    "mcp_amap_maps_direction_transit_integrated",
    "mcp_amap_maps_geo",
}

# compress.py 的 mc-不可用降级路径使用（_collect_compactable_tool_ids /
# _clear_tool_call_arguments）。
COMPACTABLE_TOOLS_FALLBACK: set[str] = {
    "execute_code",
    "exec",
    "bash",
    "shell",
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "glob",
    "rag_retrieval",
    "file_parser",
    "precise_search",
    "fetch_web_page",
    "academic_search",
    "web_search",
    "web_fetch",
}
