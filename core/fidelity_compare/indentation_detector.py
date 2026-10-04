"""Paragraph indentation comparison (spec section 37)."""
from core.fidelity_compare.layout_analyzer import X_TOLERANCE_PT


def compare(original_layout, converted_layout) -> dict:
    if original_layout is None or converted_layout is None or original_layout.page_width <= 0 \
            or converted_layout.page_width <= 0:
        return {"changed": False}
    delta = converted_layout.indentation - original_layout.indentation
    if abs(delta) <= X_TOLERANCE_PT:
        return {"changed": False}
    if original_layout.indentation <= X_TOLERANCE_PT and converted_layout.indentation > X_TOLERANCE_PT:
        change_type = "INDENTATION_ADDED"
    elif original_layout.indentation > X_TOLERANCE_PT and converted_layout.indentation <= X_TOLERANCE_PT:
        change_type = "INDENTATION_REMOVED"
    else:
        change_type = "INDENTATION_CHANGED"
    return {"changed": True, "type": change_type, "original": original_layout.indentation,
            "converted": converted_layout.indentation, "severity": "MEDIUM", "confidence": 0.85}
