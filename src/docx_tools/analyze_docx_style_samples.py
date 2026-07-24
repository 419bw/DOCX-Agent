import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from workspace.guard import resolve_workspace_path, WorkspacePathError, to_relative_path

from .common import (
    NS,
    W,
    element_index_among_same_tag,
    json_result,
    load_document_xml,
    nearest_ancestor,
    paragraph_location,
    paragraph_text,
    paragraphs,
    tables,
)


def analyze_docx_style_samples(
    session_id: str,  # v2: 后端 dispatcher 隐式注入, LLM 不可见 (避坑 1)
    docx_path: str,
    output_profile_path: str = "",
    max_samples: int = 16,
    examples_per_sample: int = 4,
) -> str:
    """v2: 提取 session workspace 内 docx 的格式样本 (沙箱化)"""
    try:
        docx_path_resolved = resolve_workspace_path(session_id, docx_path, must_exist=True, must_be_file=True)
    except WorkspacePathError as e:
        return json_result({"status": "error", "code": e.code, "message": e.user_message})
    root = load_document_xml(str(docx_path_resolved))
    all_tables = tables(root)
    table_index_by_id = {id(table): index for index, table in enumerate(all_tables, start=1)}

    groups = {}
    for paragraph_index, paragraph in enumerate(paragraphs(root), start=1):
        text = paragraph_text(paragraph).strip()
        if not text:
            continue
        dominant = _dominant_run_format(paragraph)
        if dominant is None:
            continue

        context = _paragraph_context(paragraph, table_index_by_id)
        para_format = _paragraph_format(paragraph)
        signature = _style_signature(dominant, para_format, context)
        if signature not in groups:
            groups[signature] = {
                "format": dominant,
                "paragraph_format": para_format,
                "context": _context_kind(context),
                "examples": [],
                "total_occurrences": 0,
                "total_text_chars": 0,
                "candidate_role_hints": Counter(),
            }

        group = groups[signature]
        group["total_occurrences"] += 1
        group["total_text_chars"] += len(text)
        for hint in _candidate_role_hints(text, dominant, context, paragraph_index):
            group["candidate_role_hints"][hint] += 1
        if len(group["examples"]) < examples_per_sample:
            group["examples"].append(
                {
                    "text": text[:160],
                    "paragraph_index": paragraph_index,
                    "location": paragraph_location(paragraph),
                    "style_context": context,
                }
            )

    sorted_groups = sorted(
        groups.values(),
        key=lambda item: (item["total_occurrences"], item["total_text_chars"]),
        reverse=True,
    )

    style_samples = []
    for index, group in enumerate(sorted_groups[:max_samples], start=1):
        hints = [
            {"role": role, "evidence_count": count}
            for role, count in group["candidate_role_hints"].most_common()
        ]
        style_samples.append(
            {
                "sample_id": f"S{index:03d}",
                "context": group["context"],
                "format": group["format"],
                "paragraph_format": group["paragraph_format"],
                "total_occurrences": group["total_occurrences"],
                "candidate_role_hints": hints,
                "examples": group["examples"],
            }
        )

    result = {
        "status": "ok",
        "docx_path": to_relative_path(session_id, docx_path_resolved),
        "needs_user_review": True,
        "style_profile_path": "",
        "style_samples": style_samples,
        "review_instructions": [
            "这些 sample_id 只是候选格式样本，不是最终样式决策。",
            "请让用户确认哪些 sample_id 对应正文、章节标题、表格字段名、表格填写值、占位提示等角色。",
            "大规模写入前，优先按用户确认的 sample_id 仿写格式，避免把正文写成标题或加粗格式。",
        ],
    }
    profile_path = _resolve_profile_path(docx_path_resolved, output_profile_path, session_id)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json_result(result), encoding="utf-8")
    result["style_profile_path"] = to_relative_path(session_id, profile_path)
    profile_path.write_text(json_result(result), encoding="utf-8")
    return json_result(result)


def _resolve_profile_path(docx_path: Path, output_profile_path: str, session_id: str) -> Path:
    """v2: profile 强制写到 session_workspace/style_profiles/ 下 (沙箱化)"""
    from workspace.guard import workspace_dir
    style_profiles_dir = workspace_dir(session_id) / "style_profiles"
    style_profiles_dir.mkdir(parents=True, exist_ok=True)

    if output_profile_path:
        # 用户指定了文件名 — 强制放进 style_profiles/, basename 化防越界
        return style_profiles_dir / Path(output_profile_path).name
    stem = docx_path.stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return style_profiles_dir / f"{stem}_{timestamp}.json"


def _dominant_run_format(paragraph):
    weighted = Counter()
    formats = {}
    for run in paragraph.xpath("./w:r", namespaces=NS):
        text = "".join(t.text or "" for t in run.xpath(".//w:t", namespaces=NS))
        if not text:
            continue
        fmt = _run_format(run)
        signature = tuple(sorted(fmt.items()))
        weighted[signature] += len(text)
        formats[signature] = fmt
    if not weighted:
        return None
    return formats[weighted.most_common(1)[0][0]]


