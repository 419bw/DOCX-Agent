"""共享 docx 构造器 + read 端辅助 + HTTP mock helper + fixture

设计目标 (3 批次回归测试补齐计划 - PR-1.0 基础设施):
  1. 一次定义, 多文件复用: 所有新 test_*.py 都 import 此文件
  2. 严格遵守"陷阱 1 铁律": 所有带副作用代码 (import workspace.guard、
     Path 创建、monkeypatch、mkdir) 严格封印在 fixture 函数体内部,
     模块顶层只能出现: 常量、纯函数定义、@pytest.fixture 装饰器
  3. namespace 集中 (陷阱 2 防御): NS / NS_R 字典 + get_xml_elements 辅助
  4. lxml / zipfile 的 import 也封装在 read 辅助函数内部, 避免顶层依赖
     pytest 收集时强制 import lxml

使用方式 (在 test_*.py 顶部):
    pytest_plugins = ["_docx_factory"]   # 让 pytest 能发现本文件的 fixture
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    def test_xxx(tmp_root, session_id):
        docx = tmp_root / session_id / "workspace" / "test.docx"
        _build_minimal_docx(docx, ["第一段", "第二段"])
        ...
        paras = get_xml_elements(docx, "//w:p")
        assert len(paras) == 2

为什么不放 conftest.py:
  - 用户决策: "抽到 tests/_docx_factory.py (推荐)"
  - conftest.py 是 pytest 约定的"自动加载"位置, 但所有 fixture 都堆在那里
    会让 fixtures 跟工具函数混在一起, 后续难维护
  - 折中: 把所有内容放 _docx_factory.py, 配合 pytest_plugins 让 fixture 自动发现
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest


# =====================================================================
# Namespace 常量 (陷阱 2 防御: 集中管理避免散落)
# =====================================================================

# OpenXML wordprocessingml 主命名空间
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
NS_R = {"r": NS["r"]}


# =====================================================================
# Docx 构造器 (无副作用, 纯函数)
# =====================================================================

WORD_XML_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
    '  <w:body>\n'
    '{body}\n'
    '  </w:body>\n'
    '</w:document>\n'
)


def _escape_xml(text: str) -> str:
    """XML 转义: & < > 三字符, 防测试样本里被解析为节点."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _para_xml(text: str) -> str:
    """单段最小 <w:p> 节点 (来自 test_docx_preview_diff.py:48-53)."""
    return (
        f'    <w:p><w:r><w:t xml:space="preserve">'
        f'{_escape_xml(text)}</w:t></w:r></w:p>'
    )


def _build_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    """写一个最小 docx zip, 只含 word/document.xml.

    来源: tests/test_docx_preview_diff.py:56-66.
    Notes:
      - 不写 [Content_Types].xml / rels, 因为大多数 docx_tools 只读 document.xml
      - 真实 Word 打开会报"文件损坏", 但本项目工具不依赖完整结构
    """
    body = "\n".join(_para_xml(t) for t in paragraphs)
    xml_bytes = WORD_XML_TEMPLATE.format(body=body).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with __import__("zipfile").ZipFile(path, "w", __import__("zipfile").ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml_bytes)


def _build_docx_with_table(
    path: Path,
    rows: int,
    cols: int,
    cells_data: list[list[str]] | None = None,
) -> None:
    """写一个含 <w:tbl> 的 docx, 用于表操作工具测试.

    cells_data[row_idx][col_idx] = cell 文本.
    不传则用 'r{row}c{col}' 占位.
    """
    import zipfile

    if cells_data is None:
        cells_data = [[f"r{r}c{c}" for c in range(cols)] for r in range(rows)]
    assert len(cells_data) == rows, (
        f"cells_data 行数 {len(cells_data)} != {rows}"
    )
    for r in range(rows):
        assert len(cells_data[r]) == cols, (
            f"row {r} cell 数 {len(cells_data[r])} != {cols}"
        )

    col_w = 8000 // cols  # twips, 总表宽 8000

    grid_cols = "\n".join(
        f'      <w:gridCol w:w="{col_w}"/>' for _ in range(cols)
    )

    table_rows = []
    for r in range(rows):
        cells_xml = "\n".join(
            f'        <w:tc>'
            f'<w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/></w:tcPr>'
            f'<w:p><w:r><w:t xml:space="preserve">'
            f'{_escape_xml(cells_data[r][c])}</w:t></w:r></w:p>'
            f'</w:tc>'
            for c in range(cols)
        )
        table_rows.append(f'      <w:tr>\n{cells_xml}\n      </w:tr>')

    table_xml = (
        f'    <w:tbl>\n'
        f'      <w:tblGrid>\n{grid_cols}\n      </w:tblGrid>\n'
        + "\n".join(table_rows) + "\n"
        f'    </w:tbl>\n'
    )

    xml_bytes = WORD_XML_TEMPLATE.format(body=table_xml).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml_bytes)


