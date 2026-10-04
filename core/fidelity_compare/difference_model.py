"""
EPUBForge Fidelity Comparison - Difference Model

Single result model used by all fidelity detectors.

Supports:
    PDF <-> PDF
    PDF <-> EPUB <-> PDF
    PDF -> XHTML

Every Difference can carry:
    - original/converted text
    - page numbers
    - exact bounding boxes
    - paragraph/word/character positions
    - Unicode information
    - formatting information
    - visual highlight information
    - comparison stage
    - result kind
"""

from dataclasses import dataclass
from typing import Optional, Any


# ============================================================
# RESULT LENSES
# ============================================================

KIND_RAW = "RAW"
KIND_SEMANTIC = "SEMANTIC"
KIND_LAYOUT = "LAYOUT"


# ============================================================
# CATEGORIES
# ============================================================

CATEGORY_CONTENT = "content"
CATEGORY_UNICODE = "unicode"
CATEGORY_HYPHENATION = "hyphenation"
CATEGORY_STRUCTURE = "structure"
CATEGORY_LAYOUT = "layout"
CATEGORY_FIGURE = "figure"
CATEGORY_TABLE = "table"
CATEGORY_FOOTNOTE = "footnote"
CATEGORY_REFERENCE = "reference"
CATEGORY_INDEX = "index"
CATEGORY_LINK = "link"
CATEGORY_OCR = "ocr"


# ============================================================
# SEVERITY
# ============================================================

SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"


# ============================================================
# CONFIDENCE
# ============================================================

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"


# ============================================================
# VISUAL RESULT STATUS
# ============================================================

STATUS_MATCH = "MATCH"
STATUS_MISSING = "MISSING"
STATUS_EXTRA = "EXTRA"
STATUS_CHANGED = "CHANGED"
STATUS_SYMBOL = "SYMBOL_MISMATCH"
STATUS_STYLE = "STYLE_MISMATCH"
STATUS_LAYOUT = "LAYOUT_MISMATCH"


# ============================================================
# HIGHLIGHT COLORS
#
# These are semantic names. The actual GUI/report can decide
# the exact RGB values.
# ============================================================

HIGHLIGHT_GREEN = "GREEN"
HIGHLIGHT_RED = "RED"
HIGHLIGHT_YELLOW = "YELLOW"


# ============================================================
# BOUNDING BOX
# ============================================================

@dataclass
class BBoxDict:
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0

    def to_dict(self) -> dict:
        return {
            "x0": float(self.x0),
            "y0": float(self.y0),
            "x1": float(self.x1),
            "y1": float(self.y1),
        }

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    def is_valid(self) -> bool:
        return (
            self.x1 >= self.x0
            and self.y1 >= self.y0
            and self.width > 0
            and self.height > 0
        )


# ============================================================
# HIGHLIGHT REGION
#
# Allows multiple boxes for one Difference.
#
# Example:
#
#   A missing word can occupy one box.
#   A changed sentence can occupy several boxes.
# ============================================================

@dataclass
class HighlightRegion:
    page: Optional[int] = None
    bbox: Optional[BBoxDict] = None
    side: str = "original"
    color: str = HIGHLIGHT_RED
    label: str = ""
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "side": self.side,
            "color": self.color,
            "label": self.label,
            "text": self.text,
        }


# ============================================================
# DIFFERENCE
# ============================================================

