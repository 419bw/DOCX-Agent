from .common import (
    CUSTOM_FORMAT_PROPERTIES,
    apply_format_policy_to_paragraph,
    apply_format_policy_to_run,
    json_result,
    load_document_xml,
    make_paragraph_like,
    paragraph_location,
    paragraph_text,
    paragraphs,
    replace_text_range_in_paragraph,
    split_text_for_paragraphs,
    write_document_xml,
    resolve_docx_io,
)


def replace_text(
    session_id: str,
    docx_path: str,
    output_path: str,
    old_text: str,
    new_text: str,
    occurrence: int = 1,
    newline_mode: str = "paragraphs",
    format_policy: str = "preserve",
    **custom,
) -> str:
    """按逻辑段落文本替换内容，支持跨 run 命中。"""
    input_path, output_path_resolved = resolve_docx_io(session_id, docx_path, output_path)
    root = load_document_xml(str(input_path))
    current_occurrence = 0

    for paragraph_index, paragraph in enumerate(paragraphs(root), start=1):
        logical_text = paragraph_text(paragraph)
        search_from = 0
        while True:
            hit = logical_text.find(old_text, search_from)
            if hit == -1:
                break
            current_occurrence += 1
            if current_occurrence != occurrence:
                search_from = hit + max(1, len(old_text))
                continue

            before_text = paragraph_text(paragraph)
            replacement_text, extra_paragraphs = _prepare_replacement_text(new_text, newline_mode)
            change = replace_text_range_in_paragraph(paragraph, hit, hit + len(old_text), replacement_text)
            if change.get("run") is not None:
                apply_format_policy_to_run(
                    change["run"],
                    format_policy,
                    **custom,
                )
            inserted_paragraph_count = 0
            if extra_paragraphs:
                inserted_paragraph_count = _insert_extra_paragraphs(paragraph, extra_paragraphs)
                for inserted in _following_paragraphs(paragraph, inserted_paragraph_count):
                    apply_format_policy_to_paragraph(
                        inserted,
                        format_policy,
                        **custom,
                    )
            after_text = paragraph_text(paragraph)
            change_for_result = {key: value for key, value in change.items() if key != "run"}
            write_document_xml(str(input_path), str(output_path_resolved), root)
            return json_result(
                {
                    "status": "ok",
                    "docx_path": str(input_path),
                    "output_path": str(output_path_resolved),
                    "paragraph_index": paragraph_index,
                    "location": paragraph_location(paragraph),
                    "old_text": old_text,
                    "new_text": new_text,
                    "occurrence": occurrence,
                    "newline_mode": newline_mode,
                    "format_policy": format_policy,
                    "before_paragraph_text": before_text,
                    "after_paragraph_text": after_text,
                    "inserted_paragraph_count": inserted_paragraph_count,
                    "change": change_for_result,
                }
            )

    return json_result(
        {
            "status": "not_found",
            "docx_path": str(input_path),
            "old_text": old_text,
            "occurrence": occurrence,
        }
    )


def _prepare_replacement_text(new_text: str, newline_mode: str):
    return split_text_for_paragraphs(new_text, newline_mode)


def _insert_extra_paragraphs(anchor_paragraph, lines):
    current = anchor_paragraph
    count = 0
    for line in lines:
        new_paragraph = make_paragraph_like(anchor_paragraph, line)
        current.addnext(new_paragraph)
        current = new_paragraph
        count += 1
    return count


def _following_paragraphs(anchor_paragraph, count: int):
    result = []
    current = anchor_paragraph
    while len(result) < count:
        current = current.getnext()
        if current is None:
            break
        if current.tag.endswith("}p"):
            result.append(current)
    return result


tools_schema = {
    "type": "function",
    "function": {
        "name": "replace_text",
        "description": "替换 docx 中的指定文本，按段落逻辑文本查找，支持目标文本跨多个 run，并尽量继承原格式。",
        "parameters": {
            "type": "object",
            "properties": {
                "docx_path": {"type": "string", "description": "输入 .docx 文件路径 (相对 workspace 根)"},
                "output_path": {"type": "string", "description": "输出 .docx 文件路径 (相对 workspace 根)"},
                "old_text": {"type": "string", "description": "要替换的原文本"},
                "new_text": {"type": "string", "description": "替换后的新文本"},
                "occurrence": {"type": "integer", "description": "第几个匹配项，1-based，默认 1"},
                "newline_mode": {
                    "type": "string",
                    "description": "新文本包含换行时的处理方式：paragraphs 拆成多个段落，inline 替换为空格；默认 paragraphs",
                    "enum": ["paragraphs", "inline"],
                },
                "format_policy": {
                    "type": "string",
                    "description": "替换后文本的格式策略：preserve 保留原格式，clear 清除直接字符格式，body 转正文格式，custom 使用显式格式；默认 preserve",
                    "enum": ["preserve", "clear", "body", "custom"],
                },
                **CUSTOM_FORMAT_PROPERTIES,
            },
            "required": ["docx_path", "output_path", "old_text", "new_text"],
        },
    },
}