def _build_full_docx(path: Path, paragraphs: list[str]) -> None:
    """写一个**完整**的 docx zip, 含 [Content_Types].xml / _rels/.rels /
    word/_rels/document.xml.rels.

    适用场景: 工具要写 word/_rels/document.xml.rels (e.g. 插入图片 / 加超链接).
    _build_minimal_docx 只写 word/document.xml, 这种工具调会报:
    "There is no item named 'word/_rels/document.xml.rels' in the archive".

    跟 _build_minimal_docx 的区别:
      - + [Content_Types].xml
      - + _rels/.rels (root package rels)
      - + word/_rels/document.xml.rels (空 rels, 工具自己加)
    """
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_para_xml(t) for t in paragraphs)
    document_xml = WORD_XML_TEMPLATE.format(body=body).encode("utf-8")

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        '</Types>\n'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>\n'
        '</Relationships>\n'
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>\n'
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/_rels/document.xml.rels", document_rels)
        zf.writestr("word/document.xml", document_xml)


# =====================================================================
# Read 端辅助 (陷阱 2 防御: lxml import 严格封装)
# =====================================================================

def get_xml_elements(zip_path: Path, xpath: str) -> list:
    """打开 docx zip, 跑 xpath, 返回节点列表.

    lxml 在调用时才 import, 避免本模块加载时强制依赖.
    """
    import zipfile
    from lxml import etree
    with zipfile.ZipFile(zip_path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    return root.xpath(xpath, namespaces=NS)


def get_xml_attr(zip_path: Path, xpath: str, attr: str) -> str | None:
    """便捷: 取第一个匹配节点的某个属性. 节点为空时 assert 失败.

    attr 支持三种写法:
      - 本地名: "left"
      - 带前缀: "w:left" — 从 NS 找命名空间, 翻译成 Clark notation
      - Clark notation: "{http://...}left"
    修复原因: lxml 的 Element.get() 只认 Clark notation,
    "w:left" 直接传会被当作不同属性名.
    """
    nodes = get_xml_elements(zip_path, xpath)
    assert nodes, f"xpath {xpath!r} 在 {zip_path} 内无匹配"
    if ":" in attr and not attr.startswith("{"):
        prefix, local = attr.split(":", 1)
        if prefix in NS:
            attr = f"{{{NS[prefix]}}}{local}"
    return nodes[0].get(attr)


def get_xml_text(zip_path: Path, xpath: str) -> str:
    """便捷: 取所有匹配节点的 <w:t> 文本拼接."""
    nodes = get_xml_elements(zip_path, xpath)
    return "".join(n.text or "" for n in nodes)


# =====================================================================
# Run / Paragraph XML 片段构造器 (无副作用, 纯字符串)
# =====================================================================

def _build_run_xml(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    font: str | None = None,
    size: int | None = None,
) -> str:
    """构造含 rPr 的 <w:r> 节点, 用于 set_text_format 等测试.

    rPr 仅在有非默认属性时生成, 避免噪音.
    size 是磅, 内部乘 2 转 sz w:val 半磅单位.
    """
    rpr_parts: list[str] = []
    if bold:
        rpr_parts.append("<w:b/>")
    if italic:
        rpr_parts.append("<w:i/>")
    if font:
        f = _escape_xml(font)
        rpr_parts.append(
            f'<w:rFonts w:ascii="{f}" w:hAnsi="{f}" w:eastAsia="{f}"/>'
        )
    if size is not None:
        sz = int(size * 2)
        rpr_parts.append(f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>')

    rpr = f"<w:rPr>{''.join(rpr_parts)}</w:rPr>" if rpr_parts else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_escape_xml(text)}</w:t></w:r>'


def _build_paragraph_xml(
    text: str = "",
    *,
    style: str | None = None,
    indent: dict | None = None,
    runs: list[str] | None = None,
) -> str:
    """构造含 pPr 的 <w:p> 节点.

    indent 形如:
      {"first_line_chars": 2}    → firstLineChars=200
      {"hanging_chars": 1}       → hangingChars=100
      {"left_chars": 2}          → leftChars=200, left=480
    runs 优先于 text: 传 runs 时用 runs 拼接, 忽略 text.
    """
    ppr_parts: list[str] = []
    if style:
        ppr_parts.append(f'<w:pStyle w:val="{_escape_xml(style)}"/>')
    if indent:
        ind_attrs: list[str] = []
        if "first_line_chars" in indent:
            ind_attrs.append(
                f'w:firstLineChars="{indent["first_line_chars"] * 100}"'
            )
        if "hanging_chars" in indent:
            ind_attrs.append(
                f'w:hangingChars="{indent["hanging_chars"] * 100}"'
            )
        if "left_chars" in indent:
            ind_attrs.append(
                f'w:leftChars="{indent["left_chars"] * 100}" '
                f'w:left="{indent["left_chars"] * 240}"'
            )
        if ind_attrs:
            ppr_parts.append(f'<w:ind {" ".join(ind_attrs)}/>')

    ppr = f"<w:pPr>{''.join(ppr_parts)}</w:pPr>" if ppr_parts else ""

    if runs is not None:
        body = "".join(runs)
    elif text:
        body = f'<w:r><w:t xml:space="preserve">{_escape_xml(text)}</w:t></w:r>'
    else:
        body = ""

    return f"<w:p>{ppr}{body}</w:p>"


def _build_docx_with_custom_body(path: Path, body_xml: str) -> None:
    """高级入口: 用户给完整 <w:body> 内部 XML, 自己组装.

    适用于需要混合段落 + 表格 + 列表 + 图片的复杂测试样本.
    """
    import zipfile
    xml_bytes = WORD_XML_TEMPLATE.format(body=body_xml).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml_bytes)


