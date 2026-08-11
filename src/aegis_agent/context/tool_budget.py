# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (near-verbatim port):
#   * ``ctx-compress-opt/tool_budget.py`` — two-level tool-result budget with
#     persist-to-disk + preview replacement and cross-turn stable decisions.
"""工具结果预算剪裁（发送前 API 副本级，跨轮缓存稳定）。

复现 Claude Code 的「转存 + 预览」策略：工具结果太大时不硬截断，而是把完整
内容写盘，发给模型的只留一段预览 + 文件路径。两级预算：

  第一级 —— 单条工具结果级（maybe_persist_large_tool_result）
     只看"这一个工具结果有多大"。超过阈值就整块转存 + 换成预览。

  第二级 —— 单轮聚合级（enforce_tool_result_budget）
     看"同一轮里所有并行工具结果加起来有多大"。合计超预算就把最大的几个
     转存替换，直到降回预算内。

跨轮状态（ContentReplacementState）：记住"哪些结果做过什么决定"，保证后续每轮
对同一批历史做出完全相同的替换 —— 前缀逐字节不变，提示缓存才不会失效。

本模块只依赖标准库；消息格式为 OpenAI Chat Completions 的 dict 形状：
  assistant: {"role":"assistant","tool_calls":[{"id":..,"function":{"name":..}}]}
  tool:      {"role":"tool","tool_call_id":..,"content":..,"name":..}

【Aegis 适配说明】本模块工作在压缩管线产出的 dict 视图上（见
``aegis_agent.context.compress`` 的 Message↔dict 边界转换），只改发给模型的
那份派生上下文，原始消息历史保持完整。content 若不是 str（多模态
content-part list / dict），一律视为"不可剪裁"原样放行，绝不参与大小统计或
转存 —— 保护 vision 工具结果结构不被破坏。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

# =============================================================================
# 一、配置
# =============================================================================

# 默认阈值。均可通过 BudgetConfig 覆盖。
DEFAULT_RESULT_SIZE_CHARS = 20_000     # 第一级：单条结果转存阈值（~5K token）
DEFAULT_TURN_BUDGET_CHARS = 80_000     # 第二级：单批并行结果总量上限（~20K token）
DEFAULT_PREVIEW_SIZE_CHARS = 1_500     # 转存后保留给模型看的预览长度
DEFAULT_READ_FILE_MAX_CHARS = 20_000   # 第三级兜底：read_file 返回内容硬上限（~5K token）

# 永不转存的工具（threshold = inf 表示"永不转存"）。
#
# 【历史】这里曾把 read_file 钉成 inf，原因是白名单托管环境下工具本身有最大字符数
# 上限，read_file 结果不会太大；而防死循环靠的是"read_file 输出再被 read 回来会无限
# 转存"这一顾虑。但本地开发的 agent 没有这个字符上限，read_file 完全可能吐出巨大结果，
# 却因白名单被放行、白白撑爆上下文。
#
# 现在改为：read_file 不再豁免，照常参与两级预算。防死循环改由 is_readback_of_persisted()
# 精准识别——只有"读回我们自己转存的缓存文件"这一种真正会成环的场景才跳过，其余超大
# read_file 结果一律正常转存。
PINNED_THRESHOLDS: dict[str, float] = {}

PERSISTED_OUTPUT_TAG = "[TOOL_RESULT_TRUNCATED]"


@dataclass(frozen=True)
class BudgetConfig:
    """两级预算的可调常量。frozen 保证跨轮不可变（配置不应在运行中变）。"""

    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    # read_file 返回内容的硬截断上限（第三级兜底）。用于处理那些被判定为"读回缓存"
    # 而故意不转存、又确实很大的 read_file 结果——转存会成环，所以只能就地硬截断。
    read_file_max_chars: int = DEFAULT_READ_FILE_MAX_CHARS
    # 按工具名覆盖单条阈值（优先级低于 PINNED_THRESHOLDS）。
    tool_overrides: dict[str, float] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> float:
        """解析某工具的单条转存阈值。优先级：pinned > overrides > default。"""
        if tool_name in PINNED_THRESHOLDS:
            return PINNED_THRESHOLDS[tool_name]
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        return self.default_result_size


DEFAULT_BUDGET = BudgetConfig()


# =============================================================================
# 二、跨轮状态
# =============================================================================


@dataclass
class ContentReplacementState:
    """第二级预算的跨轮状态（保住提示缓存的关键）。

      seen_ids     —— 已经过一次预算检查的结果 id（无论是否被替换）。
                      一旦见过，本会话内命运冻结、不再改变。
      replacements —— seen_ids 的子集：真的被转存替换过的结果，映射到当时发给
                      模型的"确切预览字符串"。后续重放直接查表，零 I/O、字节一致。
    """

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


def create_state() -> ContentReplacementState:
    return ContentReplacementState()


# =============================================================================
# 三、通用小工具
# =============================================================================


def is_trimmable(content) -> bool:
    """content 是否可参与剪裁。只有纯字符串可剪；多模态 list / dict / None 放行。"""
    return isinstance(content, str)


def content_size(content) -> int:
    """工具结果大小。OpenAI tool 消息 content 是纯字符串，非字符串视为 0。"""
    return len(content) if isinstance(content, str) else 0


def is_empty(content) -> bool:
    """实质为空：None / 空串 / 纯空白。空结果要注入占位，防模型提前停止。

    仅对字符串判断空白；非字符串（多模态）一律视为非空。
    """
    if not isinstance(content, str):
        return content is None
    if not content:
        return True
    return content.strip() == ""


def is_already_compacted(content) -> bool:
    """是否已是替换版本。所有替换版本都以 PERSISTED_OUTPUT_TAG 开头（精确前缀
    判断，避免标签恰好出现在正文里误判）。已压缩的不再作为候选。"""
    return isinstance(content, str) and content.startswith(PERSISTED_OUTPUT_TAG)


def is_readback_of_persisted(tool_name: str, arguments, storage_dir: str) -> bool:
    """判断一次 read_file 调用是否在"读回我们自己转存的缓存文件"。

    这是去掉 read_file 白名单后唯一需要防的死循环：某条 read_file 结果太大被转存成
    "预览 + 缓存路径"，模型照提示去 read 那个缓存文件 → 又拿回完整原文 → 又被转存……
    命中这种情况就把该结果钉成"永不转存"，让完整原文原样发回，打破环。

    两条判据（任一命中即视为读回）：
      1) arguments 里字面出现 PERSISTED_OUTPUT_TAG（模型把预览文本粘进了参数）；
      2) path 参数指向 storage_dir 内部（真正的缓存回读，主判据）。

    arguments 可能是 dict 或 JSON 字符串（OpenAI function.arguments 常为字符串）。
    任何解析异常都保守返回 False（不误钉，宁可正常转存）。
    """
    if tool_name != "read_file":
        return False

    # 统一成字符串做字面标签检测；同时尽量解析出 dict 取 path。
    raw = arguments
    args_dict = None
    if isinstance(arguments, str):
        if PERSISTED_OUTPUT_TAG in arguments:
            return True
        try:
            args_dict = json.loads(arguments)
        except Exception:  # noqa: BLE001 — 解析失败保守按非读回处理
            args_dict = None
    elif isinstance(arguments, dict):
        args_dict = arguments
        try:
            raw = json.dumps(arguments, ensure_ascii=False)
        except Exception:  # noqa: BLE001 — 序列化失败只做字面检测
            raw = ""
        if PERSISTED_OUTPUT_TAG in raw:
            return True

    if not isinstance(args_dict, dict):
        return False

    path = args_dict.get("path") or args_dict.get("file_path") or args_dict.get("filename")
    if not isinstance(path, str) or not path:
        return False

    try:
        storage_abs = os.path.abspath(storage_dir)
        path_abs = os.path.abspath(path)
    except Exception:  # noqa: BLE001 — 路径异常时保守返回 False（不误钉）
        return False
    # path 落在 storage_dir 内部即判定为缓存回读。
    return os.path.commonpath([storage_abs, path_abs]) == storage_abs


def _format_size(num_chars: int) -> str:
    """字符数 → 人类可读大小，仅用于展示。"""
    if num_chars < 1024:
        return f"{num_chars}B"
    if num_chars < 1024 * 1024:
        return f"{num_chars / 1024:.1f}KB"
    return f"{num_chars / (1024 * 1024):.1f}MB"


def generate_preview(content: str, max_chars: int) -> tuple[str, bool]:
    """生成预览：取前 max_chars 字符，尽量在换行边界切断。

    返回 (预览文本, 是否还有更多)。
    """
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    # 换行符要足够靠后才用它切，否则浪费太多预览额度
    cut = last_nl if last_nl > max_chars * 0.5 else max_chars
    return content[:cut], True


def hard_truncate_readback(content: str, max_chars: int) -> str:
    """就地硬截断 read_file 的缓存回读内容（第三级兜底，不写盘、不给回读路径）。

    只用于 is_readback_of_persisted() 命中的结果：这类内容转存会成环，所以既不能转存、
    也不该原样放行撑爆上下文——只能保留头部一段并明确告知已截断。
    """
    if len(content) <= max_chars:
        return content
    head, _ = generate_preview(content, max_chars)
    omitted = len(content) - len(head)
    return (
        f"{head}\n\n"
        f"{PERSISTED_OUTPUT_TAG} 已省略约 {_format_size(omitted)}"
        f"（完整内容共 {_format_size(len(content))}，此处为缓存回读，只保留头部）。\n"
        f"如需后续内容，请用 read_file 的 offset/limit 分段读取，不要重复整份读取。"
    )


# =============================================================================
# 四、转存 + 构造替换消息（真实写文件）
# =============================================================================


def _storage_path(storage_dir: str, tool_call_id: str) -> str:
    """转存文件路径：<storage_dir>/<tool_call_id>.txt"""
    return os.path.join(storage_dir, f"{tool_call_id}.txt")


def persist_to_disk(storage_dir: str, content: str, tool_call_id: str) -> str | None:
    """把完整内容写盘。已存在不覆盖（内容确定，保证字节一致）。返回文件路径。

    失败返回 None（调用方应原样保留原文，宁可超预算也不丢内容）。
    """
    if not isinstance(content, str):
        return None
    os.makedirs(storage_dir, exist_ok=True)
    path = _storage_path(storage_dir, tool_call_id)
    # 'x' 模式：文件已存在则报错 → 等价于"已存在不覆盖"。失败说明已写过，跳过。
    if not os.path.exists(path):
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(content)
        except FileExistsError:
            pass
        except OSError:
            return None
    return path


def build_persisted_message(
    preview: str, has_more: bool, original_size: int, file_path: str, preview_size: int
) -> str:
    """Build the replacement text shown to the model. Starts with PERSISTED_OUTPUT_TAG."""
    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"preview: {preview}"
    msg += "...\n" if has_more else "\n"
    msg += f'\n完整结果：read_file(path="{file_path}", full_lines=true)\n\n'
    msg += "若 preview 足够，请直接继续；若相关但信息不足，优先读取完整结果，不要重新搜索或重复调用原工具。"
    return msg


# =============================================================================
# 五、第一级预算：单条工具结果级
# =============================================================================


def maybe_persist_large_tool_result(
    storage_dir: str,
    msg: dict,
    tool_name: str,
    config: BudgetConfig,
    threshold: float | None = None,
) -> dict:
    """第一级：只看这一条 role="tool" 消息本身有多大。

    处理顺序：
      1) 非字符串 content（多模态）→ 原样返回，不剪裁。
      2) 已是替换版本（带 PERSISTED_OUTPUT_TAG）→ 原样返回，不重复转存。
      3) 空内容  → 注入 "(<tool> completed with no output)" 占位，直接返回。
      4) 未超阈值 → 原样返回。
      5) 超阈值   → 整块转存，content 换成"文件路径 + 预览"。转存失败则原样返回。

    返回新 dict，不就地改原消息。
    """
    content = msg.get("content")

    # 1) 多模态守卫：非字符串一律放行
    if not isinstance(content, str):
        return msg

    # 2) 已剪裁守卫：内容若已是替换版本（以 PERSISTED_OUTPUT_TAG 开头），说明之前
    #    某轮已经转存过。此时预览很小、原文已在磁盘，直接放行，绝不二次转存。
    if is_already_compacted(content):
        return msg

    # 3) 空守卫
    if is_empty(content):
        return {**msg, "content": f"({tool_name} completed with no output)"}

    # 4) 阈值解析
    effective = threshold if threshold is not None else config.resolve_threshold(tool_name)
    if effective == math.inf:
        return msg  # 永不转存（如 read_file）
    size = content_size(content)
    if size <= effective:
        return msg

    # 5) 超阈值：转存并替换
    tool_call_id = msg.get("tool_call_id", "")
    path = persist_to_disk(storage_dir, content, tool_call_id)
    if path is None:
        return msg  # 转存失败，原样返回，不丢内容
    preview, has_more = generate_preview(content, config.preview_size)
    return {
        **msg,
        "content": build_persisted_message(preview, has_more, size, path, config.preview_size),
    }


# =============================================================================
# 六、第二级预算：候选收集 + 分区 + 选择
# =============================================================================


@dataclass
class _Candidate:
    """一个参与第二级评估的 tool 结果。"""

    tool_call_id: str
    content: str
    size: int


def _build_tool_name_map(messages: list[dict]) -> dict[str, str]:
    """从 assistant.tool_calls 建 tool_call_id -> 工具名 映射。"""
    name_map: dict[str, str] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            cid = call.get("id")
            name = (call.get("function") or {}).get("name", "")
            if cid:
                name_map[cid] = name
    return name_map


def _build_tool_args_map(messages: list[dict]) -> dict[str, object]:
    """从 assistant.tool_calls 建 tool_call_id -> arguments 映射。

    arguments 原样保留（可能是 dict 或 JSON 字符串），交给 is_readback_of_persisted 解析。
    """
    args_map: dict[str, object] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            cid = call.get("id")
            args = (call.get("function") or {}).get("arguments")
            if cid:
                args_map[cid] = args
    return args_map


def collect_candidate_groups(messages: list[dict]) -> list[list[_Candidate]]:
    """把相邻的一串 tool 消息视作"一批并行结果"（等价于 Anthropic 的一条 user 消息）。

    分组规则：tool 消息加入当前组；遇到非 tool 消息则切断当前组。
    跳过空结果、非字符串结果和已压缩结果。只返回非空组。
    """
    groups: list[list[_Candidate]] = []
    current: list[_Candidate] = []

    def flush():
        nonlocal current
        if current:
            groups.append(current)
        current = []

    for m in messages:
        if m.get("role") == "tool":
            content = m.get("content")
            if not is_trimmable(content) or not content or is_already_compacted(content):
                continue
            current.append(
                _Candidate(
                    tool_call_id=m.get("tool_call_id", ""),
                    content=content,
                    size=content_size(content),
                )
            )
        else:
            flush()
    flush()
    return groups


def _partition(candidates: list[_Candidate], state: ContentReplacementState):
    """按历史决定分三类：must_reapply / frozen / fresh。"""
    must_reapply, frozen, fresh = [], [], []
    for c in candidates:
        repl = state.replacements.get(c.tool_call_id)
        if repl is not None:
            must_reapply.append((c, repl))
        elif c.tool_call_id in state.seen_ids:
            frozen.append(c)
        else:
            fresh.append(c)
    return must_reapply, frozen, fresh


def _select_fresh_to_replace(
    fresh: list[_Candidate], frozen_size: int, limit: int
) -> list[_Candidate]:
    """从全新候选里从大到小挑出要替换的，直到总量降回预算内。frozen 是固定地板。"""
    selected: list[_Candidate] = []
    remaining = frozen_size + sum(c.size for c in fresh)
    for c in sorted(fresh, key=lambda x: x.size, reverse=True):
        if remaining <= limit:
            break
        selected.append(c)
        remaining -= c.size
    return selected


# =============================================================================
# 七、第二级预算：核心执行
# =============================================================================


@dataclass
class EnforceStats:
    """第二级执行统计，供报告使用。"""

    reapplied: int = 0          # 重放条数（纯查表）
    newly_persisted: int = 0    # 本轮新转存条数
    shed_chars: int = 0         # 本轮新转存去掉的字符数
    over_budget_groups: int = 0 # 超预算的组数


def enforce_tool_result_budget(
    storage_dir: str,
    messages: list[dict],
    state: ContentReplacementState,
    config: BudgetConfig,
    skip_tool_names: set[str] | None = None,
    skip_ids: set[str] | None = None,
) -> tuple[list[dict], EnforceStats]:
    """第二级主体。state 会被【就地修改】（跨轮累积决定）。

    skip_ids —— 按 tool_call_id 粒度跳过转存的结果（如"读回缓存"的 read_file，
    转存会成环）。与 skip_tool_names（按工具名跳过）取并集。

    返回 (处理后的消息列表, 统计)。
    """
    if skip_tool_names is None:
        skip_tool_names = set()
    if skip_ids is None:
        skip_ids = set()

    groups = collect_candidate_groups(messages)
    name_by_id = _build_tool_name_map(messages) if skip_tool_names else {}

    def should_skip(cid: str) -> bool:
        if cid in skip_ids:
            return True
        return bool(skip_tool_names) and name_by_id.get(cid, "") in skip_tool_names

    limit = config.turn_budget
    replacement_map: dict[str, str] = {}  # 本轮要施加的所有替换（重放 + 新转存）
    to_persist: list[_Candidate] = []
    stats = EnforceStats()

    for candidates in groups:
        must_reapply, frozen, fresh = _partition(candidates, state)

        # (a) 重放此前替换过的
        for c, repl in must_reapply:
            replacement_map[c.tool_call_id] = repl
        stats.reapplied += len(must_reapply)

        # (b) 没有全新候选 → 整组都是老消息，补记已见后跳过
        if not fresh:
            for c in candidates:
                state.seen_ids.add(c.tool_call_id)
            continue

        # (c) 剔除"永不转存"工具：标记已见（冻结），不参与大小统计
        skipped = [c for c in fresh if should_skip(c.tool_call_id)]
        for c in skipped:
            state.seen_ids.add(c.tool_call_id)
        eligible = [c for c in fresh if not should_skip(c.tool_call_id)]

        # (d) 算总量：冻结地板 + 可替换全新部分
        frozen_size = sum(c.size for c in frozen)
        fresh_size = sum(c.size for c in eligible)

        # (e) 超预算才挑东西替换
        selected = (
            _select_fresh_to_replace(eligible, frozen_size, limit)
            if frozen_size + fresh_size > limit
            else []
        )

        # (f) 未被选中的现在就标记已见（冻结）；选中的等转存成功后再连同 replacements 标记
        selected_ids = {c.tool_call_id for c in selected}
        for c in candidates:
            if c.tool_call_id not in selected_ids:
                state.seen_ids.add(c.tool_call_id)

        if not selected:
            continue
        stats.over_budget_groups += 1
        to_persist.extend(selected)

    if not replacement_map and not to_persist:
        return messages, stats

    # 执行转存
    for c in to_persist:
        path = persist_to_disk(storage_dir, c.content, c.tool_call_id)
        state.seen_ids.add(c.tool_call_id)
        if path is None:
            continue  # 转存失败：已见但未替换，原文照发
        preview, has_more = generate_preview(c.content, config.preview_size)
        repl = build_persisted_message(preview, has_more, c.size, path, config.preview_size)
        replacement_map[c.tool_call_id] = repl
        state.replacements[c.tool_call_id] = repl
        stats.newly_persisted += 1
        stats.shed_chars += c.size

    if not replacement_map:
        return messages, stats

    # 施加替换，产出新消息列表
    out: list[dict] = []
    for m in messages:
        cid = m.get("tool_call_id")
        if m.get("role") == "tool" and cid in replacement_map:
            out.append({**m, "content": replacement_map[cid]})
        else:
            out.append(m)
    return out, stats


# =============================================================================
# 八、便捷入口：两级一起跑
# =============================================================================


def apply_budget(
    storage_dir: str,
    messages: list[dict],
    state: ContentReplacementState | None = None,
    config: BudgetConfig | None = None,
    skip_tool_names: set[str] | None = None,
) -> tuple[list[dict], ContentReplacementState, EnforceStats]:
    """两级预算一起跑。返回 (处理后消息, state, 统计)。

    若不传 state 则新建（一次性场景）。先跑第一级（逐条），再跑第二级（聚合）。
    """
    config = config or BudgetConfig()
    state = state or create_state()

    name_by_id = _build_tool_name_map(messages)
    args_by_id = _build_tool_args_map(messages)

    # 永不转存的工具（阈值为 inf）：第一级靠 resolve_threshold 自动放行，
    # 第二级靠 skip_tool_names 跳过。从 PINNED + tool_overrides 里收集 inf 项。
    if skip_tool_names is None:
        skip_tool_names = {
            name for name, t in {**PINNED_THRESHOLDS, **config.tool_overrides}.items()
            if t == math.inf
        }

    # "读回缓存"的 read_file 结果：转存会成环，按 id 精准豁免转存（改用第三级硬截断）。
    readback_ids: set[str] = set()
    for m in messages:
        if m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id", "")
        name = name_by_id.get(cid, "") or m.get("name", "")
        if is_readback_of_persisted(name, args_by_id.get(cid), storage_dir):
            readback_ids.add(cid)

    # 第一级：逐条（读回缓存的结果跳过转存）
    after_l1: list[dict] = []
    for m in messages:
        if m.get("role") == "tool":
            cid = m.get("tool_call_id", "")
            name = name_by_id.get(cid, "") or m.get("name", "")
            if cid in readback_ids:
                after_l1.append(m)  # 不转存，交给第三级硬截断
            else:
                after_l1.append(maybe_persist_large_tool_result(storage_dir, m, name, config))
        else:
            after_l1.append(m)

    # 第二级：聚合（读回缓存的结果同样跳过转存）
    after_l2, stats = enforce_tool_result_budget(
        storage_dir, after_l1, state, config, skip_tool_names, skip_ids=readback_ids
    )

    # 第三级兜底：对 read_file 的返回内容做硬截断上限。
    # 主要目的是接住那些被判定为"读回缓存"、故意不转存、却依然巨大的 read_file 结果，
    # 防止它们原样撑爆上下文；其余 read_file 结果通常已在前两级转存，这里等同二次保险。
    limit = config.read_file_max_chars
    if limit and limit > 0:
        after_l3: list[dict] = []
        for m in after_l2:
            if m.get("role") == "tool":
                cid = m.get("tool_call_id", "")
                name = name_by_id.get(cid, "") or m.get("name", "")
                content = m.get("content")
                if (
                    name == "read_file"
                    and isinstance(content, str)
                    and PERSISTED_OUTPUT_TAG not in content  # 已转存/已硬截断 → 幂等跳过
                    and content_size(content) > limit
                ):
                    after_l3.append({**m, "content": hard_truncate_readback(content, limit)})
                    continue
            after_l3.append(m)
        after_l2 = after_l3

    return after_l2, state, stats
