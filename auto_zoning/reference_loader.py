"""Loads a reference project (PDF + ZoneTool project JSON) for the auto-
zoning engine to learn from. Read-only: never touches the live ZoneManager,
never writes to the reference PDF or JSON. Reuses core.project_manager's
existing save/load format unchanged - a "reference project" IS a normal
ZoneTool project file, nothing new."""
import os
from dataclasses import dataclass

from core import project_manager
from core.pdf_loader import PDFDocument
from core.zone_manager import Zone


@dataclass
class RawReference:
    json_path: str
    pdf_path: str
    pdf_document: PDFDocument
    zones: list          # list[Zone], as loaded from the reference project JSON
    page_sizes: dict      # page_number (1-indexed) -> (width, height) in PDF points


def load_reference(json_path: str, pdf_path_override: str = None) -> RawReference:
    """Loads json_path via project_manager.load_project (the exact same
    reader used for normal projects) and opens its PDF to obtain real page
    dimensions (needed to normalize zone bboxes - see reference_analyzer.py).
    `pdf_path_override` lets a caller supply a relocated PDF path when the
    JSON's own stored "pdf" path no longer exists (mirrors the same
    situation App.load_project() already guards against for a live
    project). Raises FileNotFoundError (never silently proceeds) if no
    usable PDF path is available."""
    data = project_manager.load_project(json_path)
    pdf_path = pdf_path_override or data.get("pdf")
    if not pdf_path or not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path or "(no pdf path stored in reference project)")
    pdf_document = PDFDocument(pdf_path)
    zones = [Zone.from_dict(zd) for zd in data.get("zones", [])]
    page_sizes = {p: pdf_document.page_size(p) for p in range(1, pdf_document.page_count + 1)}
    return RawReference(json_path=json_path, pdf_path=pdf_path, pdf_document=pdf_document,
                         zones=zones, page_sizes=page_sizes)
