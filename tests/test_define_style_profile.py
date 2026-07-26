"""test_define_style_profile.py — define_style_profile 工具测试

替代 bind_styles_to_roles：AI 直接定义各角色格式参数。
测试覆盖：
  1. 正常定义 → status=ok, style_profile.json 写入, 与 load_style_sample 兼容
  2. styles 为空 → status=error
  3. 非标准角色 → status=error
  4. 非法字段值 → status=error (alignment/highlight/color/shading_fill/font_size)
  5. fallback 推导 → 未定义角色继承 body
  6. shading_fill 写入 → apply_sample_paragraph_format 生成 w:shd
"""
import json
import sys
from pathlib import Path

import pytest

pytest_plugins = ["_docx_factory"]
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from docx_tools.define_style_profile import define_style_profile
from docx_tools.style_profile import FIXED_ROLES, derive_style_mapping_from_bindings, load_style_sample


def _ws(tmp_root, session_id: str) -> Path:
    return tmp_root / session_id / "workspace"


class TestDefineStyleProfile:
    def test_basic_define_writes_profile(self, tmp_root, session_id):
        """正常定义 body + title → status=ok, profile 写入, role_bindings 正确."""
        styles = {
            "body": {
                "format": {"font_east_asia": "宋体", "font_ascii": "Times New Roman", "font_size_half_points": "24"},
                "paragraph_format": {},
            },
            "title": {
                "format": {"font_east_asia": "黑体", "font_size_half_points": "32", "bold": True},
                "paragraph_format": {"alignment": "center"},
            },
        }
        result = json.loads(define_style_profile(session_id, styles))
        assert result["status"] == "ok"
        assert "style_profile_path" in result
        assert result["role_bindings"]["body"] == "S001"
        assert result["role_bindings"]["title"] == "S002"

        # 验证文件存在且可读
        profile_path = _ws(tmp_root, session_id) / result["style_profile_path"]
        assert profile_path.exists()
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        assert len(profile["style_samples"]) == 2

    def test_output_compatible_with_load_style_sample(self, tmp_root, session_id):
        """输出的 style_profile.json 可被 load_style_sample 正常读取."""
        styles = {
            "body": {
                "format": {"font_east_asia": "宋体", "font_size_half_points": "24"},
                "paragraph_format": {"alignment": "both"},
            },
        }
        result = json.loads(define_style_profile(session_id, styles))
        profile_path = result["style_profile_path"]
        sample = load_style_sample(session_id, profile_path, "S001")
        assert sample["format"]["font_east_asia"] == "宋体"
        assert sample["format"]["font_size_half_points"] == "24"
        assert sample["paragraph_format"]["alignment"] == "both"
        # 缺失字段应为 None / False
        assert sample["format"]["bold"] is False
        assert sample["format"]["color"] is None

    def test_empty_styles_returns_error(self, tmp_root, session_id):
        """styles={} → status=error."""
        result = json.loads(define_style_profile(session_id, {}))
        assert result["status"] == "error"
        assert "不能为空" in result["message"]

    def test_invalid_role_returns_error(self, tmp_root, session_id):
        """非标准角色 → status=error, 列出合法角色."""
        result = json.loads(define_style_profile(session_id, {
            "nonexistent_role": {"format": {}, "paragraph_format": {}},
        }))
        assert result["status"] == "error"
        assert "非标准角色" in result["message"]
        assert set(result["fixed_roles"]) == set(FIXED_ROLES)

    def test_invalid_alignment_returns_error(self, tmp_root, session_id):
        """alignment 非法值 → status=error."""
        result = json.loads(define_style_profile(session_id, {
            "body": {"format": {}, "paragraph_format": {"alignment": "justify"}},
        }))
        assert result["status"] == "error"
        assert "alignment" in result["errors"][0]

    def test_invalid_highlight_returns_error(self, tmp_root, session_id):
        """highlight 不在枚举内 → status=error."""
        result = json.loads(define_style_profile(session_id, {
            "body": {"format": {"highlight": "rainbow"}, "paragraph_format": {}},
        }))
        assert result["status"] == "error"
        assert "highlight" in result["errors"][0]

    def test_invalid_color_returns_error(self, tmp_root, session_id):
        """color 非 6 位 hex → status=error."""
        result = json.loads(define_style_profile(session_id, {
            "body": {"format": {"color": "red"}, "paragraph_format": {}},
        }))
        assert result["status"] == "error"
        assert "color" in result["errors"][0]

    def test_invalid_shading_fill_returns_error(self, tmp_root, session_id):
        """shading_fill 非 6 位 hex → status=error."""
        result = json.loads(define_style_profile(session_id, {
            "body": {"format": {}, "paragraph_format": {"shading_fill": "GGGGGG"}},
        }))
        assert result["status"] == "error"
        assert "shading_fill" in result["errors"][0]

    def test_invalid_font_size_returns_error(self, tmp_root, session_id):
        """font_size_half_points 非正整数 → status=error."""
        result = json.loads(define_style_profile(session_id, {
            "body": {"format": {"font_size_half_points": "abc"}, "paragraph_format": {}},
        }))
        assert result["status"] == "error"
        assert "font_size_half_points" in result["errors"][0]

    def test_font_size_as_int_accepted(self, tmp_root, session_id):
        """font_size_half_points 传 int 也能接受（转为 str）."""
        result = json.loads(define_style_profile(session_id, {
            "body": {"format": {"font_size_half_points": 24}, "paragraph_format": {}},
        }))
        assert result["status"] == "ok"
        sample = load_style_sample(session_id, result["style_profile_path"], "S001")
        assert sample["format"]["font_size_half_points"] == "24"


