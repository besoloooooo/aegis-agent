# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# Behavioural source (port with adaptation):
#   * ``ctx-compress-opt/compress.py`` — the three-phase context-compression
#     pipeline (oversized tool-result offload -> local micro-compaction ->
#     per-round LLM summary -> single-round overflow fallback).
#
# Aegis adaptations relative to the Hermes prototype:
#   * ``configs.config`` / ``utils.log_utils`` -> stdlib ``logging``;
#   * the async ``llm_provider.chat(...)`` summary call -> the synchronous
#     :class:`~aegis_agent.models.base.ModelProvider` Protocol
#     (``stream()`` + :func:`~aegis_agent.events.collect_response``);
#   * the hard-coded ``ROOT_PATH/tool-budget-cache`` offload directory ->
#     an injectable ``storage_dir`` (default ``~/.aegis/tool-result-cache``);
#   * added ``budget_state`` / ``summary_provider`` parameters so the caller
#     (AgentRuntime) can hold the tool-budget state across turns (byte-stable
#     prompt prefix -> cache hits) and route summaries to a deterministic
#     provider;
#   * flat same-dir imports -> absolute package imports;
#   * the optional ``agent.redact`` dependency -> regex-only redaction
#     (Aegis has no redact module; the regex fallback was kept);
#   * dropped dead code from the prototype: ``_handle_single_round_overflow_v1``
#     / ``_v2`` and ``_truncate_oversized_tools`` (never called), and the
#     ``__main__`` self-test block;
#   * added the ``Message`` <-> OpenAI-dict boundary converters and the public
#     :func:`compress_context` entry point.  The algorithm core still operates
#     on OpenAI-shaped dicts, byte-faithful to the prototype.
"""上下文压缩：会话剪裁与按轮摘要（三阶段管线）。

管线总览（`_compress_context`）：
  阶段 A：超大工具结果剪裁（tool_budget.py）——无条件执行。单条结果超阈值整块
          转存磁盘、content 换成预览 + 文件路径；同一批并行结果合计超预算再从大
          到小转存。完整内容不丢失，发给模型的只有预览。
  阶段 B：阈值触发的渐进式微压缩（micro_compact.py）——本地、无 LLM 调用。
          掐头去尾 → 去重 / 摘要化旧 tool result → JSON 感知截断 tool call 参数
          → 删历史 reasoning → 兜底扩大范围。每步后重估，达标即返回。
  阶段 C：按轮 LLM 摘要——仍超限时从最早的完整轮次开始逐轮调用模型生成结构化
          摘要（保留原始 user 问题 + "[Context Summary]" assistant 摘要），直到
          回落到阈值内；摘要全部用完仍超限则交给单轮兜底。
  单轮兜底（_handle_single_round_overflow）：只有一轮可压缩或摘要压完仍超限时，
          按「信息化摘要旧工具结果 → 清历史 reasoning → 缩减参数 → 当前轮
          reasoning 去重/截断/清空 → 硬截断工具结果 → 原子删除最早工具调用组」
          逐级处理。

核心不变式（与 Aegis 架构约定一致）：
  * 压缩只作用于**派生上下文**——输入消息列表（原始历史）绝不被修改；
  * 工具调用协议保持合法：删除时整组（assistant tool_calls + 对应 tool 结果）
    原子删除，绝不留下孤立的工具结果；
  * 摘要失败 / 不可用时不替换原轮次，宁可超预算也不丢内容。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注；运行时经函数内惰性导入（保留原型容错语义）
    from aegis_agent.context.tool_budget import ContentReplacementState

# 统一参数与标记串（阈值/占位符/工具名单集中在 compress_config.py）。
# 别名在导入后显式赋值（保持原本地名），避免 isort 将别名导入拆成多个块。
from aegis_agent.context.compress_config import (
    CLEARED_TOOL_RESULT,
    COMPACTABLE_TOOLS_FALLBACK,
    CONTEXT_SUMMARY_TAG,
    DUP_REASONING_PLACEHOLDER,
    KEEP_RECENT_TOOL_RESULTS,
    REASONING_DEDUP_MIN_CHARS,
    REASONING_TRUNCATE_HEAD,
    REASONING_TRUNCATE_INFIX,
    REASONING_TRUNCATE_TAIL,
    REASONING_TRUNCATE_THRESHOLD,
    RUNTIME_CONTEXT_MARKER,
    SINGLE_ROUND_TOOL_TRUNCATE_DIVISOR,
    SUMMARY_ARGS_FIELD_HEAD,
    SUMMARY_CONTENT_HEAD,
    SUMMARY_CONTENT_MAX,
    SUMMARY_CONTENT_TAIL,
    SUMMARY_MAX_TOKENS,
    SUMMARY_TOOL_ARGS_MAX,
)
from aegis_agent.events import collect_response
from aegis_agent.models.base import Message, ModelProvider, Role, ToolCall

COMPACTABLE_TOOLS = COMPACTABLE_TOOLS_FALLBACK            # 可压缩工具名单（降级路径用）
_DUP_REASONING_PLACEHOLDER = DUP_REASONING_PLACEHOLDER    # 重复思维链占位
_REASONING_DEDUP_MIN_CHARS = REASONING_DEDUP_MIN_CHARS
_REASONING_TRUNCATE_HEAD = REASONING_TRUNCATE_HEAD
_REASONING_TRUNCATE_INFIX = REASONING_TRUNCATE_INFIX
_REASONING_TRUNCATE_TAIL = REASONING_TRUNCATE_TAIL
_REASONING_TRUNCATE_THRESHOLD = REASONING_TRUNCATE_THRESHOLD
_TOOL_TRUNCATE_DIVISOR = SINGLE_ROUND_TOOL_TRUNCATE_DIVISOR
_SUMMARY_ARGS_FIELD_HEAD = SUMMARY_ARGS_FIELD_HEAD
_SUMMARY_CONTENT_HEAD = SUMMARY_CONTENT_HEAD
_SUMMARY_CONTENT_MAX = SUMMARY_CONTENT_MAX
_SUMMARY_CONTENT_TAIL = SUMMARY_CONTENT_TAIL
_SUMMARY_MAX_TOKENS = SUMMARY_MAX_TOKENS
_SUMMARY_TOOL_ARGS_MAX = SUMMARY_TOOL_ARGS_MAX

# 统一日志器（适配点：Hermes 原型使用项目自定义 get_logger + 文件输出，
# Aegis 走标准库 logging，日志归集由应用层配置）。
logger = logging.getLogger(__name__)


def _default_storage_dir() -> str:
    """阶段 A 超大工具结果的默认转存目录（适配点：替代 Hermes 的 ROOT_PATH 拼路径）。"""
    return os.path.join(os.path.expanduser("~"), ".aegis", "tool-result-cache")


def _estimate_tokens(messages) -> int:
    """使用 tiktoken 精确统计消息历史的 token 数量

    使用 cl100k_base 编码器（兼容 GPT-4 / Claude / M2 等主流模型）
    """
    try:
        # 惰性 import：未装 tiktoken 时在此抛 ImportError，落到下面的字符估算兜底。
        import tiktoken  # type: ignore[import-not-found]  # 可选依赖，缺失时走兜底估算
        # 获取 cl100k_base 编码器（GPT-4 及多数现代模型使用的编码）
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 — tiktoken 缺失/初始化失败均退化为字符估算
        # 兜底：若 tiktoken 缺失或初始化失败，退化到简单的字符估算
        return _estimate_tokens_fallback(messages)

    total_tokens = 0  # token 累加器

    # 逐条遍历消息，累计各字段占用的 token
    for msg in messages:
        # ── 统计 content 字段 ──────────────────────────────────
        if isinstance(msg.get("content"), str):
            # content 为纯字符串：直接编码后计数
            total_tokens += len(encoding.encode(msg["content"]))
        elif isinstance(msg.get("content"), list):
            # content 为多模态块列表（文本块 / 图片块等）
            for block in msg["content"]:
                if isinstance(block, dict):
                    if block.get("type") == "image_url":
                        # 图片内容按固定 token 估算，避免把图片 URL/base64 算爆
                        total_tokens += 2000
                    elif block.get("type") == "text":
                        # 文本块按实际文本编码计数
                        total_tokens += len(encoding.encode(block.get("text", "")))

        # ── 统计 reasoning_content（模型思维链）────────────────
        if "reasoning_content" in msg:
            total_tokens += len(encoding.encode(msg["reasoning_content"]))

        # ── 统计 tool_calls（工具调用结构）────────────────────
        if "tool_calls" in msg:
            # 直接把整个 tool_calls 结构转字符串编码计数（含函数名、参数等）
            total_tokens += len(encoding.encode(str(msg["tool_calls"])))

        # 每条消息的元数据开销（role/分隔符等），约 4 个 token
        total_tokens += 4
    return total_tokens


def _estimate_tokens_fallback(messages) -> int:
    """兜底的 token 估算方法（当 tiktoken 不可用时）"""
    total_chars = 0  # 字符累加器
    for msg in messages:
        # 累计 content 字段的字符数
        if isinstance(msg.get("content"), str):
            total_chars += len(msg["content"])
        elif isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict):
                    total_chars += len(str(block))

        # 累计思维链字符数
        if "reasoning_content" in msg:
            total_chars += len(msg["reasoning_content"])

        # 累计工具调用结构的字符数
        if "tool_calls" in msg:
            total_chars += len(str(msg["tool_calls"]))

    # 粗略换算：平均 2.5 个字符 ≈ 1 个 token
    return int(total_chars / 2.5)


def _generic_truncate(content: str, max_tokens: int) -> str:
    """
    通用截断策略。
    把过长的字符串截断到大约 max_tokens 对应的长度，并附上截断说明。
    """
    # 用 2.5 字符/token 的比例把 token 预算换算为字符预算
    max_chars = max_tokens * 2.5

    # 长度在预算内，无需截断，原样返回
    if len(content) <= max_chars:
        return content

    # 只保留预算的 80%，剩余 20% 留给截断提示，避免截断后仍然超预算
    keep_chars = int(max_chars * 0.8)

    # 拼接：保留的头部内容 + 截断说明（告知被删掉了多少字符）
    truncated = (
            content[:keep_chars] +
            f"\n\n... [TRUNCATED: {len(content) - keep_chars} characters] ..."
    )

    return truncated


def _trim_oversized_tools_via_budget(
    messages: list[dict],
    storage_dir: str,
    state: ContentReplacementState | None = None,
) -> list[dict]:
    """用 tool_budget 的「转存磁盘 + 预览」策略剪裁超大工具结果。

    复用包内 aegis_agent/context/tool_budget.py 的两级预算：
      - 第一级：单条工具结果超阈值 → 整块转存磁盘，content 换成预览 + 文件路径；
      - 第二级：同一批并行工具结果合计超预算 → 从大到小挑几条转存替换，直到降回预算内。
    完整内容写盘不丢失，发给模型的只保留一段预览，从而在不硬截断的前提下压缩上下文。

    为保持 `_compress_context()` 接口稳定，转存目录由调用方经 storage_dir 注入
    （适配点：替代 Hermes 原型的 ROOT_PATH/tool-budget-cache 硬编码）。
    ``state`` 是跨轮的 ContentReplacementState：同一条结果被替换的决定跨轮冻结，
    保证派生上下文前缀逐字节稳定（提示缓存不失效）；缺省新建一次性 state。
    任何异常都降级为原样返回，绝不因剪裁失败而中断整个压缩流程。
    """
    # 惰性导入（保留原型的容错语义）：tool_budget 只依赖标准库，
    # 放到函数内导入，导入失败时降级为原样返回而不是拖垮整个模块。
    try:
        from aegis_agent.context.tool_budget import (
            BudgetConfig,
            apply_budget,
            create_state,
        )
    except Exception as e:  # noqa: BLE001 — 剪裁模块缺失时原样返回，绝不中断压缩
        logger.info(f"tool_budget unavailable, skip oversized-tool trim: {e}")
        return messages

    try:
        trimmed, _state, stats = apply_budget(
            storage_dir,
            messages,
            state=state or create_state(),
            config=BudgetConfig(),
        )
    except Exception as e:  # noqa: BLE001 — 宁可超预算也不丢内容
        # 剪裁失败：宁可超预算也不丢内容，原样返回交给后续按轮压缩处理
        logger.info(f"tool_budget apply failed, keep original messages: {e}")
        return messages

    if stats.newly_persisted or stats.reapplied:
        logger.info(
            f"Oversized tool trim: persisted {stats.newly_persisted}, "
            f"reapplied {stats.reapplied}, shed ~{stats.shed_chars} chars"
        )
    return trimmed


def _run_micro_compact(messages: list[dict], max_tokens: int) -> list[dict]:
    """用 micro_compact 的阈值触发渐进式清理压缩上下文（本地、无 LLM 调用）。

    复用包内 aegis_agent/context/micro_compact.py：掐头去尾 → 清空旧 tool result
    → 瘦身旧 tool call 参数 → 删旧 reasoning → 占位符兜底，每步后重估、达标即返回。
    只作用于副本，不触碰系统消息 / 运行时标记 / 已压缩摘要区与当前 session。

    任何异常都降级为原样返回，绝不因清理失败而中断整个压缩流程。
    """
    try:
        from aegis_agent.context.micro_compact import micro_compact
    except Exception as e:  # noqa: BLE001 — 模块缺失时跳过本阶段，绝不中断压缩
        logger.info(f"micro_compact unavailable, skip: {e}")
        return messages

    try:
        return micro_compact(messages, max_tokens)
    except Exception as e:  # noqa: BLE001 — 清理失败时原样返回
        logger.info(f"micro_compact failed, keep original messages: {e}")
        return messages


def _collect_compactable_tool_ids(messages: list[dict]) -> list[str]:
    """按出现顺序收集所有可压缩工具的 tool_call ID。"""
    ids: list[str] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            # 遍历该 assistant 消息发起的每个工具调用
            for call in msg.get("tool_calls") or []:
                func_name = (call.get("function") or {}).get("name", "")  # 工具名
                call_id = call.get("id", "")                              # 工具调用 ID
                # 仅收集在白名单内且有 ID 的调用
                if func_name in COMPACTABLE_TOOLS and call_id:
                    ids.append(call_id)
    return ids


def _clear_tool_results(messages: list[dict], clear_set: set[str]) -> list[dict]:
    """将 clear_set 中 ID 对应的 tool 消息内容替换为占位符，同时保留消息结构。"""
    result = []
    for msg in messages:
        # 命中条件：是 tool 消息 + ID 在待清除集合 + 尚未被清除过
        if (
                msg.get("role") == "tool"
                and msg.get("tool_call_id") in clear_set
                and msg.get("content") != CLEARED_TOOL_RESULT
        ):
            # 用解包生成新 dict，替换 content 为占位符（不改动原消息）
            msg = {**msg, "content": CLEARED_TOOL_RESULT}
        result.append(msg)
    return result


def _clear_tool_call_arguments(messages: list[dict]) -> list[dict]:
    """
    清空可压缩工具的 arguments。
    execute_code → {"code": ""}，exec/bash/shell → {"command": ""}，其它 → {}。
    """
    # 不同工具对应的"空参数"模板
    EMPTY_ARGS: dict[str, str] = {
        "execute_code": '{"code": ""}',
        "exec": '{"command": ""}',
        "bash": '{"command": ""}',
        "shell": '{"command": ""}',
    }
    result = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            new_calls = []
            for call in msg["tool_calls"]:
                func = call.get("function") or {}
                name = func.get("name", "")
                if name in COMPACTABLE_TOOLS:
                    # 命中白名单：用对应空参数模板替换，找不到就用 "{}"
                    call = {
                        **call,
                        "function": {**func, "arguments": EMPTY_ARGS.get(name, "{}")},
                    }
                new_calls.append(call)
            msg = {**msg, "tool_calls": new_calls}
        result.append(msg)
    return result


def _find_deletable_tool_groups(messages: list[dict]) -> list[list[int]]:
    """
    识别所有可原子删除的"工具调用组"：每组 = [assistant_idx, tool_idx1, ...]。

    一组 = 一条带 tool_calls 的 assistant 消息 + 紧随其后属于它的 tool 消息。
    删除时必须整组删除；只删 assistant 或只删 tool 都会产生孤立的工具结果。
    """
    groups: list[list[int]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # 收集该 assistant 发起的所有工具调用 ID
            tool_ids = {tc.get("id") for tc in msg.get("tool_calls", [])}
            group = [i]      # 组内首个索引为 assistant 本身
            j = i + 1
            # 紧随其后的 tool 消息都属于这个 assistant
            while j < len(messages) and messages[j].get("role") == "tool":
                # 只把 ID 匹配的 tool 归入本组
                if messages[j].get("tool_call_id") in tool_ids:
                    group.append(j)
                j += 1
            groups.append(group)
        i += 1
    return groups


def _load_micro_compact_module():
    """惰性加载包内 micro_compact（阶段 B）模块本身，
    以便复用它内部的工具结果信息化摘要 / 去重 / 参数截断 / reasoning 清理等 helper。

    加载失败返回 None，调用方降级为本地占位符清理策略，绝不中断压缩。
    """
    try:
        from aegis_agent.context import micro_compact as mc
        return mc
    except Exception as e:  # noqa: BLE001 — helper 不可用时走本地降级策略
        logger.info(f"micro_compact helpers unavailable in single-round overflow: {e}")
        return None


# ── 单轮兜底专用的 reasoning 渐进清理（仅 _handle_single_round_overflow 使用）──
# 阈值常量统一定义在 compress_config.py（REASONING_* / DUP_REASONING_PLACEHOLDER）。
# 设计要点：阶段 B（micro_compact）刻意保留当前轮 reasoning，因为后面还有阶段 C 兜底；
# 但走到单轮兜底时阶段 C 已压无可压，当前轮 reasoning 往往是最大的 token 来源
# （思维链可能占数万 token），此时按「去重+截断（温和手段合并） → 清空（保留最后
# 一条并截断）」两级处理。
# 范围覆盖 [lo, len) 全列表（仅头部保护区除外）：这两步只碰 reasoning_content 字段，
# 不动 content / tool_calls / 消息结构——末尾最近若干条的保护是为了对话内容的连续性，
# 而思维链是模型草稿纸（多数 provider 不会回传），不在该保护意图之内；
# 本函数的 mc-不可用降级路径同样是全列表清 reasoning。阶段 B 不受影响。


def _head_tail_truncate_reasoning(reasoning: str) -> str:
    """对单条超长 reasoning 做头尾截断：保留开头（思考起点）与结尾（结论常在末尾）。"""
    return (
        reasoning[:_REASONING_TRUNCATE_HEAD]
        + _REASONING_TRUNCATE_INFIX
        + reasoning[-_REASONING_TRUNCATE_TAIL:]
    )


def _dedupe_reasoning(messages: list[dict], lo: int, hi: int) -> int:
    """对 [lo, hi) 区间内的 reasoning_content 按内容去重。

    与 micro_compact 的工具结果去重同一哲学：**保留最新一份**，更旧的重复
    （长度 >= _REASONING_DEDUP_MIN_CHARS）替换为占位符——最新的 reasoning 离当前
    上下文最近、价值最高，且让后续 3.5c 的 keep-last 能留住真思维链而非占位符。
    模型陷入循环时 reasoning 常整段重复，此步零信息损失。
    直接就地修改（调用方传入的已是深拷贝）。返回去重条数。
    """
    seen: set[str] = set()  # 已见过的 reasoning 内容哈希（从最新往最旧记录）
    deduped = 0
    # 从区间末尾往前扫：首次见到（即最新的一份）保留，更旧的同内容替换为占位符
    for i in range(hi - 1, lo - 1, -1):
        msg = messages[i]
        reasoning = msg.get("reasoning_content")
        if not isinstance(reasoning, str) or len(reasoning) < _REASONING_DEDUP_MIN_CHARS:
            continue
        h = hashlib.md5(reasoning.encode("utf-8", errors="replace")).hexdigest()
        if h in seen:
            msg["reasoning_content"] = _DUP_REASONING_PLACEHOLDER
            deduped += 1
        else:
            seen.add(h)
    return deduped


def _truncate_oversized_reasoning(messages: list[dict], lo: int, hi: int) -> int:
    """对 [lo, hi) 区间内超长的 reasoning_content 做头尾截断。

    单条超过 _REASONING_TRUNCATE_THRESHOLD 的保留头部 + 尾部（结论常在末尾），
    中间替换为截断提示；已被去重替换为占位符的因长度不足自然跳过。
    返回被截断的条数。
    """
    truncated = 0
    for i in range(lo, hi):
        msg = messages[i]
        reasoning = msg.get("reasoning_content")
        if not isinstance(reasoning, str) or len(reasoning) <= _REASONING_TRUNCATE_THRESHOLD:
            continue
        msg["reasoning_content"] = _head_tail_truncate_reasoning(reasoning)
        truncated += 1
    return truncated


def _clear_reasoning_except_last(messages: list[dict], lo: int, hi: int) -> int:
    """清空 [lo, hi) 区间内的 reasoning_content，只保留全列表最后一条非空 reasoning。

    与 mc._clear_reasoning 的区别：范围覆盖当前轮（单轮兜底时当前轮 reasoning
    不再受保护）。被保留的最后一条若自身超长，也做头尾截断——避免留一条
    比整个 token 预算还大的思维链。返回被清空/截断的条数。
    """
    # 找全列表最后一条非空 reasoning 的下标（整条保留，超长则截断）
    last_keep = -1
    for i in range(len(messages) - 1, -1, -1):
        r = messages[i].get("reasoning_content")
        if isinstance(r, str) and r.strip():
            last_keep = i
            break

    cleared = 0
    for i in range(lo, hi):
        msg = messages[i]
        reasoning = msg.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            continue
        if i == last_keep:
            # 保留最后一条，但超长仍截断（占位符等短内容不受影响）
            if len(reasoning) > _REASONING_TRUNCATE_THRESHOLD:
                msg["reasoning_content"] = _head_tail_truncate_reasoning(reasoning)
                cleared += 1
            continue
        msg["reasoning_content"] = ""
        cleared += 1
    return cleared


def _handle_single_round_overflow(
        messages: list[dict],
        max_tokens: int,
) -> list[dict]:
    """
    处理单轮对话上下文超限。（复用阶段 B 的工具结果信息化摘要策略）

    压缩策略（按优先级，每步后重估 token，达标即返回）：
    1. [复用阶段 B] 去重 + 把较早的工具结果替换为「信息化摘要」——保留工具名称、
       关键参数、执行结果、报错和文件路径（如 "[terminal] ran `npm test` -> exit 0"），
       而非无信息量的占位符；最近若干条工具结果不动。
    2. 清除历史 reasoning_content（思维链，占用大且对结果影响小）。
    3. [复用阶段 B] JSON 感知地缩减过长的工具调用参数（parse→截断长字符串字段→
       重新序列化，保证参数仍是合法 JSON）。
    3.5. [兜底专属] reasoning 渐进清理（范围为除头部保护区外的全列表，只碰
       reasoning_content 字段，不动 content / tool_calls / 消息结构）：
       去重+超长头尾截断（温和手段合并为一道，去重保留最新一份）→ 清空但保留
       最后一条（超长做头尾截断）。走到这里说明阶段 C 已压无可压，当前轮的
       思维链往往是最大 token 来源，清它优于删工具组。
    4. 截断仍然超大的工具结果（硬截断，保留残余内容而非删除）。
    5. [最后手段] 原子删除最早且完整的工具调用组（assistant tool_calls + 对应
       tool result 整组删除），避免破坏工具调用协议；尽量保留最近工具组。

    micro_compact（阶段 B）不可用时，降级为旧的占位符清理策略，绝不中断压缩。
    """
    logger.warning("Single round overflow, applying microcompact")

    # 内部小工具：判断是否已满足 token 限制
    def fits(msgs: list[dict]) -> bool:
        return _estimate_tokens(msgs) <= max_tokens

    # 深拷贝，彻底避免影响调用方的数据（含嵌套的 tool_calls）
    result = copy.deepcopy(messages)
    original_tokens = _estimate_tokens(result)

    # 未超限直接返回
    if original_tokens <= max_tokens:
        return result

    logger.warning(
        f"Overflow: {original_tokens} tokens > limit {max_tokens}"
    )

    mc = _load_micro_compact_module()

    if mc is not None:
        # ── 复用阶段 B：先算保护范围（保护头部 system/运行时标记/已压缩摘要区，
        #    以及末尾最近 KEEP_RECENT_MESSAGES 条消息），只在可清理区间内操作。──
        try:
            lo, tool_hi, reason_hi = mc._clearable_ranges(result, mc.KEEP_RECENT_MESSAGES)
            call_id_map = mc._build_call_id_map(result)

            # Step 1: 去重 + 信息化摘要（较早的工具结果 → 一行有信息量摘要）
            if tool_hi > lo:
                mc._deduplicate_tool_results(result, lo, tool_hi)
                mc._summarize_old_tool_results(result, lo, tool_hi, call_id_map)
                if fits(result):
                    logger.info(
                        f"Resolved by summarizing old tool results (Stage-B reuse): "
                        f"{original_tokens} -> {_estimate_tokens(result)}"
                    )
                    return result

            # Step 2: 清除历史 reasoning_content（仅历史轮，当前轮保留）
            if reason_hi > lo:
                mc._clear_reasoning(result, lo, reason_hi)
                if fits(result):
                    logger.info(
                        f"Resolved by clearing reasoning_content: "
                        f"{original_tokens} -> {_estimate_tokens(result)}"
                    )
                    return result

            # Step 3: JSON 感知地缩减过长的工具调用参数
            if tool_hi > lo:
                mc._truncate_tool_call_args(result, lo, tool_hi)
                if fits(result):
                    logger.info(
                        f"Resolved by truncating tool call arguments (JSON-aware): "
                        f"{original_tokens} -> {_estimate_tokens(result)}"
                    )
                    return result

            # Step 3.5: 当前轮 reasoning 的渐进清理。范围 [lo, len) 全列表（仅头部
            # 保护区除外）——这两步只碰 reasoning_content，不碰 content / tool_calls /
            # 消息结构，末尾最近若干条的内容保护不延伸到思维链（模型草稿纸）。
            # 破坏性递增：温和手段（去重 + 头尾截断）合并为一道 → 清空（只留最后一条）。
            if tool_hi > lo:
                # Step 3.5a: 去重（无损）+ 超长头尾截断（损中间），温和手段合并，
                # 只做一次 fits 检查
                deduped = _dedupe_reasoning(result, lo, len(result))
                truncated = _truncate_oversized_reasoning(result, lo, len(result))
                if (deduped or truncated) and fits(result):
                    logger.info(
                        f"Resolved by dedup+truncating reasoning_content "
                        f"(incl. current round, {deduped} duplicates, "
                        f"{truncated} truncated): "
                        f"{original_tokens} -> {_estimate_tokens(result)}"
                    )
                    return result

                # Step 3.5b: 清空 reasoning（保留最后一条，超长做头尾截断），
                # 删工具组前的最后一道
                cleared = _clear_reasoning_except_last(result, lo, len(result))
                if cleared and fits(result):
                    logger.info(
                        f"Resolved by clearing reasoning incl. current round "
                        f"(kept last, cleared {cleared}): "
                        f"{original_tokens} -> {_estimate_tokens(result)}"
                    )
                    return result
        except Exception as e:  # noqa: BLE001 — 阶段 B 复用失败时继续走硬截断/删除兜底
            # 复用阶段 B 出现意外：不中断，继续走下面的硬截断 / 原子删除兜底
            logger.info(
                f"Stage-B reuse failed, falling back to local truncation/deletion: {e}"
            )
    else:
        # ── 降级：micro_compact 不可用时，沿用旧的占位符清理策略 ──
        compactable_ids = _collect_compactable_tool_ids(result)
        keep_recent = max(1, KEEP_RECENT_TOOL_RESULTS)
        keep_set = set(compactable_ids[-keep_recent:])
        clear_set = set(compactable_ids) - keep_set
        if clear_set:
            result = _clear_tool_results(result, clear_set)
            if fits(result):
                logger.info(
                    f"Resolved by clearing old tool results "
                    f"(kept {len(keep_set)}, cleared {len(clear_set)}): "
                    f"{original_tokens} -> {_estimate_tokens(result)}"
                )
                return result

        for msg in result:
            if "reasoning_content" in msg:
                msg["reasoning_content"] = ""
        if fits(result):
            logger.info(
                f"Resolved by clearing reasoning_content: "
                f"{original_tokens} -> {_estimate_tokens(result)}"
            )
            return result

        result = _clear_tool_call_arguments(result)
        if fits(result):
            logger.info(
                f"Resolved by clearing tool call arguments: "
                f"{original_tokens} -> {_estimate_tokens(result)}"
            )
            return result

    # ------------------------------------------------------------------
    # Step 4: 硬截断仍然超大的工具结果（不区分工具类型，保留残余而非删除）。
    #         已被信息化摘要的一行内容长度远低于阈值，会被 len() 判断自然跳过。
    # ------------------------------------------------------------------
    tool_tokens = int(max_tokens / _TOOL_TRUNCATE_DIVISOR)  # 单条 tool 允许保留的 token 上限
    for msg in result:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and content != CLEARED_TOOL_RESULT and len(content) > tool_tokens:
                msg["content"] = _generic_truncate(content, tool_tokens)

    if fits(result):
        logger.info(
            f"Resolved by hard-truncating tool results: "
            f"{original_tokens} -> {_estimate_tokens(result)}"
        )
        return result

    # ------------------------------------------------------------------
    # Step 5: 最后手段——原子删除最早且完整的工具调用组
    #         （assistant tool_calls + 对应 tool result 整组删除），
    #         保持工具调用协议合法，最早的组先删、尽量保留最近工具组。
    # ------------------------------------------------------------------
    while not fits(result):
        groups = _find_deletable_tool_groups(result)
        if not groups:
            # 已无可删除的工具调用组，无法继续压缩
            logger.warning(
                f"Cannot reduce further, remaining: {_estimate_tokens(result)} tokens"
            )
            break
        # 删除最早的那组（索引最小）
        earliest_group = groups[0]
        indices_to_delete = set(earliest_group)
        # 通过枚举过滤掉待删索引，重建消息列表
        result = [msg for i, msg in enumerate(result) if i not in indices_to_delete]
        logger.info(
            f"Deleted tool group ({len(earliest_group)} messages), "
            f"remaining: {_estimate_tokens(result)} tokens"
        )

    logger.info(
        f"Final: {_estimate_tokens(result)} tokens (limit {max_tokens})"
    )
    return result


def _compress_context(
    messages: list[dict],
    llm_provider: ModelProvider,
    max_tokens: int,
    *,
    storage_dir: str | None = None,
    budget_state: ContentReplacementState | None = None,
    summary_provider: ModelProvider | None = None,
) -> list[dict]:
    """
    压缩上下文（三阶段管线，同步版本）：
    1. 阶段 A：超大工具结果转存磁盘 + 预览（无条件执行）；
    2. 阶段 B：阈值触发的本地微压缩（无 LLM 调用）；
    3. 阶段 C：从前往后按完整轮次调用 LLM 生成摘要，直到总 token 数 < max_tokens；
       仍超限则交给单轮兜底。

    适配点：Hermes 原型是 async 且摘要调用走 ``llm_provider.chat(..., model=...,
    temperature=0.0, max_tokens=...)``；Aegis 的 ModelProvider Protocol 只有
    ``stream()``（模型名 / 采样参数由 provider 自身持有），因此本函数为同步，
    摘要走 ``stream()`` + ``collect_response``。

    ``budget_state``：阶段 A 的跨轮状态（由会话级持有者跨轮传入，保证替换决定
    跨轮冻结、派生上下文前缀逐字节稳定）；缺省新建一次性 state。
    ``summary_provider``：阶段 C 摘要专用的 provider（可配置 temperature=0 等
    确定性采样参数）；缺省与主 provider 相同。
    """
    if not messages:
        return messages

    # 先估算当前总 token，仅用于日志与后续阶段的触发判断。
    # 阶段 A（超大工具响应转存）无条件执行：它是发送前的保护性处理，
    # 与当前是否超限无关——超限时转存可直接压回阈值，未超限时也能提前
    # 把单条巨型工具结果换成预览，避免后续轮次积累后才超限。
    current_tokens = _estimate_tokens(messages)
    if current_tokens > max_tokens:
        logger.info(f"Context too large ({current_tokens} tokens), limit={max_tokens}, trimming messages")
    else:
        logger.info(f"Context within limit ({current_tokens} tokens), running phase A anyway")

    # ##########################################################################
    # ## 阶段 A：超大工具响应剪裁（转存磁盘 + 保留预览）                       ##
    # ##########################################################################
    # 无条件执行：完整内容写盘不丢失，发给模型的只留预览。
    # 未超限时可直接返回（阶段 B/C 不需要跑）；超限时继续走后续压缩。
    messages = _trim_oversized_tools_via_budget(
        messages, storage_dir or _default_storage_dir(), state=budget_state
    )

    # 重新估算：若已回落到阈值内（或本来就未超限），直接返回，跳过后续所有压缩
    current_tokens = _estimate_tokens(messages)
    if current_tokens <= max_tokens:
        logger.info(
            f"Context within limit after oversized-tool trim ({current_tokens} tokens), no further trimming needed"
        )
        return messages
    # ## ===================== 阶段 A 结束 ================================== ##

    # ##########################################################################
    # ## 阶段 B：阈值触发的渐进式微压缩 micro_compact（本地清理，无 LLM 调用） ##
    # ## 掐头去尾 / 清旧 tool result / 瘦身参数 / 删旧 reasoning / 占位兜底。   ##
    # ##########################################################################

    # 超大工具剪裁后仍超限 → 先本地做一轮 micro_compact。往往一步就能压回阈值内，
    # 从而省掉后续按轮 LLM 摘要的开销与信息损失；仍超限才继续走摘要。
    messages = _run_micro_compact(messages, max_tokens)
    current_tokens = _estimate_tokens(messages)
    if current_tokens <= max_tokens:
        logger.info(
            f"Context within limit after micro_compact ({current_tokens} tokens), no further trimming needed"
        )
        return messages

    # ## ===================== 阶段 B 结束 ================================== ##

    # ##########################################################################
    # ## 阶段 C：按轮 LLM 摘要（以下全部）                                     ##
    # ## 前面阶段都压不回阈值时，按对话轮次逐轮调用 LLM 生成摘要；仍超限则最后 ##
    # ## 交给 _handle_single_round_overflow 做单轮兜底。                       ##
    # ##########################################################################

    # ── 分离系统消息与运行时上下文标记 ────────────────────────────
    # 第一条若是 system，则视为系统提示词单独保留
    system_msg = messages[0] if messages[0].get("role") == "system" else None
    # 第二条若是带"运行时上下文"标记的 user 消息，也单独识别（元数据，不参与轮次压缩）
    # （适配点：补了 len > 1 与 content 为 None 的守卫，原型在极端短列表下会 IndexError）
    _RUNTIME_CONTEXT_TAG = (
        messages[1]
        if len(messages) > 1
        and messages[1].get("role") == "user"
        and RUNTIME_CONTEXT_MARKER in (messages[1].get("content") or "")
        else None
    )
    start_idx = 1 if system_msg else 0  # 真正对话内容的起始下标
    if _RUNTIME_CONTEXT_TAG:
        start_idx += 1  # 若存在运行时上下文标记，起点再往后挪一位

    # ── 统计已压缩过的记录数量 ────────────────────────────────────
    # 每个历史摘要占 2 条消息（user + 带 [Context Summary] 的 assistant）
    start_idx_compressed = 0
    for message in messages:
        if (
            message.get("role") == "assistant"
            and message.get("content")
            and CONTEXT_SUMMARY_TAG in message["content"]
        ):
            start_idx_compressed += 2

    # 将"系统消息 + 已压缩部分"之后的消息按轮次切分
    all_rounds = _split_into_rounds(messages[start_idx+start_idx_compressed:])

    # 只有一轮（或没有完整轮次）→ 无法按轮压缩，走单轮兜底逻辑
    if len(all_rounds) <= 1:
        return _handle_single_round_overflow(messages, max_tokens)

    # 分离最后一轮（当前进行中的对话，不压缩）
    last_round = all_rounds[-1]
    compressible_rounds = all_rounds[:-1]  # 可压缩的历史轮次

    # 进度：阶段 C 入口，打印总轮数（最后一轮保留、其余可逐轮摘要）
    _total_compressible = len(compressible_rounds)
    logger.info(
        f"[Phase C] Round-based LLM summary START: {current_tokens} tokens > {max_tokens} limit; "
        f"{len(all_rounds)} rounds total ({_total_compressible} compressible, last kept)"
    )

    # ── 从前往后逐轮压缩，直到满足 token 限制 ─────────────────────
    compressed_rounds: list[list[dict]] = []  # 已压缩（或已固定保留）的轮次
    # 若之前已有压缩摘要（start_idx_compressed > 0），把这段历史摘要区间原样纳入结果头部。
    # 摘要区位于 messages[start_idx : start_idx + start_idx_compressed]。
    # 注意：守卫必须是 "> 0" 而非 ">= start_idx" —— 后者在"无 system 消息
    # (start_idx=0) 且无历史摘要 (start_idx_compressed=0)"时会因 0>=0 误触发，
    # 把第 0 条消息（当轮 user 问题）重复搬入结果，导致压缩后出现两条相同的 user。
    if start_idx_compressed > 0:
        compressed_rounds.append(messages[start_idx:start_idx + start_idx_compressed])

    uncompressed_rounds = compressible_rounds.copy()  # 尚未压缩的轮次队列

    while uncompressed_rounds:
        # 构建当前候选结果（系统消息 + 已压缩轮次 + 未压缩轮次 + 最后一轮）
        candidate_messages = []
        if system_msg:
            candidate_messages.append(system_msg)

        # 添加已压缩的轮次
        for round_msgs in compressed_rounds:
            candidate_messages.extend(round_msgs)

        # 添加未压缩的轮次
        for round_msgs in uncompressed_rounds:
            candidate_messages.extend(round_msgs)

        # 添加最后一轮次
        candidate_messages.extend(last_round)

        # 计算当前候选的 token 数
        candidate_tokens = _estimate_tokens(candidate_messages)

        # 如果已经满足限制，返回结果
        if candidate_tokens <= max_tokens:
            logger.info(
                f"Compressed context: {current_tokens} → {candidate_tokens} tokens "
                f"({len(compressed_rounds)} rounds compressed, {len(uncompressed_rounds)} rounds kept)"
            )
            return candidate_messages

        # 否则，压缩最早的一轮
        if not uncompressed_rounds:
            # 所有轮次都已压缩，但仍超限
            # 应该不会走到这个判断里
            logger.info(
                f"Cannot compress further: {candidate_tokens} tokens still exceeds limit"
            )
            return candidate_messages

        # 取出最早的一轮进行压缩
        first_round = uncompressed_rounds.pop(0)

        # 进度：当前压缩到第几轮（1-based），还剩多少轮、当前候选 token
        _round_no = _total_compressible - len(uncompressed_rounds)
        logger.info(
            f"[Phase C] Summarizing round {_round_no}/{_total_compressible} "
            f"({len(first_round)} msgs in round); candidate now {candidate_tokens} tokens, "
            f"{len(uncompressed_rounds)} rounds still queued"
        )

        # 压缩这一轮
        if _is_complete_round(first_round):
            # 完整轮次 → 调用 LLM 生成摘要（优先用摘要专用 provider）
            summary = _summarize_round(first_round, summary_provider or llm_provider)

            if summary is None:
                # 摘要失败 / 结果为空 / 结果不可用：不使用兜底文本替换整轮，
                # 直接保留该轮原始消息（日志已在 _summarize_round 内记录）。
                # 该轮已从 uncompressed_rounds 弹出并放入 compressed_rounds，
                # 因此本次压缩循环不会再重复摘要同一轮。
                logger.info(
                    f"[Phase C] Round {_round_no}/{_total_compressible}: summary unusable, "
                    f"keeping original round"
                )
                compressed_rounds.append(first_round)
            else:
                logger.info(
                    f"[Phase C] Round {_round_no}/{_total_compressible}: summarized OK "
                    f"({len(summary)} chars)"
                )
                # 取出该轮的原始用户问题（保留问题，压缩回答）
                user_msg = next((m for m in first_round if m.get("role") == "user"), None)

                # 用"原始用户问题 + 摘要 assistant 消息"替换整轮
                # （完整轮次必以 user 开头，user_msg 不会为 None，守卫仅为类型收窄）
                compressed_round: list[dict] = [user_msg] if user_msg is not None else []
                compressed_round.append(
                    {
                        "role": "assistant",
                        "content": f"{CONTEXT_SUMMARY_TAG}\n{summary}",
                        "timestamp": first_round[-1].get("timestamp")
                    }
                )
                compressed_rounds.append(compressed_round)
        else:
            # 不完整轮次保留原样（无法安全摘要）
            logger.info(
                f"[Phase C] Round {_round_no}/{_total_compressible}: incomplete round, "
                f"kept as-is (not summarized)"
            )
            compressed_rounds.append(first_round)

    # 理论上不会到这里，走到这里是因为对话还很长，可压缩的都压完之后仍然超长，这就要对当前轮次做处理了
    # 构建当前候选结果
    candidate_messages = []
    if system_msg:
        candidate_messages.append(system_msg)

    # 添加已压缩的轮次
    for round_msgs in compressed_rounds:
        candidate_messages.extend(round_msgs)

    # 添加未压缩的轮次
    for round_msgs in uncompressed_rounds:
        candidate_messages.extend(round_msgs)

    # 添加最后一轮次
    candidate_messages.extend(last_round)
    # 摘要都压完仍超限 → 交给单轮兜底逻辑做更激进的压缩
    return _handle_single_round_overflow(candidate_messages, max_tokens)


def _split_into_rounds(messages: list[dict]) -> list[list[dict]]:
    """
    将消息列表分割为对话轮次。
    每轮从 user 消息开始，到下一个 user 消息之前结束。
    """
    rounds = []                   # 所有轮次
    current_round: list[dict] = []  # 当前正在累积的轮次

    for msg in messages:
        if msg.get("role") == "user":
            # 遇到新的 user 消息，开始新轮次
            if current_round:
                rounds.append(current_round)  # 先把上一轮收尾
            current_round = [msg]             # 新轮次以该 user 消息开头
        else:
            # assistant 或 tool 消息，追加到当前轮次
            current_round.append(msg)

    # 添加最后一轮（可能不完整）
    if current_round:
        rounds.append(current_round)

    return rounds


def _is_complete_round(round_msgs: list[dict]) -> bool:
    """
    判断是否为完整轮次：
    - 必须以 user 消息开始
    - 必须以 assistant 消息结束（content 不为空）
    - 最后一条 assistant 不能包含 tool_calls
    """
    if not round_msgs:
        return False  # 空轮次

    # 检查开头：必须是 user
    if round_msgs[0].get("role") != "user":
        return False

    # 检查结尾：必须是 assistant
    last_msg = round_msgs[-1]
    if last_msg.get("role") != "assistant":
        return False

    # 结尾 assistant 的 content 必须为非空字符串
    content = last_msg.get("content")
    if not isinstance(content, str) or not content.strip():
        return False

    # 最后一条 assistant 不能还在发起工具调用（否则轮次尚未结束）
    return not last_msg.get("tool_calls")


# ── 阶段 C 摘要序列化相关常量（统一定义在 compress_config.py，SUMMARY_*）──

# 摘要模型输出被判定为"不可用"的占位/前缀（命中则视为失败，保留原轮次）。
_UNUSABLE_SUMMARY_MARKERS = (
    "i cannot", "i can't", "i'm sorry", "i am sorry",
    "as an ai", "sorry, i",
)


def _summary_content_to_text(content) -> str:
    """把消息 content 规整为纯文本：字符串原样返回，多模态 list 拼接其中文本块。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _summary_head_tail_truncate(
    text: str,
    max_chars: int = _SUMMARY_CONTENT_MAX,
    head_chars: int = _SUMMARY_CONTENT_HEAD,
    tail_chars: int = _SUMMARY_CONTENT_TAIL,
) -> str:
    """对过长文本做"头部 + 尾部"截断，保留结论/报错/测试汇总常在的末尾。

    参考 hermes context_compressor._serialize_for_summary 的思路：
    只保留头部会丢掉末尾的报错、测试结果和最终结论，因此中间截断、两头保留。
    """
    if not isinstance(text, str) or len(text) <= max_chars:
        return text
    return (
        text[:head_chars]
        + "\n...[truncated for summary]...\n"
        + text[-tail_chars:]
    )


