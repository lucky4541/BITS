"""Line spacing and paragraph spacing comparison (spec sections 39-40)."""
from core.fidelity_compare.layout_analyzer import SPACING_TOLERANCE_PT


def compare_line_spacing(original_layout, converted_layout) -> dict:
    if original_layout is None or converted_layout is None:
        return {"changed": False}
    o, c = original_layout.line_spacing, converted_layout.line_spacing
    if o is None or c is None:
        return {"changed": False}
    if abs(c - o) <= SPACING_TOLERANCE_PT:
        return {"changed": False}
    return {"changed": True, "type": "LINE_SPACING_CHANGED", "original": round(o, 2),
            "converted": round(c, 2), "severity": "MEDIUM", "confidence": 0.8}


def compare_paragraph_spacing(original_layout, converted_layout) -> list:
    if original_layout is None or converted_layout is None:
        return []
    findings = []
    for attr in ("paragraph_spacing_before", "paragraph_spacing_after"):
        o, c = getattr(original_layout, attr), getattr(converted_layout, attr)
        if o is None or c is None:
            continue
        if abs(c - o) > SPACING_TOLERANCE_PT:
            findings.append({"changed": True, "type": "PARAGRAPH_SPACING_CHANGED", "which": attr,
                              "original": round(o, 2), "converted": round(c, 2),
                              "severity": "LOW", "confidence": 0.75})
    return findings
