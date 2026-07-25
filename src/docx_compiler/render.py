import copy

from lxml import etree

try:
    from docx_tools.common import (
        NS,
        W,
        XML,
        apply_sample_format_to_paragraph,
        set_run_bold,
        set_text_preserve_space,
    )
except ModuleNotFoundError:
    from src.docx_tools.common import (
        NS,
        W,
        XML,
        apply_sample_format_to_paragraph,
        set_run_bold,
        set_text_preserve_space,
    )

from .ir import CellIR, CodeBlockIR, FormulaIR, ParagraphIR, RunIR, TableIR, ImageIR
from .optimizer import optimize_paragraph

DEFAULT_TABLE_WIDTH_TWIPS = 9000
MIN_COLUMN_WIDTH_TWIPS = 900


def render_blocks_to_container(
    container,
    blocks: list[ParagraphIR | TableIR | CodeBlockIR | FormulaIR | ImageIR],
    style_samples: dict[str, dict] | None = None,
    clear_existing: bool = False,
) -> list:
    """Render block IR into a body-like or cell-like OpenXML container."""
    if clear_existing:
        for child in list(container):
            if child.tag != f"{W}tcPr":
                container.remove(child)

    rendered = []
    for block in blocks:
        if isinstance(block, ParagraphIR):
            element = render_paragraph(block, style_samples=style_samples)
        elif isinstance(block, TableIR):
            element = render_table(
                block,
                style_samples=style_samples,
                available_width_twips=container_table_width_twips(container),
            )
        elif isinstance(block, CodeBlockIR):
            element = render_code_block(block, style_samples=style_samples)
        elif isinstance(block, FormulaIR):
            element = render_formula(block, style_samples=style_samples)
        elif isinstance(block, ImageIR):
            element = render_image(block, style_samples=style_samples)
        else:
            raise TypeError(f"unsupported block IR: {type(block).__name__}")
        elements = element if isinstance(element, list) else [element]
        for item in elements:
            container.append(item)
            rendered.append(item)

    if container.tag == f"{W}tc" and (len(container) == 1 or container[-1].tag != f"{W}p"):
        container.append(etree.Element(f"{W}p", nsmap=container.nsmap))
    return rendered


def render_paragraph(paragraph_ir: ParagraphIR, style_samples: dict[str, dict] | None = None, template_paragraph=None):
    nsmap = template_paragraph.nsmap if template_paragraph is not None else None
    paragraph = etree.Element(f"{W}p", nsmap=nsmap)
    if template_paragraph is not None:
        ppr = template_paragraph.find(f"{W}pPr")
        if ppr is not None:
            paragraph.append(copy.deepcopy(ppr))

    run_flags = []
    for run_ir in paragraph_ir.runs:
        run = _append_run(paragraph, run_ir)
        if run is not None:
            run_flags.append((run, run_ir))

    if not run_flags:
        paragraph.append(etree.Element(f"{W}r", nsmap=paragraph.nsmap))

    style_sample = None
    if style_samples and paragraph_ir.style_sample_id:
        style_sample = style_samples.get(paragraph_ir.style_sample_id)
    if style_sample:
        apply_sample_format_to_paragraph(paragraph, style_sample)

    for run, run_ir in run_flags:
        if run_ir.bold:
            set_run_bold(run, True)
        if run_ir.italic:
            _set_run_italic(run, True)
        if run_ir.code:
            _set_run_code_font(run)

    _apply_paragraph_indent(paragraph, paragraph_ir)
    optimize_paragraph(paragraph)
    return paragraph


def render_code_block(code_ir: CodeBlockIR, style_samples: dict[str, dict] | None = None) -> list:
    normalized = code_ir.code.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        lines = [""]

    paragraphs = []
    for line in lines:
        paragraph_ir = ParagraphIR(
            runs=_code_line_runs(line),
            style_sample_id=code_ir.style_sample_id,
            block_id=code_ir.block_id,
            block_type="code_block",
            line_start=code_ir.line_start,
            line_end=code_ir.line_end,
        )
        paragraph = render_paragraph(paragraph_ir, style_samples=style_samples)
        _apply_code_format(paragraph)
        paragraphs.append(paragraph)
    return paragraphs