def _summary_truncate_tool_args(args: str) -> str:
    """对过长的工具参数做 JSON 感知截断（移植自 hermes _truncate_tool_call_args_json 思路）。

    先 parse JSON → 递归截断过长的字符串叶子字段 → 重新序列化，
    从而避免直接切断 JSON 造成参数结构非法。非合法 JSON 则退化为头尾截断。
    """
    if not isinstance(args, str) or len(args) <= _SUMMARY_TOOL_ARGS_MAX:
        return args

    try:
        parsed = json.loads(args)
    except (ValueError, TypeError):
        # 非合法 JSON：不解析，退化为普通头尾截断（仅用于喂给摘要模型阅读）。
        return _summary_head_tail_truncate(
            args, _SUMMARY_TOOL_ARGS_MAX, _SUMMARY_ARGS_FIELD_HEAD, 200
        )

    def _shrink(obj):
        if isinstance(obj, str):
            if len(obj) > _SUMMARY_ARGS_FIELD_HEAD:
                return obj[:_SUMMARY_ARGS_FIELD_HEAD] + "...[truncated]"
            return obj
        if isinstance(obj, dict):
            return {k: _shrink(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_shrink(v) for v in obj]
        return obj

    return json.dumps(_shrink(parsed), ensure_ascii=False)


def _summary_redact(text: str) -> str:
    """发送摘要模型前的敏感信息脱敏。

    适配点：Hermes 原型优先复用项目内的 ``agent.redact``；Aegis 没有该模块，
    因此只保留轻量正则兜底，最差也原样返回，绝不因脱敏失败而中断摘要流程。
    """
    if not isinstance(text, str) or not text:
        return text
    # 轻量兜底：常见 token 形态（GitHub token、Bearer、类 API key 长串）。
    try:
        import re
        text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b", "[REDACTED]", text)
        text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}", "Bearer [REDACTED]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9]{16,}\b", "[REDACTED]", text)
    except Exception:  # noqa: BLE001 — 脱敏失败时原样返回，绝不中断摘要
        return text
    return text


