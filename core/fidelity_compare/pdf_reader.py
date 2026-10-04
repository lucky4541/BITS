"""Extracts a document_model.Document from a PDF via PyMuPDF - the SAME
function is used for the Original PDF and the Converted (EPUB-to-PDF)
PDF (spec section 6: "Do not use a different comparison algorithm for
each PDF"). READ-ONLY: only ever calls fitz's own read APIs
(get_text/get_images/find_tables), never anything that mutates the
document, and never touches core/pdf_loader.py or any other existing
zoning/OCR module - Advanced Fidelity Compare is a fully independent
subsystem that happens to also use PyMuPDF for its own read-only parsing
(spec section 69/71)."""
import re

import fitz

from core.fidelity_compare import cache

from core.fidelity_compare import text_forms
from core.fidelity_compare.document_model import (
    BBox, Block, Document, Figure, Footnote, FontInfo, IndexEntry, Line, Page, Reference, Table, TableCell, Word,
)
from core.fidelity_compare.layout_analyzer import compute_font, compute_layout

_BULLET_RE = re.compile(r"^\s*([•‣◦⁃∙*\-])\s+")
_ORDERED_RE = re.compile(r"^\s*(\d+|[a-zA-Z]|[ivxlcdmIVXLCDM]+)[.)]\s+")
_PRINTED_PAGE_RE = re.compile(r"^\s*\[?-?\s*(\d{1,5})\s*-?\]?\s*$")


def _span_bbox(span) -> BBox:
    x0, y0, x1, y1 = span["bbox"]
    return BBox(x0, y0, x1, y1)


