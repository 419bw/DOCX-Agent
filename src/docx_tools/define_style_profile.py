import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from workspace.guard import resolve_workspace_path, WorkspacePathError, to_relative_path, workspace_dir

from .common import json_result
from .style_profile import FIXED_ROLES

# ---------- 校验常量 ----------

VALID_ALIGNMENTS = {"left", "center", "right", "both"}

VALID_HIGHLIGHTS = {
    "yellow", "green", "cyan", "magenta", "blue", "red",
    "darkBlue", "darkCyan", "darkGreen", "darkMagenta",
    "darkRed", "darkYellow", "lightGray", "black", "none",
}

VALID_UNDERLINES = {
    "single", "double", "thick", "dotted", "dottedHeavy",
    "dash", "dashedHeavy", "dashLong", "dashLongHeavy",
    "dotDash", "dashDotHeavy", "dotDotDash", "dashDotDotHeavy",
    "wave", "wavyHeavy", "wavyDouble", "words", "none",
}

_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def define_style_profile(
    session_id: str,
    styles: dict[str, dict],
    output_profile_path: str = "",
) -> str:
    """AI 直接定义各角色的格式参数，写入 style_profile.json。

    替代 bind_styles_to_roles：不再从模板提取的 sample 里选 sample_id，
    而是由 AI 参考模板分析结果后自行决定每个角色的字体、字号、加粗、对齐等格式。
    输出的 style_profile.json 与 analyze_docx_style_samples 格式兼容，
    下游 derive_style_mapping_from_bindings / load_style_sample / render 管线无需修改。
    """
    if not styles:
        return json_result({
            "status": "error",
            "message": "styles 不能为空。请为至少 body 角色定义格式。",
            "fixed_roles": list(FIXED_ROLES),
        })

    invalid_roles = sorted(set(styles) - set(FIXED_ROLES))
    if invalid_roles:
        return json_result({
            "status": "error",
            "message": f"非标准角色 {invalid_roles}。合法角色: {list(FIXED_ROLES)}",
            "fixed_roles": list(FIXED_ROLES),
        })

    errors = _validate_styles(styles)
    if errors:
        return json_result({
            "status": "error",
            "message": "格式字段校验失败",
            "errors": errors,
            "fixed_roles": list(FIXED_ROLES),
        })

    style_samples = []
    role_bindings = {}
    for index, (role, fmt_dict) in enumerate(styles.items(), start=1):
        sample_id = f"S{index:03d}"
        style_samples.append({
            "sample_id": sample_id,
            "context": f"ai_defined_{role}",
            "format": _normalize_format(fmt_dict.get("format") or {}),
            "paragraph_format": _normalize_paragraph_format(fmt_dict.get("paragraph_format") or {}),
            "total_occurrences": 0,
            "candidate_role_hints": [{"role": role, "evidence_count": 1}],
            "examples": [],
        })
        role_bindings[role] = sample_id

    result = {
        "status": "ok",
        "needs_user_review": True,
        "style_profile_path": "",
        "style_samples": style_samples,
        "role_bindings": role_bindings,
        "review_instructions": [
            "这些格式由 AI 根据模板分析结果自行定义。",
            "请确认各角色的字体、字号、加粗、对齐等是否符合预期。",
        ],
    }

    profile_path = _resolve_profile_path(session_id, output_profile_path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json_result(result), encoding="utf-8")
    result["style_profile_path"] = to_relative_path(session_id, profile_path)
    profile_path.write_text(json_result(result), encoding="utf-8")

    return json_result(result)


# ---------- 校验 ----------

def _validate_styles(styles: dict[str, dict]) -> list[str]:
    errors = []
    for role, fmt_dict in styles.items():
        prefix = f"{role}: "
        fmt = fmt_dict.get("format") or {}
        pf = fmt_dict.get("paragraph_format") or {}

        alignment = pf.get("alignment")
        if alignment is not None and alignment not in VALID_ALIGNMENTS:
            errors.append(f"{prefix}alignment '{alignment}' 不合法，可选: {sorted(VALID_ALIGNMENTS)}")

        highlight = fmt.get("highlight")
        if highlight is not None and highlight not in VALID_HIGHLIGHTS:
            errors.append(f"{prefix}highlight '{highlight}' 不合法，可选: {sorted(VALID_HIGHLIGHTS)}")

        underline = fmt.get("underline")
        if underline is not None and underline not in VALID_UNDERLINES:
            errors.append(f"{prefix}underline '{underline}' 不合法，可选: {sorted(VALID_UNDERLINES)}")

        color = fmt.get("color")
        if color is not None and not _HEX_COLOR_RE.match(str(color)):
            errors.append(f"{prefix}color '{color}' 应为 6 位 hex RGB（如 FF0000）")

        shading_fill = pf.get("shading_fill")
        if shading_fill is not None and not _HEX_COLOR_RE.match(str(shading_fill)):
            errors.append(f"{prefix}shading_fill '{shading_fill}' 应为 6 位 hex RGB（如 F2F2F2）")

        for size_key in ("font_size_half_points", "font_size_cs_half_points"):
            val = fmt.get(size_key)
            if val is not None:
                try:
                    n = int(val)
                    if n <= 0:
                        errors.append(f"{prefix}{size_key} 必须为正整数，当前: {val}")
                except (ValueError, TypeError):
                    errors.append(f"{prefix}{size_key} 必须为正整数，当前: {val}")

    return errors


