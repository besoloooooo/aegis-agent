# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# PORT (near-verbatim) of Hermes ``tools/fuzzy_match.py``.  The module is
# self-contained (only ``re`` + ``difflib``) with zero Hermes coupling, so it
# is carried over whole rather than reimplemented — it is the smallest coherent
# unit and its entire dependency closure is stdlib.  Only cosmetic changes
# (typing imports) were made.
"""Fuzzy matching for the ``patch`` tool.

Implements a multi-strategy matching chain to robustly find and replace text,
accommodating variations in whitespace, indentation, and escaping common in
LLM-generated code.

The 9-strategy chain (inspired by OpenCode), tried in order:
1. Exact match — direct string comparison
2. Line-trimmed — strip leading/trailing whitespace per line
3. Whitespace normalized — collapse multiple spaces/tabs to a single space
4. Indentation flexible — ignore indentation differences entirely
5. Escape normalized — convert ``\\n`` literals to actual newlines
6. Trimmed boundary — trim first/last line whitespace only
7. Unicode normalized — smart quotes / dashes / ellipsis → ASCII
8. Block anchor — match first+last lines, use similarity for the middle
9. Context-aware — 50% line-similarity threshold

Multi-occurrence matching is handled via the ``replace_all`` flag.

Usage::

    from aegis_agent.tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, strategy, error = fuzzy_find_and_replace(
        content="def foo():\\n    pass",
        old_string="def foo():",
        new_string="def bar():",
        replace_all=False,
    )
"""

from __future__ import annotations

import re
from collections.abc import Callable
from difflib import SequenceMatcher

UNICODE_MAP = {
    "“": '"', "”": '"',   # smart double quotes
    "‘": "'", "’": "'",   # smart single quotes
    "—": "--", "–": "-",  # em/en dashes
    "…": "...", " ": " ",  # ellipsis and non-breaking space
}


def _unicode_normalize(text: str) -> str:
    """Normalize Unicode characters to their standard ASCII equivalents."""
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> tuple[str, int, str | None, str | None]:
    """Find and replace text using a chain of increasingly fuzzy strategies.

    Args:
        content: The file content to search in.
        old_string: The text to find.
        new_string: The replacement text.
        replace_all: If True, replace all occurrences; if False, require uniqueness.

    Returns:
        Tuple of ``(new_content, match_count, strategy_name, error_message)``.
        - On success: ``(modified_content, n_replacements, strategy_used, None)``
        - On failure: ``(original_content, 0, None, error_description)``
    """
    if not old_string:
        return content, 0, None, "old_string cannot be empty"

    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    strategies: list[tuple[str, Callable[[str, str], list[tuple[int, int]]]]] = [
        ("exact", _strategy_exact),
        ("line_trimmed", _strategy_line_trimmed),
        ("whitespace_normalized", _strategy_whitespace_normalized),
        ("indentation_flexible", _strategy_indentation_flexible),
        ("escape_normalized", _strategy_escape_normalized),
        ("trimmed_boundary", _strategy_trimmed_boundary),
        ("unicode_normalized", _strategy_unicode_normalized),
        ("block_anchor", _strategy_block_anchor),
        ("context_aware", _strategy_context_aware),
    ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)

        if matches:
            if len(matches) > 1 and not replace_all:
                return content, 0, None, (
                    f"Found {len(matches)} matches for old_string. "
                    f"Provide more context to make it unique, or use replace_all=True."
                )

            # Escape-drift guard: when the matched strategy is NOT `exact`, we
            # matched via some normalization.  If new_string contains
            # shell/JSON-style escape sequences (\' or \") that would be written
            # literally but the matched region has no such sequences, this is
            # almost certainly tool-call serialization drift — writing
            # new_string as-is would corrupt the file.
            if strategy_name != "exact":
                drift_err = _detect_escape_drift(content, matches, old_string, new_string)
                if drift_err:
                    return content, 0, None, drift_err

            effective_new = _maybe_unescape_new_string(new_string, content, matches)
            new_content = _apply_replacements(
                content, matches, effective_new,
                old_string=old_string if strategy_name != "exact" else None,
            )
            return new_content, len(matches), strategy_name, None

    return content, 0, None, "Could not find a match for old_string in the file"


