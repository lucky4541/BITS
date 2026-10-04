"""OCR pixel-space bbox <-> PDF point-space bbox. Uses the EXACT SAME
zoom = dpi / 72.0 factor core/pdf_loader.py's own render_page_image/
render_page/crop_region already use for PDF->pixel rendering - a
different conversion here would silently misalign every OCR-derived zone
against the PDF the moment DPI != 72, so this must stay a single shared
formula, not independently re-derived."""

POINTS_PER_INCH = 72.0


def pixel_to_pdf(bbox_px, dpi: int) -> list:
    """[x0, y0, x1, y1] in image pixels (as PaddleOCR/any OCREngine
    reports them, relative to the page image rendered at `dpi`) -> the
    same box in PDF point space, ready to become a Zone.bbox with zero
    further conversion."""
    zoom = dpi / POINTS_PER_INCH
    x0, y0, x1, y1 = bbox_px
    return [x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom]


def pdf_to_pixel(bbox_pdf, dpi: int) -> list:
    """Inverse of pixel_to_pdf - PDF point-space bbox -> pixel-space bbox
    at `dpi` (e.g. for cropping a preview image around an existing zone)."""
    zoom = dpi / POINTS_PER_INCH
    x0, y0, x1, y1 = bbox_pdf
    return [x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom]


def polygon_to_bbox(points) -> list:
    """PaddleOCR (and most OCR engines) report a 4-point polygon per line
    ([[x,y], [x,y], [x,y], [x,y]], not necessarily axis-aligned - text can
    be slightly rotated/skewed) rather than a plain rectangle. ZoneTool's
    Zone.bbox is always an axis-aligned [x0,y0,x1,y1] rectangle, so this
    takes the polygon's own bounding rectangle - the smallest axis-aligned
    box containing all points. A real skew angle is intentionally
    discarded here (Zone has no rotation field); if that precision is ever
    needed, it belongs in a new zone.attributes value, not a Zone.bbox
    change (spec 39: "do not change the stored meaning of zone.coordinates
    unless required")."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]
