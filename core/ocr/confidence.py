"""Thin re-export, not a duplicate: OCR-sourced zones use the EXACT SAME
attributes["source"]/["confidence"] convention and 0-100 confidence scale
auto_zoning/hierarchy_builder.py already established for reference-based
Auto Zone/Auto Analyse - so gui/pdf_viewer.py's existing dashed-outline
confidence display (auto_zoning.confidence.bucket, high/medium/low)
already works for OCR zones with no new drawing code, as long as OCR
confidence is produced on the same 0-100 scale (see ocr_service.py -
OCRBlock.confidence is 0.0-1.0 from the engine, multiplied by 100 exactly
once, at the point a PredictedZone is built, never re-scaled again)."""
from auto_zoning.confidence import bucket, DEFAULT_THRESHOLDS

__all__ = ["bucket", "DEFAULT_THRESHOLDS"]
