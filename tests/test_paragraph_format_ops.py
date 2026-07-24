"""test_paragraph_format_ops.py — 段落 / 格式 3 工具的回归测试 (PR-1.2)

覆盖 (共 14 case, 其中 5 个 xfail 因 Bug #2):
  - insert_paragraph_after: 4 case
  - set_text_format:        5 case
  - set_paragraph_indent:   5 case (全部 @pytest.mark.xfail, 详见 BUGS.md Bug #2)

设计:
  - 复用 PR-1.0 抽出的 _docx_factory.py
  - 输入 docx 建在 tmp_root / session_id / "workspace" 下
  - 工具调用传相对路径
  - 用 get_xml_elements / get_xml_attr 验证输出 docx 的节点结构
  - 跨 run / 含特定格式 用 _build_docx_with_custom_body 手写 XML

API 提示 (从源码核对, 跟原计划有偏差):
  - set_paragraph_indent 用 paragraph_index (1-based) + 三个 *twips* 参数,
    不是 *chars* 参数. 计划写错, 按实际 API 测.
  - set_text_format 支持 color / bold / italic / underline / font_name / font_size (半磅或磅).
  - 三个工具的 JSON result 都不含 lxml 元素, 没有 Bug #1 那种序列化问题.
"""
import json
import sys
from pathlib import Path

import pytest

pytest_plugins = ["_docx_factory"]
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from docx_tools.insert_paragraph_after import insert_paragraph_after
from docx_tools.set_paragraph_indent import set_paragraph_indent
from docx_tools.set_text_format import set_text_format

from _docx_factory import (
    _build_docx_with_custom_body,
    _build_minimal_docx,
    get_xml_attr,
    get_xml_elements,
    get_xml_text,
)


def _ws(tmp_root, session_id: str) -> Path:
    """workspace 根路径 (sandbox 内 sessions/<id>/workspace)."""
    return tmp_root / session_id / "workspace"


# =====================================================================
# insert_paragraph_after: 4 case
# =====================================================================

class TestInsertParagraphAfter:
    """insert_paragraph_after 工具测试."""

    def test_basic_insert_after_anchor_paragraph(self, tmp_root, session_id):
        """基本用法: 锚点段后插新段, 新段文本正确, 段数 +1."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["anchor text", "second"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_paragraph_after(
            session_id, "in.docx", "out.docx",
            anchor_text="anchor", new_text="NEW"
        ))

        assert result["status"] == "ok"
        assert result["inserted_paragraph_count"] == 1
        # 输出 docx 应有 3 段: anchor, NEW, second
        paras = get_xml_elements(out_path, "//w:p")
        assert len(paras) == 3
        # 第 2 段文本应是 NEW
        assert get_xml_text(out_path, "//w:p[2]//w:t") == "NEW"

    def test_anchor_not_found_returns_not_found(self, tmp_root, session_id):
        """锚点文本不存在 → status=not_found, 不抛."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello"])
        result = json.loads(insert_paragraph_after(
            session_id, "in.docx", "out.docx",
            anchor_text="absent", new_text="X"
        ))
        assert result["status"] == "not_found"

    def test_occurrence_selects_nth_anchor_match(self, tmp_root, session_id):
        """occurrence=N: 第 N 个匹配的锚点段后插段."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["first anchor", "second anchor", "third"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_paragraph_after(
            session_id, "in.docx", "out.docx",
            anchor_text="anchor", new_text="INS", occurrence=2
        ))

        assert result["status"] == "ok"
        # 输出 4 段: first anchor, second anchor, INS, third
        paras_text = get_xml_text(out_path, "//w:t")
        assert paras_text == "first anchorsecond anchorINSthird"

    def test_newline_mode_splits_into_multiple_paragraphs(self, tmp_root, session_id):
        """newline_mode=paragraphs: new_text 含 \\n 时拆成多个连续段."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["anchor"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(insert_paragraph_after(
            session_id, "in.docx", "out.docx",
            anchor_text="anchor", new_text="LINE1\nLINE2",
            newline_mode="paragraphs"
        ))

        assert result["status"] == "ok"
        assert result["inserted_paragraph_count"] == 2
        # 输出 3 段: anchor, LINE1, LINE2
        paras = get_xml_elements(out_path, "//w:p")
        assert len(paras) == 3
        assert get_xml_text(out_path, "//w:p[2]//w:t") == "LINE1"
        assert get_xml_text(out_path, "//w:p[3]//w:t") == "LINE2"


# =====================================================================
# set_text_format: 5 case
# =====================================================================