def _bbox_intersection_area(a: BBox, b: BBox) -> float:
    x0 = max(a.x0, b.x0)
    y0 = max(a.y0, b.y0)
    x1 = min(a.x1, b.x1)
    y1 = min(a.y1, b.y1)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _span_geometry_italic(span: dict) -> bool:
    """Conservative native-PDF geometry fallback for synthetic italic.

    Uses rawdict character origin/bbox when available. The signal is only
    accepted when several glyphs agree on a non-zero horizontal shear; it
    never overrides an explicit italic flag/name. This is intentionally
    scale-free and page-local rather than a PDF-specific pixel threshold.
    """
    chars = span.get("chars") or []
    slopes = []
    for ch in chars:
        bbox = ch.get("bbox")
        origin = ch.get("origin")
        if not bbox or not origin:
            continue
        x0, y0, x1, y1 = map(float, bbox)
        ox, oy = float(origin[0]), float(origin[1])
        h = max(0.01, oy - y0)
        dx = x0 - ox
        if h > 0.5:
            slopes.append(dx / h)
    if len(slopes) < 3:
        return False
    slopes.sort()
    med = slopes[len(slopes)//2]
    # Only a coherent positive/negative slant is useful. Very small values
    # are normal glyph side-bearing variation.
    return abs(med) >= 0.08 and sum(1 for v in slopes if abs(v - med) <= 0.035) >= max(3, int(len(slopes) * 0.6))


def _font_from_span(span: dict) -> FontInfo:
    font = compute_font(span)
    if not font.italic and _span_geometry_italic(span):
        font.italic = True
    return font


def _build_format_runs(line_spans: list) -> list:
    runs = []
    for span in line_spans:
        text = span.get("text", "")
        if not text:
            continue
        runs.append({
            "text": text,
            "bbox": _span_bbox(span),
            "font": _font_from_span(span),
            "source": "native",
        })
    return runs


def _dominant_span(spans: list) -> dict:
    if not spans:
        return {}
    return max(spans, key=lambda s: len(s.get("text", "")))


def _detect_columns(block_bboxes: list, page_width: float) -> int:
    """Cheap column-count estimate for the fidelity-compare model only
    (independent of, and never feeding into, the existing zoning column
    detector) - clusters block left-edges into <=3 candidate bands and
    counts bands with a real population, used purely to label
    column_index/column_count on LayoutInfo for column_analyzer.py."""
    if not block_bboxes:
        return 1
    lefts = sorted(b.x0 for b in block_bboxes)
    bands = [lefts[0]]
    for x in lefts[1:]:
        if x - bands[-1] > page_width * 0.15:
            bands.append(x)
    return max(1, min(len(bands), 4))


def _column_index_for(bbox: BBox, band_edges: list) -> int:
    for i, edge in enumerate(band_edges):
        if bbox.x0 < edge:
            return max(0, i - 1)
    return len(band_edges) - 1


def _classify_block_kind(text: str, font_size: float, body_font_size: float, is_bold: bool):
    stripped = text.strip()
    if _BULLET_RE.match(stripped):
        return "list_item", "unordered", 0
    m = _ORDERED_RE.match(stripped)
    if m and len(stripped) < 400:
        return "list_item", "ordered", 0
    if body_font_size and font_size >= body_font_size * 1.15 and len(stripped) < 200:
        return "heading", None, 0
    if is_bold and body_font_size and font_size >= body_font_size and len(stripped) < 120 \
            and not stripped.endswith((".", ",", ";")):
        return "heading", None, 0
    return "paragraph", None, 0


def _heading_level(font_size: float, size_ranks: list) -> int:
    for i, size in enumerate(size_ranks):
        if font_size >= size - 0.5:
            return min(i + 1, 6)
    return 6


def read_pdf(path: str, source_kind: str = "original_pdf", progress_cb=None) -> Document:
    cached = cache.load_persistent_document(path, source_kind)
    if cached is not None:
        if progress_cb:
            progress_cb("Using cached PDF model", 1, 1)
        return cached
    doc = fitz.open(path)
    try:
        document = Document(source_kind=source_kind, source_path=path)
        # Body font size estimate across the whole document (mode of span
        # sizes) - used only for this model's own heading/list heuristics,
        # never for zoning/OCR font logic.
        all_sizes = []
        raw_pages = []
        for page_index in range(doc.page_count):
            page = doc[page_index]
            text_dict = page.get_text("dict")
            raw_dict = page.get_text("rawdict")
            raw_pages.append((page, text_dict, raw_dict))
            for block in text_dict.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            all_sizes.append(round(float(span.get("size", 0.0)), 1))
        body_font_size = max(set(all_sizes), key=all_sizes.count) if all_sizes else 0.0
        size_ranks = sorted({s for s in all_sizes if s > body_font_size}, reverse=True)

        for page_index, (page, text_dict, raw_dict) in enumerate(raw_pages, start=1):
            page_width, page_height = page.rect.width, page.rect.height
            words_raw = page.get_text("words")  # x0,y0,x1,y1,word,block_no,line_no,word_no
            blocks_out = []
            block_bboxes_for_columns = []
            text_blocks = [b for b in text_dict.get("blocks", []) if b.get("type", 0) == 0]
            raw_blocks = [b for b in raw_dict.get("blocks", []) if b.get("type", 0) == 0]
            for block_index, block in enumerate(text_blocks):
                lines_out = []
                raw_block = raw_blocks[block_index] if block_index < len(raw_blocks) else {}
                raw_lines = raw_block.get("lines", [])
                block_words = []
                block_text_parts = []
                block_x0 = block_y0 = float("inf")
                block_x1 = block_y1 = float("-inf")
                spans_in_block = []
                for line_index, line in enumerate(block.get("lines", [])):
                    line_spans = line.get("spans", [])
                    raw_line = raw_lines[line_index] if line_index < len(raw_lines) else {}
                    raw_spans = raw_line.get("spans", [])
                    # rawdict spans contain chars rather than a text field.
                    # Pair them with the ordinary dict spans by position.
                    enriched_spans = []
                    for si, sp in enumerate(line_spans):
                        rsp = raw_spans[si] if si < len(raw_spans) else {}
                        cp = dict(sp)
                        if rsp.get("chars"):
                            cp["chars"] = rsp.get("chars")
                        enriched_spans.append(cp)
                    line_spans = enriched_spans
                    if not line_spans:
                        continue
                    spans_in_block.extend(line_spans)
                    line_text = "".join(s.get("text", "") for s in line_spans)
                    if not line_text.strip():
                        continue
                    lx0, ly0, lx1, ly1 = line["bbox"]
                    line_words = []
                    for w in words_raw:
                        wx0, wy0, wx1, wy1, wtext = w[0], w[1], w[2], w[3], w[4]
                        if wy0 >= ly0 - 1 and wy1 <= ly1 + 1 and wx0 >= lx0 - 1 and wx1 <= lx1 + 1:
                            wb = BBox(wx0, wy0, wx1, wy1)
                            candidates = [( _bbox_intersection_area(wb, _span_bbox(sp)), sp)
                                          for sp in line_spans if sp.get("bbox")]
                            best_span = max(candidates, key=lambda x: x[0])[1] if candidates else {}
                            line_words.append(Word(text=text_forms.build(wtext),
                                                    bbox=wb, font=_font_from_span(best_span)))
                    lines_out.append(Line(text=text_forms.build(line_text),
                                           bbox=BBox(lx0, ly0, lx1, ly1), words=line_words,
                                           baseline=ly1, format_runs=_build_format_runs(line_spans)))
                    block_words.extend(line_words)
                    block_text_parts.append(line_text)
                    block_x0, block_y0 = min(block_x0, lx0), min(block_y0, ly0)
                    block_x1, block_y1 = max(block_x1, lx1), max(block_y1, ly1)
                if not lines_out:
                    continue
                block_bbox = BBox(block_x0, block_y0, block_x1, block_y1)
                block_bboxes_for_columns.append(block_bbox)
                full_text = "\n".join(block_text_parts)
                dominant = _dominant_span(spans_in_block)
                font = _font_from_span(dominant) if dominant else FontInfo()
                kind, list_kind, depth = _classify_block_kind(full_text, font.size, body_font_size, font.bold)
                heading_level = _heading_level(font.size, size_ranks) if kind == "heading" else None
                line_spacing = None
                if len(lines_out) >= 2:
                    baselines = [ln.baseline for ln in lines_out if ln.baseline is not None]
                    if len(baselines) >= 2:
                        diffs = [b - a for a, b in zip(baselines, baselines[1:])]
                        line_spacing = sum(diffs) / len(diffs) if diffs else None
                layout = compute_layout(
                    bbox=block_bbox, page_width=page_width, page_height=page_height,
                    container_left=0.0, container_right=page_width,
                    line_bboxes=[ln.bbox for ln in lines_out], font=font, line_spacing=line_spacing,
                )
                blocks_out.append(Block(text=text_forms.build(full_text), bbox=block_bbox,
                                         lines=lines_out, layout=layout, kind=kind,
                                         heading_level=heading_level, list_kind=list_kind, list_depth=depth))

            column_count = _detect_columns(block_bboxes_for_columns, page_width)
            if column_count > 1:
                band_edges = sorted({round(b.x0) for b in block_bboxes_for_columns})
                for blk in blocks_out:
                    blk.layout.column_count = column_count
                    blk.layout.column_index = _column_index_for(blk.bbox, band_edges)

            figures = _extract_figures(doc, page, page_index)
            tables = _extract_tables(page, page_index)

            printed_number = _detect_printed_page_number(blocks_out, page_height)

            document.pages.append(Page(number=page_index, printed_number=printed_number,
                                        width=page_width, height=page_height, blocks=blocks_out,
                                        figures=figures, tables=tables, column_count=column_count))
            if progress_cb:
                progress_cb("Parsing PDF", page_index, doc.page_count)
        _classify_structural_sections(document, body_font_size)
        cache.save_persistent_document(path, source_kind, document)
        return document
    finally:
        doc.close()


_SECTION_HEADING_RE = re.compile(
    r"^(references|bibliography|works cited|reference list|cited works|literature cited)$", re.I)
_INDEX_HEADING_RE = re.compile(r"^index$", re.I)
_FOOTNOTE_MARKER_RE = re.compile(r"^\s*(\d{1,3}|\*+|[a-z])\s*[.)]?\s*")


def _classify_structural_sections(document: Document, body_font_size: float):
    """Best-effort, PDF-only heuristic reclassification (a real PDF has no
    semantic tags, unlike the EPUB side) so footnote/reference/index
    comparison has something real to compare against on the PDF side too
    (spec section 6: same model shape for every source). A heading whose
    OWN text exactly matches a known bibliography/index heading starts a
    section that lasts until the next heading of equal-or-higher rank;
    footnotes are identified independently, per page, as blocks sitting
    in the bottom band of the page with a visibly smaller font than the
    body text - the closest offline equivalent of "the printed page has a
    footnote rule near the bottom in a smaller size" without any ground
    truth to confirm against."""
    section = None  # None / "references" / "index"
    section_heading_level = None
    for page in document.pages:
        kept_blocks = []
        for block in page.blocks:
            if block.kind == "heading":
                if _SECTION_HEADING_RE.match(block.text.raw.strip()):
                    section, section_heading_level = "references", block.heading_level
                    kept_blocks.append(block)
                    continue
                if _INDEX_HEADING_RE.match(block.text.raw.strip()):
                    section, section_heading_level = "index", block.heading_level
                    kept_blocks.append(block)
                    continue
                if section and block.heading_level is not None and section_heading_level is not None \
                        and block.heading_level <= section_heading_level:
                    section = None
            if section == "references" and block.kind in ("paragraph", "list_item"):
                document.references.append(Reference(text=block.text, order=len(document.references)))
                continue
            if section == "index" and block.kind in ("paragraph", "list_item"):
                document.index_entries.append(IndexEntry(text=block.text, order=len(document.index_entries)))
                continue
            if block.kind == "paragraph" and block.layout and body_font_size \
                    and block.layout.font.size <= body_font_size * 0.85 \
                    and block.bbox.y1 >= page.height * 0.75:
                marker_match = _FOOTNOTE_MARKER_RE.match(block.text.raw)
                marker = marker_match.group(1) if marker_match else ""
                document.footnotes.append(Footnote(marker=marker, text=block.text, page=page.number,
                                                     bbox=block.bbox, order=len(document.footnotes)))
                continue
            kept_blocks.append(block)
        page.blocks = kept_blocks


def _extract_figures(doc: fitz.Document, page: fitz.Page, page_number: int) -> list:
    figures = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        bbox = rects[0] if rects else fitz.Rect(0, 0, 0, 0)
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.n - pix.alpha >= 4:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            image_bytes = pix.tobytes("png")
            width, height = pix.width, pix.height
        except Exception:
            image_bytes, width, height = None, bbox.width, bbox.height
        figures.append(Figure(bbox=BBox(bbox.x0, bbox.y0, bbox.x1, bbox.y1), page=page_number,
                               image_bytes=image_bytes, width=width, height=height))
    return figures


def _extract_tables(page: fitz.Page, page_number: int) -> list:
    tables = []
    try:
        found = page.find_tables()
    except Exception:
        return tables
    for t in found.tables:
        try:
            extracted = t.extract()
        except Exception:
            continue
        rows = len(extracted)
        cols = max((len(r) for r in extracted), default=0)
        cells = []
        for r_idx, row in enumerate(extracted):
            for c_idx, cell_text in enumerate(row):
                cells.append(TableCell(text=text_forms.build(cell_text or ""), row=r_idx, col=c_idx))
        bbox = BBox(*t.bbox)
        tables.append(Table(bbox=bbox, page=page_number, rows=rows, cols=cols, cells=cells))
    return tables


def _detect_printed_page_number(blocks: list, page_height: float):
    """Best-effort: a short, purely-numeric block sitting in the top/bottom
    10% of the page - distinct from the physical PDF page index (spec
    section 27: "distinguish physical PDF page from printed page number").
    Independent of, and never calling, core/page_number_detector.py (that
    module's job is zoning-side header/footer exclusion; this is a
    read-only label for the comparison report only)."""
    for blk in blocks:
        if blk.bbox.y0 <= page_height * 0.1 or blk.bbox.y1 >= page_height * 0.9:
            m = _PRINTED_PAGE_RE.match(blk.text.raw)
            if m:
                return m.group(1)
    return None
