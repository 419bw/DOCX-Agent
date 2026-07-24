import copy

from .common import (
    CUSTOM_FORMAT_PROPERTIES,
    apply_format_policy_to_paragraph,
    append_run_to_paragraph,
    cell_paragraphs,
    cell_text,
    clear_cell_to_empty_paragraph,
    first_text_run,
    get_cell_by_index,
    get_row_by_index,
    get_table_by_index,
    insert_paragraphs_after,
    json_result,
    load_document_xml,
    split_text_for_paragraphs,
    table_summary,
    write_document_xml,
    resolve_docx_io,
)


def replace_table_cell_text(
    session_id: str,
    docx_path: str,
    output_path: str,
    table_index: int,
    row_index: int,
    cell_index: int,
    new_text: str,
    newline_mode: str = "paragraphs",
    format_policy: str = "preserve",
    **custom,
) -> str:
    """用坐标定位表格单元格，清空原内容后写入新文本。"""
    input_path, output_path_resolved = resolve_docx_io(session_id, docx_path, output_path)
    root = load_document_xml(str(input_path))
    try:
        table = get_table_by_index(root, table_index)
        row = get_row_by_index(table, row_index)
        cell = get_cell_by_index(row, cell_index)
    except IndexError as exc:
        return json_result({"status": "error", "message": str(exc)})

    before_table = table_summary(table)
    before_text = cell_text(cell)
    source_run = first_text_run(cell)
    source_paragraphs = cell_paragraphs(cell)
    style_paragraph = copy.deepcopy(source_paragraphs[0]) if source_paragraphs else None
    paragraph = clear_cell_to_empty_paragraph(cell)
    first_text, extra_paragraphs = split_text_for_paragraphs(new_text, newline_mode)

    if first_text:
        append_run_to_paragraph(paragraph, first_text, source_run)
        apply_format_policy_to_paragraph(paragraph, format_policy, **custom)

    source_paragraph = style_paragraph if style_paragraph is not None else paragraph
    inserted_paragraph_count = insert_paragraphs_after(paragraph, extra_paragraphs, style_paragraph=source_paragraph)
    current = paragraph
    for _ in range(inserted_paragraph_count):
        current = current.getnext()
        if current is not None:
            apply_format_policy_to_paragraph(current, format_policy, **custom)

    after_text = cell_text(cell)
    write_document_xml(str(input_path), str(output_path_resolved), root)
    return json_result(
        {
            "status": "ok",
            "docx_path": str(input_path),
            "output_path": str(output_path_resolved),
            "table_index": table_index,
            "row_index": row_index,
            "cell_index": cell_index,
            "newline_mode": newline_mode,
            "format_policy": format_policy,
            "before_text": before_text,
            "after_text": after_text,
            "inserted_paragraph_count": inserted_paragraph_count,
            "before_table": before_table,
            "after_table": table_summary(table),
        }
    )


tools_schema = {
    "type": "function",
    "function": {
        "name": "replace_table_cell_text",
        "description": "按表格坐标替换单元格的全部文本。会保留单元格结构，并尽量继承原单元格第一个文本 run 的格式。",
        "parameters": {
            "type": "object",
            "properties": {
                "docx_path": {"type": "string", "description": "输入 .docx 文件路径 (相对 workspace 根)"},
                "output_path": {"type": "string", "description": "输出 .docx 文件路径 (相对 workspace 根)"},
                "table_index": {"type": "integer", "description": "第几个表格，按 //w:tbl 计数，1-based；注意嵌套表格也会计数"},
                "row_index": {"type": "integer", "description": "第几行，1-based"},
                "cell_index": {"type": "integer", "description": "第几个单元格，1-based"},
                "new_text": {"type": "string", "description": "替换后的单元格文本"},
                "newline_mode": {
                    "type": "string",
                    "description": "新文本包含换行时的处理方式：paragraphs 拆成多个单元格内段落，inline 替换为空格；默认 paragraphs",
                    "enum": ["paragraphs", "inline"],
                },
                "format_policy": {
                    "type": "string",
                    "description": "写入文本的格式策略：preserve 保留原格式，clear 清除直接字符格式，body 转正文格式，custom 使用显式格式；默认 preserve",
                    "enum": ["preserve", "clear", "body", "custom"],
                },
                **CUSTOM_FORMAT_PROPERTIES,
            },
            "required": ["docx_path", "output_path", "table_index", "row_index", "cell_index", "new_text"],
        },
    },
}
