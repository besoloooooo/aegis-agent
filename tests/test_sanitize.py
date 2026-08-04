"""Tests for models/sanitize.py — surrogate scrubbing + tool-call arg repair."""

from __future__ import annotations

import json

from aegis_agent.models.sanitize import repair_tool_call_arguments, sanitize_surrogates


class TestSanitizeSurrogates:
    def test_clean_text_unchanged(self):
        assert sanitize_surrogates("hello 你好") == "hello 你好"

    def test_empty(self):
        assert sanitize_surrogates("") == ""

    def test_low_surrogate_replaced(self):
        assert sanitize_surrogates("a\udce5b") == "a�b"

    def test_high_surrogate_replaced(self):
        assert sanitize_surrogates("a\ud800b") == "a�b"

    def test_multiple_replaced(self):
        assert sanitize_surrogates("\udce5\ud800") == "��"


class TestRepairToolCallArguments:
    def test_valid_json_passes_through_unchanged(self):
        # Pretty-printed (spaced) JSON is valid — returned verbatim, not compacted.
        assert repair_tool_call_arguments('{"path": "a"}', "t") == '{"path": "a"}'

    def test_valid_json_does_not_warn(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="aegis_agent.models.sanitize"):
            repair_tool_call_arguments('{"path": "."}', "read_file")
        assert caplog.records == []  # a formatting difference is not a repair

    def test_control_char_repair_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="aegis_agent.models.sanitize"):
            repair_tool_call_arguments('{"cmd": "ls\t-la"}', "run_shell")
        assert any("Repaired unescaped control chars" in r.message for r in caplog.records)

    def test_empty_becomes_object(self):
        assert repair_tool_call_arguments("", "t") == "{}"
        assert repair_tool_call_arguments("   ", "t") == "{}"

    def test_python_none_becomes_object(self):
        assert repair_tool_call_arguments("None", "t") == "{}"

    def test_trailing_comma_removed(self):
        assert json.loads(repair_tool_call_arguments('{"a": 1,}', "t")) == {"a": 1}

    def test_unclosed_braces_closed(self):
        assert json.loads(repair_tool_call_arguments('{"a": {"b": 1}', "t")) == {"a": {"b": 1}}

    def test_unclosed_top_level_bracket_closed(self):
        assert json.loads(repair_tool_call_arguments("[1, 2", "t")) == [1, 2]

    def test_mixed_unclosed_falls_back(self):
        # Hermes' algorithm closes } before ], so a mixed-unclosed payload
        # like this one is unrepairable and falls back to the empty object.
        assert repair_tool_call_arguments('{"a": [1, 2', "t") == "{}"

    def test_literal_control_chars_in_strings(self):
        # A literal tab inside a JSON string value (llama.cpp-style output).
        repaired = repair_tool_call_arguments('{"cmd": "ls\t-la"}', "t")
        assert json.loads(repaired) == {"cmd": "ls\t-la"}

    def test_garbage_falls_back_to_empty_object(self):
        assert repair_tool_call_arguments("not json at all {{{", "t") == "{}"

    def test_non_dict_json_passes_through(self):
        # Valid JSON that is not an object is preserved for the caller to reject.
        assert json.loads(repair_tool_call_arguments("[1, 2]", "t")) == [1, 2]
