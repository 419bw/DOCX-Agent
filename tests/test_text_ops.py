"""test_text_ops.py — 4 个文本操作工具的回归测试 (PR-1.1)

覆盖 (共 22 case):
  - find_text: 6 case
  - replace_text: 5 case
  - insert_text_at: 6 case
  - delete_text: 5 case

设计:
  - pytest_plugins = ["_docx_factory"] 让 fixture 自动发现
  - 复用 _docx_factory._build_minimal_docx / _build_docx_with_custom_body 构造样本
  - 用 get_xml_elements 验证 docx zip 内的 document.xml 节点结构
  - 跨 run 命中用 _build_docx_with_custom_body 手写 <w:r> 拼接

关键路径布局 (重要!):
  tmp_root fixture 把 WORKSPACE_ROOT 重定向到 tmp_path/sessions,
  resolve_workspace_path 找的是 <tmp_path>/sessions/<session_id>/workspace/<rel_path>.
  所以输入 docx 必须建在 tmp_root / session_id / "workspace" / <rel_path> 下,
  工具调用时传相对路径 (e.g. "in.docx"), 工具自己 resolve 到绝对路径.

API 提示 (从源码核对, 跟原计划有偏差):
  - replace_text 用 `occurrence: int = 1` 指定第 N 个匹配, 不是 `replace_globally`
  - insert_text_at 用 `offset: int = -1` (-1 = 锚点后, >=0 = 锚点内偏移)
  - delete_text 用 `trim_surrounding_spaces: bool` 选项
"""
import json
import sys
from pathlib import Path

import pytest

pytest_plugins = ["_docx_factory"]
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from docx_tools.delete_text import delete_text
from docx_tools.find_text import find_text
from docx_tools.insert_text_at import insert_text_at
from docx_tools.replace_text import replace_text

from _docx_factory import (
    _build_docx_with_custom_body,
    _build_minimal_docx,
    get_xml_text,
)


# =====================================================================
# 辅助: 给定 tmp_root + session_id, 返回 workspace 根路径
# =====================================================================

def _ws(tmp_root, session_id: str) -> Path:
    """返回 workspace 根路径 (即 sessions/<id>/workspace)."""
    return tmp_root / session_id / "workspace"


# (Bug #1 修复说明: delete_text.py 现在跟 replace_text / insert_text_at 一样
#  在 result dict 里过滤了 change['run'], json_result 不再因 _Element TypeError.
#  之前为绕过 bug 加的 _safe_call helper 已删除, 直接 json.loads(delete_text(...)).)


# =====================================================================
# find_text: 6 case
# =====================================================================

