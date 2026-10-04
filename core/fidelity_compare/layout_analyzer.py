"""Computes LayoutInfo (geometry + typography classification) for one
block, given its own page dimensions and constituent lines - the single
place alignment/indentation/margin/spacing/font/column values get derived
from raw bbox data, shared by pdf_reader.py for BOTH the Original PDF and
the Converted PDF (spec section 6: one pipeline, not two) and read back
by every *_detector.py comparator.

Tolerances (spec section 35: "small coordinate differences caused by PDF
rendering must NOT be reported") live here as named constants rather than
scattered magic numbers through the comparators - callers doing their own
threshold checks (alignment_detector, position_detector, etc.) import
these same constants instead of inventing their own."""
from dataclasses import dataclass

from core.fidelity_compare.document_model import LayoutInfo, FontInfo, BBox

# In points (PDF user-space units at 72 dpi) unless noted.
X_TOLERANCE_PT = 3.0           # horizontal position noise tolerance
Y_TOLERANCE_PT = 3.0           # vertical position noise tolerance
ALIGNMENT_TOLERANCE_PT = 6.0   # how close left/right/center distances must be to call an alignment
SPACING_TOLERANCE_PT = 1.5     # line/paragraph spacing noise tolerance
FONT_SIZE_TOLERANCE_PT = 0.5
MARGIN_TOLERANCE_PT = 4.0


@dataclass
class _Distances:
    left: float
    right: float
    center_offset: float


def _distances(bbox: BBox, container_left: float, container_right: float) -> _Distances:
    container_width = container_right - container_left
    left = bbox.x0 - container_left
    right = container_right - bbox.x1
    block_center = (bbox.x0 + bbox.x1) / 2.0
    container_center = (container_left + container_right) / 2.0
    return _Distances(left=left, right=right, center_offset=abs(block_center - container_center))


def classify_alignment(bbox: BBox, container_left: float, container_right: float,
                        line_bboxes: list = None) -> str:
    """LEFT / CENTER / RIGHT / JUSTIFIED / UNKNOWN (spec section 29/34).
    Never decided from a single line/word alone when multiple lines are
    available (section 34): JUSTIFIED requires >=2 lines whose left AND
    right edges both hug the container within tolerance (the hallmark of
    justification - a single line can't be distinguished from LEFT this
    way, which is why single-line blocks never return JUSTIFIED here)."""
    d = _distances(bbox, container_left, container_right)
    if line_bboxes and len(line_bboxes) >= 2:
        lefts_flush = all(abs(lb.x0 - container_left) <= ALIGNMENT_TOLERANCE_PT for lb in line_bboxes[:-1])
        rights_flush = all(abs(container_right - lb.x1) <= ALIGNMENT_TOLERANCE_PT for lb in line_bboxes[:-1])
        if lefts_flush and rights_flush:
            return "JUSTIFIED"
    if d.center_offset <= ALIGNMENT_TOLERANCE_PT:
        return "CENTER"
    if d.left <= ALIGNMENT_TOLERANCE_PT:
        return "LEFT"
    if d.right <= ALIGNMENT_TOLERANCE_PT:
        return "RIGHT"
    # Neither edge flush and not centered (e.g. a short ragged-right line
    # of otherwise-left-set body text) - fall back to whichever edge the
    # block sits closer to, rather than defaulting to LEFT unconditionally
    # (a real right-aligned block must still be able to win here).
    return "LEFT" if d.left <= d.right else "RIGHT"


def compute_indentation(bbox: BBox, container_left: float) -> float:
    return max(0.0, bbox.x0 - container_left)


def compute_font(dominant_span: dict) -> FontInfo:
    """dominant_span: a PyMuPDF text-dict span (has 'font','size','flags').
    PyMuPDF flag bits: 2**0 superscript, 2**1 italic, 2**4 bold (serif/
    mono bits ignored here - not requested by the spec)."""
    flags = dominant_span.get("flags", 0)
    font_name = dominant_span.get("font", "") or ""
    return FontInfo(
        family=font_name,
        size=float(dominant_span.get("size", 0.0)),
        bold=bool(flags & (1 << 4)) or "bold" in font_name.lower(),
        italic=bool(flags & (1 << 1)) or "italic" in font_name.lower() or "oblique" in font_name.lower(),
        underline=False,
        superscript=bool(flags & (1 << 0)),
        subscript=False,
    )


def compute_layout(bbox: BBox, page_width: float, page_height: float, container_left: float,
                    container_right: float, line_bboxes: list = None, font: FontInfo = None,
                    line_spacing: float = None, spacing_before: float = None,
                    spacing_after: float = None, column_index: int = 0, column_count: int = 1) -> LayoutInfo:
    alignment = classify_alignment(bbox, container_left, container_right, line_bboxes)
    indentation = compute_indentation(bbox, container_left)
    return LayoutInfo(
        bbox=bbox, page_width=page_width, page_height=page_height, alignment=alignment,
        indentation=indentation, line_spacing=line_spacing, paragraph_spacing_before=spacing_before,
        paragraph_spacing_after=spacing_after, font=font or FontInfo(), column_index=column_index,
        column_count=column_count,
    )
