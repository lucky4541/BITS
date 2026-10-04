"""Whole-PDF, per-page classification report (spec section 3: "Do NOT
classify the entire PDF using one global decision... Process each page
independently"). Thin orchestration over text_quality.py - no new
heuristics here, just applying it across every page and caching the
per-document result so repeated calls (Auto Detect on several pages, the
OCR status indicator, etc.) don't re-run get_text()/get_images() on pages
that haven't changed."""
from dataclasses import dataclass, field

from core.ocr import text_quality


@dataclass
class PageClassification:
    page_number: int
    category: str  # DIGITAL | SCANNED | MIXED | OCR_REQUIRED | UNKNOWN
    metrics: dict


def classify_document(pdf_document, page_numbers=None) -> list:
    """Returns a list[PageClassification], one per page (or per page in
    `page_numbers` if given - 1-indexed, matching PDFDocument's own
    convention). Independent per page - a single PDF can freely mix
    DIGITAL/SCANNED/MIXED/OCR_REQUIRED results (spec 3/6)."""
    pages = page_numbers if page_numbers is not None else range(1, pdf_document.page_count + 1)
    results = []
    for page_number in pages:
        metrics = text_quality.analyze_page(pdf_document, page_number)
        category = text_quality.classify_page(metrics)
        results.append(PageClassification(page_number=page_number, category=category, metrics=metrics))
    return results


def classify_page(pdf_document, page_number: int) -> PageClassification:
    metrics = text_quality.analyze_page(pdf_document, page_number)
    return PageClassification(page_number=page_number, category=text_quality.classify_page(metrics), metrics=metrics)


def summarize(classifications: list) -> dict:
    """{"DIGITAL": 12, "SCANNED": 3, ...} - for the OCR status indicator
    (spec 42) and the Auto Detect pre-flight summary."""
    summary = {"DIGITAL": 0, "SCANNED": 0, "MIXED": 0, "OCR_REQUIRED": 0, "UNKNOWN": 0}
    for c in classifications:
        summary[c.category] = summary.get(c.category, 0) + 1
    return summary
