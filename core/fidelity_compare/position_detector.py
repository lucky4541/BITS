"""Text position comparison (spec section 36) - relative movement within
the page/block, gated by X_TOLERANCE_PT/Y_TOLERANCE_PT so ordinary
rendering jitter never gets reported (spec section 35)."""
from core.fidelity_compare.layout_analyzer import X_TOLERANCE_PT, Y_TOLERANCE_PT


def compare(original_layout, converted_layout) -> dict:
    if original_layout is None or converted_layout is None or original_layout.page_width <= 0 \
            or converted_layout.page_width <= 0:
        return {"changed": False}
    ob, cb = original_layout.bbox, converted_layout.bbox
    dx = cb.x0 - ob.x0
    dy = cb.y0 - ob.y0
    types = []
    if abs(dx) > X_TOLERANCE_PT:
        types.append("TEXT_MOVED_RIGHT" if dx > 0 else "TEXT_MOVED_LEFT")
    if abs(dy) > Y_TOLERANCE_PT:
        types.append("TEXT_MOVED_DOWN" if dy > 0 else "TEXT_MOVED_UP")
    if not types:
        return {"changed": False}
    return {"changed": True, "types": types, "dx": dx, "dy": dy,
            "severity": "MEDIUM", "confidence": 0.75}