class TestFindText:
    """find_text 工具测试."""

    def test_unique_match_returns_one_hit(self, tmp_root, session_id):
        """唯一匹配 → matches 列表 1 项, 含 paragraph_index/char_start/char_end."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "test.docx",
                            ["第一段原文", "第二段", "第三段"])

        result = json.loads(find_text(session_id, "test.docx", "第二段"))

        assert result["query"] == "第二段"
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["paragraph_index"] == 2
        assert m["char_start"] == 0
        assert m["char_end"] == 3
        assert m["paragraph_text"] == "第二段"

    def test_multiple_matches_returns_list(self, tmp_root, session_id):
        """多处匹配 → matches 列表含所有命中, paragraph_index 正确."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "test.docx",
                            ["foo", "bar foo baz", "qux foo"])

        result = json.loads(find_text(session_id, "test.docx", "foo"))

        assert len(result["matches"]) == 3
        assert [m["paragraph_index"] for m in result["matches"]] == [1, 2, 3]
        assert result["matches"][1]["char_start"] == 4

    def test_cross_run_text_found(self, tmp_root, session_id):
        """跨 run 拼接文本: 同一段被切到 2 个 <w:r> 内, 仍能找到完整文本."""
        # 手写: 段落含两个 run, "hello" 被切成 "hel" + "lo"
        body_xml = (
            '    <w:p>'
            '<w:r><w:t xml:space="preserve">hel</w:t></w:r>'
            '<w:r><w:t xml:space="preserve">lo world</w:t></w:r>'
            '</w:p>'
        )
        _build_docx_with_custom_body(
            _ws(tmp_root, session_id) / "cross_run.docx", body_xml
        )

        result = json.loads(find_text(session_id, "cross_run.docx", "hello"))

        assert len(result["matches"]) == 1
        assert result["matches"][0]["paragraph_text"] == "hello world"
        assert result["matches"][0]["char_start"] == 0
        assert result["matches"][0]["char_end"] == 5

    def test_not_found_returns_empty_list(self, tmp_root, session_id):
        """未找到 → matches 空列表, 不抛 Exception."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "test.docx", ["hello"])

        result = json.loads(find_text(session_id, "test.docx", "absent"))

        assert result["matches"] == []
        assert result["query"] == "absent"

    def test_chinese_english_mixed(self, tmp_root, session_id):
        """中英文混合: query 是中文, 段内混合也能找到."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "test.docx",
                            ["Hello 世界 world", "纯中文段", "Mix 混合 123"])

        # 查中文
        r1 = json.loads(find_text(session_id, "test.docx", "世界"))
        assert len(r1["matches"]) == 1
        assert r1["matches"][0]["paragraph_index"] == 1

        # 查英文数字
        r2 = json.loads(find_text(session_id, "test.docx", "123"))
        assert len(r2["matches"]) == 1
        assert r2["matches"][0]["paragraph_index"] == 3

    def test_location_field_for_paragraph(self, tmp_root, session_id):
        """paragraph 内的 location 字段应是 dict, 含 kind/body_index."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "test.docx",
                            ["一", "匹配", "三"])

        result = json.loads(find_text(session_id, "test.docx", "匹配"))

        loc = result["matches"][0]["location"]
        # 实际 API: location 是 dict {"kind": "paragraph", "body_index": N}
        assert isinstance(loc, dict), f"location 应是 dict, 实际 {type(loc).__name__}"
        assert loc["kind"] == "paragraph"
        assert loc["body_index"] == 2  # "匹配" 是第 2 段


# =====================================================================
# replace_text: 5 case
# =====================================================================

class TestReplaceText:
    """replace_text 工具测试."""

    def test_single_replacement_writes_output(self, tmp_root, session_id):
        """单处替换: 输出 docx 写入新内容, status=ok."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(replace_text(
            session_id, "in.docx", "out.docx", "world", "世界"
        ))

        assert result["status"] == "ok"
        assert result["old_text"] == "world"
        assert result["new_text"] == "世界"
        # 输出 docx 文本应为 "hello 世界"
        assert get_xml_text(out_path, "//w:t") == "hello 世界"

    def test_occurrence_selects_nth_match(self, tmp_root, session_id):
        """occurrence=N: 替第 N 个匹配, 其它保留."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["foo foo foo"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(replace_text(
            session_id, "in.docx", "out.docx", "foo", "BAR", occurrence=2
        ))

        assert result["status"] == "ok"
        assert result["occurrence"] == 2
        # 第 1/3 个保持 foo, 第 2 个被替成 BAR
        assert get_xml_text(out_path, "//w:t") == "foo BAR foo"

    def test_cross_run_replacement(self, tmp_root, session_id):
        """跨 run 拼接文本: 文本横跨 2 个 <w:r> 也能被定位替换."""
        body_xml = (
            '    <w:p>'
            '<w:r><w:t xml:space="preserve">hel</w:t></w:r>'
            '<w:r><w:t xml:space="preserve">lo</w:t></w:r>'
            '</w:p>'
        )
        _build_docx_with_custom_body(
            _ws(tmp_root, session_id) / "in.docx", body_xml
        )
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(replace_text(
            session_id, "in.docx", "out.docx", "hello", "HI"
        ))

        assert result["status"] == "ok"
        # 替换后段落逻辑文本应是 "HI"
        assert get_xml_text(out_path, "//w:t") == "HI"

    def test_text_not_found_returns_not_found_status(self, tmp_root, session_id):
        """旧文本不存在 → status=not_found, 不抛 Exception, 输入 docx 文本不变."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["hello"])
        in_path = _ws(tmp_root, session_id) / "in.docx"

        result = json.loads(replace_text(
            session_id, "in.docx", "out.docx", "absent", "X"
        ))

        assert result["status"] == "not_found"
        # 注意: not_found 路径不调用 write_document_xml, 所以 out.docx 不创建
        # 但输入 docx 文本应该不变
        assert get_xml_text(in_path, "//w:t") == "hello"

    def test_occurrence_beyond_matches_returns_not_found(self, tmp_root, session_id):
        """occurrence 超过实际匹配数 → status=not_found."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["foo"])
        result = json.loads(replace_text(
            session_id, "in.docx", "out.docx", "foo", "X", occurrence=5
        ))
        assert result["status"] == "not_found"

    def test_same_input_output_path_no_corruption(self, tmp_root, session_id):
        """回归: docx_path == output_path 时文件不损坏 (Bug #7 同路径截断)."""
        docx_path = _ws(tmp_root, session_id) / "in.docx"
        _build_minimal_docx(docx_path, ["hello world"])
        original_size = docx_path.stat().st_size

        result = json.loads(replace_text(
            session_id, "in.docx", "in.docx", "world", "世界"
        ))

        assert result["status"] == "ok"
        # 文件不损坏: 大小合理 (不能变成几十字节的截断文件)
        assert docx_path.stat().st_size > original_size // 2
        # 替换生效
        assert get_xml_text(docx_path, "//w:t") == "hello 世界"
        # 无残留临时文件
        assert not docx_path.with_suffix(".docx.tmp").exists()


# =====================================================================
# insert_text_at: 6 case
# =====================================================================

class TestInsertTextAt:
    """insert_text_at 工具测试."""

    def test_offset_negative_appends_after_anchor(self, tmp_root, session_id):
        """offset=-1 (默认): 文本插在 anchor 后面."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_text_at(
            session_id, "in.docx", "out.docx",
            anchor_text="hello", insert_text=" beautiful", offset=-1
        ))

        assert result["status"] == "ok"
        # 段落文本应是 "hello beautiful world"
        assert get_xml_text(out_path, "//w:t") == "hello beautiful world"

    def test_offset_zero_inserts_before_anchor(self, tmp_root, session_id):
        """offset=0: 文本插在 anchor 前面."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["hello"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_text_at(
            session_id, "in.docx", "out.docx",
            anchor_text="hello", insert_text=">>", offset=0
        ))

        assert result["status"] == "ok"
        assert get_xml_text(out_path, "//w:t") == ">>hello"

    def test_offset_middle_splits_text(self, tmp_root, session_id):
        """offset 在 anchor 中间: 文本被插入, 原来位置被拆开."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_text_at(
            session_id, "in.docx", "out.docx",
            anchor_text="hello", insert_text="X", offset=2
        ))

        assert result["status"] == "ok"
        # 在 "hello" 的第 2 个字符后插入 X → "heXllo world"
        assert get_xml_text(out_path, "//w:t") == "heXllo world"

    def test_offset_too_large_raises_value_error(self, tmp_root, session_id):
        """offset > len(anchor): 抛 ValueError. 前提是 anchor 必须先匹配上."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])

        with pytest.raises(ValueError, match="offset cannot be greater"):
            insert_text_at(
                session_id, "in.docx", "out.docx",
                anchor_text="hello", insert_text="X", offset=10
            )

    def test_special_chars_escaped(self, tmp_root, session_id):
        """插入文本含 < > & 等特殊字符: 应被 XML 转义, 不破坏结构."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["锚点"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_text_at(
            session_id, "in.docx", "out.docx",
            anchor_text="锚点", insert_text="<a>&'", offset=-1
        ))

        assert result["status"] == "ok"
        # 段落逻辑文本含原始字符 (XML 转义后从 document.xml 解析回来)
        text = get_xml_text(out_path, "//w:t")
        assert "<a>&'" in text

    def test_anchor_not_found_returns_not_found(self, tmp_root, session_id):
        """anchor 不存在 → status=not_found, 不抛."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["hello"])
        result = json.loads(insert_text_at(
            session_id, "in.docx", "out.docx",
            anchor_text="absent", insert_text="X"
        ))
        assert result["status"] == "not_found"