def _serialize_round_for_summary(round_msgs) -> str:
    """按原始顺序把完整轮次序列化为带角色标签的文本，供摘要模型阅读。

    参考 hermes context_compressor._serialize_for_summary，但仅针对"单个轮次"：
      - user      → [USER]
      - assistant → [ASSISTANT]（正文）+ [TOOL CALL]（每个工具调用的名称与参数）
      - tool      → [TOOL RESULT]（保留工具返回内容）
    长内容做"头部+尾部"截断，工具参数做 JSON 感知截断，发送前统一脱敏。

    这样摘要模型能看到文件路径、报错、测试结果、关键数据和执行状态，
    而不再只有"用户问题 + 最终回答"。
    """
    parts: list[str] = []

    for msg in round_msgs:
        role = msg.get("role")

        if role == "user":
            text = _summary_redact(_summary_content_to_text(msg.get("content")))
            text = _summary_head_tail_truncate(text)
            if text.strip():
                parts.append(f"[USER]\n{text}")
            continue

        if role == "assistant":
            text = _summary_redact(_summary_content_to_text(msg.get("content")))
            text = _summary_head_tail_truncate(text)
            if text.strip():
                parts.append(f"[ASSISTANT]\n{text}")
            # 附加该 assistant 发起的工具调用（名称 + 截断后的参数）
            for call in msg.get("tool_calls") or []:
                func = call.get("function") or {}
                name = func.get("name", "unknown")
                args = func.get("arguments") or ""
                args = _summary_truncate_tool_args(args)
                args = _summary_redact(args)
                parts.append(f"[TOOL CALL]\n{name}({args})")
            continue

        if role == "tool":
            text = _summary_redact(_summary_content_to_text(msg.get("content")))
            text = _summary_head_tail_truncate(text)
            tool_name = msg.get("name", "")
            header = "[TOOL RESULT]" + (f" {tool_name}" if tool_name else "")
            parts.append(f"{header}\n{text}")
            continue

        # 其它角色（少见）：原样带标签输出
        text = _summary_redact(_summary_content_to_text(msg.get("content")))
        text = _summary_head_tail_truncate(text)
        if text.strip():
            parts.append(f"[{str(role).upper()}]\n{text}")

    return "\n\n".join(parts)