def render_formula(formula_ir: FormulaIR, style_samples: dict[str, dict] | None = None):
    text = formula_ir.source.strip()
    paragraph_ir = ParagraphIR(
        runs=_parse_text_runs(text),
        style_sample_id=formula_ir.style_sample_id,
        block_id=formula_ir.block_id,
        block_type="formula_block",
        line_start=formula_ir.line_start,
        line_end=formula_ir.line_end,
    )
    return render_paragraph(paragraph_ir, style_samples=style_samples)


def render_table(
    table_ir: TableIR,
    style_samples: dict[str, dict] | None = None,
    template_table=None,
    available_width_twips: int | None = None,
):
    table = etree.Element(f"{W}tbl", nsmap=template_table.nsmap if template_table is not None else None)
    if template_table is not None:
        tbl_pr = template_table.find(f"{W}tblPr")
        if tbl_pr is not None:
            table.append(copy.deepcopy(tbl_pr))
    else:
        tbl_pr = etree.SubElement(table, f"{W}tblPr")
        tbl_w = etree.SubElement(tbl_pr, f"{W}tblW")
        width = _table_width(table_ir, available_width_twips)
        if width is None:
            tbl_w.set(f"{W}w", "0")
            tbl_w.set(f"{W}type", "auto")
        else:
            tbl_w.set(f"{W}w", str(width))
            tbl_w.set(f"{W}type", "dxa")
        _append_default_borders(tbl_pr)

    widths = table_ir.column_widths_twips or _default_widths(table_ir, available_width_twips)
    grid = etree.SubElement(table, f"{W}tblGrid")
    for width in widths:
        col = etree.SubElement(grid, f"{W}gridCol")
        col.set(f"{W}w", str(int(width)))

    for row_ir in table_ir.rows:
        row = etree.SubElement(table, f"{W}tr")
        for index, cell_ir in enumerate(row_ir.cells):
            row.append(_render_cell(cell_ir, style_samples, widths[index] if index < len(widths) else None))
    return table


def _render_cell(cell_ir: CellIR, style_samples: dict[str, dict] | None, width_twips: int | None):
    cell = etree.Element(f"{W}tc")
    tc_pr = etree.SubElement(cell, f"{W}tcPr")
    width = cell_ir.width_twips if cell_ir.width_twips is not None else width_twips
    if width is not None:
        tc_w = etree.SubElement(tc_pr, f"{W}tcW")
        tc_w.set(f"{W}w", str(int(width)))
        tc_w.set(f"{W}type", "dxa")
    render_blocks_to_container(cell, cell_ir.blocks, style_samples=style_samples)
    return cell


def _append_run(paragraph, run_ir: RunIR):
    if run_ir.kind == "text" and run_ir.text == "":
        return None
    run = etree.SubElement(paragraph, f"{W}r")
    if run_ir.kind == "tab":
        etree.SubElement(run, f"{W}tab")
    elif run_ir.kind == "break":
        etree.SubElement(run, f"{W}br")
    else:
        _append_text_fragments(run, run_ir.text)
    return run


def _code_line_runs(line: str) -> list[RunIR]:
    runs = _parse_text_runs(line)
    return runs or [RunIR.text_run("")]


def _parse_text_runs(text: str) -> list[RunIR]:
    runs = []
    token = []
    for char in text:
        if char == "\t":
            if token:
                runs.append(RunIR.text_run("".join(token)))
                token = []
            runs.append(RunIR.tab())
        elif char == "\n":
            if token:
                runs.append(RunIR.text_run("".join(token)))
                token = []
            runs.append(RunIR.line_break())
        else:
            token.append(char)
    if token:
        runs.append(RunIR.text_run("".join(token)))
    return runs


