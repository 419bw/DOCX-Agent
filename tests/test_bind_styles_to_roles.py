"""test_bind_styles_to_roles.py — bind_styles_to_roles 工具 4 case (PR-3.2)

工具: 读 style_profile.json, 把 sample_id 绑定到 5 个标准角色
(title / section_heading / body / table_cell / placeholder), 写回 JSON.

3 case + 1 smoke:
  1. 正常绑定 → status=ok, role_bindings 字段写入
  2. bindings 为空 → status=error, 提示必须显式提供
  3. 用了非标准角色 → status=error, 列出合法角色
  4. 绑定的 sample_id 不存在 → status=error, 列出可用 sample_id
"""
import json
import sys
from pathlib import Path

import pytest

pytest_plugins = ["_docx_factory"]
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from docx_tools.bind_styles_to_roles import bind_styles_to_roles
from docx_tools.style_profile import FIXED_ROLES


def _ws(tmp_root, session_id: str) -> Path:
    return tmp_root / session_id / "workspace"


def _write_profile(path: Path, sample_ids: list[str]) -> None:
    """构造最小合法 style_profile.json 含 style_samples 数组."""
    profile = {
        "version": "1.0",
        "style_samples": [
            {"sample_id": sid, "format": "test", "paragraph_format": {}, "context": "test"}
            for sid in sample_ids
        ],
    }
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


class TestBindStylesToRoles:
    def test_basic_bind_writes_role_bindings(self, tmp_root, session_id):
        """正常绑定 8 个角色 → 写回 profile.role_bindings 字段."""
        profile_path = _ws(tmp_root, session_id) / "style_profile.json"
        _write_profile(profile_path, [f"S{i:03d}" for i in range(1, 9)])

        # FIXED_ROLES 已扩展到 8 个角色 (style-review-redesign)
        assert len(FIXED_ROLES) == 8, f"FIXED_ROLES 应 8 个, 实际 {len(FIXED_ROLES)}"

        bindings = {role: f"S{i+1:03d}" for i, role in enumerate(FIXED_ROLES)}
        result = json.loads(bind_styles_to_roles(
            session_id, "style_profile.json", bindings
        ))

        assert result["status"] == "ok"
        assert result["role_bindings"] == bindings
        # 验证文件确实被修改
        reloaded = json.loads(profile_path.read_text(encoding="utf-8"))
        assert reloaded["role_bindings"] == bindings

    def test_empty_bindings_returns_error(self, tmp_root, session_id):
        """bindings={} → status=error, 提示必须显式提供."""
        profile_path = _ws(tmp_root, session_id) / "style_profile.json"
        _write_profile(profile_path, ["S001"])

        result = json.loads(bind_styles_to_roles(
            session_id, "style_profile.json", {}
        ))
        assert result["status"] == "error"
        assert "必须显式提供" in result["message"]
        # 应列出可用的 sample_id
        assert "available_sample_ids" in result

    def test_invalid_role_name_returns_error(self, tmp_root, session_id):
        """用了非标准角色 → status=error, 列出合法角色."""
        profile_path = _ws(tmp_root, session_id) / "style_profile.json"
        _write_profile(profile_path, ["S001"])

        result = json.loads(bind_styles_to_roles(
            session_id, "style_profile.json",
            {"non_existent_role": "S001"},
        ))
        assert result["status"] == "error"
        assert "非标准角色" in result["message"]
        # 列出合法角色
        assert "fixed_roles" in result
        assert set(result["fixed_roles"]) == set(FIXED_ROLES)

    def test_sample_id_not_in_profile_returns_error(self, tmp_root, session_id):
        """绑定的 sample_id 不在 profile → status=error."""
        profile_path = _ws(tmp_root, session_id) / "style_profile.json"
        _write_profile(profile_path, ["S001"])  # 只有 S001
        # 试图绑 S999
        bindings = {role: "S999" for role in FIXED_ROLES}

        result = json.loads(bind_styles_to_roles(
            session_id, "style_profile.json", bindings
        ))
        assert result["status"] == "error"
        assert "S999" in result["message"]
        assert "available_sample_ids" in result
        assert "S001" in result["available_sample_ids"]
