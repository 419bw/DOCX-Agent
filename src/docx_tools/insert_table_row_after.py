import copy

from .common import (
    CUSTOM_FORMAT_PROPERTIES,
    apply_format_policy_to_paragraph,
    append_run_to_paragraph,
    cell_paragraphs,
    cell_text,
    clear_cell_to_empty_paragraph,
    first_text_run,
    get_row_by_index,
    get_table_by_index,
    insert_paragraphs_after,
    json_result,
    load_document_xml,
    row_cells,
    split_text_for_paragraphs,
    table_rows,
    table_summary,
    write_document_xml,
    resolve_docx_io,
)


def insert_table_row_after(
    session_id: str,
    docx_path: str,
    output_path: str,
    table_index: int,
    row_index: int,
    cell_texts: list[str],
    copy_from: str = "target",
    newline_mode: str = "paragraphs",
    format_policy: str = "preserve",
    **custom,
) -> str:
    """在指定表格行后插入一整行，复制邻近行结构后写入各单元格文本。"""
    input_path, output_path_resolved = resolve_docx_io(session_id, docx_path, output_path)
    root = load_document_xml(str(input_path))
    try:
        table = get_table_by_index(root, table_index)
        target_row = get_row_by_index(table, row_index)
        source_row = _select_source_row(table, row_index, copy_from)
    except (IndexError, ValueError) as exc:
        return json_result({"status": "error", "message": str(exc)})

    before_table = table_summary(table)
    new_row = copy.deepcopy(source_row)
    _write_row_texts(
        new_row,
        cell_texts,
        newline_mode=newline_mode,
        format_policy=format_policy,
        **custom,
    )
    target_row.addnext(new_row)
    write_document_xml(str(input_path), str(output_path_resolved), root)

    return json_result(
        {
            "status": "ok",
            "docx_path": str(input_path),
            "output_path": str(output_path_resolved),
            "table_index": table_index,
            "inserted_after_row_index": row_index,
            "inserted_row_index": row_index + 1,
            "copy_from": copy_from,
            "newline_mode": newline_mode,
            "format_policy": format_policy,
            "cell_texts": cell_texts,
            "before_row_count": len(before_table["rows"]),
            "after_row_count": len(table_rows(table)),
            "inserted_row_cells": [cell_text(cell) for cell in row_cells(new_row)],
            "before_table": before_table,
            "after_table": table_summary(table),
        }
    )


def _select_source_row(table, row_index: int, copy_from: str):
    rows = table_rows(table)
    mode = (copy_from or "target").lower()
    if mode == "target":
        source_index = row_index
    elif mode == "above":
        source_index = row_index
    elif mode == "below":
        source_index = row_index + 1
    else:
        raise ValueError("copy_from must be target, above or below")
    if source_index < 1 or source_index > len(rows):
        raise IndexError(f"copy_from row out of range: {source_index}, row_count={len(rows)}")
    return rows[source_index - 1]


def _write_row_texts(
    row,
    texts,
    newline_mode: str,
    format_policy: str,
    **custom,
):
    cells = row_cells(row)
    for index, cell in enumerate(cells):
        source_run = first_text_run(cell)
        source_paragraphs = cell_paragraphs(cell)
        style_paragraph = copy.deepcopy(source_paragraphs[0]) if source_paragraphs else None
        paragraph = clear_cell_to_empty_paragraph(cell)
        text = texts[index] if index < len(texts) else ""
        first_text, extra_paragraphs = split_text_for_paragraphs(text, newline_mode)
        if first_text:
            run = append_run_to_paragraph(paragraph, first_text, source_run)
            apply_format_policy_to_paragraph(paragraph, format_policy, **custom)
        if extra_paragraphs:
            source_paragraph = style_paragraph if style_paragraph is not None else paragraph
            insert_paragraphs_after(paragraph, extra_paragraphs, style_paragraph=source_paragraph)
            current = paragraph
            for _ in extra_paragraphs:
                current = current.getnext()
                if current is not None:
                    apply_format_policy_to_paragraph(current, format_policy, **custom)


tools_schema = {
    "type": "function",
    "function": {
        "name": "insert_table_row_after",
        "description": "在指定表格行后插入一整行。工具会复制目标行/相邻行的行结构、列宽和单元格属性，再写入每个单元格文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "docx_path": {"type": "string", "description": "输入 .docx 文件路径 (相对 workspace 根)"},
                "output_path": {"type": "string", "description": "输出 .docx 文件路径 (相对 workspace 根)"},
                "table_index": {"type": "integer", "description": "第几个表格，按 //w:tbl 计数，1-based；注意嵌套表格也会计数"},
                "row_index": {"type": "integer", "description": "在哪一行后面插入，1-based"},
                "cell_texts": {
                    "type": "array",
                    "description": "新行每个单元格的文本，按列顺序填写；少于列数的后续单元格会置空",
                    "items": {"type": "string"},
                },
                "copy_from": {
                    "type": "string",
                    "description": "复制哪一行的结构：target/above 复制目标行，below 复制下一行；默认 target",
                    "enum": ["target", "above", "below"],
                },
                "newline_mode": {
                    "type": "string",
                    "description": "单元格文本包含换行时的处理方式：paragraphs 拆成多个单元格内段落，inline 替换为空格；默认 paragraphs",
                    "enum": ["paragraphs", "inline"],
                },
                "format_policy": {
                    "type": "string",
                    "description": "写入文本的格式策略：preserve 保留复制行原格式，clear 清除直接字符格式，body 转正文格式，custom 使用显式格式；默认 preserve",
                    "enum": ["preserve", "clear", "body", "custom"],
                },
                **CUSTOM_FORMAT_PROPERTIES,
            },
            "required": ["docx_path", "output_path", "table_index", "row_index", "cell_texts"],
        },
    },
}