def _is_summary_usable(summary) -> bool:
    """判断摘要模型返回结果是否可用：非空字符串、有实质内容、非明显拒答。"""
    if not isinstance(summary, str):
        return False
    stripped = summary.strip()
    if len(stripped) < 10:
        return False
    lowered = stripped.lower()
    # 命中拒答类前缀且整体很短 → 视为不可用
    return not (len(stripped) < 200 and any(m in lowered for m in _UNUSABLE_SUMMARY_MARKERS))

def _summarize_round(round_msgs, llm_provider: ModelProvider):
    """为一个完整的对话轮次生成结构化摘要。

    返回：
      - 成功 → 摘要字符串；
      - 失败 / 结果为空 / 结果不可用 → None（由调用方保留原轮次，不做兜底替换）。

    与旧版的区别：不再只提取"用户问题 + 最终回答 + 工具名"，而是按原始顺序
    序列化完整轮次（user/assistant/tool call/tool result），使摘要模型能看到
    文件路径、报错、测试结果、关键数据与执行状态。

    适配点：Hermes 原型调用 ``await llm_provider.chat(messages=..., model=...,
    temperature=0.0, max_tokens=_SUMMARY_MAX_TOKENS)``；Aegis 的 ModelProvider
    Protocol 只有 ``stream()``，模型与采样参数由 provider 自身配置，
    因此这里用 ``stream()`` + ``collect_response`` 取回完整文本。
    """
    # ── 按原始顺序序列化完整轮次（含工具调用与工具结果）────────────
    serialized = _serialize_round_for_summary(round_msgs)
    if not serialized.strip():
        # 轮次内容为空（异常情况）→ 视为无法摘要，保留原轮次
        logger.info("Round has no serializable content, keeping original round")
        return None

    # ── 构建结构化摘要提示词 ──────────────────────────────────────
    summary_prompt = f"""你是上下文压缩助手。请将下面这一"对话轮次"压缩成可供后续模型直接续接任务的结构化摘要，目标约 {_SUMMARY_MAX_TOKENS} tokens 以内。
轮次原文按原始顺序提供，可能包含用户消息、助手消息、工具调用与工具结果：

---
{serialized}

---
请按以下格式输出，没有内容的字段可以省略：
用户请求
已执行操作
关键工具结果
结论与决策
相关文件或状态
未解决事项
摘要要求：
- 优先保留用户最近尚未回答或尚未完成的请求，确保后续模型可以直接继续处理。
- 只将实际执行且有明确结果的操作写入"已执行操作"；计划、建议和未执行方案不得写成已完成。
- 保留后续任务所需的关键对象、操作过程、输入输出、参数与条件、结果数据、异常信息和当前状态。
- 原文中出现的具体名称、数值、路径、命令或其他关键细节，应根据其对后续任务的重要性保留。
- 区分用户明确确认的决定、工具验证的事实和助手提出的建议；未经验证的内容必须标记为"未验证"。
- 如果早期方案、猜测或结论已被后续内容推翻，只保留最终有效结论；必要时简要注明原方案已放弃。
- 合并重复的工具调用、搜索和解释，删除无效尝试、寒暄以及与后续任务无关的信息。
- 不得根据上下文自行推断操作结果、当前状态、结论或用户决定。
- 不要自动生成用户没有提出的后续任务。
绝对不要保留 API key、访问令牌、密码、密钥、Cookie、连接字符串或其他凭证。如有出现，将具体值替换为 [REDACTED]。
当内容超过预算时，按以下优先级保留：
1. 用户最近尚未完成的请求；
2. 已确认的结论、决策和当前状态；
3. 关键结果、数据、异常信息和验证结果；
4. 后续续接所需的具体细节；
5. 一般背景和解释。
使用当前对话所使用的语言撰写。
只输出摘要正文，不要添加寒暄、前言、解释或额外前缀。"""

    try:
        # 调用 LLM 生成摘要（适配点：同步 stream + collect_response；
        # temperature / max_tokens 由 provider 自身配置，不在 Protocol 内）
        summary_response = collect_response(
            llm_provider.stream([Message(role=Role.USER, content=summary_prompt)])
        )
    except Exception as e:  # noqa: BLE001 — 摘要失败时保留原轮次，绝不中断压缩
        # 调用失败：记录日志并返回 None（调用方保留原轮次，不用兜底文本替换）
        logger.info(f"Failed to generate summary, keeping original round: {e}")
        return None

    summary = summary_response.content
    if not _is_summary_usable(summary):
        # 结果为空或明显不可用：返回 None，保留原轮次
        logger.info(
            "Summary result empty or unusable, keeping original round "
            f"(len={len(summary) if isinstance(summary, str) else 'n/a'})"
        )
        return None

    return summary.strip()


