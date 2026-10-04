"""Compares two blocks' LayoutInfo.alignment values (spec sections 29-34).
Only ever invoked between two REAL PDFs (Original vs Converted) - a block
whose page_width is 0 (an EPUB-sourced block, see epub_reader.py) has no
meaningful alignment at all and is skipped by the caller before this
module is ever reached."""
from core.fidelity_compare import layout_analyzer

_TRANSITION_LABELS = {
    ("CENTER", "RIGHT"): "CENTER_TO_RIGHT",
    ("LEFT", "CENTER"): "LEFT_TO_CENTER",
    ("RIGHT", "LEFT"): "RIGHT_TO_LEFT_ALIGNMENT_CHANGE",
    ("JUSTIFIED", "LEFT"): "JUSTIFIED_TO_LEFT",
}


def compare(original_layout, converted_layout) -> dict:
    if original_layout is None or converted_layout is None:
        return {"changed": False}
    if original_layout.page_width <= 0 or converted_layout.page_width <= 0:
        return {"changed": False}  # EPUB side has no fixed geometry - never compared here
    if original_layout.alignment == converted_layout.alignment:
        return {"changed": False}
    if original_layout.alignment == "UNKNOWN" or converted_layout.alignment == "UNKNOWN":
        return {"changed": False}
    transition = _TRANSITION_LABELS.get((original_layout.alignment, converted_layout.alignment))
    return {
        "changed": True,
        "type": "ALIGNMENT_CHANGED",
        "transition": transition,
        "original": original_layout.alignment,
        "converted": converted_layout.alignment,
        "severity": "HIGH",
        "confidence": 0.9,
    }