# ---------- 规范化 ----------

def _normalize_format(fmt: dict) -> dict:
    """补全缺失字段为 None，与 analyze_docx_style_samples 输出格式对齐。"""
    return {
        "bold": fmt.get("bold", False),
        "bold_cs": fmt.get("bold_cs", False),
        "italic": fmt.get("italic", False),
        "underline": fmt.get("underline"),
        "color": fmt.get("color"),
        "highlight": fmt.get("highlight"),
        "font_size_half_points": _str_or_none(fmt.get("font_size_half_points")),
        "font_size_cs_half_points": _str_or_none(fmt.get("font_size_cs_half_points")),
        "font_ascii": fmt.get("font_ascii"),
        "font_east_asia": fmt.get("font_east_asia"),
    }


def _normalize_paragraph_format(pf: dict) -> dict:
    return {
        "style_id": pf.get("style_id"),
        "alignment": pf.get("alignment"),
        "shading_fill": pf.get("shading_fill"),
    }


def _str_or_none(val) -> str | None:
    if val is None:
        return None
    return str(val)


def _resolve_profile_path(session_id: str, output_profile_path: str) -> Path:
    style_profiles_dir = workspace_dir(session_id) / "style_profiles"
    style_profiles_dir.mkdir(parents=True, exist_ok=True)
    if output_profile_path:
        return style_profiles_dir / Path(output_profile_path).name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return style_profiles_dir / f"ai_defined_{timestamp}.json"


# ---------- LLM 工具 schema ----------

_FORMAT_PROPERTIES = {
    "bold": {"type": "boolean", "description": "加粗"},
    "bold_cs": {"type": "boolean", "description": "复杂文字加粗"},
    "italic": {"type": "boolean", "description": "斜体"},
    "underline": {"type": "string", "description": "下划线线型: single/double/dash/dotted 等，不设置则无下划线"},
    "color": {"type": "string", "description": "文字颜色，6 位 hex RGB（如 FF0000 红色）"},
    "highlight": {"type": "string", "description": "荧光笔颜色: yellow/green/cyan/magenta/blue/red/darkBlue/darkCyan/darkGreen/darkMagenta/darkRed/darkYellow/lightGray/black/none"},
    "font_size_half_points": {"type": "string", "description": "字号，半磅值（如小四=24, 五号=21）"},
    "font_size_cs_half_points": {"type": "string", "description": "复杂文字字号，半磅值"},
    "font_ascii": {"type": "string", "description": "西文字体名（如 Times New Roman, Calibri）"},
    "font_east_asia": {"type": "string", "description": "中文字体名（如 宋体, 黑体, 楷体）"},
}

_PARAGRAPH_FORMAT_PROPERTIES = {
    "style_id": {"type": "string", "description": "Word 段落样式 ID（如 Heading1），通常不需要设置"},
    "alignment": {"type": "string", "description": "对齐方式: left/center/right/both（两端对齐）"},
    "shading_fill": {"type": "string", "description": "段落底纹填充色，6 位 hex RGB（如 F2F2F2 浅灰）"},
}

tools_schema = {
    "type": "function",
    "function": {
        "name": "define_style_profile",
        "description": (
            "定义各角色的文字格式，写入样式画像 JSON。"
            "参考 analyze_docx_style_samples 的分析结果，为每个角色决定字体、字号、加粗、对齐等格式参数。"
            "只需定义与 body 不同的角色，未定义的角色自动继承 body 格式。"
            "输出的 style_profile.json 可直接被 markdown_to_word 使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "styles": {
                    "type": "object",
                    "description": (
                        "角色到格式的映射。key 为角色名（title/section_heading/body/list_item/"
                        "table_cell/code_block/image/placeholder），value 为 "
                        "{\"format\": {...}, \"paragraph_format\": {...}}。"
                        "至少定义 body 角色。"
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "format": {
                                "type": "object",
                                "description": "Run 级格式（字体、字号、加粗、颜色等）",
                                "properties": _FORMAT_PROPERTIES,
                            },
                            "paragraph_format": {
                                "type": "object",
                                "description": "段落级格式（对齐、底纹等）",
                                "properties": _PARAGRAPH_FORMAT_PROPERTIES,
                            },
                        },
                    },
                },
                "output_profile_path": {
                    "type": "string",
                    "description": "可选，样式画像 JSON 输出文件名（仅 basename）; 默认 ai_defined_<timestamp>.json",
                },
            },
            "required": ["styles"],
        },
    },
}
