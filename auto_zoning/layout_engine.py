"""Advanced page LAYOUT analysis for the Auto Zone / Auto Tag engine.

Pure analysis - never touches a ZoneManager. For one page it produces a list
of LayoutBlocks in reading order, each carrying the geometric, typographic
and contextual features the semantic classifier (semantic_classifier.py)
scores roles from. Reuses the project's existing detectors wherever they
already do the job (column_detector bands/columns, list_detector marker runs,
paragraph_auto_zone paragraph segmentation, table_detector, the cached
rawdict and the character-level decoration engine) instead of re-deriving a
competing second version.

    PDF page -> glyph lines (rawdict, decorations) -> header/footer bands
             -> figures (images + vector clusters) / tables
             -> bands & columns -> list runs / verse runs / paragraphs
             -> footnote region -> reading order (band > column > y,
                page number first, footnotes last)

A DocumentContext (body size, heading-size ladder, repeated running
headers/footers, typical paragraph indent) is built across several pages so
page-level decisions use document-level evidence.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from core import column_detector
from core.text_extractor import _get_rawdict
from core.formatting_detector import detect_bold_italic, line_baseline_stats
from auto_zoning import list_detector, paragraph_auto_zone, table_detector, page_number_detector
from auto_zoning.pdf_block_detector import detect_images

HEADER_BAND = 0.09
FOOTER_BAND = 0.09
SAME_LINE_DY = 0.35           # fraction of font size - baseline tolerance for joining fragments of one visual line
SAME_LINE_GAP = 1.6           # fraction of font size - max horizontal gap joining fragments of one visual line
FIGURE_MIN_SIDE = 24.0        # pt - smallest vector cluster treated as a figure
FLOAT_CAPTION_GAP = 30.0      # pt - max gap between a float and its caption block
VERSE_SHORT_RATIO = 0.8       # line width / column width under which a line is "short"
_LABEL_NUMBER_RE = re.compile(r"^\s*([A-Za-zÀ-ɏ]{2,}\.?)\s*(\d+[A-Za-z]?([.\-:]\d+)*|[IVXLC]+)\b")
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\.?\s+\S")
_NOTE_MARKER_RE = re.compile(r"^\s*(\d{1,3}|[*†‡§¶]+|[a-z])[.)]?\s")
_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)[a-z]?\b")
_MATH_CHARS = set("=+−×÷∑∫√∞≤≥≠±∂∆∇αβγδεθλμπσφψω^_")


# ------------------------------------------------------------------ lines
@dataclass
class LineFeature:
    """One visual text line. Shape-compatible with pdf_block_detector.
    LineInfo (bbox/text/font_size/bold/italic) so every existing detector
    accepts it unchanged."""
    bbox: tuple
    text: str
    font_size: float
    bold: bool
    italic: bool
    bold_ratio: float = 0.0
    italic_ratio: float = 0.0
    font: str = ""
    baseline: float = 0.0
    caps_ratio: float = 0.0
    decorated_ratio: float = 0.0
    source: str = "pdf"

    @property
    def width(self):
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self):
        return self.bbox[3] - self.bbox[1]


def extract_page_lines(page, with_decorations: bool = True) -> list:
    """Every visual text line on the page with typographic features.
    Fragments PyMuPDF reports as separate lines but which sit on one
    baseline next to each other (font switches, kerning gaps) are joined."""
    raw = _get_rawdict(page)
    frags = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            chars, bold_n, italic_n, total, fonts = [], 0, 0, 0, Counter()
            for span in spans:
                sc = span.get("chars", [])
                if not sc:
                    continue
                b, i = detect_bold_italic(span.get("flags", 0), span.get("font", ""))
                n = sum(1 for c in sc if not c.get("c", " ").isspace())
                total += n
                bold_n += n if b else 0
                italic_n += n if i else 0
                fonts[span.get("font", "")] += n
                chars.extend(sc)
            text = "".join(c.get("c", "") for c in chars)
            if not text.strip():
                continue
            ink = [c for c in chars if not c.get("c", " ").isspace()]
            x0 = min(c["bbox"][0] for c in ink)
            y0 = min(c["bbox"][1] for c in ink)
            x1 = max(c["bbox"][2] for c in ink)
            y1 = max(c["bbox"][3] for c in ink)
            size, base = line_baseline_stats(line)
            letters = [c.get("c") for c in ink if c.get("c", "").isalpha()]
            frags.append({
                "chars": chars, "bbox": (x0, y0, x1, y1), "size": size or (y1 - y0) / 1.2,
                "baseline": base or y1, "bold_n": bold_n, "italic_n": italic_n, "total": max(total, 1),
                "font": fonts.most_common(1)[0][0] if fonts else "",
                "caps": sum(1 for c in letters if c.isupper()) / max(len(letters), 1),
            })
    frags.sort(key=lambda f: (round(f["baseline"], 0), f["bbox"][0]))
    merged = []
    for f in frags:
        prev = merged[-1] if merged else None
        if prev is not None:
            size = max(prev["size"], f["size"], 1.0)
            if abs(prev["baseline"] - f["baseline"]) <= SAME_LINE_DY * size and \
                    0 <= f["bbox"][0] - prev["bbox"][2] <= SAME_LINE_GAP * size:
                gap_char = [{"c": " ", "bbox": (prev["bbox"][2], prev["bbox"][1], f["bbox"][0], prev["bbox"][3])}] \
                    if not prev["chars"][-1].get("c", "").isspace() and not f["chars"][0].get("c", "").isspace() \
                    and f["bbox"][0] - prev["bbox"][2] > 0.15 * size else []
                prev["chars"] = prev["chars"] + gap_char + f["chars"]
                prev["bbox"] = (prev["bbox"][0], min(prev["bbox"][1], f["bbox"][1]),
                                f["bbox"][2], max(prev["bbox"][3], f["bbox"][3]))
                for k in ("bold_n", "italic_n", "total"):
                    prev[k] += f[k]
                continue
        merged.append(dict(f))
    out = []
    for f in merged:
        text = "".join(c.get("c", "") for c in f["chars"]).strip()
        deco = 0.0
        if with_decorations:
            try:
                from core import underline_detector
                u, s, _ = underline_detector.line_decorations(page, f["chars"], f["size"])
                n = max(1, sum(1 for c in f["chars"] if not c.get("c", " ").isspace()))
                deco = sum(1 for i, c in enumerate(f["chars"]) if (u[i] or s[i]) and not c.get("c", " ").isspace()) / n
            except Exception:
                deco = 0.0
        out.append(LineFeature(
            bbox=tuple(f["bbox"]), text=text, font_size=round(float(f["size"]), 2),
            bold=f["bold_n"] / f["total"] >= 0.5, italic=f["italic_n"] / f["total"] >= 0.5,
            bold_ratio=f["bold_n"] / f["total"], italic_ratio=f["italic_n"] / f["total"],
            font=f["font"], baseline=f["baseline"], caps_ratio=f["caps"], decorated_ratio=deco))
    out.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
    return out


def ocr_page_lines(ocr_result) -> list:
    """LineFeatures from a cached OCR result (scanned page)."""
    out = []
    for b in getattr(ocr_result, "blocks", []) or []:
        text = (b.text or "").strip()
        if not text:
            continue
        for k, part in enumerate(text.split("\n")):
            x0, y0, x1, y1 = b.bbox
            parts = max(1, text.count("\n") + 1)
            h = (y1 - y0) / parts
            bbox = (x0, y0 + k * h, x1, y0 + (k + 1) * h)
            letters = [c for c in part if c.isalpha()]
            out.append(LineFeature(bbox=bbox, text=part.strip(), font_size=round(h / 1.2, 2), bold=False,
                                   italic=False, baseline=bbox[3], source="ocr",
                                   caps_ratio=sum(1 for c in letters if c.isupper()) / max(len(letters), 1)))
    out.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
    return out


# --------------------------------------------------------------- context
@dataclass
class DocumentContext:
    body_size: float = 10.0
    heading_sizes: list = field(default_factory=list)       # descending distinct heading sizes
    running_signatures: set = field(default_factory=set)
    paragraph_indent: float = 0.0                            # typical first-line indent (pt), 0 = block style
    page_number_pages: dict = field(default_factory=dict)    # page -> printed value
    pages_seen: list = field(default_factory=list)

    def to_dict(self):
        return {"body_size": self.body_size, "heading_sizes": list(self.heading_sizes),
                "running_signatures": sorted(self.running_signatures),
                "paragraph_indent": self.paragraph_indent, "pages_seen": list(self.pages_seen)}

    def heading_level(self, size: float, bold: bool) -> int:
        """1-based level from the document's own heading-size ladder."""
        for i, s in enumerate(self.heading_sizes):
            if abs(size - s) <= max(0.6, 0.04 * s):
                return i + 1
        bigger = [s for s in self.heading_sizes if s > size + 0.6]
        level = len(bigger) + 1
        if not bigger and size <= self.body_size * 1.05:
            level = len(self.heading_sizes) + 1 if bold else len(self.heading_sizes) + 2
        return max(1, min(6, level))


