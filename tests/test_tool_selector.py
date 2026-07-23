"""tool_selector 单元测试: 工具目录构建 + JSON 解析 + 选用逻辑 + 降级 + 再选"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import json
from unittest.mock import MagicMock

from agents.tool_selector import build_tool_catalog, _parse_tool_names, select_tools
from prompts import DETAIL_EDIT_BASE_TOOLS
from docx_tools import TOOLS


# === Mock LLM ===

class MockLLM:
    """Mock LLMClientAdapter — 返回预设的 content"""
    def __init__(self, response_content: str = "[]"):
        self._content = response_content

    def create_chat_completion_blocking(self, *, messages, tools=None, **kwargs):
        choice = MagicMock()
        choice.message.content = self._content
        response = MagicMock()
        response.choices = [choice]
        return response

    def get_provider(self): return "test"
    def get_model_name(self): return "mock"


# === build_tool_catalog ===

def test_catalog_excludes_base_tools():
    """基础工具不出现在目录中"""
    catalog = build_tool_catalog()
    for name in DETAIL_EDIT_BASE_TOOLS:
        # request_more_tools 不在 TOOLS_SCHEMA, 本来就不会出现
        if name == "request_more_tools":
            continue
        assert f"- {name}:" not in catalog, f"{name} 不应出现在工具目录中"


def test_catalog_includes_write_tools():
    """写入类工具出现在目录中"""
    catalog = build_tool_catalog()
    assert "- replace_text:" in catalog
    assert "- insert_text_at:" in catalog
    assert "- set_text_format:" in catalog


def test_catalog_line_format():
    """目录包含工具名 + 描述"""
    catalog = build_tool_catalog()
    # 至少包含几个已知工具
    assert "- replace_text:" in catalog
    assert "- insert_text_at:" in catalog
    # 非空
    assert len(catalog.strip()) > 0


# === _parse_tool_names ===

def test_parse_pure_json():
    assert _parse_tool_names('["a", "b"]') == ["a", "b"]


def test_parse_markdown_wrapped():
    text = '```json\n["replace_text", "find_text"]\n```'
    assert _parse_tool_names(text) == ["replace_text", "find_text"]


def test_parse_with_surrounding_text():
    text = '我建议选择以下工具：\n["replace_text", "set_text_format"]\n以上。'
    assert _parse_tool_names(text) == ["replace_text", "set_text_format"]


def test_parse_empty_array():
    assert _parse_tool_names("[]") == []


def test_parse_invalid_json():
    assert _parse_tool_names("这不是 JSON") == []


def test_parse_non_list_json():
    assert _parse_tool_names('{"key": "value"}') == []


# === select_tools ===

def test_select_tools_basic():
    """基本选用: LLM 返回有效工具名"""
    llm = MockLLM('["replace_text", "set_text_format"]')
    result = select_tools(llm, "替换标题文字", '{"paragraphs": []}')
    # 应包含选用的工具 + 基础工具
    assert "replace_text" in result
    assert "set_text_format" in result
    for base in DETAIL_EDIT_BASE_TOOLS:
        assert base in result, f"基础工具 {base} 应始终包含"


def test_select_tools_filters_invalid_names():
    """过滤不存在的工具名"""
    llm = MockLLM('["replace_text", "nonexistent_tool", "set_text_format"]')
    result = select_tools(llm, "测试", "{}")
    assert "replace_text" in result
    assert "set_text_format" in result
    assert "nonexistent_tool" not in result


def test_select_tools_degrades_on_bad_json():
    """LLM 返回非 JSON → 降级为基础工具集"""
    llm = MockLLM("我无法理解你的请求")
    result = select_tools(llm, "测试", "{}")
    assert set(result) == DETAIL_EDIT_BASE_TOOLS


def test_select_tools_degrades_on_llm_error():
    """LLM 调用异常 → 降级为基础工具集"""
    llm = MagicMock()
    llm.create_chat_completion_blocking.side_effect = RuntimeError("API error")
    result = select_tools(llm, "测试", "{}")
    assert set(result) == DETAIL_EDIT_BASE_TOOLS


def test_select_tools_reselect_only_adds():
    """再选时只增不减"""
    current = {"replace_text", "set_text_format"} | DETAIL_EDIT_BASE_TOOLS
    # LLM 返回一个新工具 + 一个已有工具
    llm = MockLLM('["insert_paragraph_after", "replace_text"]')
    result = select_tools(
        llm, "还需要插入段落", "{}",
        current_tools=current,
        reason="需要插入新段落",
    )
    # 新工具加入
    assert "insert_paragraph_after" in result
    # 旧工具不移除
    assert "replace_text" in result
    assert "set_text_format" in result
    # 基础工具仍在
    for base in DETAIL_EDIT_BASE_TOOLS:
        assert base in result


def test_select_tools_result_is_sorted():
    """返回值是排序的列表"""
    llm = MockLLM('["set_text_format", "replace_text"]')
    result = select_tools(llm, "测试", "{}")
    assert result == sorted(result)


def test_select_tools_all_names_valid():
    """返回的所有工具名都在 TOOLS dict 中"""
    llm = MockLLM('["replace_text", "insert_text_at", "diff_docx"]')
    result = select_tools(llm, "测试", "{}")
    for name in result:
        assert name in TOOLS or name in DETAIL_EDIT_BASE_TOOLS, f"{name} 不在 TOOLS 中"