class TestDeriveStyleMappingFallback:
    def test_undefined_roles_inherit_body(self, tmp_root, session_id):
        """只定义 body + table_cell → 其余 block_type 继承 body 的 sample_id."""
        styles = {
            "body": {"format": {"font_east_asia": "宋体"}, "paragraph_format": {}},
            "table_cell": {"format": {"font_east_asia": "黑体"}, "paragraph_format": {}},
        }
        result = json.loads(define_style_profile(session_id, styles))
        mapping = derive_style_mapping_from_bindings(session_id, result["style_profile_path"])

        body_id = result["role_bindings"]["body"]
        table_id = result["role_bindings"]["table_cell"]

        assert mapping["paragraph"] == body_id
        assert mapping["table_cell"] == table_id
        # 未定义的角色应继承 body
        assert mapping["heading1"] == body_id
        assert mapping["heading2"] == body_id
        assert mapping["list_item"] == body_id
        assert mapping["code_block"] == body_id
        assert mapping["image"] == body_id

    def test_all_roles_defined_no_fallback_needed(self, tmp_root, session_id):
        """所有角色都定义 → 每个 block_type 用自己的 sample_id."""
        styles = {role: {"format": {}, "paragraph_format": {}} for role in FIXED_ROLES if role != "placeholder"}
        result = json.loads(define_style_profile(session_id, styles))
        mapping = derive_style_mapping_from_bindings(session_id, result["style_profile_path"])

        for role, sample_id in result["role_bindings"].items():
            if role in ("title", "section_heading", "body", "list_item", "table_cell", "code_block", "image"):
                from docx_tools.style_profile import ROLE_TO_BLOCK_TYPES
                for bt in ROLE_TO_BLOCK_TYPES.get(role, ()):
                    assert mapping[bt] == sample_id


class TestShadingFill:
    def test_shading_fill_in_profile(self, tmp_root, session_id):
        """shading_fill 正确写入 style_profile.json."""
        styles = {
            "body": {
                "format": {},
                "paragraph_format": {"shading_fill": "F2F2F2"},
            },
        }
        result = json.loads(define_style_profile(session_id, styles))
        sample = load_style_sample(session_id, result["style_profile_path"], "S001")
        assert sample["paragraph_format"]["shading_fill"] == "F2F2F2"

    def test_shading_fill_applied_to_paragraph(self, tmp_root, session_id):
        """apply_sample_paragraph_format 正确生成 w:shd 元素."""
        from lxml import etree
        from docx_tools.common import apply_sample_paragraph_format, W

        styles = {
            "body": {
                "format": {},
                "paragraph_format": {"shading_fill": "FFFFCC"},
            },
        }
        result = json.loads(define_style_profile(session_id, styles))
        sample = load_style_sample(session_id, result["style_profile_path"], "S001")

        paragraph = etree.Element(f"{W}p")
        apply_sample_paragraph_format(paragraph, sample)

        shd = paragraph.find(f"{W}pPr/{W}shd")
        assert shd is not None
        assert shd.get(f"{W}fill") == "FFFFCC"
        assert shd.get(f"{W}val") == "clear"
