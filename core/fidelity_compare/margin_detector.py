"""Page margin comparison (spec section 38) - compares the smallest
left/right/top/bottom text-block distance from the page edge across an
entire page (the page's own effective margins), not per-block, since a
single indented block is indentation_detector's job, not a margin."""
from core.fidelity_compare.layout_analyzer import MARGIN_TOLERANCE_PT


def compute_page_margins(page) -> dict:
    blocks_with_geometry = [b for b in page.blocks if page.width > 0]
    if not blocks_with_geometry:
        return {"left": None, "right": None, "top": None, "bottom": None}
    left = min(b.bbox.x0 for b in blocks_with_geometry)
    right = page.width - max(b.bbox.x1 for b in blocks_with_geometry)
    top = min(b.bbox.y0 for b in blocks_with_geometry)
    bottom = page.height - max(b.bbox.y1 for b in blocks_with_geometry)
    return {"left": left, "right": right, "top": top, "bottom": bottom}


def compare(original_page, converted_page) -> list:
    if original_page.width <= 0 or converted_page.width <= 0:
        return []
    orig = compute_page_margins(original_page)
    conv = compute_page_margins(converted_page)
    findings = []
    for side in ("left", "right", "top", "bottom"):
        o, c = orig[side], conv[side]
        if o is None or c is None:
            continue
        if abs(c - o) > MARGIN_TOLERANCE_PT:
            findings.append({"changed": True, "type": "MARGIN_CHANGED", "side": side,
                              "original": o, "converted": c, "severity": "LOW", "confidence": 0.7})
    return findings
