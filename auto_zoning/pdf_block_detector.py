"""Page-wide raw layout extraction for the auto-zoning engine: every text
line (bbox + font stats) and every embedded image bbox on a page - the raw
material candidate zones are built from. Extraction only, no tagging
decisions (see zone_matcher.py/tag_predictor.py for that), and consecutive-
line grouping into paragraph/heading-candidate blocks.

Reuses the project's existing PDF-text pipeline (core.text_extractor's
cached rawdict + core.formatting_detector's font-flag decoding) rather than
re-parsing pages a second, competing way."""
from collections import Counter
from dataclasses import dataclass, field

from core.text_extractor import _get_rawdict
from core.formatting_detector import detect_bold_italic, line_baseline_stats

BLOCK_GAP_MAX_RATIO = 1.8       # a vertical gap more than this many line-heights breaks a block
BLOCK_SIZE_TOLERANCE = 0.12     # relative font-size difference allowed within one block


@dataclass
class LineInfo:
    bbox: tuple            # (x0, y0, x1, y1) in PDF points
    text: str               # plain text of this line
    font_size: float
    bold: bool
    italic: bool


def detect_lines(page) -> list:
    """Every text line on the whole page, top-to-bottom document order -
    unlike text_extractor.extract_lines (which needs a target bbox and
    returns inline-formatted text for XML), this scans the ENTIRE page
    unconditionally and keeps raw per-line font metrics, since that's what
    candidate-zone detection/scoring needs."""
    raw = _get_rawdict(page)
    lines_out = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            dom_size, _ = line_baseline_stats(line)
            text_parts = []
            line_bbox = None
            bold_chars = italic_chars = total_chars = 0
            for span in spans:
                span_text = "".join(c.get("c", "") for c in span.get("chars", []))
                if not span_text:
                    continue
                text_parts.append(span_text)
                sb = span.get("bbox")
                if sb:
                    line_bbox = sb if line_bbox is None else (
                        min(line_bbox[0], sb[0]), min(line_bbox[1], sb[1]),
                        max(line_bbox[2], sb[2]), max(line_bbox[3], sb[3]))
                bold, italic = detect_bold_italic(span.get("flags", 0), span.get("font", ""))
                n = len(span_text)
                total_chars += n
                if bold:
                    bold_chars += n
                if italic:
                    italic_chars += n
            text = "".join(text_parts).strip()
            if not text or line_bbox is None:
                continue
            lines_out.append(LineInfo(
                bbox=line_bbox, text=text, font_size=dom_size,
                bold=total_chars > 0 and bold_chars / total_chars >= 0.5,
                italic=total_chars > 0 and italic_chars / total_chars >= 0.5,
            ))
    lines_out.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
    return lines_out


def page_body_font_size(lines: list) -> float:
    """The page's dominant ("body text") font size - the mode of every
    line's own dominant size, weighted by that line's character count, so a
    page of mostly-paragraph text isn't skewed by a few short heading
    lines. Used to turn every OTHER font size on the page into a size
    RATIO (see layout_template.compute_features) - comparable across two
    different PDFs that use different absolute point sizes for body text."""
    counts = Counter()
    for li in lines:
        counts[round(li.font_size, 1)] += max(len(li.text), 1)
    if not counts:
        return 0.0
    return counts.most_common(1)[0][0]


def detect_images(page) -> list:
    """Every embedded image's placement bbox on the page (PDF points),
    unconditionally - the same PyMuPDF primitive core.image_extractor
    already uses (page.get_image_info(xrefs=True)) to MATCH an
    already-known bbox, called here instead with no target bbox to
    ENUMERATE every image as a figure candidate."""
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return []
    out = []
    for im in infos:
        bbox = im.get("bbox")
        if bbox and (bbox[2] - bbox[0]) > 1 and (bbox[3] - bbox[1]) > 1:
            out.append(tuple(bbox))
    return out


@dataclass
class BlockInfo:
    lines: list = field(default_factory=list)

    @property
    def bbox(self):
        x0 = min(li.bbox[0] for li in self.lines)
        y0 = min(li.bbox[1] for li in self.lines)
        x1 = max(li.bbox[2] for li in self.lines)
        y1 = max(li.bbox[3] for li in self.lines)
        return (x0, y0, x1, y1)

    @property
    def text(self):
        return "\n".join(li.text for li in self.lines)

    @property
    def font_size(self):
        sizes = [li.font_size for li in self.lines if li.font_size]
        return sizes[0] if sizes else 0.0

    @property
    def bold(self):
        return sum(1 for li in self.lines if li.bold) >= (len(self.lines) + 1) // 2

    @property
    def italic(self):
        return sum(1 for li in self.lines if li.italic) >= (len(self.lines) + 1) // 2


def group_lines_into_blocks(lines: list) -> list:
    """Groups consecutive LineInfo (already page/column order) into
    paragraph/heading-candidate blocks: consecutive lines with similar font
    size, same bold/italic majority, and a small enough vertical gap belong
    to the same block; a real style change or a big gap starts a new one.
    Callers exclude any lines already consumed by
    list_detector.detect_list_runs BEFORE calling this, so a list item's
    own wrapped lines are grouped there instead, not duplicated here."""
    blocks = []
    current = []
    prev = None
    for li in lines:
        if not current:
            current = [li]
        else:
            gap = li.bbox[1] - prev.bbox[3]
            same_style = (prev.bold == li.bold and prev.italic == li.italic)
            size_close = (prev.font_size <= 0 or abs(li.font_size - prev.font_size) / max(prev.font_size, 1.0)
                          <= BLOCK_SIZE_TOLERANCE)
            median_h = (prev.bbox[3] - prev.bbox[1]) or 10.0
            if same_style and size_close and gap <= median_h * BLOCK_GAP_MAX_RATIO:
                current.append(li)
            else:
                blocks.append(BlockInfo(lines=current))
                current = [li]
        prev = li
    if current:
        blocks.append(BlockInfo(lines=current))
    return blocks
