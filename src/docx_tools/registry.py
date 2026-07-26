import json

from md_tools.apply_markdown_ir_to_table_cell import (
    apply_markdown_ir_to_table_cell,
    tools_schema as apply_markdown_ir_to_table_cell_schema,
)
from md_tools.markdown_to_word import markdown_to_word, tools_schema as markdown_to_word_schema
from md_tools.parse_markdown_draft import parse_markdown_draft, tools_schema as parse_markdown_draft_schema
from md_tools.read_markdown_draft import read_markdown_draft, tools_schema as read_markdown_draft_schema
from md_tools.write_markdown_draft import write_markdown_draft, tools_schema as write_markdown_draft_schema

from .analyze_docx_style_samples import analyze_docx_style_samples, tools_schema as analyze_docx_style_samples_schema
from .clear_table_cell import clear_table_cell, tools_schema as clear_table_cell_schema
from .define_style_profile import define_style_profile, tools_schema as define_style_profile_schema
from .delete_text import delete_text, tools_schema as delete_text_schema
from .delete_table_row import delete_table_row, tools_schema as delete_table_row_schema
from .diff_docx import diff_docx, tools_schema as diff_docx_schema
from .find_text import find_text, tools_schema as find_text_schema
from .insert_image_after_paragraph import (
    insert_image_after_paragraph,
    tools_schema as insert_image_after_paragraph_schema,
)
from .insert_paragraph_after import insert_paragraph_after, tools_schema as insert_paragraph_after_schema
from .insert_paragraph_after_like_sample import (
    insert_paragraph_after_like_sample,
    tools_schema as insert_paragraph_after_like_sample_schema,
)
from .insert_table_after_paragraph import insert_table_after_paragraph, tools_schema as insert_table_after_paragraph_schema
from .insert_table_column_after import insert_table_column_after, tools_schema as insert_table_column_after_schema
from .insert_table_in_cell import insert_table_in_cell, tools_schema as insert_table_in_cell_schema
from .insert_table_row_after import insert_table_row_after, tools_schema as insert_table_row_after_schema
from .insert_text_at import insert_text_at, tools_schema as insert_text_at_schema
from .insert_text_in_table_cell import insert_text_in_table_cell, tools_schema as insert_text_in_table_cell_schema
from .merge_table_cells_horizontal import merge_table_cells_horizontal, tools_schema as merge_table_cells_horizontal_schema
from .read_docx_structure import read_docx_structure, tools_schema as read_docx_structure_schema
from .replace_table_cell_like_sample import (
    replace_table_cell_like_sample,
    tools_schema as replace_table_cell_like_sample_schema,
)
from .replace_table_cell_text import replace_table_cell_text, tools_schema as replace_table_cell_text_schema
from .replace_text_like_sample import replace_text_like_sample, tools_schema as replace_text_like_sample_schema
from .replace_text import replace_text, tools_schema as replace_text_schema
from .set_paragraph_indent import set_paragraph_indent, tools_schema as set_paragraph_indent_schema
from .set_text_format import set_text_format, tools_schema as set_text_format_schema
from .unzip_docx import unzip_docx, tools_schema as unzip_docx_schema

from basic_tools.analyze_image_content import analyze_image_content, tools_schema as analyze_image_content_schema
from basic_tools.generate_image import generate_image, tools_schema as generate_image_schema
from basic_tools.render_diagram import render_diagram, tools_schema as render_diagram_schema
from basic_tools.ls import ls, tools_schema as ls_schema
from basic_tools.read import read, tools_schema as read_schema


