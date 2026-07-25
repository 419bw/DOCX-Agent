from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


RunKind = Literal["text", "tab", "break"]


@dataclass
class RunIR:
    """A Word run intent. Non-text runs preserve tabs and line breaks."""

    text: str = ""
    bold: bool = False
    italic: bool = False
    code: bool = False
    kind: RunKind = "text"

    @classmethod
    def text_run(cls, text: str, bold: bool = False, italic: bool = False, code: bool = False) -> "RunIR":
        return cls(text=text, bold=bold, italic=italic, code=code, kind="text")

    @classmethod
    def tab(cls) -> "RunIR":
        return cls(kind="tab")

    @classmethod
    def line_break(cls) -> "RunIR":
        return cls(kind="break")


@dataclass
class ParagraphIndent:
    left_twips: int | None = None
    first_line_twips: int | None = None
    hanging_twips: int | None = None

    def is_empty(self) -> bool:
        return self.left_twips is None and self.first_line_twips is None and self.hanging_twips is None


@dataclass
class ParagraphIR:
    runs: list[RunIR] = field(default_factory=list)
    style_sample_id: str | None = None
    block_id: str | None = None
    block_type: str = "paragraph"
    line_start: int | None = None
    line_end: int | None = None
    indent: ParagraphIndent | None = None
    list_level: int | None = None
    list_marker: str | None = None


@dataclass
class CellIR:
    blocks: list[ParagraphIR | "TableIR" | "CodeBlockIR" | "FormulaIR" | "ImageIR"] = field(default_factory=list)
    width_twips: int | None = None


@dataclass
class TableRowIR:
    cells: list[CellIR] = field(default_factory=list)


@dataclass
class TableIR:
    rows: list[TableRowIR] = field(default_factory=list)
    column_widths_twips: list[int] = field(default_factory=list)


@dataclass
class CodeBlockIR:
    code: str
    language: str | None = None
    style_sample_id: str | None = None
    block_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    render_mode: str = "code_paragraphs"


@dataclass
class FormulaIR:
    source: str
    source_format: str = "latex"
    display: bool = True
    style_sample_id: str | None = None
    block_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    render_mode: str = "plain_text_fallback"


@dataclass
class ImageIR:
    src_path: str
    alt_text: str = ""
    width_cm: float | None = None
    height_cm: float | None = None
    style_sample_id: str | None = None
    block_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    alignment: str = "center"