class TestSetTextFormat:
    """set_text_format 工具测试."""

    def test_bold_true_adds_w_b_to_target_run(self, tmp_root, session_id):
        """bold=True: 目标 run 出现 <w:b/> 元素, 其它 run 没有."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="hello", bold=True
        ))

        assert result["status"] == "ok"
        assert result["formatted_run_count"] >= 1
        # 目标 run 应含 <w:b/>
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'hello')]]")
        assert len(target_runs) >= 1
        assert any(
            r.xpath(".//w:b", namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"})
            for r in target_runs
        ), "目标 run 应含 <w:b/>"

    def test_font_size_pt_sets_w_sz(self, tmp_root, session_id):
        """font_size_pt=12: 目标 run 出现 <w:sz w:val="24"/> (12pt = 24 半磅)."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="world", font_size_pt=12.0
        ))

        assert result["status"] == "ok"
        # 目标 run 的 rPr 应有 <w:sz w:val="24"/>
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'world')]]")
        assert len(target_runs) >= 1
        sz = target_runs[0].xpath(
            ".//w:sz/@w:val",
            namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
        )
        assert "24" in sz, f"<w:sz w:val='24'/> 应在 world run 中, 实际 {sz}"

    def test_color_hex_sets_w_color(self, tmp_root, session_id):
        """color='FF0000': 目标 run 出现 <w:color w:val="FF0000"/>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["red text here"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="red", color="FF0000"
        ))

        assert result["status"] == "ok"
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'red')]]")
        assert len(target_runs) >= 1
        color_val = target_runs[0].xpath(
            ".//w:color/@w:val",
            namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
        )
        assert "FF0000" in color_val, f"<w:color w:val='FF0000'/> 应在 red run 中, 实际 {color_val}"

    def test_target_not_found_returns_not_found(self, tmp_root, session_id):
        """目标文本不存在 → status=not_found."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx", ["hello"])
        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="absent", bold=True
        ))
        assert result["status"] == "not_found"

    def test_format_policy_clear_omits_run_format(self, tmp_root, session_id):
        """format_policy='clear': 目标 run 不应有 <w:b/> (跟 custom 相反)."""
        # 输入段含 bold 段, 工具用 'clear' 应清掉
        body_xml = (
            '    <w:p>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">hello world</w:t></w:r>'
            '</w:p>'
        )
        _build_docx_with_custom_body(
            _ws(tmp_root, session_id) / "in.docx", body_xml
        )
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="world", format_policy="clear"
        ))

        assert result["status"] == "ok"
        # 'clear' 策略: 目标 run 不应有 <w:b/>
        target_runs = get_xml_elements(
            out_path, "//w:r[w:t[contains(text(), 'world')]]"
        )
        if target_runs:
            b_elems = target_runs[0].xpath(
                ".//w:b",
                namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
            )
            assert b_elems == [], f"'clear' 策略下目标 run 不应有 <w:b/>, 实际 {len(b_elems)} 个"

    def test_underline_true_adds_w_u(self, tmp_root, session_id):
        """underline=True: 目标 run 出现 <w:u w:val="single"/>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="hello", underline=True
        ))

        assert result["status"] == "ok"
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'hello')]]")
        assert len(target_runs) >= 1
        u_elems = target_runs[0].xpath(
            ".//w:u/@w:val",
            namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
        )
        assert "single" in u_elems, f"目标 run 应含 <w:u w:val='single'/>, 实际 {u_elems}"

    def test_italic_true_adds_w_i_and_iCs(self, tmp_root, session_id):
        """italic=True: 目标 run 同时出现 <w:i/> 和 <w:iCs/>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="world", italic=True
        ))

        assert result["status"] == "ok"
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'world')]]")
        assert len(target_runs) >= 1
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        assert target_runs[0].xpath(".//w:i", namespaces=ns), "目标 run 应含 <w:i/>"
        assert target_runs[0].xpath(".//w:iCs", namespaces=ns), "目标 run 应含 <w:iCs/>"

    def test_font_name_sets_rfonts(self, tmp_root, session_id):
        """font_name='楷体': 目标 run 的 rFonts ascii/hAnsi/eastAsia 全设为楷体."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="hello", font_name="楷体"
        ))

        assert result["status"] == "ok"
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'hello')]]")
        assert len(target_runs) >= 1
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        rfonts = target_runs[0].xpath(".//w:rFonts", namespaces=ns)
        assert len(rfonts) >= 1, "目标 run 应含 <w:rFonts>"
        rf = rfonts[0]
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        assert rf.get(f"{{{w}}}ascii") == "楷体"
        assert rf.get(f"{{{w}}}hAnsi") == "楷体"
        assert rf.get(f"{{{w}}}eastAsia") == "楷体"

    def test_combo_bold_italic_underline_color(self, tmp_root, session_id):
        """组合格式: bold + italic + underline + color 全部生效."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["hello world"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="hello",
            bold=True, italic=True, underline=True, color="FF0000"
        ))

        assert result["status"] == "ok"
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'hello')]]")
        assert len(target_runs) >= 1
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        r = target_runs[0]
        assert r.xpath(".//w:b", namespaces=ns), "应含 <w:b/>"
        assert r.xpath(".//w:i", namespaces=ns), "应含 <w:i/>"
        assert r.xpath(".//w:iCs", namespaces=ns), "应含 <w:iCs/>"
        u_val = r.xpath(".//w:u/@w:val", namespaces=ns)
        assert "single" in u_val, "应含 <w:u w:val='single'/>"
        color_val = r.xpath(".//w:color/@w:val", namespaces=ns)
        assert "FF0000" in color_val, "应含 <w:color w:val='FF0000'/>"

    def test_underline_false_removes_w_u(self, tmp_root, session_id):
        """underline=False: 已有下划线的 run 移除 <w:u>."""
        body_xml = (
            '    <w:p>'
            '<w:r><w:rPr><w:u w:val="single"/></w:rPr>'
            '<w:t xml:space="preserve">hello world</w:t></w:r>'
            '</w:p>'
        )
        _build_docx_with_custom_body(
            _ws(tmp_root, session_id) / "in.docx", body_xml
        )
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_text_format(
            session_id, "in.docx", "out.docx",
            target_text="hello", underline=False
        ))

        assert result["status"] == "ok"
        target_runs = get_xml_elements(out_path, "//w:r[w:t[contains(text(), 'hello')]]")
        if target_runs:
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            u_elems = target_runs[0].xpath(".//w:u", namespaces=ns)
            assert u_elems == [], f"underline=False 后不应有 <w:u/>, 实际 {len(u_elems)} 个"


