from lxml import etree

from .common import (
    CUSTOM_FORMAT_PROPERTIES,
    NS,
    W,
    apply_format_policy_to_paragraph,
    apply_format_policy_to_run,
    append_run_to_paragraph,
    insert_paragraphs_after,
    json_result,
    load_document_xml,
    make_paragraph_like,
    paragraph_text,
    split_text_for_paragraphs,
    tables,
    write_document_xml,
    resolve_docx_io,
)


def insert_text_in_table_cell(
    session_id: str,
    docx_path: str,
    output_path: str,
    table_index: int,
    row_index: int,
    cell_index: int,
    insert_text: str,
    paragraph_index: int = 1,
    append: bool = True,
    newline_mode: str = "paragraphs",
    format_policy: str = "preserve",
    **custom,
) -> str:
    """向表格单元格插入文本。表格、行、单元格索引都从 1 开始计数。"""
    input_path, output_path_resolved = resolve_docx_io(session_id, docx_path, output_path)
    root = load_document_xml(str(input_path))
    all_tables = tables(root)
    if table_index < 1 or table_index > len(all_tables):
        return json_result({"status": "error", "message": "table_index out of range", "table_count": len(all_tables)})

    table = all_tables[table_index - 1]
    rows = table.xpath("./w:tr", namespaces=NS)
    if row_index < 1 or row_index > len(rows):
        return json_result({"status": "error", "message": "row_index out of range", "row_count": len(rows)})

    cells = rows[row_index - 1].xpath("./w:tc", namespaces=NS)
    if cell_index < 1 or cell_index > len(cells):
        return json_result({"status": "error", "message": "cell_index out of range", "cell_count": len(cells)})

    cell = cells[cell_index - 1]
    cell_paragraphs = cell.xpath("./w:p", namespaces=NS)
    if not cell_paragraphs:
        paragraph = etree.SubElement(cell, f"{W}p")
        cell_paragraphs = [paragraph]

    if paragraph_index < 1 or paragraph_index > len(cell_paragraphs):
        return json_result(
            {
                "status": "error",
                "message": "paragraph_index out of range",
                "paragraph_count": len(cell_paragraphs),
            }
        )

    paragraph = cell_paragraphs[paragraph_index - 1]
    before_text = paragraph_text(paragraph)
    existing_runs = paragraph.xpath("./w:r", namespaces=NS)
    first_text, extra_paragraphs = split_text_for_paragraphs(insert_text, newline_mode)

    if append and existing_runs:
        new_run = append_run_to_paragraph(paragraph, first_text, existing_runs[-1])
        apply_format_policy_to_run(
            new_run,
            format_policy,
            **custom,
        )
        mode = "append_new_run"
    else:
        source_paragraph = paragraph
        if first_text:
            new_paragraph = make_paragraph_like(source_paragraph, first_text)
            run = new_paragraph.find(f"{W}r")
            paragraph.append(run)
            apply_format_policy_to_run(
                run,
                format_policy,
                **custom,
            )
        mode = "create_run_in_cell_paragraph"

    inserted_paragraph_count = insert_paragraphs_after(paragraph, extra_paragraphs)
    current = paragraph
    for _ in range(inserted_paragraph_count):
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
            "table_index": table_index,
            "row_index": row_index,
            "cell_index": cell_index,
            "paragraph_index": paragraph_index,
            "mode": mode,
            "newline_mode": newline_mode,
            "format_policy": format_policy,
            "inserted_paragraph_count": inserted_paragraph_count,
            "before_text": before_text,
            "after_text": paragraph_text(paragraph),
        }
    )


tools_schema = {
    "type": "function",
    "function": {
        "name": "insert_text_in_table_cell",
        "description": "向指定表格单元格插入文本。适合空白单元格或明确知道第几个表格、第几行、第几列的场景。",
        "parameters": {
            "type": "object",
            "properties": {
                "docx_path": {"type": "string", "description": "输入 .docx 文件路径 (相对 workspace 根)"},
                "output_path": {"type": "string", "description": "输出 .docx 文件路径 (相对 workspace 根)"},
                "table_index": {"type": "integer", "description": "第几个表格，按 //w:tbl 计数，1-based"},
                "row_index": {"type": "integer", "description": "第几行，1-based"},
                "cell_index": {"type": "integer", "description": "第几个单元格，1-based"},
                "insert_text": {"type": "string", "description": "要插入的文本"},
                "paragraph_index": {"type": "integer", "description": "单元格内第几个直接段落，默认 1"},
                "append": {"type": "boolean", "description": "是否追加到现有段落末尾，默认 true"},
                "newline_mode": {
                    "type": "string",
                    "description": "插入文本包含换行时的处理方式：paragraphs 拆成多个单元格内段落，inline 替换为空格；默认 paragraphs",
                    "enum": ["paragraphs", "inline"],
                },
                "format_policy": {
                    "type": "string",
                    "description": "插入后文本的格式策略：preserve 保留原格式，clear 清除直接字符格式，body 转正文格式，custom 使用显式格式；默认 preserve",
                    "enum": ["preserve", "clear", "body", "custom"],
                },
                **CUSTOM_FORMAT_PROPERTIES,
            },
            "required": ["docx_path", "output_path", "table_index", "row_index", "cell_index", "insert_text"],
        },
    },
}