# =============================================================================
# Message <-> OpenAI dict 边界转换（Aegis 适配层）
# =============================================================================
# 上面的算法核心逐字节移植自原型，操作的是 OpenAI Chat Completions 的 dict
# 形状；Aegis 内部的会话单元是 models.base.Message dataclass。下面两个转换器
# 是唯一的边界：进入压缩管线前 Message -> dict，出来后 dict -> Message。
# reasoning_content 双向携带（Message 持有该字段；压缩对它的清理由此生效）；
# timestamp 等 dict 侧扩展字段在转换回 Message 时丢弃（Message 无对应字段）。


def message_to_dict(message: Message) -> dict:
    """把内部 Message 转成压缩管线使用的 OpenAI 形状 dict。"""
    d: dict = {"role": message.role.value, "content": message.content}
    if message.reasoning_content:
        d["reasoning_content"] = message.reasoning_content
    if message.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in message.tool_calls
        ]
    if message.role is Role.TOOL:
        d["tool_call_id"] = message.tool_call_id or ""
        if message.name:
            d["name"] = message.name
    return d


def dict_to_message(d: dict) -> Message:
    """把压缩管线产出的 dict 转回内部 Message。

    content 非字符串（None / 多模态 list）时规整为纯文本；未知 role 兜底为 user。
    reasoning_content 双向携带；timestamp 等 dict 侧扩展字段被丢弃
    （Message 无对应字段）。
    """
    try:
        role = Role(d.get("role", "user"))
    except ValueError:
        role = Role.USER
    content = d.get("content")
    if not isinstance(content, str):
        content = _summary_content_to_text(content)
    reasoning = d.get("reasoning_content")
    tool_calls = [
        ToolCall(
            id=str(call.get("id", "")),
            name=str((call.get("function") or {}).get("name", "")),
            arguments=str((call.get("function") or {}).get("arguments", "")),
        )
        for call in d.get("tool_calls") or []
        if isinstance(call, dict)
    ]
    tool_call_id = d.get("tool_call_id")
    name = d.get("name")
    return Message(
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=str(tool_call_id) if tool_call_id else None,
        name=str(name) if name else None,
        reasoning_content=reasoning if isinstance(reasoning, str) else "",
    )