def _run_format(run):
    rpr = run.find("w:rPr", namespaces=NS)
    if rpr is None:
        return {
            "bold": False,
            "bold_cs": False,
            "italic": False,
            "underline": None,
            "color": None,
            "highlight": None,
            "font_size_half_points": None,
            "font_size_cs_half_points": None,
            "font_ascii": None,
            "font_east_asia": None,
        }

    fonts = rpr.find("w:rFonts", namespaces=NS)
    return {
        "bold": rpr.find("w:b", namespaces=NS) is not None,
        "bold_cs": rpr.find("w:bCs", namespaces=NS) is not None,
        "italic": rpr.find("w:i", namespaces=NS) is not None or rpr.find("w:iCs", namespaces=NS) is not None,
        "underline": _w_val(rpr.find("w:u", namespaces=NS)),
        "color": _w_val(rpr.find("w:color", namespaces=NS)),
        "highlight": _w_val(rpr.find("w:highlight", namespaces=NS)),
        "font_size_half_points": _w_val(rpr.find("w:sz", namespaces=NS)),
        "font_size_cs_half_points": _w_val(rpr.find("w:szCs", namespaces=NS)),
        "font_ascii": _w_attr(fonts, "ascii"),
        "font_east_asia": _w_attr(fonts, "eastAsia"),
    }


def _paragraph_format(paragraph):
    ppr = paragraph.find("w:pPr", namespaces=NS)
    if ppr is None:
        return {"style_id": None, "alignment": None}
    return {
        "style_id": _w_val(ppr.find("w:pStyle", namespaces=NS)),
        "alignment": _w_val(ppr.find("w:jc", namespaces=NS)),
    }


def _paragraph_context(paragraph, table_index_by_id):
    table_cell = nearest_ancestor(paragraph, f"{W}tc")
    if table_cell is None:
        return {"in_table": False, "kind": "paragraph"}

    row = nearest_ancestor(paragraph, f"{W}tr")
    table = nearest_ancestor(paragraph, f"{W}tbl")
    return {
        "in_table": True,
        "kind": "table_cell",
        "global_table_index": table_index_by_id.get(id(table)),
        "row_index": element_index_among_same_tag(row),
        "cell_index": element_index_among_same_tag(table_cell),
    }


def _context_kind(context):
    if not context.get("in_table"):
        return "normal_paragraph"
    cell_index = context.get("cell_index")
    if cell_index == 1:
        return "table_cell_first_column"
    return "table_cell_other_column"


def _candidate_role_hints(text, fmt, context, paragraph_index):
    hints = []
    length = len(text)
    size = _int_or_none(fmt.get("font_size_half_points")) or _int_or_none(fmt.get("font_size_cs_half_points"))
    is_bold = bool(fmt.get("bold") or fmt.get("bold_cs"))
    is_blue = (fmt.get("color") or "").upper() in {"0000FF", "0000EE", "0563C1"}

    if is_blue:
        hints.append("blue_placeholder_or_prompt")
    if paragraph_index <= 8 and length <= 40 and (is_bold or (size is not None and size >= 28)):
        hints.append("cover_or_document_title")
    if _looks_like_section_heading(text) and (is_bold or length <= 30):
        hints.append("section_heading")
    if context.get("in_table") and context.get("cell_index") == 1 and length <= 20 and text.endswith(("：", ":")):
        hints.append("table_label_cell")
    if context.get("in_table") and context.get("cell_index", 1) > 1:
        hints.append("table_value_cell")
    if length >= 18 and not is_blue and not _looks_like_section_heading(text):
        hints.append("body_text")
    if not hints:
        hints.append("uncertain")
    return hints


def _looks_like_section_heading(text):
    patterns = [
        r"^[一二三四五六七八九十]+、",
        r"^\d+(\.\d+)*[\.、 ]",
        r"^【[^】]+】$",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def _style_signature(run_format, paragraph_format, context):
    keys = (
        "bold",
        "bold_cs",
        "italic",
        "underline",
        "color",
        "highlight",
        "font_size_half_points",
        "font_size_cs_half_points",
        "font_ascii",
        "font_east_asia",
    )
    return (
        _context_kind(context),
        paragraph_format.get("style_id"),
        paragraph_format.get("alignment"),
        tuple((key, run_format.get(key)) for key in keys),
    )


def _w_val(element):
    return _w_attr(element, "val")


def _w_attr(element, name):
    if element is None:
        return None
    return element.get(f"{W}{name}")


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


tools_schema = {
    "type": "function",
    "function": {
        "name": "analyze_docx_style_samples",
        "description": "只读分析 session workspace 内 DOCX 中的常见格式样本，返回 sample_id、格式字段、位置、示例文本和候选角色提示。",
        "parameters": {
            "type": "object",
            "properties": {
                "docx_path": {"type": "string", "description": "要分析的 .docx 文件路径 (相对 workspace 根)"},
                "output_profile_path": {
                    "type": "string",
                    "description": "可选，样式画像 JSON 输出文件名（仅 basename）; 默认 <stem>_<timestamp>.json",
                },
                "max_samples": {"type": "integer", "description": "最多返回多少组格式样本，默认 16"},
                "examples_per_sample": {"type": "integer", "description": "每组格式最多返回多少个示例文本，默认 4"},
            },
            "required": ["docx_path"],
        },
    },
}
