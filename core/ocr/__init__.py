"""Modular OCR subsystem - a MODULAR CAPABILITY layered on top of the
existing ZoneTool architecture, never a replacement for it.

Nothing in this package is imported by core/pdf_loader.py, core/
text_extractor.py, core/zone_manager.py, core/xml_generator.py, core/
epub_xml_generator.py, core/mapping_engine.py, or core/xhtml_writer.py -
the existing digital-PDF pipeline has ZERO dependency on this package and
keeps working identically whether or not an OCR engine is installed.
gui/main_window.py is the only integration point, and only for the new
OCR-triggering actions (Auto Detect, OCR Settings) - it still produces
ordinary Zone objects through the ordinary ZoneManager API, so every
existing zone operation (split/merge/undo/redo/tag change/reading order/
CUPEPUB mapping/XML/XHTML generation) already works on OCR-sourced zones
with no changes needed there.

Module map:
    result_model.py      - OCRBlock/OCRResult dataclasses (the OCR result
                            shape every engine must produce)
    text_quality.py       - pure-heuristic per-page metrics from the
                            EXISTING PyMuPDF extraction (no OCR call) -
                            is_text_usable()/needs_ocr()
    page_classifier.py    - DIGITAL/SCANNED/MIXED/OCR_REQUIRED/UNKNOWN
                            per-page classification built on text_quality
    ocr_engine.py          - abstract OCREngine interface + a small registry
    paddle_engine.py       - PaddleOCREngine(OCREngine) - the primary engine
    coordinate_mapper.py   - OCR pixel-space bbox <-> PDF point-space bbox
    preprocessing.py       - optional page-image preprocessing (grayscale/
                            denoise/contrast/adaptive-threshold/deskew/
                            orientation) - never touches the source PDF
    confidence.py          - OCR confidence bucketing (mirrors
                            auto_zoning/confidence.py's own bucket
                            convention for the existing dashed-outline
                            confidence display, reused rather than
                            duplicated for OCR-sourced zones)
    ocr_cache.py           - on-disk cache keyed by PDF identity + page +
                            engine + version + language + settings
    ocr_service.py         - the orchestration layer (PDF Analyzer -> Page
                            Classifier -> existing extraction OR OCR ->
                            candidate zones), designed to run off the
                            Tkinter main thread
"""
