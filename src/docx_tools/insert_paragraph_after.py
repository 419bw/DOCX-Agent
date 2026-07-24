from .common import (
    CUSTOM_FORMAT_PROPERTIES,
    apply_format_policy_to_paragraph,
    json_result,
    insert_paragraphs_after,
    load_document_xml,
    make_paragraph_like,
    paragraph_location,
    paragraph_text,
    paragraphs,
    split_text_for_paragraphs,
    write_document_xml,
    resolve_docx_io,
)


def insert_paragraph_after(
    session_id: str,
    docx_path: str,
    output_path: str,
    anchor_text: str,
    new_text: str,
    occurrence: int = 1,
    style_source: str = "previous",
    newline_mode: str = "paragraphs",
    format_policy: str = "preserve",
    **custom,
) -> str:
    """在包含锚点文本的段落后新增段落。"""
    input_path, output_path_resolved = resolve_docx_io(session_id, docx_path, output_path)
    root = load_document_xml(str(input_path))
    current_occurrence = 0

    for paragraph_index, paragraph in enumerate(paragraphs(root), start=1):
        logical_text = paragraph_text(paragraph)
        if anchor_text not in logical_text:
            continue

        search_from = 0
        while True:
            hit = logical_text.find(anchor_text, search_from)
            if hit == -1:
                break
            current_occurrence += 1
            if current_occurrence != occurrence:
                search_from = hit + max(1, len(anchor_text))
                continue

            first_text, extra_paragraphs = split_text_for_paragraphs(new_text, newline_mode)
            new_paragraph = _make_paragraph(paragraph, first_text, style_source)
            paragraph.addnext(new_paragraph)
            apply_format_policy_to_paragraph(
                new_paragraph,
                format_policy,
                **custom,
            )
            inserted_extra_count = insert_paragraphs_after(new_paragraph, extra_paragraphs, new_paragraph)
            current = new_paragraph
            for _ in range(inserted_extra_count):
                current = current.getnext()
                if current is not None:
                    apply_format_policy_to_paragraph(
                        current,
                        format_policy,
                        **custom,
                    )
            write_document_xml(str(input_path), str(output_path_resolved), root)
            return json_result(
                {
                    "status": "ok",
                    "docx_path": str(input_path),
                    "output_path": str(output_path_resolved),
                    "anchor_text": anchor_text,
                    "new_text": new_text,
                    "occurrence": occurrence,
                    "style_source": style_source,
                    "newline_mode": newline_mode,
                    "format_policy": format_policy,
                    "anchor_paragraph_index": paragraph_index,
                    "new_paragraph_index_estimate": paragraph_index + 1,
                    "inserted_paragraph_count": 1 + inserted_extra_count,
                    "location": paragraph_location(paragraph),
                }
            )

    return json_result(
        {
            "status": "not_found",
            "docx_path": str(input_path),
            "anchor_text": anchor_text,
            "occurrence": occurrence,
        }
    )


def _make_paragraph(anchor_paragraph, new_text: str, style_source: str):
    style_paragraph = _select_style_paragraph(anchor_paragraph, style_source)
    if style_paragraph is None:
        style_paragraph = anchor_paragraph
    return make_paragraph_like(style_paragraph, new_text)


def _select_style_paragraph(anchor_paragraph, style_source: str):
    style_source = (style_source or "previous").lower()
    if style_source == "empty":
        return None
    if style_source == "next":
        sibling = anchor_paragraph.getnext()
        while sibling is not None:
            if sibling.tag.endswith("}p"):
                return sibling
            sibling = sibling.getnext()
        return anchor_paragraph
    return anchor_paragraph


tools_schema = {
    "type": "function",
    "function": {
        "name": "insert_paragraph_after",
        "description": "在包含锚点文本的段落后新增一个段落，可选择复制前一段、后一段或空样式。",
        "parameters": {
            "type": "object",
            "properties": {
                "docx_path": {"type": "string", "description": "输入 .docx 文件路径 (相对 workspace 根)"},
                "output_path": {"type": "string", "description": "输出 .docx 文件路径 (相对 workspace 根)"},
                "anchor_text": {"type": "string", "description": "用于定位段落的文本"},
                "new_text": {"type": "string", "description": "新增段落的文本"},
                "occurrence": {"type": "integer", "description": "第几个匹配项，1-based，默认 1"},
                "style_source": {
                    "type": "string",
                    "description": "样式来源：previous 复制锚点段落，next 复制后一段，empty 不复制段落/run 样式",
                    "enum": ["previous", "next", "empty"],
                },
                "newline_mode": {
                    "type": "string",
                    "description": "新增文本包含换行时的处理方式：paragraphs 拆成多个连续段落，inline 替换为空格；默认 paragraphs",
                    "enum": ["paragraphs", "inline"],
                },
                "format_policy": {
                    "type": "string",
                    "description": "新增段落文本的格式策略：preserve 保留样式来源格式，clear 清除直接字符格式，body 转正文格式，custom 使用显式格式；默认 preserve",
                    "enum": ["preserve", "clear", "body", "custom"],
                },
                **CUSTOM_FORMAT_PROPERTIES,
            },
            "required": ["docx_path", "output_path", "anchor_text", "new_text"],
        },
    },
}