TOOLS = {
    "analyze_docx_style_samples": analyze_docx_style_samples,
    "define_style_profile": define_style_profile,
    "read_docx_structure": read_docx_structure,
    "find_text": find_text,
    "write_markdown_draft": write_markdown_draft,
    "read_markdown_draft": read_markdown_draft,
    "parse_markdown_draft": parse_markdown_draft,
    "apply_markdown_ir_to_table_cell": apply_markdown_ir_to_table_cell,
    "markdown_to_word": markdown_to_word,
    "replace_text_like_sample": replace_text_like_sample,
    "insert_paragraph_after_like_sample": insert_paragraph_after_like_sample,
    "replace_table_cell_like_sample": replace_table_cell_like_sample,
    "insert_text_at": insert_text_at,
    "insert_text_in_table_cell": insert_text_in_table_cell,
    "insert_table_row_after": insert_table_row_after,
    "set_paragraph_indent": set_paragraph_indent,
    "insert_table_after_paragraph": insert_table_after_paragraph,
    "insert_table_in_cell": insert_table_in_cell,
    "insert_table_column_after": insert_table_column_after,
    "merge_table_cells_horizontal": merge_table_cells_horizontal,
    "clear_table_cell": clear_table_cell,
    "delete_table_row": delete_table_row,
    "replace_table_cell_text": replace_table_cell_text,
    "replace_text": replace_text,
    "delete_text": delete_text,
    "insert_paragraph_after": insert_paragraph_after,
    "insert_image_after_paragraph": insert_image_after_paragraph,
    "set_text_format": set_text_format,
    "diff_docx": diff_docx,
    "unzip_docx": unzip_docx,
    "ls": ls,
    "read": read,
    "analyze_image_content": analyze_image_content,
    "generate_image": generate_image,
    "render_diagram": render_diagram,
}

TOOLS_SCHEMA = [
    analyze_docx_style_samples_schema,
    define_style_profile_schema,
    read_docx_structure_schema,
    find_text_schema,
    write_markdown_draft_schema,
    read_markdown_draft_schema,
    parse_markdown_draft_schema,
    apply_markdown_ir_to_table_cell_schema,
    markdown_to_word_schema,
    replace_text_like_sample_schema,
    insert_paragraph_after_like_sample_schema,
    replace_table_cell_like_sample_schema,
    insert_text_at_schema,
    insert_text_in_table_cell_schema,
    insert_table_row_after_schema,
    set_paragraph_indent_schema,
    insert_table_after_paragraph_schema,
    insert_table_in_cell_schema,
    insert_table_column_after_schema,
    merge_table_cells_horizontal_schema,
    clear_table_cell_schema,
    delete_table_row_schema,
    replace_table_cell_text_schema,
    replace_text_schema,
    delete_text_schema,
    insert_paragraph_after_schema,
    insert_image_after_paragraph_schema,
    set_text_format_schema,
    diff_docx_schema,
    unzip_docx_schema,
    ls_schema,
    read_schema,
    analyze_image_content_schema,
    generate_image_schema,
    render_diagram_schema,
]


def render_tools_prompt(tool_schemas=None) -> str:
    """生成精简工具说明，方便注入到不支持原生工具调用的 agent 提示词中。"""
    schemas = tool_schemas if tool_schemas is not None else TOOLS_SCHEMA
    lines = [
        "你可以使用以下本地 DOCX 工具。每个工具都是一个独立 Python 文件，便于维护。",
        "当前只允许使用本列表中的工具；没有出现在本列表中的工具不能调用。",
        "",
    ]
    for schema in schemas:
        fn = schema["function"]
        params = fn["parameters"]
        required = params.get("required", [])
        properties = params.get("properties", {})
        lines.append(f"- {fn['name']}: {fn['description']}")
        lines.append(f"  required: {', '.join(required) if required else 'none'}")
        optional = [name for name in properties if name not in required]
        lines.append(f"  optional: {', '.join(optional) if optional else 'none'}")
    return "\n".join(lines)


def call_tool(name: str, arguments: str) -> str:
    if name not in TOOLS:
        return json.dumps({"status": "error", "message": f"unknown tool: {name}"}, ensure_ascii=False)
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    return TOOLS[name](**args)
