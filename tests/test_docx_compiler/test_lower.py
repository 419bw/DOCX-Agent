"""test_lower.py — docx_compiler/lower.py 内联解析测试

_parse_inline_runs(text) -> list[RunIR] 负责把 Markdown 内联标记
拆成带格式标志的 RunIR 序列。

覆盖:
  1. 纯文本 → 单个 text run
  2. **bold** → bold run
  3. `code` → code run
  4. `code **inside**` → code run，** 保留为字面文本
  5. 未闭合 ` → 普通文本
  6. 未闭合 ** → 普通文本
  7. **bold** 和 `code` 混合
  8. 多个 code span
  9. 空字符串
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from docx_compiler.ir import RunIR
from docx_compiler.lower import _parse_inline_runs


class TestParseInlineRuns:
    """_parse_inline_runs: Markdown 内联标记 → RunIR 列表."""

    def test_plain_text(self):
        runs = _parse_inline_runs("hello world")
        assert runs == [RunIR.text_run("hello world")]

    def test_bold(self):
        runs = _parse_inline_runs("**bold**")
        assert runs == [RunIR.text_run("bold", bold=True)]

    def test_bold_with_surrounding_text(self):
        runs = _parse_inline_runs("a **bold** b")
        assert runs == [
            RunIR.text_run("a "),
            RunIR.text_run("bold", bold=True),
            RunIR.text_run(" b"),
        ]

    def test_code_span(self):
        runs = _parse_inline_runs("`src/prompts.py`")
        assert runs == [RunIR.text_run("src/prompts.py", code=True)]

    def test_code_span_with_surrounding_text(self):
        runs = _parse_inline_runs("use `pip install` to install")
        assert runs == [
            RunIR.text_run("use "),
            RunIR.text_run("pip install", code=True),
            RunIR.text_run(" to install"),
        ]

    def test_code_span_preserves_bold_markers(self):
        runs = _parse_inline_runs("`code **inside**`")
        assert runs == [RunIR.text_run("code **inside**", code=True)]

    def test_unclosed_backtick(self):
        runs = _parse_inline_runs("text `unclosed")
        assert all(not r.code and not r.bold for r in runs)
        assert "".join(r.text for r in runs) == "text `unclosed"

    def test_unclosed_bold(self):
        runs = _parse_inline_runs("text **unclosed")
        assert all(not r.bold and not r.code for r in runs)
        assert "".join(r.text for r in runs) == "text **unclosed"

    def test_bold_and_code_mixed(self):
        runs = _parse_inline_runs("**bold** and `code`")
        assert runs == [
            RunIR.text_run("bold", bold=True),
            RunIR.text_run(" and "),
            RunIR.text_run("code", code=True),
        ]

    def test_multiple_code_spans(self):
        runs = _parse_inline_runs("`a` and `b`")
        assert runs == [
            RunIR.text_run("a", code=True),
            RunIR.text_run(" and "),
            RunIR.text_run("b", code=True),
        ]

    def test_empty_string(self):
        runs = _parse_inline_runs("")
        assert runs == []

    def test_backtick_before_bold(self):
        runs = _parse_inline_runs("`code` then **bold**")
        assert runs == [
            RunIR.text_run("code", code=True),
            RunIR.text_run(" then "),
            RunIR.text_run("bold", bold=True),
        ]

    def test_bold_before_backtick(self):
        runs = _parse_inline_runs("**bold** then `code`")
        assert runs == [
            RunIR.text_run("bold", bold=True),
            RunIR.text_run(" then "),
            RunIR.text_run("code", code=True),
        ]