# =====================================================================
# set_paragraph_indent: 5 case
# =====================================================================

class TestSetParagraphIndent:
    """set_paragraph_indent 工具测试.

    API 提示:
      - paragraph_index 是 1-based (按 //w:p 计数)
      - left_twips / first_line_twips / hanging_twips 单位是 twips (1/1440 英寸)
      - 全部 None → 写入 <w:pPr> 但不写 <w:ind> 元素 (或保持原样)
    """

    def test_left_indent_sets_w_ind_left(self, tmp_root, session_id):
        """left_twips=720: 输出段 <w:pPr> 含 <w:ind w:left="720"/>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["first", "second", "third"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_paragraph_indent(
            session_id, "in.docx", "out.docx",
            paragraph_index=2, left_twips=720
        ))

        assert result["status"] == "ok"
        left = get_xml_attr(out_path, "//w:p[2]/w:pPr/w:ind", "w:left")
        assert left == "720", f"w:left 应是 720, 实际 {left!r}"

    def test_first_line_indent_sets_w_ind_first_line(self, tmp_root, session_id):
        """first_line_twips=480: <w:ind w:firstLine="480"/>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["first", "second"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_paragraph_indent(
            session_id, "in.docx", "out.docx",
            paragraph_index=1, first_line_twips=480
        ))

        assert result["status"] == "ok"
        first_line = get_xml_attr(out_path, "//w:p[1]/w:pPr/w:ind", "w:firstLine")
        assert first_line == "480", f"w:firstLine 应是 480, 实际 {first_line!r}"

    def test_hanging_indent_sets_w_ind_hanging(self, tmp_root, session_id):
        """hanging_twips=240: <w:ind w:hanging="240"/>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["first", "second"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_paragraph_indent(
            session_id, "in.docx", "out.docx",
            paragraph_index=1, hanging_twips=240
        ))

        assert result["status"] == "ok"
        hanging = get_xml_attr(out_path, "//w:p[1]/w:pPr/w:ind", "w:hanging")
        assert hanging == "240", f"w:hanging 应是 240, 实际 {hanging!r}"

    def test_all_none_omits_w_ind(self, tmp_root, session_id):
        """三个 indent 字段全 None → 目标段 <w:pPr> 不写 <w:ind>."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["first", "second"])
        out_path = _ws(tmp_root, session_id) / "out.docx"

        result = json.loads(set_paragraph_indent(
            session_id, "in.docx", "out.docx",
            paragraph_index=1
        ))

        # 工具返回 status=ok, 但写入的 <w:pPr> 应不含 <w:ind>
        assert result["status"] == "ok"
        ind_elems = get_xml_elements(out_path, "//w:p[1]/w:pPr/w:ind")
        assert ind_elems == [], f"三 indent 全 None 时 <w:ind> 应不写, 实际 {len(ind_elems)} 个"

    def test_out_of_range_paragraph_index(self, tmp_root, session_id):
        """paragraph_index 越界 → status=error, 友好错误信息."""
        _build_minimal_docx(_ws(tmp_root, session_id) / "in.docx",
                            ["first"])
        result = json.loads(set_paragraph_indent(
            session_id, "in.docx", "out.docx",
            paragraph_index=99, left_twips=720
        ))
        assert result["status"] == "error"
        assert "message" in result