def _detect_escape_drift(
    content: str,
    matches: list[tuple[int, int]],
    old_string: str,
    new_string: str,
) -> str | None:
    """Detect tool-call escape-drift artifacts in new_string.

    Looks for ``\\'`` or ``\\"`` sequences present in both old_string and
    new_string but absent from the matched region of the file — a pattern that
    indicates the transport layer inserted spurious shell-style escapes.

    Returns an error string if drift is detected, None otherwise.
    """
    if "\\'" not in new_string and '\\"' not in new_string:
        return None

    matched_regions = "".join(content[start:end] for start, end in matches)

    for suspect in ("\\'", '\\"'):
        if suspect in new_string and suspect in old_string and suspect not in matched_regions:
            plain = suspect[1]  # "'" or '"'
            return (
                f"Escape-drift detected: old_string and new_string contain "
                f"the literal sequence {suspect!r} but the matched region of "
                f"the file does not. This is almost always a tool-call "
                f"serialization artifact where an apostrophe or quote got "
                f"prefixed with a spurious backslash. Re-read the file with "
                f"read_file and pass old_string/new_string without "
                f"backslash-escaping {plain!r} characters."
            )
    return None


def _leading_whitespace(line: str) -> str:
    """Return the leading whitespace prefix of a line (spaces/tabs)."""
    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return line[:i]


def _first_meaningful_line(text: str) -> str | None:
    """Return the first line of ``text`` with any non-whitespace content, else None."""
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    """Adjust ``new_string`` so its indentation matches ``file_region``.

    Used after a non-exact fuzzy match: the LLM may have sent old_string and
    new_string with a different indent than the file actually has.  Preserves
    the LLM's intended relative nesting while anchoring to the file's indent.
    """
    if not new_string:
        return new_string

    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string

    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)

    if old_indent == file_indent:
        return new_string

    out_lines: list[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        line_indent = _leading_whitespace(line)
        if line_indent.startswith(old_indent):
            remainder = line[len(old_indent):]
            out_lines.append(file_indent + remainder)
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


def _maybe_unescape_new_string(
    new_string: str,
    content: str,
    matches: list[tuple[int, int]],
) -> str:
    """Conditionally unescape ``\\t``/``\\r`` in new_string.

    Only applied per-sequence when the matched region of the file actually
    contains the corresponding control character.  ``\\n`` is intentionally
    excluded (it serializes correctly through JSON).
    """
    if "\\t" not in new_string and "\\r" not in new_string:
        return new_string

    matched_regions = "".join(content[start:end] for start, end in matches)
    out = new_string
    if "\\t" in out and "\t" in matched_regions:
        out = out.replace("\\t", "\t")
    if "\\r" in out and "\r" in matched_regions:
        out = out.replace("\\r", "\r")
    return out


def _apply_replacements(
    content: str,
    matches: list[tuple[int, int]],
    new_string: str,
    old_string: str | None = None,
) -> str:
    """Apply replacements at the given positions (end→start to keep offsets)."""
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)

    result = content
    for start, end in sorted_matches:
        if old_string is not None:
            file_region = content[start:end]
            adjusted = _reindent_replacement(file_region, old_string, new_string)
        else:
            adjusted = new_string
        result = result[:start] + adjusted + result[end:]

    return result


# =============================================================================
# Matching strategies
# =============================================================================

def _strategy_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 1: exact string match."""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + 1
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 2: match with line-by-line whitespace trimming."""
    pattern_lines = [line.strip() for line in pattern.split("\n")]
    pattern_normalized = "\n".join(pattern_lines)

    content_lines = content.split("\n")
    content_normalized_lines = [line.strip() for line in content_lines]

    return _find_normalized_matches(
        content, content_lines, content_normalized_lines,
        pattern, pattern_normalized,
    )