# =====================================================================
# HTTP mock helper (无副作用, 纯函数)
# =====================================================================

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _fake_png_bytes() -> bytes:
    """最小合法 PNG 字节 (来自 test_render_diagram.py:57-59)."""
    return PNG_MAGIC + b"\x00" * 32


def _make_resp(status_code: int, content: bytes = b"", text: str = "") -> Mock:
    """构造 mock 的 requests.Response (来自 test_render_diagram.py:62-68)."""
    resp = Mock()
    resp.status_code = status_code
    resp.content = content
    resp.text = text or content.decode("utf-8", errors="replace")
    return resp


# =====================================================================
# Fixture 层 (副作用严格封印, 陷阱 1 防御)
# =====================================================================

@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """重定向 workspace.guard.WORKSPACE_ROOT 到 tmp_path/sessions.

    关键设计 (陷阱 1 铁律):
      - `import workspace.guard` 严格在 fixture 函数体内
      - 模块加载时只装饰 fixture, 不执行函数体
      - 所以真实 out/sessions/ 不会被任何测试 import 时污染

    来源: tests/test_unzip_docx_sandbox.py:17 / test_render_diagram.py:36.
    """
    import workspace.guard as guard  # 严格在 fixture 内
    mock_root = tmp_path / "out" / "sessions"
    monkeypatch.setattr(guard, "WORKSPACE_ROOT", mock_root)
    return mock_root


@pytest.fixture
def session_id(tmp_root):
    """建好 sessions/<id>/workspace 的 session_id, 触发 mkdir.

    来源: tests/test_render_diagram.py:48-54.
    """
    sid = "sess-test-default"
    from workspace.guard import workspace_dir  # 严格在 fixture 内
    workspace_dir(sid)  # 触发 mkdir
    return sid


@pytest.fixture
def make_session_id(tmp_root):
    """工厂: 返回 sid -> str 函数, 用于同一测试多 session 场景.

    来源: tests/test_unzip_docx_sandbox.py 类似的 session_with_workspace.
    """
    from workspace.guard import workspace_dir  # 严格在 fixture 内
    created: list[str] = []

    def _make(sid: str) -> str:
        workspace_dir(sid)
        created.append(sid)
        return sid

    return _make
