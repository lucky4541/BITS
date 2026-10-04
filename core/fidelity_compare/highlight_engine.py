"""Draws highlight rectangles for confirmed differences onto a rendered
PDF page image (spec section 46/62: every confirmed difference must be
visually highlighted). Pure rendering utility - takes an already-open
core.pdf_loader.PDFDocument (READ-ONLY: only ever calls its existing
render_page_image()/page_size(), never anything that writes to the PDF)
and a list of bboxes/colors, returns a new PIL.Image; never touches the
source file."""
from PIL import Image, ImageDraw

from core.fidelity_compare.bbox_mapper import pdf_to_pixels

SEVERITY_COLORS = {
    "HIGH": (220, 50, 50),
    "MEDIUM": (230, 160, 30),
    "LOW": (90, 140, 220),
}
DEFAULT_COLOR = (220, 50, 50)


def draw_boxes(img: Image.Image, boxes: list, dpi: int) -> Image.Image:
    """Draws highlight rectangles onto a COPY of an already-rendered page
    image (spec: "ZONETOOL - ADVANCED PDF VIEWER PERFORMANCE" section 38 -
    "If only comparison highlight changes: DO NOT rerender the PDF") -
    split out of render_highlighted_page below so a caller with its own
    cached base render (app.comparison.synchronized_pdf_viewer's page
    cache) only ever pays for this cheap draw, never a fresh PDF-to-pixmap
    render, when the user selects a different difference on the same
    already-rendered page. boxes: list of (BBox, severity_or_color)
    tuples, in PDF-point space. Never mutates `img` itself."""
    out = img.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    for bbox, sev in boxes:
        px = pdf_to_pixels(bbox, dpi)
        color = SEVERITY_COLORS.get(sev, DEFAULT_COLOR) if isinstance(sev, str) else sev
        draw.rectangle([px.x0, px.y0, px.x1, px.y1], outline=color + (255,), width=3)
        draw.rectangle([px.x0, px.y0, px.x1, px.y1], fill=color + (60,))
    return out


def render_highlighted_page(pdf_document, page_number: int, boxes: list, dpi: int = 150) -> Image.Image:
    """boxes: list of (BBox, severity_or_color) tuples, in PDF-point space."""
    img = pdf_document.render_page_image(page_number, dpi=dpi).convert("RGB")
    return draw_boxes(img, boxes, dpi)


def render_alignment_marker(img: Image.Image, bbox_px, original_alignment: str, converted_alignment: str):
    """Draws the '[CENTER] -> [RIGHT]' style label above a highlighted
    block (spec section 46) directly onto the already-highlighted image."""
    draw = ImageDraw.Draw(img)
    label = f"[{original_alignment}] -> [{converted_alignment}]"
    y = max(0, bbox_px.y0 - 16)
    draw.rectangle([bbox_px.x0, y, bbox_px.x0 + 8 * len(label), y + 14], fill=(255, 255, 255))
    draw.text((bbox_px.x0 + 2, y), label, fill=(180, 30, 30))
    return img