def _strategy_whitespace_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 3: collapse multiple whitespace to a single space."""
    def normalize(s: str) -> str:
        return re.sub(r"[ \t]+", " ", s)

    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)

    matches_in_normalized = _strategy_exact(content_normalized, pattern_normalized)
    if not matches_in_normalized:
        return []

    return _map_normalized_positions(content, content_normalized, matches_in_normalized)


def _strategy_indentation_flexible(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 4: ignore indentation differences entirely."""
    content_lines = content.split("\n")
    content_stripped_lines = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split("\n")]

    return _find_normalized_matches(
        content, content_lines, content_stripped_lines,
        pattern, "\n".join(pattern_lines),
    )


def _strategy_escape_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 5: convert escape sequences to actual characters."""
    def unescape(s: str) -> str:
        return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

    pattern_unescaped = unescape(pattern)
    if pattern_unescaped == pattern:
        return []

    return _strategy_exact(content, pattern_unescaped)


def _strategy_trimmed_boundary(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 6: trim whitespace from first and last lines only."""
    pattern_lines = pattern.split("\n")
    if not pattern_lines:
        return []

    pattern_lines[0] = pattern_lines[0].strip()
    if len(pattern_lines) > 1:
        pattern_lines[-1] = pattern_lines[-1].strip()

    modified_pattern = "\n".join(pattern_lines)

    content_lines = content.split("\n")

    matches = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i:i + pattern_line_count]

        check_lines = block_lines.copy()
        check_lines[0] = check_lines[0].strip()
        if len(check_lines) > 1:
            check_lines[-1] = check_lines[-1].strip()

        if "\n".join(check_lines) == modified_pattern:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _build_orig_to_norm_map(original: str) -> list[int]:
    """Map each original char index to its normalized index (UNICODE_MAP expands)."""
    result: list[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)  # sentinel: one past the last character
    return result


def _map_positions_norm_to_orig(
    orig_to_norm: list[int],
    norm_matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Convert (start, end) positions in the normalized string to original positions."""
    norm_to_orig_start: dict[int, int] = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm[:-1]):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos

    results: list[tuple[int, int]] = []
    orig_len = len(orig_to_norm) - 1

    for norm_start, norm_end in norm_matches:
        if norm_start not in norm_to_orig_start:
            continue
        orig_start = norm_to_orig_start[norm_start]

        orig_end = orig_start
        while orig_end < orig_len and orig_to_norm[orig_end] < norm_end:
            orig_end += 1

        results.append((orig_start, orig_end))

    return results


def _strategy_unicode_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 7: Unicode normalization (smart quotes/dashes/ellipsis → ASCII)."""
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []

    norm_matches = _strategy_exact(norm_content, norm_pattern)
    if not norm_matches:
        norm_matches = _strategy_line_trimmed(norm_content, norm_pattern)

    if not norm_matches:
        return []

    orig_to_norm = _build_orig_to_norm_map(content)
    return _map_positions_norm_to_orig(orig_to_norm, norm_matches)


def _strategy_block_anchor(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 8: anchor on first and last lines, similarity for the middle."""
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)

    pattern_lines = norm_pattern.split("\n")
    if len(pattern_lines) < 2:
        return []

    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()

    norm_content_lines = norm_content.split("\n")
    orig_content_lines = content.split("\n")

    pattern_line_count = len(pattern_lines)

    potential_matches = []
    for i in range(len(norm_content_lines) - pattern_line_count + 1):
        if (norm_content_lines[i].strip() == first_line and
                norm_content_lines[i + pattern_line_count - 1].strip() == last_line):
            potential_matches.append(i)

    matches = []
    candidate_count = len(potential_matches)

    threshold = 0.50 if candidate_count == 1 else 0.70

    for i in potential_matches:
        if pattern_line_count <= 2:
            similarity = 1.0
        else:
            content_middle = "\n".join(norm_content_lines[i + 1:i + pattern_line_count - 1])
            pattern_middle = "\n".join(pattern_lines[1:-1])
            similarity = SequenceMatcher(None, content_middle, pattern_middle).ratio()

        if similarity >= threshold:
            start_pos, end_pos = _calculate_line_positions(
                orig_content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _strategy_context_aware(content: str, pattern: str) -> list[tuple[int, int]]:
    """Strategy 9: line-by-line similarity with a 50% threshold."""
    pattern_lines = pattern.split("\n")
    content_lines = content.split("\n")

    if not pattern_lines:
        return []

    matches = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i:i + pattern_line_count]

        high_similarity_count = 0
        for p_line, c_line in zip(pattern_lines, block_lines):
            sim = SequenceMatcher(None, p_line.strip(), c_line.strip()).ratio()
            if sim >= 0.80:
                high_similarity_count += 1

        if high_similarity_count >= len(pattern_lines) * 0.5:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


# =============================================================================
# Helper functions
# =============================================================================

def _calculate_line_positions(
    content_lines: list[str],
    start_line: int,
    end_line: int,
    content_length: int,
) -> tuple[int, int]:
    """Calculate start/end character positions from line indices."""
    start_pos = sum(len(line) + 1 for line in content_lines[:start_line])
    end_pos = sum(len(line) + 1 for line in content_lines[:end_line]) - 1
    end_pos = min(content_length, end_pos)
    return start_pos, end_pos


def _find_normalized_matches(
    content: str,
    content_lines: list[str],
    content_normalized_lines: list[str],
    pattern: str,
    pattern_normalized: str,
) -> list[tuple[int, int]]:
    """Find matches in normalized content and map back to original positions."""
    pattern_norm_lines = pattern_normalized.split("\n")
    num_pattern_lines = len(pattern_norm_lines)

    matches = []

    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        block = "\n".join(content_normalized_lines[i:i + num_pattern_lines])

        if block == pattern_normalized:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + num_pattern_lines, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _map_normalized_positions(
    original: str,
    normalized: str,
    normalized_matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Map positions from a normalized string back to the original (best-effort)."""
    if not normalized_matches:
        return []

    orig_to_norm = []

    orig_idx = 0
    norm_idx = 0

    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in " \t" and normalized[norm_idx] == " ":
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in " \t":
                norm_idx += 1
        elif original[orig_idx] in " \t":
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            orig_to_norm.append(norm_idx)
            orig_idx += 1

    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1

    norm_to_orig_start: dict[int, int] = {}
    norm_to_orig_end: dict[int, int] = {}

    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos

    original_matches = []
    for norm_start, norm_end in normalized_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)

        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)

        while orig_end < len(original) and original[orig_end] in " \t":
            orig_end += 1

        original_matches.append((orig_start, min(orig_end, len(original))))

    return original_matches