def _signature(text: str) -> str:
    t = re.sub(r"\d+", "#", (text or "").lower())
    t = re.sub(r"\b[ivxlcdm]+\b", "#", t)
    return re.sub(r"\s+", " ", t).strip()


def build_document_context(pdf_document, pages, line_cache=None) -> DocumentContext:
    """Document-level evidence from `pages` (a sample or all pages)."""
    ctx = DocumentContext()
    size_counts = Counter()
    top_sigs, bottom_sigs = Counter(), Counter()
    indents = []
    per_page_lines = {}
    for pno in pages:
        lines = (line_cache or {}).get(pno)
        if lines is None:
            try:
                lines = extract_page_lines(pdf_document.get_page(pno), with_decorations=False)
            except Exception:
                lines = []
            if line_cache is not None:
                line_cache[pno] = lines
        per_page_lines[pno] = lines
        if not lines:
            continue
        ctx.pages_seen.append(pno)
        _w, h = pdf_document.page_size(pno)
        for li in lines:
            size_counts[round(li.font_size, 1)] += max(1, len(li.text))
        page_top = [li for li in lines if li.bbox[3] <= h * HEADER_BAND * 1.3]
        page_bottom = [li for li in lines if li.bbox[1] >= h * (1 - FOOTER_BAND * 1.3)]
        for li in page_top:
            top_sigs[_signature(li.text)] += 1
        for li in page_bottom:
            bottom_sigs[_signature(li.text)] += 1
    if size_counts:
        ctx.body_size = size_counts.most_common(1)[0][0]
    n_pages = max(1, len(ctx.pages_seen))
    need = 2 if n_pages <= 4 else max(3, int(0.25 * n_pages))
    for sig, c in list(top_sigs.items()) + list(bottom_sigs.items()):
        if c >= need and sig and sig != "#":
            ctx.running_signatures.add(sig)
    # Heading-size ladder: sizes clearly above body used by short lines.
    heading_sizes = Counter()
    for pno, lines in per_page_lines.items():
        for li in lines:
            if li.font_size >= ctx.body_size * 1.12 and len(li.text) <= 120 \
                    and _signature(li.text) not in ctx.running_signatures:
                heading_sizes[round(li.font_size, 1)] += 1
    ladder = []
    for s in sorted(heading_sizes, reverse=True):
        if not ladder or abs(ladder[-1] - s) > max(0.6, 0.04 * s):
            ladder.append(s)
    ctx.heading_sizes = ladder[:6]
    # Typical paragraph first-line indent: a body line that starts right of
    # the margin, fills the measure, and is followed by a line back at the
    # margin (i.e. a real wrapped paragraph's first line - never a verse or
    # quotation line, which do not fill the measure).
    for pno, lines in per_page_lines.items():
        body = sorted((li for li in lines if abs(li.font_size - ctx.body_size) <= 0.6),
                      key=lambda li: (li.bbox[1], li.bbox[0]))
        if len(body) < 4:
            continue
        margin = Counter(round(li.bbox[0]) for li in body).most_common(1)[0][0]
        right = Counter(round(li.bbox[2]) for li in body).most_common(1)[0][0]
        for li, nxt in zip(body, body[1:]):
            d = li.bbox[0] - margin
            if ctx.body_size * 0.6 <= d <= ctx.body_size * 4 and abs(nxt.bbox[0] - margin) <= 1.5 \
                    and li.bbox[2] >= right - 2 * ctx.body_size and 0 < nxt.bbox[1] - li.bbox[1] < 2.5 * ctx.body_size:
                indents.append(d)
    if len(indents) >= 3:
        indents.sort()
        ctx.paragraph_indent = indents[len(indents) // 2]
    return ctx


# ---------------------------------------------------------------- blocks
@dataclass
class LayoutBlock:
    kind: str                       # text | list_item | verse_line | figure | table | page_number | running | footnote
    bbox: tuple
    lines: list = field(default_factory=list)
    features: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    column: int = 0
    band: int = 0
    order: int = 0
    region: str = "body"            # body | header | footer | notes
    stored_text: str = None         # text of a block restored from the analysis cache (no line objects)

    @property
    def text(self) -> str:
        if not self.lines and self.stored_text is not None:
            return self.stored_text
        return "\n".join(li.text for li in self.lines)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "bbox": list(self.bbox), "text": self.text, "features": dict(self.features),
                "column": self.column, "band": self.band, "order": self.order, "region": self.region,
                "children": [c.to_dict() for c in self.children]}


