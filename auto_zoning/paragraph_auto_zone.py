"""Paragraph Auto Zone: detects individual paragraph boundaries in ordinary
body text on a page and proposes ONE PredictedZone per paragraph (spec:
"Paragraph Auto Zone" - "A paragraph may contain 1, 2, 3, 10 or more lines.
All lines belonging to one paragraph must remain in ONE zone", and never a
fixed-height/equal-rectangle division).

Reuses the existing, already-tested primitives wherever they fit:
auto_zoning.pdf_block_detector.detect_lines (raw line/font extraction),
auto_zoning.page_number_detector.detect (header/footer band exclusion - the
SAME primitive auto_zoning.page_analyzer's own Auto Analyse pipeline already
uses, so a running header/footer never becomes its own bogus "paragraph"),
and core.column_detector.detect_bands/column_index_for_bbox (the SAME band/
column partitioning core.reading_order's own canonical engine and Auto
Analyse's own _classify_lines both already use, so paragraph zones are
produced in the identical column-major order the rest of the app already
expects). This module itself never touches ZoneManager - detection only,
the same separation auto_zoning/index_auto_zone.py already establishes;
gui/main_window.py's App.auto_zone_paragraph_* methods are responsible for
actually creating zones, only after the user confirms the preview.

WHY NOT pdf_block_detector.group_lines_into_blocks (Auto Analyse's own
block grouping): that function groups consecutive lines into ONE block
whenever font style stays consistent and the gap between them isn't
unusually large - correct for ITS OWN purpose (telling a heading apart from
body text), but wrong for this feature's requirement: a page of ten same-
style body paragraphs separated only by ordinary line-wrap-sized gaps would
collapse into a SINGLE giant block there. This module instead looks for the
two signals that actually mark a real paragraph boundary in typical body-
text layout - a first-line indent, or an extra (paragraph-spacing-sized)
vertical gap - either of which group_lines_into_blocks's own simpler style/
gap test does not distinguish from an ordinary same-paragraph line wrap."""
from dataclasses import dataclass, field
import statistics

from core.table_extractor import _cluster_1d
from core import column_detector
from auto_zoning import pdf_block_detector, page_number_detector
from auto_zoning.hierarchy_builder import PredictedZone

SOURCE_TAG = "auto_paragraph"
PARAGRAPH_TAG = "p"   # same literal tag auto_zoning.heading_detector.classify_blocks already
# uses for ordinary body text, regardless of active profile - see this module's own
# docstring in the caller for why no per-profile tag lookup is needed here.

CONFIDENCE = 90.0

# A vertical gap bigger than this many line-heights marks a paragraph break even
# with NO indentation signal at all (block-style/blank-line-separated paragraphs) -
# same ratio convention (not the same constant object) as auto_zoning.
# pdf_block_detector.BLOCK_GAP_MAX_RATIO / auto_zoning.index_auto_zone.GAP_MAX_RATIO,
# reused here for consistency rather than inventing a third, unrelated magic number.
PARAGRAPH_GAP_RATIO = 1.4
# The trailing-final-line special case (see its own comment at the split-
# decision site below) requires a much larger gap than the ordinary
# PARAGRAPH_GAP_RATIO before trusting gap-only evidence with nothing after
# it to corroborate a genuine new paragraph.
TRAILING_LINE_GAP_RATIO = 2.0
# How much narrower than this paragraph's own typical line width counts as
# a short trailing wrap remnant (a wrapped paragraph's last, partial line),
# rather than a plausible new paragraph's own first line.
SHORT_TRAILING_FRAGMENT_RATIO = 0.6
# A first line at least this far right of the column's own flush-left margin counts
# as a first-line indent (never a bare fixed constant alone - scaled by this page's
# own body font size too, the same 0.35*body_size convention auto_zoning.
# index_auto_zone._indentation_tolerance already uses for an analogous "is this
# really an indent, or just coordinate noise" question).
INDENT_MIN_PT = 6.0


def _indent_tolerance(body_font_size: float) -> float:
    return max(INDENT_MIN_PT, 0.35 * (body_font_size or 0.0))


def _column_left_margin(lines: list) -> float:
    """The flush-left edge most lines in this column return to - i.e. the
    continuation-line X of an indented-first-line paragraph, or every line's
    own X in a block-style (no indent) paragraph. The LARGEST x0 cluster's
    center (never a bare min/mean, which a single stray indented line would
    skew) - the baseline a first-line indent is measured against."""
    if not lines:
        return 0.0
    x0s = [li.bbox[0] for li in lines]
    centers = _cluster_1d(x0s, tolerance=3.0)
    if not centers:
        return min(x0s)
    counts = {c: sum(1 for x in x0s if abs(x - c) <= 3.0) for c in centers}
    return max(counts, key=counts.get)