def find_closest_lines(
    old_string: str,
    content: str,
    context_lines: int = 2,
    max_results: int = 3,
) -> str:
    """Find lines in content most similar to old_string for 'did you mean?' feedback."""
    if not old_string or not content:
        return ""

    old_lines = old_string.splitlines()
    content_lines = content.splitlines()

    if not old_lines or not content_lines:
        return ""

    anchor = old_lines[0].strip()
    if not anchor:
        candidates = [line.strip() for line in old_lines if line.strip()]
        if not candidates:
            return ""
        anchor = candidates[0]

    scored = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = SequenceMatcher(None, anchor, stripped).ratio()
        if ratio > 0.3:
            scored.append((ratio, i))

    if not scored:
        return ""

    scored.sort(key=lambda x: -x[0])
    top = scored[:max_results]

    parts = []
    seen_ranges = set()
    for _, line_idx in top:
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = "\n".join(
            f"{start + j + 1:4d}| {content_lines[start + j]}"
            for j in range(end - start)
        )
        parts.append(snippet)

    if not parts:
        return ""

    return "\n---\n".join(parts)


def format_no_match_hint(
    error: str | None,
    match_count: int,
    old_string: str,
    content: str,
) -> str:
    """Return a '\\n\\nDid you mean...' snippet for plain no-match errors."""
    if match_count != 0:
        return ""
    if not error or not error.startswith("Could not find"):
        return ""
    hint = find_closest_lines(old_string, content)
    if not hint:
        return ""
    return "\n\nDid you mean one of these sections?\n" + hint


__all__ = [
    "find_closest_lines",
    "format_no_match_hint",
    "fuzzy_find_and_replace",
]