def _append_text_fragments(run, text: str) -> None:
    token = []
    for char in text:
        if char == "\t":
            _flush_text_token(run, token)
            etree.SubElement(run, f"{W}tab")
        elif char == "\n":
            _flush_text_token(run, token)
            etree.SubElement(run, f"{W}br")
        else:
            token.append(char)
    _flush_text_token(run, token)


def _flush_text_token(run, token: list[str]) -> None:
    if not token:
        return
    value = "".join(token)
    text_node = etree.SubElement(run, f"{W}t")
    set_text_preserve_space(text_node, value)
    if "  " in value:
        text_node.set(f"{XML}space", "preserve")
    token.clear()


def _apply_paragraph_indent(paragraph, paragraph_ir: ParagraphIR) -> None:
    indent = paragraph_ir.indent
    if indent is None and paragraph_ir.block_type == "list_item":
        level = int(paragraph_ir.list_level or 0)
        indent = type("Indent", (), {"left_twips": 360 * (level + 1), "first_line_twips": None, "hanging_twips": 180})()
    if indent is None or getattr(indent, "is_empty", lambda: False)():
        return

    ppr = paragraph.find(f"{W}pPr")
    if ppr is None:
        ppr = etree.Element(f"{W}pPr")
        paragraph.insert(0, ppr)
    for child in list(ppr):
        if child.tag == f"{W}ind":
            ppr.remove(child)
    ind = etree.Element(f"{W}ind")
    if indent.left_twips is not None:
        ind.set(f"{W}left", str(int(indent.left_twips)))
    if indent.first_line_twips is not None:
        ind.set(f"{W}firstLine", str(int(indent.first_line_twips)))
    if indent.hanging_twips is not None:
        ind.set(f"{W}hanging", str(int(indent.hanging_twips)))
    ppr.append(ind)


def _set_run_italic(run, enabled: bool) -> None:
    rpr = run.find(f"{W}rPr")
    if rpr is None:
        rpr = etree.Element(f"{W}rPr")
        run.insert(0, rpr)
    for child in list(rpr):
        if child.tag in {f"{W}i", f"{W}iCs"}:
            rpr.remove(child)
    if enabled:
        rpr.append(etree.Element(f"{W}i"))
        rpr.append(etree.Element(f"{W}iCs"))


def _set_run_code_font(run) -> None:
    rpr = run.find(f"{W}rPr")
    if rpr is None:
        rpr = etree.Element(f"{W}rPr")
        run.insert(0, rpr)
    for child in list(rpr):
        if child.tag == f"{W}rFonts":
            rpr.remove(child)
    fonts = etree.Element(f"{W}rFonts")
    fonts.set(f"{W}ascii", "Consolas")
    fonts.set(f"{W}hAnsi", "Consolas")
    fonts.set(f"{W}eastAsia", "Consolas")
    rpr.insert(0, fonts)


def _apply_code_format(paragraph) -> None:
    for run in paragraph.xpath("./w:r", namespaces=NS):
        rpr = run.find(f"{W}rPr")
        if rpr is None:
            rpr = etree.Element(f"{W}rPr")
            run.insert(0, rpr)
        for child in list(rpr):
            if child.tag == f"{W}rFonts":
                rpr.remove(child)
        fonts = etree.Element(f"{W}rFonts")
        fonts.set(f"{W}ascii", "Consolas")
        fonts.set(f"{W}hAnsi", "Consolas")
        fonts.set(f"{W}eastAsia", "Consolas")
        rpr.insert(0, fonts)


def _append_default_borders(tbl_pr) -> None:
    borders = etree.SubElement(tbl_pr, f"{W}tblBorders")
    for tag in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = etree.SubElement(borders, f"{W}{tag}")
        border.set(f"{W}val", "single")
        border.set(f"{W}sz", "4")
        border.set(f"{W}space", "0")
        border.set(f"{W}color", "auto")


