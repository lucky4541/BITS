"""Coordinate-space conversion between PDF points (72/inch, the space
every BBox in document_model is stored in) and rendered pixel space at a
given DPI - used by highlight_engine.py to draw a highlight rectangle on
a page image rendered at whatever DPI the GUI/report currently uses."""
from core.fidelity_compare.document_model import BBox


def pdf_to_pixels(bbox: BBox, dpi: int) -> BBox:
    scale = dpi / 72.0
    return BBox(bbox.x0 * scale, bbox.y0 * scale, bbox.x1 * scale, bbox.y1 * scale)


def pixels_to_pdf(bbox: BBox, dpi: int) -> BBox:
    scale = 72.0 / dpi
    return BBox(bbox.x0 * scale, bbox.y0 * scale, bbox.x1 * scale, bbox.y1 * scale)


def expand(bbox: BBox, margin: float) -> BBox:
    return BBox(bbox.x0 - margin, bbox.y0 - margin, bbox.x1 + margin, bbox.y1 + margin)