def group_lines_into_paragraphs(lines: list) -> list:
    """Group one column's extracted lines into real paragraph candidates.

    Paragraph boundaries are determined from two independent, document-local
    signals:

      1. a statistically significant first-line indentation transition;
      2. a vertical gap that is substantially larger than the column's normal
         line leading.

    IMPORTANT: normal leading is NOT the minimum observed leading.  The old
    minimum-leading method is unstable when PyMuPDF reports one unusually
    small inter-line distance: that single value becomes the reference and
    causes every ordinary line to look like a paragraph break.  Instead we
    estimate the dominant normal-leading cluster from the column itself.

    Wrapped lines therefore remain in one paragraph even when their x0 values
    have small extraction jitter.  A paragraph with no indentation can still
    be split by its actual extra vertical spacing.
    """
    if not lines:
        return []

    ordered = sorted(
        lines,
        key=lambda li: (li.bbox[1], li.bbox[0])
    )

    body_size = pdf_block_detector.page_body_font_size(ordered)

    # ------------------------------------------------------------------
    # 1. Estimate NORMAL line leading from the column itself.
    #
    # Do not use min(leadings).  A single anomalously close pair can be a
    # glyph/bbox extraction artifact and must not become the reference for
    # the entire column.
    # ------------------------------------------------------------------
    leadings = [
        ordered[i].bbox[1] - ordered[i - 1].bbox[1]
        for i in range(1, len(ordered))
        if ordered[i].bbox[1] > ordered[i - 1].bbox[1]
    ]

    def _fallback_leading():
        if body_size:
            return max(1.0, float(body_size) * 1.20)

        heights = [
            float(li.bbox[3]) - float(li.bbox[1])
            for li in ordered
            if float(li.bbox[3]) > float(li.bbox[1])
        ]
        return max(1.0, statistics.median(heights) if heights else 10.0)

    if len(leadings) < 2:
        normal_leading = _fallback_leading()
    else:
        # Cluster observed leadings using a scale derived from the data.
        # This is deliberately local to this column/page; no PDF coordinate
        # or fixed font size is assumed.
        ordered_leadings = sorted(leadings)
        typical = statistics.median(ordered_leadings)
        cluster_tol = max(0.75, typical * 0.12)

        clusters = []
        current = [ordered_leadings[0]]

        for value in ordered_leadings[1:]:
            center = statistics.median(current)
            if abs(value - center) <= cluster_tol:
                current.append(value)
            else:
                clusters.append(current)
                current = [value]
        clusters.append(current)

        # The normal line spacing is the most frequently occurring cluster.
        # If two clusters have the same count, prefer the smaller one because
        # paragraph gaps are expected to be the larger spacing.
        best = max(
            clusters,
            key=lambda c: (len(c), -statistics.median(c))
        )

        normal_leading = max(1.0, statistics.median(best))

        # If the selected cluster is implausibly different from the page's
        # body-size-derived leading, use the stronger of the two local signals.
        # This guards against a tiny sample where the only repeated spacing is
        # actually a paragraph gap.
        size_leading = float(body_size) * 1.20 if body_size else normal_leading
        if size_leading > 0:
            ratio = normal_leading / size_leading
            if ratio < 0.65 or ratio > 1.55:
                normal_leading = size_leading

    # ------------------------------------------------------------------
    # 2. Determine the column's flush-left margin.
    # ------------------------------------------------------------------
    left_margin = _column_left_margin(ordered)
    indent_tol = _indent_tolerance(body_size)

    def _is_flush_left(line):
        return (line.bbox[0] - left_margin) <= indent_tol

    def _is_first_line_indent(index):
        """Recognize an indentation TRANSITION, not merely a right-shift."""
        if index <= 0:
            return False

        current = ordered[index]
        previous = ordered[index - 1]

        # Current line must be clearly indented.
        if (current.bbox[0] - left_margin) <= indent_tol:
            return False

        # Previous line must return to the normal margin.
        if not _is_flush_left(previous):
            return False

        # A genuine first-line indent normally has a continuation line at
        # the normal margin. If the following line is also indented, do not
        # split here: that pattern is much more likely to be a block/layout
        # variation than a one-line paragraph indent.
        if index + 1 < len(ordered):
            following = ordered[index + 1]
            if not _is_flush_left(following):
                return False

        return True

    # ------------------------------------------------------------------
    # 3. Group lines.
    #
    # A vertical break must be clearly larger than the locally learned
    # ordinary leading. The indentation signal is independent.
    # ------------------------------------------------------------------
    paragraphs = [pdf_block_detector.BlockInfo(lines=[ordered[0]])]

    for index, line in enumerate(ordered[1:], start=1):
        current = paragraphs[-1]
        previous = current.lines[-1]

        leading = line.bbox[1] - previous.bbox[1]

        # Extra spacing is a RELATIVE signal.  The reference came from the
        # dominant line-spacing cluster above, so ordinary line wraps do not
        # become separate paragraphs just because one earlier pair was tight.
        gap_ratio = (
            leading / normal_leading
            if normal_leading > 0 and leading > 0
            else 0.0
        )

        big_gap = gap_ratio >= PARAGRAPH_GAP_RATIO

        first_line_indent = _is_first_line_indent(index)

        # A wrapped paragraph's own FINAL line - whatever short remainder of
        # the last sentence happens to wrap onto its own line - is the one
        # split candidate with no following line at all to corroborate it:
        # every other gap-only candidate can at least be checked against
        # what comes after (a real new paragraph normally continues; see
        # _is_first_line_indent's own "following line" check above), but the
        # true last line of a column never gets that chance. Confirmed
        # directly as a real false-positive source: a short final line's own
        # bbox/leading can measure a mildly larger-than-normal gap purely
        # from being short (fewer/no descenders, a tighter glyph-derived
        # bbox) - not from actually starting anything new. Gap-only evidence
        # (no indent) at this ONE position is therefore trusted only when it
        # is NOT also a short trailing fragment (far narrower than this
        # paragraph's own typical line width - a wrap remnant's own
        # signature) or when the gap is dramatically larger than the
        # ordinary paragraph-break threshold, which still lets a genuinely
        # separate, deliberately-spaced short trailing line (e.g. a one-line
        # epigraph attribution) split away as before.
        is_last_line = (index == len(ordered) - 1)
        if big_gap and not first_line_indent and is_last_line:
            current_widths = [li.bbox[2] - li.bbox[0] for li in current.lines]
            typical_width = statistics.median(current_widths) if current_widths else 0.0
            line_width = line.bbox[2] - line.bbox[0]
            is_short_trailing_fragment = (
                typical_width > 0 and line_width < SHORT_TRAILING_FRAGMENT_RATIO * typical_width
            )
            if is_short_trailing_fragment and gap_ratio < TRAILING_LINE_GAP_RATIO:
                big_gap = False

        if big_gap or first_line_indent:
            paragraphs.append(
                pdf_block_detector.BlockInfo(lines=[line])
            )
        else:
            current.lines.append(line)

    return paragraphs