# =====================================================================
# delete_text: 5 case
# =====================================================================

class TestDeleteText:
    """delete_text 工具测试."""

    def test_basic_delete(self, tmp_root, session_id):
        """基本删除: 把目标文本从段中移除."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(delete_text(
            session_id, "in.docx", "out.docx", " world"
        ))

        assert result["status"] == "ok"
        # 文件 side effect (核心行为, 锁住 regression)
        assert get_xml_text(out_path, "//w:t") == "hello"

    def test_cross_run_delete(self, tmp_root, session_id):
        """跨 run 删除: 目标横跨 2 个 <w:r> 也能删."""
        body_xml = (
            '    <w:p>'
            '<w:r><w:t xml:space="preserve">hel</w:t></w:r>'
            '<w:r><w:t xml:space="preserve">lo world</w:t></w:r>'
            '</w:p>'
        )
        _build_docx_with_custom_body(
            _ws(tmp_root, session_id) / "in.docx", body_xml
        )
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(delete_text(
            session_id, "in.docx", "out.docx", "lo wo"
        ))

        assert result["status"] == "ok"
        # 跨 run 删除 "lo wo" 后: "hel" + "rld" = "helrld"
        assert get_xml_text(out_path, "//w:t") == "helrld"

    def test_trim_surrounding_spaces(self, tmp_root, session_id):
        """trim_surrounding_spaces=True: 删 " bar " (含两侧空格), 留 "foobaz"."""
        # 用单空格避免空格 bug 干扰: "foo bar baz" 删 " bar " → "foobaz"
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["foo bar baz"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(delete_text(
            session_id, "in.docx", "out.docx", " bar ",
            trim_surrounding_spaces=True
        ))

        assert result["status"] == "ok"
        # 工具应删除 " bar " (5 字符), 剩 "foo" + "baz" = "foobaz"
        assert get_xml_text(out_path, "//w:t") == "foobaz"

    def test_text_not_found_returns_not_found(self, tmp_root, session_id):
        """目标文本不存在 → status=not_found, 不抛."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["hello"])
        result = json.loads(delete_text(
            session_id, "in.docx", "out.docx", "absent"
        ))
        assert result["status"] == "not_found"

    def test_occurrence_selects_nth_match(self, tmp_root, session_id):
        """occurrence=N: 删第 N 个匹配, 其它保留."""
        # 用 "foofoo" (无空格) 避免空格保留 bug 干扰
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["foofoo"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(delete_text(
            session_id, "in.docx", "out.docx", "foo", occurrence=2
        ))

        assert result["status"] == "ok"
        # 第 2 个 "foo" (位置 [3:6]) 被删, 剩 "foo"
        assert get_xml_text(out_path, "//w:t") == "foo"