def container_table_width_twips(container) -> int | None:
    if container.tag != f"{W}tc":
        return None
    tc_w = container.find(f"{W}tcPr/{W}tcW")
    if tc_w is None or tc_w.get(f"{W}type") != "dxa":
        return None
    try:
        width = int(tc_w.get(f"{W}w"))
    except (TypeError, ValueError):
        return None
    left_margin, right_margin = _cell_horizontal_margins_twips(container)
    return max(1200, width - left_margin - right_margin)


def _cell_horizontal_margins_twips(cell) -> tuple[int, int]:
    tc_mar = cell.find(f"{W}tcPr/{W}tcMar")
    if tc_mar is None:
        return 108, 108
    return _margin_width(tc_mar.find(f"{W}left"), 108), _margin_width(tc_mar.find(f"{W}right"), 108)


def _margin_width(element, default: int) -> int:
    if element is None or element.get(f"{W}type") not in {None, "dxa"}:
        return default
    try:
        return int(element.get(f"{W}w"))
    except (TypeError, ValueError):
        return default


def _table_width(table_ir: TableIR, available_width_twips: int | None) -> int | None:
    if table_ir.column_widths_twips:
        return sum(int(width) for width in table_ir.column_widths_twips)
    return None


def _default_widths(table_ir: TableIR, available_width_twips: int | None) -> list[int]:
    col_count = max((len(row.cells) for row in table_ir.rows), default=1)
    total_width = available_width_twips or DEFAULT_TABLE_WIDTH_TWIPS
    min_width = MIN_COLUMN_WIDTH_TWIPS if total_width >= MIN_COLUMN_WIDTH_TWIPS * col_count else 240
    width = max(min_width, total_width // max(1, col_count))
    return [width] * col_count


def render_image(image_ir: ImageIR, style_samples: dict[str, dict] | None = None):
    """Programmatically construct DrawingML node tree to avoid hardcoded XML strings."""
    from PIL import Image
    from pathlib import Path
    import random

    img_path = Path(image_ir.src_path)
    width_emu = 3810000
    height_emu = 2857500

    if img_path.exists():
        try:
            with Image.open(img_path) as img:
                img_w, img_h = img.size
                aspect_ratio = img_w / img_h
                if image_ir.width_cm is not None:
                    width_emu = int(image_ir.width_cm * 360000)
                    if image_ir.height_cm is None:
                        height_emu = int(width_emu / aspect_ratio)
                if image_ir.height_cm is not None:
                    height_emu = int(image_ir.height_cm * 360000)
                    if image_ir.width_cm is None:
                        width_emu = int(height_emu * aspect_ratio)
                if image_ir.width_cm is None and image_ir.height_cm is None:
                    width_emu = 3810000
                    height_emu = int(width_emu / aspect_ratio)
        except Exception:
            pass

    resolved_path = str(img_path.resolve())
    temp_rId = f"TEMP_IMG_REL:{resolved_path}"
    unique_id = str(random.randint(100000, 999999999))

    # Declarations of Drawing namespaces
    NS_MAP = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture"
    }

    # 1. Create w:p
    paragraph = etree.Element(f"{{{NS_MAP['w']}}}p")
    ppr = etree.SubElement(paragraph, f"{{{NS_MAP['w']}}}pPr")
    jc = etree.SubElement(ppr, f"{{{NS_MAP['w']}}}jc")
    align = image_ir.alignment or "center"
    jc.set(f"{{{NS_MAP['w']}}}val", align)

    # Apply style sample
    style_sample = None
    if style_samples and image_ir.style_sample_id:
        style_sample = style_samples.get(image_ir.style_sample_id)
    if style_sample:
        apply_sample_format_to_paragraph(paragraph, style_sample)
        # Ensure alignment is preserved/applied
        jc_existing = ppr.find(f"{{{NS_MAP['w']}}}jc")
        if jc_existing is None:
            jc_existing = etree.SubElement(ppr, f"{{{NS_MAP['w']}}}jc")
        jc_existing.set(f"{{{NS_MAP['w']}}}val", align)

    # 2. Create w:r
    run = etree.SubElement(paragraph, f"{{{NS_MAP['w']}}}r")
    rpr = etree.SubElement(run, f"{{{NS_MAP['w']}}}rPr")
    etree.SubElement(rpr, f"{{{NS_MAP['w']}}}noProof")

    # 3. Build w:drawing
    drawing = etree.SubElement(run, f"{{{NS_MAP['w']}}}drawing")
    
    # wp:inline
    inline = etree.SubElement(drawing, f"{{{NS_MAP['wp']}}}inline")
    inline.set("distT", "0")
    inline.set("distB", "0")
    inline.set("distL", "0")
    inline.set("distR", "0")

    # wp:extent
    extent = etree.SubElement(inline, f"{{{NS_MAP['wp']}}}extent")
    extent.set("cx", str(width_emu))
    extent.set("cy", str(height_emu))

    # wp:effectExtent
    effect_extent = etree.SubElement(inline, f"{{{NS_MAP['wp']}}}effectExtent")
    effect_extent.set("l", "0")
    effect_extent.set("t", "0")
    effect_extent.set("r", "0")
    effect_extent.set("b", "0")

    # wp:docPr
    doc_pr = etree.SubElement(inline, f"{{{NS_MAP['wp']}}}docPr")
    doc_pr.set("id", unique_id)
    doc_pr.set("name", f"图片 {unique_id}")

    # wp:cNvGraphicFramePr
    c_nv_graphic_frame_pr = etree.SubElement(inline, f"{{{NS_MAP['wp']}}}cNvGraphicFramePr")
    graphic_frame_locks = etree.SubElement(c_nv_graphic_frame_pr, f"{{{NS_MAP['a']}}}graphicFrameLocks")
    graphic_frame_locks.set("noChangeAspect", "1")

    # a:graphic
    graphic = etree.SubElement(inline, f"{{{NS_MAP['a']}}}graphic")
    graphic_data = etree.SubElement(graphic, f"{{{NS_MAP['a']}}}graphicData")
    graphic_data.set("uri", "http://schemas.openxmlformats.org/drawingml/2006/picture")

    # pic:pic
    pic = etree.SubElement(graphic_data, f"{{{NS_MAP['pic']}}}pic")

    # pic:nvPicPr
    nv_pic_pr = etree.SubElement(pic, f"{{{NS_MAP['pic']}}}nvPicPr")
    c_nv_pr = etree.SubElement(nv_pic_pr, f"{{{NS_MAP['pic']}}}cNvPr")
    c_nv_pr.set("id", unique_id)
    c_nv_pr.set("name", f"图片 {unique_id}")
    etree.SubElement(nv_pic_pr, f"{{{NS_MAP['pic']}}}cNvPicPr")

    # pic:blipFill
    blip_fill = etree.SubElement(pic, f"{{{NS_MAP['pic']}}}blipFill")
    blip = etree.SubElement(blip_fill, f"{{{NS_MAP['a']}}}blip")
    blip.set(f"{{{NS_MAP['r']}}}embed", temp_rId)

    stretch = etree.SubElement(blip_fill, f"{{{NS_MAP['a']}}}stretch")
    etree.SubElement(stretch, f"{{{NS_MAP['a']}}}fillRect")

    # pic:spPr
    sp_pr = etree.SubElement(pic, f"{{{NS_MAP['pic']}}}spPr")
    xfrm = etree.SubElement(sp_pr, f"{{{NS_MAP['a']}}}xfrm")
    
    off = etree.SubElement(xfrm, f"{{{NS_MAP['a']}}}off")
    off.set("x", "0")
    off.set("y", "0")
    
    ext = etree.SubElement(xfrm, f"{{{NS_MAP['a']}}}ext")
    ext.set("cx", str(width_emu))
    ext.set("cy", str(height_emu))

    prst_geom = etree.SubElement(sp_pr, f"{{{NS_MAP['a']}}}prstGeom")
    prst_geom.set("prst", "rect")
    etree.SubElement(prst_geom, f"{{{NS_MAP['a']}}}avLst")

    return paragraph
