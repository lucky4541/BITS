"""Column layout comparison (spec section 43) - operates at the PAGE
level (column_count/order/width) using the column_index/column_count
pdf_reader.py's cheap band-clustering already attached to each block's
LayoutInfo. This is an independent, comparison-only column estimate -
never the existing zoning core/column_detector.py, and never fed back
into zoning in any way."""
from core.fidelity_compare.layout_analyzer import X_TOLERANCE_PT


def compare_page_columns(original_page, converted_page) -> list:
    findings = []
    if original_page.column_count != converted_page.column_count:
        findings.append({"type": "COLUMN_COUNT_CHANGED", "original": original_page.column_count,
                          "converted": converted_page.column_count, "severity": "HIGH", "confidence": 0.85})
    return findings


def compare_block_column_assignment(original_layout, converted_layout, original_page, converted_page) -> dict:
    if original_layout is None or converted_layout is None:
        return {"changed": False}
    if original_page.column_count <= 1 and converted_page.column_count <= 1:
        return {"changed": False}
    if original_layout.column_index != converted_layout.column_index:
        return {"changed": True, "type": "WRONG_COLUMN_ASSIGNMENT",
                "original": original_layout.column_index, "converted": converted_layout.column_index,
                "severity": "HIGH", "confidence": 0.7}
    return {"changed": False}