def _union(boxes):
    boxes = [b for b in boxes if b]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _median(vals, default=0.0):
    vals = sorted(v for v in vals if v is not None)
    return vals[len(vals) // 2] if vals else default


def _vector_figures(page, exclude_boxes, page_w, page_h, body_lines=()) -> list:
    """Clusters of vector drawings (curves, fills, strokes) large enough to
    be an illustration - excluding table regions, thin rules (text
    decorations / separators), page frames and shaded text boxes."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    entries = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = r.width, r.height
        if w <= 3.5 or h <= 3.5:
            continue  # rules, underlines, separators
        entries.append({"bbox": (r.x0, r.y0, r.x1, r.y1), "h": False, "v": False})
    if not entries:
        return []
    out = []
    for region in table_detector._merge_into_regions(entries):
        box = tuple(region["bbox"])
        x0, y0, x1, y1 = box
        if (x1 - x0) < FIGURE_MIN_SIDE or (y1 - y0) < FIGURE_MIN_SIDE:
            continue
        if (x1 - x0) > page_w * 0.95 and (y1 - y0) > page_h * 0.9:
            continue  # page frame / background
        if any(_overlap_ratio(box, e) > 0.5 for e in exclude_boxes):
            continue
        inside = [li for li in body_lines if _inside(li, box, tol=0.5)]
        if len(inside) >= 3 and sum(len(li.text) for li in inside) > 120:
            continue  # a shaded/bordered text box, not an illustration
        out.append(box)
    return out


def _overlap_ratio(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area


def _inside(line, box, tol=2.0) -> bool:
    cx = (line.bbox[0] + line.bbox[2]) / 2
    cy = (line.bbox[1] + line.bbox[3]) / 2
    return box[0] - tol <= cx <= box[2] + tol and box[1] - tol <= cy <= box[3] + tol


def _footnote_separator(page, body_lines, page_w, page_h):
    """y of a short horizontal separator rule in the lower page half with
    text below it (classic footnote rule), else None."""
    try:
        from core.underline_detector import page_decoration_segments
        segs = page_decoration_segments(page)
    except Exception:
        return None
    best = None
    for s in segs:
        if s.y < page_h * 0.45 or s.length < 20 or s.length > page_w * 0.5:
            continue
        below = [li for li in body_lines if li.bbox[1] >= s.y - 1 and li.bbox[0] >= s.x0 - 10]
        above_touch = [li for li in body_lines if 0 <= s.y - li.baseline <= li.font_size * 0.45
                       and li.bbox[0] <= s.x1 and li.bbox[2] >= s.x0]
        if below and not above_touch:
            if best is None or s.y < best:
                best = s.y
    return best


def _line_features_for_block(lines, col_bounds, ctx: DocumentContext, page_w, page_h):
    x0, y0, x1, y1 = _union([li.bbox for li in lines])
    cx0, cx1 = col_bounds
    col_w = max(1.0, cx1 - cx0)
    sizes = [li.font_size for li in lines]
    size = _median(sizes, ctx.body_size)
    text = " ".join(li.text for li in lines)
    widths = [li.width for li in lines]
    lefts = [li.bbox[0] for li in lines]
    rights = [li.bbox[2] for li in lines]
    centers = [(li.bbox[0] + li.bbox[2]) / 2 for li in lines]
    col_center = (cx0 + cx1) / 2
    n = len(lines)
    centered = all(abs(c - col_center) <= max(3.0, 0.04 * col_w) for c in centers) and \
        all(l - cx0 > size for l in lefts)
    right_aligned = (not centered) and all(abs(r - cx1) <= max(3.0, 0.03 * col_w) for r in rights) and \
        all(l - cx0 > 2 * size for l in lefts)
    rest_left = _median(lefts[1:], lefts[0]) if n > 1 else lefts[0]
    first_indent = lefts[0] - rest_left if n > 1 else lefts[0] - cx0
    letters = [c for c in text if c.isalpha()]
    return {
        "x": x0 / page_w if page_w else 0, "y": y0 / page_h if page_h else 0,
        "w": (x1 - x0) / page_w if page_w else 0, "h": (y1 - y0) / page_h if page_h else 0,
        "n_lines": n, "chars": len(text), "words": len(text.split()),
        "font_size": round(size, 2), "font_ratio": round(size / ctx.body_size, 3) if ctx.body_size else 1.0,
        "bold": sum(li.bold_ratio for li in lines) / n >= 0.5,
        "italic": sum(li.italic_ratio for li in lines) / n >= 0.5,
        "bold_ratio": round(sum(li.bold_ratio for li in lines) / n, 3),
        "italic_ratio": round(sum(li.italic_ratio for li in lines) / n, 3),
        "caps_ratio": round(sum(1 for c in letters if c.isupper()) / max(1, len(letters)), 3),
        "decorated_ratio": round(sum(li.decorated_ratio for li in lines) / n, 3),
        "left_indent": round(rest_left - cx0, 2), "right_indent": round(cx1 - max(rights), 2),
        "first_line_indent": round(first_indent, 2),
        "hanging": n > 1 and first_indent <= -0.8 * size,
        "centered": centered, "right_aligned": right_aligned,
        "line_fill": round(_median([w / col_w for w in widths], 1.0), 3),
        "short_line_share": round(sum(1 for w in widths[:-1] if w < VERSE_SHORT_RATIO * col_w) / max(1, n - 1), 3)
        if n > 1 else (1.0 if widths[0] < VERSE_SHORT_RATIO * col_w else 0.0),
        "ends_with_punct": text.rstrip()[-1:] in ".!?:;\"'”’)" if text.strip() else False,
        "starts_upper": text.strip()[:1].isupper(),
        "line_starts_upper_share": round(sum(1 for li in lines if li.text[:1].isupper()) / n, 3),
        "label_number": bool(_LABEL_NUMBER_RE.match(text)),
        "numbered_heading_depth": (_NUMBERED_HEADING_RE.match(text).group(1).count(".") + 1)
        if _NUMBERED_HEADING_RE.match(text) and len(text) < 160 else 0,
        "note_marker": bool(_NOTE_MARKER_RE.match(text)),
        "has_year": bool(_YEAR_RE.search(text)),
        "math_ratio": round(sum(1 for c in text if c in _MATH_CHARS) / max(1, len(text)), 3),
        "running_signature": _signature(text) in ctx.running_signatures,
        "col_width": round(col_w, 2), "col_left": round(cx0, 2), "col_right": round(cx1, 2),
    }


def _split_verse(lines, col_w, size):
    """Verse lines from a group: one block per verse line; a deeply indented
    lowercase-initial line is a turnover of the previous verse line."""
    out = []
    base_left = min(li.bbox[0] for li in lines)
    for li in lines:
        turnover = out and li.bbox[0] - base_left >= 2.5 * size and li.text[:1].islower()
        if turnover:
            out[-1].append(li)
        else:
            out.append([li])
    return out


def _looks_like_verse(lines, col_w, size, ctx) -> bool:
    if len(lines) < 2:
        return False
    widths = [li.width for li in lines]
    short = sum(1 for w in widths if w < VERSE_SHORT_RATIO * col_w)
    if short < max(2, int(0.75 * len(lines))):
        return False
    lefts = [li.bbox[0] for li in lines]
    # prose wraps fill the column; verse lines end where the line ends.
    hyphen_breaks = sum(1 for li in lines[:-1] if li.text.endswith("-"))
    if hyphen_breaks:
        return False
    # list-like markers are lists, not verse
    if sum(1 for li in lines if _NOTE_MARKER_RE.match(li.text)) >= len(lines) / 2:
        return False
    # line starts mostly at a shared left edge (allowing indented lines)
    left_mode = Counter(round(x) for x in lefts).most_common(1)[0][1]
    return left_mode >= max(2, len(lines) // 3)


@dataclass
class PageLayout:
    page: int
    width: float
    height: float
    blocks: list = field(default_factory=list)          # reading order
    columns: list = field(default_factory=list)
    footnote_rule_y: float = None
    source: str = "pdf"
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"page": self.page, "width": self.width, "height": self.height, "source": self.source,
                "footnote_rule_y": self.footnote_rule_y, "columns": self.columns, "stats": dict(self.stats),
                "blocks": [b.to_dict() for b in self.blocks]}


def analyse_page(pdf_document, page_num: int, ctx: DocumentContext, lines=None, ocr_result=None) -> PageLayout:
    page = pdf_document.get_page(page_num)
    page_w, page_h = pdf_document.page_size(page_num)
    layout = PageLayout(page=page_num, width=page_w, height=page_h)
    if lines is None:
        lines = extract_page_lines(page)
    if not lines and ocr_result is not None:
        lines = ocr_page_lines(ocr_result)
        layout.source = "ocr"
    images = list(detect_images(page))
    big_image = any((i[2] - i[0]) * (i[3] - i[1]) >= 0.7 * page_w * page_h for i in images)
    if big_image and layout.source == "pdf" and lines:
        # Searchable scan: the page image is the page, not a figure.
        images = [i for i in images if (i[2] - i[0]) * (i[3] - i[1]) < 0.7 * page_w * page_h]
    blocks = []

    # 1. header / footer bands: running heads and page numbers
    header_lines = [li for li in lines if li.bbox[3] <= page_h * HEADER_BAND]
    footer_lines = [li for li in lines if li.bbox[1] >= page_h * (1 - FOOTER_BAND)]
    hf_ids = set()
    for li, region in [(li, "header") for li in header_lines] + [(li, "footer") for li in footer_lines]:
        hf_ids.add(id(li))
        if page_number_detector._looks_like_page_number(li.text):
            kind = "page_number"
        elif _signature(li.text) in ctx.running_signatures:
            kind = "running"
        else:
            # a lone short line in the band that is not repeated elsewhere:
            # keep it as ordinary text so nothing is lost
            hf_ids.discard(id(li))
            continue
        blk = LayoutBlock(kind=kind, bbox=li.bbox, lines=[li], region=region)
        blk.features = _line_features_for_block([li], (0.0, page_w), ctx, page_w, page_h)
        blk.features["in_header"] = region == "header"
        blk.features["in_footer"] = region == "footer"
        blocks.append(blk)
    body_lines = [li for li in lines if id(li) not in hf_ids]

    # 2. tables and figures (floats)
    try:
        tables = table_detector.detect_tables(page, body_lines, page_h) if layout.source == "pdf" else []
    except Exception:
        tables = []
    floats = []
    for t in tables:
        inside = [li for li in body_lines if _inside(li, t)]
        blk = LayoutBlock(kind="table", bbox=tuple(t), lines=inside)
        blk.features = {"x": t[0] / page_w, "y": t[1] / page_h, "w": (t[2] - t[0]) / page_w,
                        "h": (t[3] - t[1]) / page_h, "n_lines": len(inside), "has_text": bool(inside)}
        floats.append(blk)
    body_lines = [li for li in body_lines if not any(_inside(li, t) for t in tables)]
    vector_figs = _vector_figures(page, [tuple(t) for t in tables], page_w, page_h, body_lines) \
        if layout.source == "pdf" else []
    for box in list(images) + vector_figs:
        if any(_overlap_ratio(box, f.bbox) > 0.6 for f in floats):
            continue
        # text inside an illustration (labels on a diagram) belongs to it
        inside = [li for li in body_lines if _inside(li, box, tol=0.5)]
        blk = LayoutBlock(kind="figure", bbox=tuple(box), lines=inside)
        blk.features = {"x": box[0] / page_w, "y": box[1] / page_h, "w": (box[2] - box[0]) / page_w,
                        "h": (box[3] - box[1]) / page_h, "vector": box in vector_figs}
        floats.append(blk)
        body_lines = [li for li in body_lines if not any(li is x for x in inside)]

    # 3. footnote region
    rule_y = _footnote_separator(page, body_lines, page_w, page_h) if layout.source == "pdf" else None
    layout.footnote_rule_y = rule_y

    # 4. bands / columns
    bands = _split_spanning(column_detector.detect_bands(page_w, page_h, body_lines)) if body_lines else []
    layout.columns = [list(b.column_bounds) for b in bands]
    text_blocks = []
    for band_idx, band in enumerate(bands):
        for col_idx in range(band.columns):
            col_lines = [li for li in band.items if column_detector.column_index_for_bbox(band, li.bbox) == col_idx]
            if not col_lines:
                continue
            # The measure is the real text extent of this column (most
            # common left edge .. most common right edge), not the
            # geometric slot column_detector assigns it.
            col_bounds = _measure(col_lines, band.column_bounds[col_idx])
            col_lines.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
            col_w = max(1.0, col_bounds[1] - col_bounds[0])
            for blk in _segment_column(col_lines, col_bounds, col_w, ctx, page_w, page_h, rule_y):
                blk.column, blk.band = col_idx, band_idx
                text_blocks.append(blk)

    # 5. float adjacency (caption candidates) + body context
    for blk in text_blocks:
        f = blk.features
        f["float_above"] = f["float_below"] = None
        for fl in floats:
            h_overlap = min(blk.bbox[2], fl.bbox[2]) - max(blk.bbox[0], fl.bbox[0])
            if h_overlap <= 0:
                continue
            gap_below_float = blk.bbox[1] - fl.bbox[3]
            gap_above_float = fl.bbox[1] - blk.bbox[3]
            if 0 <= gap_below_float <= FLOAT_CAPTION_GAP:
                f["float_above"] = fl.kind
            if 0 <= gap_above_float <= FLOAT_CAPTION_GAP:
                f["float_below"] = fl.kind
    # vertical context (gaps to neighbours in the same column)
    by_col = {}
    for blk in text_blocks:
        by_col.setdefault((blk.band, blk.column), []).append(blk)
    for seq in by_col.values():
        seq.sort(key=lambda b: b.bbox[1])
        for i, blk in enumerate(seq):
            prev_b = seq[i - 1] if i > 0 else None
            next_b = seq[i + 1] if i + 1 < len(seq) else None
            blk.features["gap_above"] = round(blk.bbox[1] - prev_b.bbox[3], 2) if prev_b else None
            blk.features["gap_below"] = round(next_b.bbox[1] - blk.bbox[3], 2) if next_b else None
            blk.features["prev_font_ratio"] = prev_b.features.get("font_ratio") if prev_b else None
            blk.features["next_font_ratio"] = next_b.features.get("font_ratio") if next_b else None
            blk.features["first_in_column"] = prev_b is None
            blk.features["last_in_column"] = next_b is None

    # 6. reading order: page number first, then band > column > y with
    #    floats placed by their own top edge in their column, notes last,
    #    running heads (no content role) at the very end.
    for fl in floats:
        band_idx, col_idx = 0, 0
        for bi, band in enumerate(bands):
            if band.y0 - 2 <= (fl.bbox[1] + fl.bbox[3]) / 2 <= band.y1 + 2:
                band_idx, col_idx = bi, column_detector.column_index_for_bbox(band, fl.bbox)
                break
            if (fl.bbox[1] + fl.bbox[3]) / 2 > band.y1:
                band_idx = bi
        fl.band, fl.column = band_idx, col_idx
    flow = text_blocks + floats

    def flow_key(b):
        notes = 1 if b.region == "notes" else 0
        return (notes, b.band, b.column, b.bbox[1], b.bbox[0])

    flow.sort(key=flow_key)
    head = [b for b in blocks if b.kind == "page_number"]
    tail = [b for b in blocks if b.kind != "page_number"]
    layout.blocks = head + flow + tail
    for i, b in enumerate(layout.blocks, start=1):
        b.order = i
        if layout.source == "ocr":
            b.features["ocr"] = True
    layout.stats = {"lines": len(lines), "blocks": len(layout.blocks), "tables": len(tables),
                    "figures": sum(1 for f in floats if f.kind == "figure"), "columns": max(
                        [b.columns for b in bands], default=1)}
    return layout


def _measure(lines, fallback):
    lefts = Counter(round(li.bbox[0]) for li in lines)
    rights = Counter(round(li.bbox[2]) for li in lines)
    left = min(li.bbox[0] for li in lines)
    right = max(li.bbox[2] for li in lines)
    # prefer the dominant edges when several lines share them (justified
    # prose); otherwise the extremes
    lm, lc = lefts.most_common(1)[0]
    rm, rc = rights.most_common(1)[0]
    if lc >= 3 and lm - left < 60:
        left = min(left, lm)
    if rc >= 3:
        right = max(rm, right)
    return (left, right) if right > left else fallback


def _split_spanning(bands):
    """A line that crosses a multi-column band's gutter is full-width
    content: it closes the column band and stands in its own band (so a
    full-width line below two columns is read after BOTH columns)."""
    out = []
    for band in bands:
        if band.columns <= 1:
            out.append(band)
            continue
        bounds = band.column_bounds
        gutters = [(bounds[i][1], bounds[i + 1][0]) for i in range(len(bounds) - 1)]

        def spans(li):
            return any(li.bbox[0] < g0 - 2 and li.bbox[2] > g1 + 2 for g0, g1 in gutters)

        cur = []
        for li in sorted(band.items, key=lambda x: (x.bbox[1], x.bbox[0])):
            if spans(li):
                if cur:
                    out.append(column_detector.Band(y0=min(x.bbox[1] for x in cur), y1=max(x.bbox[3] for x in cur),
                                                    columns=band.columns, column_bounds=bounds, items=cur))
                    cur = []
                out.append(column_detector.Band(y0=li.bbox[1], y1=li.bbox[3], columns=1,
                                                column_bounds=[(li.bbox[0], li.bbox[2])], items=[li]))
            else:
                cur.append(li)
        if cur:
            out.append(column_detector.Band(y0=min(x.bbox[1] for x in cur), y1=max(x.bbox[3] for x in cur),
                                            columns=band.columns, column_bounds=bounds, items=cur))
    # consecutive single-column bands merge back into one band
    merged = []
    for b in sorted(out, key=lambda b: b.y0):
        if merged and merged[-1].columns == 1 and b.columns == 1:
            m = merged[-1]
            m.items = list(m.items) + list(b.items)
            m.y1 = max(m.y1, b.y1)
            m.column_bounds = [(min(m.column_bounds[0][0], b.column_bounds[0][0]),
                                max(m.column_bounds[0][1], b.column_bounds[0][1]))]
        else:
            merged.append(b)
    return merged


def _segment_column(col_lines, col_bounds, col_w, ctx, page_w, page_h, rule_y) -> list:
    """Lists -> paragraphs/verse -> features, for one column."""
    out = []
    notes_lines = [li for li in col_lines if rule_y is not None and li.bbox[1] >= rule_y - 1]
    main_lines = [li for li in col_lines if not any(li is n for n in notes_lines)]

    def build(kind, lines, region="body", extra=None):
        blk = LayoutBlock(kind=kind, bbox=_union([li.bbox for li in lines]), lines=list(lines), region=region)
        blk.features = _line_features_for_block(lines, col_bounds, ctx, page_w, page_h)
        blk.features["region"] = region
        if extra:
            blk.features.update(extra)
        return blk

    # footnotes: one block per marker-initial entry
    if notes_lines:
        entries = []
        for li in notes_lines:
            if not entries or _NOTE_MARKER_RE.match(li.text):
                entries.append([li])
            else:
                entries[-1].append(li)
        for e in entries:
            out.append(build("text", e, region="notes", extra={"below_footnote_rule": True}))

    # list runs (reused detector), mapped to item blocks with nesting level
    runs = list_detector.detect_list_runs(main_lines) if main_lines else []
    consumed = set()

    def add_run(run, level):
        for item in run.items:
            for li in item.lines:
                consumed.add(id(li))
            blk = build("list_item", item.lines, extra={"list_type": run.list_type, "list_level": level})
            out.append(blk)
            for child in item.children:
                add_run(child, level + 1)

    for run in runs:
        add_run(run, 1)
    rest = [li for li in main_lines if id(li) not in consumed]
    if not rest:
        return out

    # paragraphs (reused segmentation), then split style changes and verse
    groups = paragraph_auto_zone.group_lines_into_paragraphs(rest)
    for g in groups:
        g_lines = sorted(g.lines, key=lambda li: (li.bbox[1], li.bbox[0]))
        for sub in _split_on_style_change(g_lines, ctx):
            size = _median([li.font_size for li in sub], ctx.body_size)
            if _looks_like_verse(sub, col_w, size, ctx):
                for vl in _split_verse(sub, col_w, size):
                    out.append(build("verse_line", vl, extra={"verse_group_size": len(sub)}))
            else:
                out.append(build("text", sub))
    # verse continuity across stanza breaks: mark neighbours
    return out


def _split_on_style_change(lines, ctx):
    """Breaks a paragraph-segmenter group where typography clearly changes
    (a heading line glued to the next paragraph, a size switch)."""
    out, cur = [], []
    for li in lines:
        if cur:
            prev = cur[-1]
            size_jump = abs(li.font_size - prev.font_size) > max(0.8, 0.12 * max(li.font_size, prev.font_size))
            weight_jump = abs(li.bold_ratio - prev.bold_ratio) > 0.6 and (len(prev.text) < 120 or len(li.text) < 120)
            if size_jump or weight_jump:
                out.append(cur)
                cur = []
        cur.append(li)
    if cur:
        out.append(cur)
    return out