def estimate_tokens(messages: Sequence[Message]) -> int:
    """公开的 token 估算：接受内部 Message 序列，内部转换为 dict 后估算。"""
    return _estimate_tokens([message_to_dict(m) for m in messages])


def compress_context(
    messages: Sequence[Message],
    llm_provider: ModelProvider,
    max_tokens: int,
    *,
    storage_dir: str | None = None,
    budget_state: ContentReplacementState | None = None,
    summary_provider: ModelProvider | None = None,
) -> list[Message]:
    """压缩派生上下文，使其尽量回落到 ``max_tokens`` 以内（三阶段管线）。

    参数：
      messages         —— 派生上下文（通常是 ContextBuilder.build 的输出）。
                         **不会被修改**；压缩只作用于转换后的 dict 副本。
      llm_provider     —— 阶段 C 逐轮摘要使用的模型提供者（ModelProvider Protocol）。
      max_tokens       —— 上下文 token 上限。
      storage_dir      —— 阶段 A 超大工具结果的转存目录；
                         缺省为 ``~/.aegis/tool-result-cache``。
      budget_state     —— 阶段 A 的跨轮 ContentReplacementState；由会话级持有者
                         跨轮传入时，同一条工具结果的替换决定跨轮冻结，发给模型的
                         上下文前缀逐字节稳定（提示缓存不失效）。缺省新建一次性
                         state（单次调用场景）。
      summary_provider —— 阶段 C 摘要专用 provider（可用 temperature=0 等确定性
                         采样参数构造）；缺省与 llm_provider 相同。

    返回新的 Message 列表（压缩后的派生视图）。原始消息历史（source of
    truth）不参与、也不被本函数修改——压缩结果只应发送给模型，不应写回会话存储。
    """
    dicts = [message_to_dict(m) for m in messages]
    compressed = _compress_context(
        dicts,
        llm_provider,
        max_tokens,
        storage_dir=storage_dir,
        budget_state=budget_state,
        summary_provider=summary_provider,
    )
    return [dict_to_message(d) for d in compressed]


__all__ = [
    "compress_context",
    "dict_to_message",
    "estimate_tokens",
    "message_to_dict",
]
