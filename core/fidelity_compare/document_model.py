"""Shared, read-only semantic document model for Advanced Fidelity Compare
(EPUBForge - "ADVANCED FIDELITY + CONTENT + UNICODE + LAYOUT COMPARISON",
sections 4/5/6/69). ONE representation is used for the Original PDF, the
EPUB, and the Converted PDF alike (section 6: "Do not use a different
comparison algorithm for each PDF") so every downstream comparator only
ever has to understand this one shape, regardless of which of the three
inputs produced it.

Every text-bearing node keeps THREE independent representations (section
7): `raw` (exact Unicode, byte-for-byte what was extracted - NEVER
mutated), `normalized` (whitespace/hyphen-collapsed, alignment-only), and
`semantic` (case/ligature-folded, content-matching-only). Callers that
need the literal characters (Unicode/homoglyph detection, hyphenation
reporting) always read `.raw`; callers doing sequence alignment read
`.normalized`/`.semantic` - see core/fidelity_compare/text_forms.py.

This module is intentionally free of any PDF/EPUB parsing logic itself
(that lives in pdf_reader.py/epub_reader.py) and free of any comparison
logic (that lives in the various *_compare.py / *_detector.py modules) -
it is purely the shared data shape both sides build and both sides read."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TextForms:
    raw: str
    normalized: str
    semantic: str

    @staticmethod
    def empty() -> "TextForms":
        return TextForms("", "", "")


@dataclass
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def to_dict(self) -> dict:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1,
                "width": self.width, "height": self.height}


@dataclass
class FontInfo:
    family: str = ""
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    superscript: bool = False
    subscript: bool = False

    def to_dict(self) -> dict:
        return dict(family=self.family, size=self.size, bold=self.bold, italic=self.italic,
                    underline=self.underline, superscript=self.superscript, subscript=self.subscript)


@dataclass
class LayoutInfo:
    """Geometry + typography for one block/line/word, relative to its own
    page - the single shape every layout-side detector (alignment_detector,
    indentation_detector, margin_detector, spacing_analyzer, font_analyzer,
    column_analyzer) reads from (section 28/64)."""
    bbox: BBox
    page_width: float
    page_height: float
    alignment: str = "UNKNOWN"       # LEFT / CENTER / RIGHT / JUSTIFIED / UNKNOWN
    indentation: float = 0.0         # left-edge distance from the block's own container left margin
    line_spacing: Optional[float] = None
    paragraph_spacing_before: Optional[float] = None
    paragraph_spacing_after: Optional[float] = None
    font: FontInfo = field(default_factory=FontInfo)
    column_index: int = 0
    column_count: int = 1
    text_direction: str = "LTR"

    def to_dict(self) -> dict:
        return {
            "bbox": self.bbox.to_dict(), "page_width": self.page_width, "page_height": self.page_height,
            "alignment": self.alignment, "indentation": self.indentation, "line_spacing": self.line_spacing,
            "paragraph_spacing_before": self.paragraph_spacing_before,
            "paragraph_spacing_after": self.paragraph_spacing_after,
            "font": self.font.to_dict(), "column_index": self.column_index,
            "column_count": self.column_count, "text_direction": self.text_direction,
        }


@dataclass
class Word:
    text: TextForms
    bbox: BBox
    font: FontInfo = field(default_factory=FontInfo)


@dataclass
class Line:
    text: TextForms
    bbox: BBox
    words: list = field(default_factory=list)  # list[Word]
    baseline: Optional[float] = None
    # Optional run-level typography retained by PDF readers. Each entry is a
    # dict with text, bbox and FontInfo. Existing callers can ignore it.
    format_runs: list = field(default_factory=list)


@dataclass
class Block:
    """A paragraph-equivalent unit: one or more Lines that belong together
    (section 4's "lines"/"paragraph spacing"). `kind` distinguishes plain
    paragraphs from headings/list-items/captions/footnotes so structural
    comparators (heading_compare.py etc.) can filter without re-deriving
    classification themselves."""
    text: TextForms
    bbox: BBox
    lines: list = field(default_factory=list)  # list[Line]
    layout: Optional[LayoutInfo] = None
    kind: str = "paragraph"          # paragraph / heading / list_item / caption / footnote / bibliography_entry / index_entry
    heading_level: Optional[int] = None
    list_kind: Optional[str] = None  # "ordered" / "unordered" / None
    list_depth: int = 0
    block_id: Optional[str] = None   # EPUB id/anchor, when known


@dataclass
class Figure:
    bbox: BBox
    page: int
    image_bytes: Optional[bytes] = None
    width: float = 0.0
    height: float = 0.0
    caption: Optional[TextForms] = None
    caption_bbox: Optional[BBox] = None
    figure_number: Optional[str] = None


@dataclass
class TableCell:
    text: TextForms
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False


@dataclass
class Table:
    bbox: BBox
    page: int
    rows: int
    cols: int
    cells: list = field(default_factory=list)  # list[TableCell]
    caption: Optional[TextForms] = None
    caption_bbox: Optional[BBox] = None


@dataclass
class Footnote:
    marker: str
    text: TextForms
    page: int
    bbox: Optional[BBox] = None
    target_id: Optional[str] = None
    backlink_id: Optional[str] = None
    order: int = 0


@dataclass
class Reference:
    text: TextForms
    order: int
    author: str = ""
    year: str = ""
    title: str = ""


@dataclass
class IndexEntry:
    text: TextForms
    order: int
    depth: int = 0
    parent: Optional[str] = None
    page_refs: list = field(default_factory=list)


@dataclass
class Link:
    text: TextForms
    href: str
    kind: str = "generic"   # chapter / figure / table / footnote / reference / index / pagebreak / generic
    source_page: Optional[int] = None
    target_id: Optional[str] = None


@dataclass
class Page:
    number: int                        # 1-indexed physical page/spine position
    printed_number: Optional[str] = None
    width: float = 0.0
    height: float = 0.0
    blocks: list = field(default_factory=list)     # list[Block], in reading order
    figures: list = field(default_factory=list)    # list[Figure]
    tables: list = field(default_factory=list)     # list[Table]
    column_count: int = 1


@dataclass
class Document:
    """Top-level semantic model for ONE of the three inputs (Original PDF /
    EPUB / Converted PDF). `source_kind` records which, purely for report
    labeling - every comparator downstream treats two Documents identically
    regardless of source_kind (section 6)."""
    source_kind: str                   # "original_pdf" / "epub" / "converted_pdf"
    source_path: str
    pages: list = field(default_factory=list)      # list[Page]
    footnotes: list = field(default_factory=list)  # list[Footnote], document order
    references: list = field(default_factory=list)  # list[Reference], document order
    index_entries: list = field(default_factory=list)  # list[IndexEntry]
    links: list = field(default_factory=list)      # list[Link]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def all_blocks(self):
        for page in self.pages:
            for block in page.blocks:
                yield page, block
