import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from workspace.guard import resolve_workspace_path

FIXED_ROLES = (
    "title",
    "section_heading",
    "body",
    "list_item",
    "table_cell",
    "code_block",
    "image",
    "placeholder",
)

ROLE_TO_BLOCK_TYPES: dict[str, tuple[str, ...]] = {
    "title": ("heading1",),
    "section_heading": ("heading2",),
    "body": ("paragraph",),
    "list_item": ("list_item",),
    "table_cell": ("table_cell",),
    "code_block": ("code_block",),
    "image": ("image",),
}

# 所有需要 style_mapping 覆盖的 block_type（用于 fallback 填充）
_ALL_BLOCK_TYPES = ("paragraph", "heading1", "heading2", "list_item", "table_cell", "code_block", "image")

_DERIVATION_ORDER = ("body", "list_item", "code_block", "image", "table_cell", "section_heading", "title")


def load_style_sample(session_id: str, style_profile_path: str, sample_id: str) -> dict:
    profile_path = resolve_workspace_path(session_id, style_profile_path, must_exist=True, must_be_file=True)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    for sample in profile.get("style_samples", []):
        if sample.get("sample_id") == sample_id:
            return sample
    raise ValueError(f"sample_id not found in style profile: {sample_id}")


def derive_style_mapping_from_bindings(session_id: str, style_profile_path: str) -> dict[str, str]:
    """从 style_profile 的 role_bindings 推导 block_type -> sample_id 映射。

    无 role_bindings 时返回空 dict，调用方应回退到默认行为。
    展开后未被覆盖的 block_type 自动继承 body 的 sample_id。
    """
    try:
        profile_path = resolve_workspace_path(session_id, style_profile_path, must_exist=True, must_be_file=True)
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    bindings = profile.get("role_bindings")
    if not isinstance(bindings, dict) or not bindings:
        return {}

    style_mapping: dict[str, str] = {}
    for role in _DERIVATION_ORDER:
        sample_id = bindings.get(role)
        if not sample_id or role not in ROLE_TO_BLOCK_TYPES:
            continue
        for block_type in ROLE_TO_BLOCK_TYPES[role]:
            style_mapping[block_type] = sample_id

    body_sample = bindings.get("body")
    if body_sample:
        for block_type in _ALL_BLOCK_TYPES:
            if block_type not in style_mapping:
                style_mapping[block_type] = body_sample

    return style_mapping

