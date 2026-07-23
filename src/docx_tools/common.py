import copy
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from lxml import etree

sys.path.append(str(Path(__file__).parent.parent))
from workspace.guard import resolve_workspace_path, WorkspacePathError  # noqa: E402


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}
W = f"{{{W_NS}}}"
XML = f"{{{XML_NS}}}"


def json_result(data) -> str:
    def _clean(val):
        if isinstance(val, str):
            val_normalized = val.replace('\\', '/')
            idx = val_normalized.find("out/sessions/")
            if idx != -1:
                remaining = val_normalized[idx + 13:]
                parts = remaining.split('/', 2)
                if len(parts) >= 3 and parts[1] == "workspace":
                    return parts[2]
                elif len(parts) >= 2 and parts[1] == "workspace":
                    return "."
            return val
        elif isinstance(val, dict):
            return {k: _clean(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [_clean(x) for x in val]
        return val

    cleaned_data = _clean(data)
    return json.dumps(cleaned_data, ensure_ascii=False, indent=2)


def resolve_docx_io(session_id: str, docx_path: str, output_path: str):
    """v2: docx 工具统一解析输入/输出路径 (沙箱化)

    Returns:
        (input_path: Path, output_path: Path) — 都已 resolve, 在 workspace 内

    Raises:
        WorkspacePathError
    """
    input_path = resolve_workspace_path(session_id, docx_path, must_exist=True, must_be_file=True)
    output_path_resolved = resolve_workspace_path(session_id, output_path, must_exist=False)
    return input_path, output_path_resolved


def load_document_xml(docx_path: str):
    with zipfile.ZipFile(docx_path, "r") as docx:
        xml_bytes = docx.read("word/document.xml")
    return etree.fromstring(xml_bytes)


def write_document_xml(input_docx: str, output_docx: str, document_root) -> None:
    # v3: 同路径防御 — input == output 时先写临时文件再原子替换, 避免 "w" 模式截断正在读的文件
    same_file = Path(input_docx).resolve() == Path(output_docx).resolve()
    actual_output = str(Path(output_docx).with_suffix(".docx.tmp")) if same_file else output_docx

    output_path = Path(actual_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. 扫描 document_root 中的所有 r:embed 属性，寻找以 TEMP_IMG_REL: 开头的值
    # 临时占位符格式为: TEMP_IMG_REL:image_path
    R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    embed_attrib_key = f"{{{R_NS}}}embed"
    
    nodes_with_embed = document_root.xpath("//*[@r:embed]", namespaces={"r": R_NS})
    
    image_replacements = {}  # TEMP_IMG_REL:path -> real_rId
    images_to_add = {}       # local_path -> zip_target_name (e.g. media/image2.png)
    
    has_images = False
    for node in nodes_with_embed:
        val = node.get(embed_attrib_key, "")
        if val.startswith("TEMP_IMG_REL:"):
            has_images = True
            break
            
    rels_bytes = None
    content_types_bytes = None
    
    if has_images:
        with zipfile.ZipFile(input_docx, "r") as z:
            rels_bytes = z.read("word/_rels/document.xml.rels")
            content_types_bytes = z.read("[Content_Types].xml")
            
        rels_root = etree.fromstring(rels_bytes)
        content_types_root = etree.fromstring(content_types_bytes)
        
        # 解析最大 rId 和最大 media 图片名
        max_rId_num = 0
        max_img_num = 0
        for rel in rels_root.xpath("//*[local-name()='Relationship']"):
            r_id = rel.get("Id", "")
            if r_id.startswith("rId"):
                try:
                    num = int(r_id[3:])
                    if num > max_rId_num:
                        max_rId_num = num
                except ValueError:
                    pass
            target = rel.get("Target", "")
            if target.startswith("media/image"):
                name_part = target[11:]
                parts = name_part.split(".")
                if parts:
                    try:
                        num = int(parts[0])
                        if num > max_img_num:
                            max_img_num = num
                    except ValueError:
                        pass
                        
        RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
        TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
        
        for node in nodes_with_embed:
            val = node.get(embed_attrib_key, "")
            if val.startswith("TEMP_IMG_REL:"):
                img_path_str = val[13:]  # 提取路径
                if val not in image_replacements:
                    # 分配新的 rId 和图片名
                    max_rId_num += 1
                    max_img_num += 1
                    new_rId = f"rId{max_rId_num}"
                    
                    img_file = Path(img_path_str)
                    image_ext = img_file.suffix.lower().lstrip(".")
                    if not image_ext:
                        image_ext = "png"
                    if image_ext == "jpg":
                        image_ext = "jpeg"
                        
                    new_image_name = f"image{max_img_num}.{image_ext}"
                    new_target = f"media/{new_image_name}"
                    
                    # 注册 Relationship
                    new_rel = etree.Element(
                        f"{{{RELS_NS}}}Relationship",
                        Id=new_rId,
                        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
                        Target=new_target
                    )
                    rels_root.append(new_rel)
                    
                    # 注册 ContentType
                    ext_declared = False
                    for default in content_types_root.xpath("//*[local-name()='Default']"):
                        if default.get("Extension", "").lower() == image_ext:
                            ext_declared = True
                            break
                    if not ext_declared:
                        mime_type = "image/png" if image_ext == "png" else "image/jpeg"
                        new_default = etree.Element(
                            f"{{{TYPES_NS}}}Default",
                            Extension=image_ext,
                            ContentType=mime_type
                        )
                        defaults = content_types_root.xpath("//*[local-name()='Default']")
                        if defaults:
                            defaults[-1].addnext(new_default)
                        else:
                            content_types_root.append(new_default)
                            
                    image_replacements[val] = new_rId
                    images_to_add[img_path_str] = new_target
                
                # 替换属性值为真实的 rId
                node.set(embed_attrib_key, image_replacements[val])
                
        # 序列化修改后的 manifests 字节
        rels_bytes = etree.tostring(rels_root, encoding="UTF-8", xml_declaration=True, standalone=True)
        content_types_bytes = etree.tostring(content_types_root, encoding="UTF-8", xml_declaration=True, standalone=True)

    # 2. 序列化 document_root 字节
    document_bytes = etree.tostring(
        document_root,
        encoding="UTF-8",
        xml_declaration=True,
        standalone=True,
    )

    # 3. 写回 ZIP 文件并注入媒体文件
    with zipfile.ZipFile(input_docx, "r") as zin:
        with zipfile.ZipFile(actual_output, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "word/document.xml":
                    zout.writestr(item, document_bytes)
                elif item.filename == "word/_rels/document.xml.rels" and rels_bytes is not None:
                    zout.writestr(item, rels_bytes)
                elif item.filename == "[Content_Types].xml" and content_types_bytes is not None:
                    zout.writestr(item, content_types_bytes)
                else:
                    zout.writestr(item, zin.read(item.filename))
            
            # 写入所有新增的图片二进制文件
            for local_path_str, zip_target in images_to_add.items():
                local_path = Path(local_path_str)
                if local_path.exists():
                    with open(local_path, "rb") as f_img:
                        zout.writestr(f"word/{zip_target}", f_img.read())

    # v3: 同路径时原子替换 (临时文件 → 原文件)
    if same_file:
        import os
        os.replace(actual_output, output_docx)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def paragraph_text(paragraph) -> str:
    return "".join(t.text or "" for t in paragraph.xpath(".//w:t", namespaces=NS))


def paragraph_text_segments(paragraph):
    segments = []
    cursor = 0
    for text_node in paragraph.xpath(".//w:t", namespaces=NS):
        value = text_node.text or ""
        start = cursor
        end = start + len(value)
        run = nearest_ancestor(text_node, f"{W}r")
        segments.append(
            {
                "start": start,
                "end": end,
                "text_node": text_node,
                "run": run,
                "text": value,
            }
        )
        cursor = end
    return segments


def nearest_ancestor(node, tag):
    current = node.getparent()
    while current is not None:
        if current.tag == tag:
            return current
        current = current.getparent()
    return None


def paragraphs(document_root):
    return document_root.xpath("//w:p", namespaces=NS)


def tables(document_root):
    return document_root.xpath("//w:tbl", namespaces=NS)


def table_rows(table):
    return table.xpath("./w:tr", namespaces=NS)


def row_cells(row):
    return row.xpath("./w:tc", namespaces=NS)


def cell_paragraphs(cell):
    return cell.xpath("./w:p", namespaces=NS)


def cell_text(cell) -> str:
    return "".join(t.text or "" for t in cell.xpath(".//w:t", namespaces=NS))


def row_text(row) -> str:
    return "".join(cell_text(cell) for cell in row_cells(row))


def get_table_by_index(document_root, table_index: int):
    all_tables = tables(document_root)
    if table_index < 1 or table_index > len(all_tables):
        raise IndexError(f"table_index out of range: {table_index}, table_count={len(all_tables)}")
    return all_tables[table_index - 1]


def get_row_by_index(table, row_index: int):
    rows = table_rows(table)
    if row_index < 1 or row_index > len(rows):
        raise IndexError(f"row_index out of range: {row_index}, row_count={len(rows)}")
    return rows[row_index - 1]


def get_cell_by_index(row, cell_index: int):
    cells = row_cells(row)
    if cell_index < 1 or cell_index > len(cells):
        raise IndexError(f"cell_index out of range: {cell_index}, cell_count={len(cells)}")
    return cells[cell_index - 1]


def table_summary(table):
    return {
        "row_count": len(table_rows(table)),
        "rows": [
            {
                "row_index": row_index,
                "text": row_text(row),
                "cells": [cell_text(cell) for cell in row_cells(row)],
            }
            for row_index, row in enumerate(table_rows(table), start=1)
        ],
    }


def clear_cell_to_empty_paragraph(cell):
    """清空单元格内容，保留 tcPr，并确保至少有一个空段落。"""
    existing_paragraphs = cell_paragraphs(cell)
    source_ppr = None
    if existing_paragraphs:
        ppr = existing_paragraphs[0].find(f"{W}pPr")
        if ppr is not None:
            source_ppr = copy.deepcopy(ppr)

    for child in list(cell):
        if child.tag != f"{W}tcPr":
            cell.remove(child)

    paragraph = etree.Element(f"{W}p", nsmap=cell.nsmap)
    if source_ppr is not None:
        paragraph.append(source_ppr)
    cell.append(paragraph)
    return paragraph


def first_text_run(element):
    for run in element.xpath(".//w:r", namespaces=NS):
        if "".join(t.text or "" for t in run.xpath(".//w:t", namespaces=NS)):
            return run
    runs = element.xpath(".//w:r", namespaces=NS)
    return runs[0] if runs else None


def element_index_among_same_tag(element) -> int:
    parent = element.getparent()
    if parent is None:
        return -1
    same = [child for child in parent if child.tag == element.tag]
    return same.index(element) + 1


def paragraph_location(paragraph):
    table_cell = nearest_ancestor(paragraph, f"{W}tc")
    if table_cell is None:
        return {"kind": "paragraph", "body_index": body_child_index(paragraph)}

    row = nearest_ancestor(paragraph, f"{W}tr")
    table = nearest_ancestor(paragraph, f"{W}tbl")
    return {
        "kind": "table_cell",
        "body_index": body_child_index(table),
        "table_index_near_parent": element_index_among_same_tag(table),
        "row_index": element_index_among_same_tag(row),
        "cell_index": element_index_among_same_tag(table_cell),
    }


def body_child_index(element) -> int:
    current = element
    while current.getparent() is not None and current.getparent().tag != f"{W}body":
        current = current.getparent()
    parent = current.getparent()
    if parent is None:
        return -1
    return list(parent).index(current) + 1


def set_text_preserve_space(text_node, value: str) -> None:
    text_node.text = value
    if value.startswith(" ") or value.endswith(" ") or "  " in value:
        text_node.set(f"{XML}space", "preserve")
    elif f"{XML}space" in text_node.attrib:
        del text_node.attrib[f"{XML}space"]


def make_run_like(source_run, text: str):
    new_run = etree.Element(f"{W}r", nsmap=source_run.nsmap)
    rpr = source_run.find(f"{W}rPr")
    if rpr is not None:
        new_run.append(copy.deepcopy(rpr))
    text_node = etree.SubElement(new_run, f"{W}t")
    set_text_preserve_space(text_node, text)
    return new_run


def append_run_to_paragraph(paragraph, text: str, source_run=None):
    if source_run is None:
        existing_runs = paragraph.xpath("./w:r", namespaces=NS)
        source_run = existing_runs[-1] if existing_runs else etree.Element(f"{W}r")
    new_run = make_run_like(source_run, text)
    paragraph.append(new_run)
    return new_run


def make_paragraph_like(style_paragraph, text: str):
    """复制一个段落的段落属性和最后一个文本 run 的格式，创建新段落。"""
    new_paragraph = etree.Element(f"{W}p", nsmap=style_paragraph.nsmap)
    ppr = style_paragraph.find(f"{W}pPr")
    if ppr is not None:
        new_paragraph.append(copy.deepcopy(ppr))

    new_run = etree.SubElement(new_paragraph, f"{W}r")
    source_run = select_source_run(style_paragraph)
    if source_run is not None:
        rpr = source_run.find(f"{W}rPr")
        if rpr is not None:
            new_run.append(copy.deepcopy(rpr))

    text_node = etree.SubElement(new_run, f"{W}t")
    set_text_preserve_space(text_node, text)
    return new_paragraph


def split_text_for_paragraphs(text: str, newline_mode: str = "paragraphs"):
    """按统一规则处理换行：拆段落或转为空格。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in normalized:
        return normalized, []

    mode = (newline_mode or "paragraphs").lower()
    if mode == "inline":
        return normalized.replace("\n", " "), []
    if mode != "paragraphs":
        raise ValueError("newline_mode must be paragraphs or inline")

    lines = normalized.split("\n")
    return lines[0], lines[1:]


def insert_paragraphs_after(anchor_paragraph, lines, style_paragraph=None) -> int:
    """在指定段落后连续插入多个段落，返回插入数量。"""
    if not lines:
        return 0
    current = anchor_paragraph
    source = style_paragraph if style_paragraph is not None else anchor_paragraph
    count = 0
    for line in lines:
        new_paragraph = make_paragraph_like(source, line)
        current.addnext(new_paragraph)
        current = new_paragraph
        count += 1
    return count


def select_source_run(paragraph):
    runs = paragraph.xpath("./w:r", namespaces=NS)
    for run in reversed(runs):
        if "".join(t.text or "" for t in run.xpath(".//w:t", namespaces=NS)):
            return run
    return runs[-1] if runs else None


def isolate_text_range_in_paragraph(paragraph, start: int, end: int):
    """把段落逻辑文本范围拆成独立 run，返回覆盖该范围的 run 列表。"""
    if start < 0 or end <= start:
        raise ValueError("invalid text range")

    segments = paragraph_text_segments(paragraph)
    affected = [seg for seg in segments if seg["end"] > start and seg["start"] < end]
    if not affected:
        raise ValueError("text range does not touch any text node")

    if affected[0]["text_node"] is affected[-1]["text_node"]:
        seg = affected[0]
        local_start = start - seg["start"]
        local_end = end - seg["start"]
        return _split_single_segment(seg, local_start, local_end)

    result_runs = []
    first = affected[0]
    first_local_start = start - first["start"]
    if first_local_start > 0:
        split_runs = _split_single_segment(first, first_local_start, len(first["text"]))
        result_runs.extend(split_runs)
    else:
        result_runs.append(first["run"])

    for seg in affected[1:-1]:
        if seg["run"] not in result_runs:
            result_runs.append(seg["run"])

    last = affected[-1]
    last_local_end = end - last["start"]
    if last_local_end < len(last["text"]):
        split_runs = _split_single_segment(last, 0, last_local_end)
        result_runs.extend([run for run in split_runs if run not in result_runs])
    elif last["run"] not in result_runs:
        result_runs.append(last["run"])

    cleanup_empty_text_runs(paragraph)
    return result_runs


def _split_single_segment(segment, local_start: int, local_end: int):
    text = segment["text"]
    run = segment["run"]
    parent = run.getparent()
    if parent is None:
        raise ValueError("run has no parent")

    before = text[:local_start]
    middle = text[local_start:local_end]
    after = text[local_end:]
    index = parent.index(run)
    parent.remove(run)

    inserted = []
    offset = 0
    if before:
        parent.insert(index + offset, make_run_like(run, before))
        offset += 1
    middle_run = make_run_like(run, middle)
    parent.insert(index + offset, middle_run)
    inserted.append(middle_run)
    offset += 1
    if after:
        parent.insert(index + offset, make_run_like(run, after))
    return inserted


def replace_text_range_in_paragraph(paragraph, start: int, end: int, replacement: str):
    """在一个段落的逻辑文本范围内替换内容，支持跨多个 run。"""
    if start < 0 or end < start:
        raise ValueError("invalid text range")

    segments = paragraph_text_segments(paragraph)
    if not segments:
        raise ValueError("target paragraph has no text node")

    affected = [seg for seg in segments if seg["end"] > start and seg["start"] < end]
    if not affected and start == end:
        raise ValueError("empty range replacement is not supported here; use insert_text_at")
    if not affected:
        raise ValueError("text range does not touch any text node")

    first = affected[0]
    last = affected[-1]
    first_local_start = max(0, start - first["start"])
    last_local_end = min(len(last["text"]), end - last["start"])

    if first["text_node"] is last["text_node"]:
        before = first["text"][:first_local_start]
        after = first["text"][last_local_end:]
        set_text_preserve_space(first["text_node"], before + replacement + after)
        cleanup_empty_text_runs(paragraph)
        return {
            "mode": "single_run_replace" if replacement else "single_run_delete",
            "before": before,
            "after": after,
            "affected_runs": 1,
            "run": first["run"],
        }

    before = first["text"][:first_local_start]
    after = last["text"][last_local_end:]
    set_text_preserve_space(first["text_node"], before + replacement + after)

    removed_runs = []
    first_run = first["run"]
    for segment in affected[1:]:
        run = segment["run"]
        if run is first_run:
            set_text_preserve_space(segment["text_node"], "")
            continue
        if id(run) in removed_runs:
            continue
        parent = run.getparent()
        if parent is not None and _can_remove_text_run(run):
            parent.remove(run)
            removed_runs.append(id(run))
        else:
            set_text_preserve_space(segment["text_node"], "")

    cleanup_empty_text_runs(paragraph)
    return {
        "mode": "multi_run_replace" if replacement else "multi_run_delete",
        "before": before,
        "after": after,
        "affected_runs": len({id(seg["run"]) for seg in affected}),
        "removed_runs": len(removed_runs),
        "run": first_run,
    }


def cleanup_empty_text_runs(paragraph) -> int:
    """清理没有文本且没有图片、换行、制表符等非文本内容的 run。"""
    removed = 0
    for run in list(paragraph.xpath("./w:r", namespaces=NS)):
        texts = run.xpath(".//w:t", namespaces=NS)
        has_text = any((t.text or "") for t in texts)
        if not has_text and _can_remove_text_run(run):
            parent = run.getparent()
            if parent is not None:
                parent.remove(run)
                removed += 1
    return removed


def _can_remove_text_run(run) -> bool:
    for child in run:
        if child.tag in {f"{W}rPr", f"{W}t"}:
            continue
        return False
    return True


def apply_format_policy_to_paragraph(paragraph, format_policy: str = "preserve", **custom):
    """对段落内所有直接文本 run 应用格式策略。"""
    changed = 0
    for run in paragraph.xpath("./w:r", namespaces=NS):
        if apply_format_policy_to_run(run, format_policy, **custom):
            changed += 1
    return changed


def apply_format_policy_to_run(run, format_policy: str = "preserve", **custom):
    """对单个 run 应用格式策略。"""
    policy = (format_policy or "preserve").lower()
    if policy == "preserve":
        return False
    if policy == "clear":
        clear_direct_run_format(run)
        return True
    if policy == "body":
        apply_body_format(run)
        return True
    if policy == "custom":
        apply_custom_run_format(run, **custom)
        return True
    raise ValueError("format_policy must be preserve, clear, body or custom")


def clear_direct_run_format(run):
    """清除常见直接字符格式，保留字体信息。"""
    rpr = run.find(f"{W}rPr")
    if rpr is None:
        return
    for tag in ("color", "b", "bCs", "i", "iCs", "sz", "szCs", "highlight", "u", "shd"):
        _remove_rpr_child(rpr, tag)
    _remove_empty_rpr(run)


def apply_body_format(run, font_size_half_points: int | None = None):
    """应用普通正文策略：去颜色、去加粗，必要时设置字号。"""
    rpr = _ensure_rpr(run)
    for tag in ("color", "b", "bCs", "highlight", "shd"):
        _remove_rpr_child(rpr, tag)
    if font_size_half_points is not None:
        set_run_font_size(run, font_size_half_points)
    _remove_empty_rpr(run)


def apply_custom_run_format(
    run,
    color: str | None = None,
    bold: bool | None = None,
    font_size_half_points: int | None = None,
    font_size_pt: float | None = None,
):
    """应用显式字符格式。字号优先使用 half-points；也可传 pt。"""
    if color is not None:
        set_run_color(run, color)
    if bold is not None:
        set_run_bold(run, bold)
    if font_size_half_points is None and font_size_pt is not None:
        font_size_half_points = int(round(font_size_pt * 2))
    if font_size_half_points is not None:
        set_run_font_size(run, font_size_half_points)
    _remove_empty_rpr(run)


def apply_sample_format_to_paragraph(paragraph, style_sample: dict):
    """把样式样本中的段落属性和 run 属性应用到段落。"""
    apply_sample_paragraph_format(paragraph, style_sample)
    for run in paragraph.xpath("./w:r", namespaces=NS):
        apply_sample_format_to_run(run, style_sample)


def apply_sample_paragraph_format(paragraph, style_sample: dict):
    paragraph_format = style_sample.get("paragraph_format") or {}
    style_id = paragraph_format.get("style_id")
    alignment = paragraph_format.get("alignment")
    if style_id is None and alignment is None:
        return
    ppr = paragraph.find(f"{W}pPr")
    if ppr is None:
        ppr = etree.Element(f"{W}pPr")
        paragraph.insert(0, ppr)
    _remove_ppr_child(ppr, "pStyle")
    _remove_ppr_child(ppr, "jc")
    if style_id:
        elem = etree.Element(f"{W}pStyle")
        elem.set(f"{W}val", style_id)
        ppr.append(elem)
    if alignment:
        elem = etree.Element(f"{W}jc")
        elem.set(f"{W}val", alignment)
        ppr.append(elem)


def apply_sample_format_to_run(run, style_sample: dict):
    fmt = style_sample.get("format") or {}
    rpr = _ensure_rpr(run)
    for tag in ("b", "bCs", "i", "iCs", "color", "highlight", "sz", "szCs", "rFonts"):
        _remove_rpr_child(rpr, tag)

    fonts = etree.Element(f"{W}rFonts")
    has_fonts = False
    if fmt.get("font_ascii"):
        fonts.set(f"{W}ascii", fmt["font_ascii"])
        fonts.set(f"{W}hAnsi", fmt["font_ascii"])
        has_fonts = True
    if fmt.get("font_east_asia"):
        fonts.set(f"{W}eastAsia", fmt["font_east_asia"])
        has_fonts = True
    if has_fonts:
        rpr.append(fonts)

    if fmt.get("bold"):
        rpr.append(etree.Element(f"{W}b"))
    if fmt.get("bold_cs"):
        rpr.append(etree.Element(f"{W}bCs"))
    if fmt.get("italic"):
        rpr.append(etree.Element(f"{W}i"))
        rpr.append(etree.Element(f"{W}iCs"))
    if fmt.get("color"):
        elem = etree.Element(f"{W}color")
        elem.set(f"{W}val", fmt["color"])
        rpr.append(elem)
    if fmt.get("highlight"):
        elem = etree.Element(f"{W}highlight")
        elem.set(f"{W}val", fmt["highlight"])
        rpr.append(elem)
    if fmt.get("font_size_half_points"):
        elem = etree.Element(f"{W}sz")
        elem.set(f"{W}val", str(fmt["font_size_half_points"]))
        rpr.append(elem)
    if fmt.get("font_size_cs_half_points"):
        elem = etree.Element(f"{W}szCs")
        elem.set(f"{W}val", str(fmt["font_size_cs_half_points"]))
        rpr.append(elem)
    _remove_empty_rpr(run)


def set_run_color(run, color: str | None):
    rpr = _ensure_rpr(run)
    _remove_rpr_child(rpr, "color")
    if color:
        elem = etree.Element(f"{W}color")
        elem.set(f"{W}val", color.upper().lstrip("#"))
        rpr.append(elem)


def set_run_bold(run, enabled: bool):
    rpr = _ensure_rpr(run)
    _remove_rpr_child(rpr, "b")
    _remove_rpr_child(rpr, "bCs")
    if enabled:
        rpr.append(etree.Element(f"{W}b"))
        rpr.append(etree.Element(f"{W}bCs"))


def set_run_font_size(run, half_points: int):
    rpr = _ensure_rpr(run)
    _remove_rpr_child(rpr, "sz")
    _remove_rpr_child(rpr, "szCs")
    for tag in ("sz", "szCs"):
        elem = etree.Element(f"{W}{tag}")
        elem.set(f"{W}val", str(int(half_points)))
        rpr.append(elem)


def _ensure_rpr(run):
    rpr = run.find(f"{W}rPr")
    if rpr is None:
        rpr = etree.Element(f"{W}rPr")
        run.insert(0, rpr)
    return rpr


def _remove_rpr_child(rpr, local_name: str):
    for child in list(rpr):
        if child.tag == f"{W}{local_name}":
            rpr.remove(child)


def _remove_ppr_child(ppr, local_name: str):
    for child in list(ppr):
        if child.tag == f"{W}{local_name}":
            ppr.remove(child)


def _remove_empty_rpr(run):
    rpr = run.find(f"{W}rPr")
    if rpr is not None and len(rpr) == 0 and not rpr.attrib:
        run.remove(rpr)