@dataclass
class Difference:

    # --------------------------------------------------------
    # Required identity
    # --------------------------------------------------------

    id: str
    type: str
    category: str
    severity: str
    confidence: str
    confidence_score: float

    # --------------------------------------------------------
    # Original document
    # --------------------------------------------------------

    original_page: Optional[int] = None
    original_bbox: Optional[BBoxDict] = None

    # --------------------------------------------------------
    # Converted document
    # --------------------------------------------------------

    converted_page: Optional[int] = None
    converted_bbox: Optional[BBoxDict] = None

    # --------------------------------------------------------
    # Text
    # --------------------------------------------------------

    original_text: str = ""
    converted_text: str = ""

    # --------------------------------------------------------
    # Exact hierarchy
    # --------------------------------------------------------

    original_paragraph: Optional[int] = None
    converted_paragraph: Optional[int] = None

    original_word_index: Optional[int] = None
    converted_word_index: Optional[int] = None

    original_character_index: Optional[int] = None
    converted_character_index: Optional[int] = None

    # --------------------------------------------------------
    # Unicode
    # --------------------------------------------------------

    original_unicode: Optional[dict] = None
    converted_unicode: Optional[dict] = None

    # --------------------------------------------------------
    # Layout/style
    # --------------------------------------------------------

    original_layout: Optional[dict] = None
    converted_layout: Optional[dict] = None

    # --------------------------------------------------------
    # Explanation
    # --------------------------------------------------------

    explanation: str = ""

    # --------------------------------------------------------
    # Result classification
    # --------------------------------------------------------

    result_kind: str = KIND_RAW

    # A = Original <-> EPUB
    # B = EPUB <-> Converted
    # C = Original <-> Converted
    comparison_stage: str = "C"

    # --------------------------------------------------------
    # Visual result
    # --------------------------------------------------------

    status: str = STATUS_CHANGED

    highlight_color: str = HIGHLIGHT_YELLOW

    # --------------------------------------------------------
    # Individual highlight regions
    # --------------------------------------------------------

    original_highlights: list = None
    converted_highlights: list = None

    # --------------------------------------------------------
    # Source information
    # --------------------------------------------------------

    original_source_file: str = ""
    converted_source_file: str = ""

    original_source_line: Optional[int] = None
    converted_source_line: Optional[int] = None

    original_element: str = ""
    converted_element: str = ""

    # --------------------------------------------------------
    # Additional metadata
    # --------------------------------------------------------

    matched: bool = False
    ignored: bool = False

    def __post_init__(self):

        if self.original_highlights is None:
            self.original_highlights = []

        if self.converted_highlights is None:
            self.converted_highlights = []

        self._set_default_visual_status()

    # ========================================================
    # VISUAL STATUS
    # ========================================================

    def _set_default_visual_status(self):

        t = (self.type or "").upper()

        if t in {
            "MATCH",
            "EXACT_MATCH",
            "TEXT_MATCH",
            "CONTENT_MATCH",
        }:
            self.status = STATUS_MATCH
            self.highlight_color = HIGHLIGHT_GREEN
            self.matched = True
            return

        if (
            "MISSING" in t
            or "NOT_FOUND" in t
            or "ABSENT" in t
        ):
            self.status = STATUS_MISSING
            self.highlight_color = HIGHLIGHT_RED
            return

        if (
            "EXTRA" in t
            or "ADDED" in t
        ):
            self.status = STATUS_EXTRA
            self.highlight_color = HIGHLIGHT_RED
            return

        if (
            "SYMBOL" in t
            or "UNICODE" in t
        ):
            self.status = STATUS_SYMBOL
            self.highlight_color = HIGHLIGHT_RED
            return

        if (
            "STYLE" in t
            or "ITALIC" in t
            or "BOLD" in t
            or "UNDERLINE" in t
            or "SUPERSCRIPT" in t
            or "SUBSCRIPT" in t
        ):
            self.status = STATUS_STYLE
            self.highlight_color = HIGHLIGHT_YELLOW
            return

        if (
            "LAYOUT" in t
            or "ALIGNMENT" in t
        ):
            self.status = STATUS_LAYOUT
            self.highlight_color = HIGHLIGHT_YELLOW
            return

        self.status = STATUS_CHANGED
        self.highlight_color = HIGHLIGHT_YELLOW

    # ========================================================
    # ADD ORIGINAL HIGHLIGHT
    # ========================================================

    def add_original_highlight(
        self,
        page: Optional[int],
        bbox,
        text: str = "",
        label: str = "",
        color: Optional[str] = None,
    ):

        region = HighlightRegion(
            page=page,
            bbox=bbox_dict(bbox),
            side="original",
            color=color or self.highlight_color,
            label=label or self.type,
            text=text,
        )

        self.original_highlights.append(region)

        if self.original_page is None:
            self.original_page = page

        if self.original_bbox is None:
            self.original_bbox = bbox_dict(bbox)

    # ========================================================
    # ADD CONVERTED HIGHLIGHT
    # ========================================================

    def add_converted_highlight(
        self,
        page: Optional[int],
        bbox,
        text: str = "",
        label: str = "",
        color: Optional[str] = None,
    ):

        region = HighlightRegion(
            page=page,
            bbox=bbox_dict(bbox),
            side="converted",
            color=color or self.highlight_color,
            label=label or self.type,
            text=text,
        )

        self.converted_highlights.append(region)

        if self.converted_page is None:
            self.converted_page = page

        if self.converted_bbox is None:
            self.converted_bbox = bbox_dict(bbox)

    # ========================================================
    # MATCH
    # ========================================================

    def mark_match(self):

        self.status = STATUS_MATCH
        self.highlight_color = HIGHLIGHT_GREEN
        self.matched = True

        return self

    # ========================================================
    # MISSING
    # ========================================================

    def mark_missing(self):

        self.status = STATUS_MISSING
        self.highlight_color = HIGHLIGHT_RED
        self.matched = False

        return self

    # ========================================================
    # EXTRA
    # ========================================================

    def mark_extra(self):

        self.status = STATUS_EXTRA
        self.highlight_color = HIGHLIGHT_RED
        self.matched = False

        return self

    # ========================================================
    # CHANGED
    # ========================================================

    def mark_changed(self):

        self.status = STATUS_CHANGED
        self.highlight_color = HIGHLIGHT_YELLOW
        self.matched = False

        return self

    # ========================================================
    # SYMBOL
    # ========================================================

    def mark_symbol_mismatch(self):

        self.status = STATUS_SYMBOL
        self.highlight_color = HIGHLIGHT_RED
        self.matched = False

        return self

    # ========================================================
    # STYLE
    # ========================================================

    def mark_style_mismatch(self):

        self.status = STATUS_STYLE
        self.highlight_color = HIGHLIGHT_YELLOW
        self.matched = False

        return self

    # ========================================================
    # SERIALIZATION
    # ========================================================

    def to_dict(self) -> dict:

        return {
            "id": self.id,
            "type": self.type,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "confidence_score": self.confidence_score,

            "status": self.status,
            "highlight_color": self.highlight_color,
            "matched": self.matched,
            "ignored": self.ignored,

            "original_page": self.original_page,

            "original_bbox": (
                self.original_bbox.to_dict()
                if self.original_bbox
                else None
            ),

            "converted_page": self.converted_page,

            "converted_bbox": (
                self.converted_bbox.to_dict()
                if self.converted_bbox
                else None
            ),

            "original_text": self.original_text,
            "converted_text": self.converted_text,

            "original_paragraph": self.original_paragraph,
            "converted_paragraph": self.converted_paragraph,

            "original_word_index": self.original_word_index,
            "converted_word_index": self.converted_word_index,

            "original_character_index":
                self.original_character_index,

            "converted_character_index":
                self.converted_character_index,

            "original_unicode": self.original_unicode,
            "converted_unicode": self.converted_unicode,

            "original_layout": self.original_layout,
            "converted_layout": self.converted_layout,

            "explanation": self.explanation,

            "result_kind": self.result_kind,
            "comparison_stage": self.comparison_stage,

            "original_source_file":
                self.original_source_file,

            "converted_source_file":
                self.converted_source_file,

            "original_source_line":
                self.original_source_line,

            "converted_source_line":
                self.converted_source_line,

            "original_element":
                self.original_element,

            "converted_element":
                self.converted_element,

            "original_highlights": [
                h.to_dict()
                if hasattr(h, "to_dict")
                else h
                for h in self.original_highlights
            ],

            "converted_highlights": [
                h.to_dict()
                if hasattr(h, "to_dict")
                else h
                for h in self.converted_highlights
            ],
        }


# ============================================================
# ID GENERATOR
# ============================================================

_counter = {"n": 0}


def next_id(prefix: str = "diff") -> str:

    _counter["n"] += 1

    return f"{prefix}-{_counter['n']:06d}"


# ============================================================
# BBOX CONVERTER
# ============================================================

def bbox_dict(bbox) -> Optional[BBoxDict]:

    if bbox is None:
        return None

    # PyMuPDF Rect
    try:
        return BBoxDict(
            float(bbox.x0),
            float(bbox.y0),
            float(bbox.x1),
            float(bbox.y1),
        )

    except AttributeError:
        pass

    # tuple/list
    try:
        return BBoxDict(
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        )

    except (TypeError, IndexError, ValueError):
        return None