def _bbox_overlaps(a, b, tol: float = 1.0) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 + tol or bx1 <= ax0 + tol or ay1 <= by0 + tol or by1 <= ay0 + tol)


def _line_center(li) -> tuple:
    return ((li.bbox[0] + li.bbox[2]) / 2.0, (li.bbox[1] + li.bbox[3]) / 2.0)


@dataclass
class ParagraphAutoZoneResult:
    predicted_zones: list = field(default_factory=list)   # flat, column-major reading order
    paragraphs_detected: int = 0
    lines_skipped_existing: int = 0
    warnings: list = field(default_factory=list)


def detect_paragraph_zones(pdf_document, page_num: int, existing_bboxes: list = None,
                            lines: list = None) -> ParagraphAutoZoneResult:
    """THE detection entry point. `existing_bboxes`: bboxes of every zone
    already on this page (spec 3: "NEVER delete or overwrite existing
    manual zones... check page/coordinates/overlap... if an equivalent zone
    already exists, SKIP IT") - any line whose CENTER falls inside one of
    these is dropped before paragraph-grouping ever sees it, so already-
    zoned content can never end up inside (or duplicated by) a newly
    proposed paragraph zone, and running this twice in a row proposes
    nothing new the second time. `lines`: an explicit pre-extracted line
    list (spec: OCR compatibility - the caller has already decided digital-
    text-layer vs. cached-OCR-result sourcing for this page, e.g. via
    gui/main_window.py's App._index_lines_for_page, the exact same helper
    Auto Zone Index already uses for this identical decision); None (the
    normal case) extracts fresh from the document's own digital text layer."""
    result = ParagraphAutoZoneResult()
    page_width, page_height = pdf_document.page_size(page_num)
    if lines is None:
        page = pdf_document.get_page(page_num)
        lines = pdf_block_detector.detect_lines(page)
    if not lines:
        result.warnings.append("No text could be detected on this page.")
        return result

    _pagenumber_line, header_footer_ids = page_number_detector.detect(lines, page_width, page_height)
    lines = [li for li in lines if id(li) not in header_footer_ids]

    existing_bboxes = existing_bboxes or []
    if existing_bboxes:
        kept = []
        for li in lines:
            cx, cy = _line_center(li)
            if any(_bbox_overlaps((cx, cy, cx, cy), b, tol=0.0) for b in existing_bboxes):
                result.lines_skipped_existing += 1
            else:
                kept.append(li)
        lines = kept
    if not lines:
        result.warnings.append("No new (unzoned) text could be detected on this page.")
        return result

    bands = column_detector.detect_bands(page_width, page_height, lines)
    predicted = []
    for band in bands:
        for col_idx in range(band.columns):
            col_lines = sorted(
                (li for li in band.items if column_detector.column_index_for_bbox(band, li.bbox) == col_idx),
                key=lambda li: (li.bbox[1], li.bbox[0]))
            if not col_lines:
                continue
            for block in group_lines_into_paragraphs(col_lines):
                predicted.append(PredictedZone(
                    bbox=block.bbox, tag=PARAGRAPH_TAG, confidence=CONFIDENCE,
                    attributes={"source": SOURCE_TAG}))

    if not predicted:
        result.warnings.append("No paragraphs could be detected on this page.")
        return result

    result.predicted_zones = predicted
    result.paragraphs_detected = len(predicted)
    return result
