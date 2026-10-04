"""Bibliography Auto Zone by Pattern: learns the visual pattern of 3-4
manually-created bibliography sample zones, then proposes zones for the
REMAINING entries elsewhere on the page/document (spec: "Bibliography Auto
Zone by Pattern" - "Learn the common visual pattern... scan the remaining
bibliography... automatically create semantic refn/refd zones for the remaining entries").

Two-phase design, matching auto_zoning/index_auto_zone.py's own pure-
detection separation (never touches ZoneManager itself; gui/main_window.py
creates the actual zones, only after the user confirms the preview):

  1. learn_pattern(): reads the actual PDF lines INSIDE each sample zone's
     own bbox (never just the sample rectangle itself - spec: "Do NOT
     simply copy the sample rectangle") to learn first-line X, continuation-
     line X (hanging indent), line height, and typical entry line-count.
  2. detect_bibliography_zones(): scans a page's remaining (not-yet-zoned)
     lines for the learned pattern and groups them into complete entries -
     a first-line-X line SOMETIMES starts a new entry (hanging-indent
     style: any recurrence of continuation-X is always folded into the
     current entry, spec 6: "X1 -> X2 does NOT mean a new entry"), or, when
     no hanging indent was learned (flush-left/blank-line-separated style),
     an entry-spacing-sized vertical gap is the boundary signal instead -
     the same two-signal approach (indent OR gap) as auto_zoning.
     paragraph_auto_zone.group_lines_into_paragraphs, just with the
     indent's role reversed: paragraph=indent starts a NEW block, hanging-
     indent bibliography=indent (past the first line) CONTINUES one.

Reuses auto_zoning.pdf_block_detector.detect_lines for raw line extraction
and core.table_extractor._cluster_1d for 1D coordinate clustering - the
same primitives index_auto_zone.py and paragraph_auto_zone.py already
build on, never a second/competing PDF-parsing layer."""
import re
import statistics
from dataclasses import dataclass, field

from core.table_extractor import _cluster_1d
from auto_zoning import pdf_block_detector, page_number_detector, bibliography_content_signals as content
from auto_zoning.hierarchy_builder import PredictedZone

SOURCE_TAG = "auto_bibliography"
REFN_TAG = "ref_n"
REFD_TAG = "ref_d"
MIN_SAMPLES = 3
# Three-tier confidence (spec 34: "HIGH -> automatically zone. MEDIUM ->
# zone only if existing Auto Zone policy allows it. LOW -> do not create
# a potentially incorrect zone; report/review.") - this module's own
# existing Auto Zone policy (matching every other Auto Zone feature in
# this codebase) is "zone it", so MEDIUM is treated the same as HIGH for
# zone CREATION, but is still reported separately (never silently folded
# into the HIGH count) so a caller/UI can distinguish them.
CONFIDENCE_THRESHOLD = 85.0     # HIGH / MEDIUM boundary
CONFIDENCE_LOW_FLOOR = 55.0      # MEDIUM / LOW boundary - below this, never create a zone
CONFIDENCE_BASE = 92.0
CONFIDENCE_PENALTY = 20.0

# At least this fraction of a page's own (non-excluded) lines must land at one
# of the learned X positions for the page to be considered "bibliography-
# pattern content" at all - an ordinary, unrelated page elsewhere in a large
# document (Entire File scope) is expected to match essentially none of its
# lines and is skipped SILENTLY (nothing to report - it was never bibliography
# content in the first place, not a "pattern changed" situation).
MIN_PATTERN_MATCH_RATIO = 0.5
# Of the entries actually segmented from a page that DID clear the ratio
# above, at least this fraction must fall within the sample-derived typical
# line-count range for the page's structure to be trusted (spec 8: "If a
# page has a substantially different structure... report Pattern changed").
STRUCTURE_CONSISTENCY_MIN = 0.3
# A continuation-X line whose gap from the previous line is bigger than this
# many line-heights is treated as a NEW entry regardless (guards against a
# genuine page/section break coincidentally sharing the continuation-indent
# position - never trusted blindly just because the X position matches).
MAX_CONTINUATION_GAP_RATIO = 3.0
# Fallback boundary signal when no hanging indent was learned at all (every
# sample was a single line, or first-line/continuation X coincide) - the
# same generic "extra whitespace marks a break" ratio convention already
# used by auto_zoning.paragraph_auto_zone.PARAGRAPH_GAP_RATIO / auto_zoning.
# pdf_block_detector.BLOCK_GAP_MAX_RATIO / auto_zoning.index_auto_zone.
# GAP_MAX_RATIO.
ENTRY_GAP_RATIO = 1.6


def _tolerance(body_font_size: float) -> float:
    return max(6.0, 0.35 * (body_font_size or 0.0))


def _mode_or_median(xs: list, tol: float) -> float:
    if not xs:
        return 0.0
    centers = _cluster_1d(xs, tolerance=tol)
    if not centers:
        return statistics.median(xs)
    counts = {c: sum(1 for x in xs if abs(x - c) <= tol) for c in centers}
    return max(counts, key=counts.get)


def _lines_for_page(pdf_document, page_num: int, cache: dict) -> list:
    """Per-document-scan page-line cache (spec 13: "Do not repeatedly reload
    or parse the same page") - shared across every sample zone AND every
    scanned page within one learn_pattern/detect_bibliography_zones call."""
    if page_num not in cache:
        cache[page_num] = pdf_block_detector.detect_lines(pdf_document.get_page(page_num))
    return cache[page_num]


def _lines_in_bbox(lines: list, bbox, tol: float = 2.0) -> list:
    x0, y0, x1, y1 = bbox
    out = []
    for li in lines:
        cx, cy = (li.bbox[0] + li.bbox[2]) / 2.0, (li.bbox[1] + li.bbox[3]) / 2.0
        if x0 - tol <= cx <= x1 + tol and y0 - tol <= cy <= y1 + tol:
            out.append(li)
    out.sort(key=lambda li: (li.bbox[1], li.bbox[0]))
    return out


def _bbox_overlaps(a, b, tol: float = 1.0) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 + tol or bx1 <= ax0 + tol or ay1 <= by0 + tol or by1 <= ay0 + tol)


@dataclass
class BibliographyPattern:
    tag: str
    attributes: dict
    first_x: float
    continuation_x: float     # == first_x when no hanging indent was learned
    hanging_indent: bool
    tolerance: float
    body_line_height: float
    entry_gap_min: float       # fallback vertical-gap threshold (used only when not hanging_indent)
    typical_min_lines: int
    typical_max_lines: int


def _page_count(pdf_document):
    try:
        return len(pdf_document)
    except Exception:
        return int(getattr(pdf_document, "page_count", 0) or 0)


def _auto_x_tolerance(body_size):
    # Page-local tolerance; never tied to a particular PDF.
    return max(3.0, min(14.0, float(body_size or 10.0) * 0.30))


def _line_text(line):
    return str(getattr(line, "text", "") or "").strip()


def _line_x(line):
    return float(getattr(line, "bbox", (0, 0, 0, 0))[0])


def _line_y(line):
    return float(getattr(line, "bbox", (0, 0, 0, 0))[1])


def _line_leading(lines):
    """
    Calculate normal top-to-top line leading from the supplied PDF lines.

    The minimum positive spacing is used so large gaps between separate
    bibliography entries do not distort the normal line spacing. If the input
    contains only one line, fall back to its measured height.
    """
    if not lines:
        return 10.0

    ordered = sorted(
        lines,
        key=lambda li: (_line_y(li), _line_x(li))
    )

    distances = []
    for previous, current in zip(ordered, ordered[1:]):
        distance = _line_y(current) - _line_y(previous)
        if distance > 0:
            distances.append(distance)

    if distances:
        return max(1.0, min(distances))

    heights = []
    for line in ordered:
        bbox = getattr(line, "bbox", (0, 0, 0, 0))
        height = float(bbox[3]) - float(bbox[1])
        if height > 0:
            heights.append(height)

    return max(1.0, statistics.median(heights) if heights else 10.0)


def _auto_bibliography_pattern(pdf_document, tag="p", attributes=None):
    """Learn bibliography geometry automatically from the document.

    No manual samples are required.  The detector uses indentation clusters,
    vertical leading and bibliography-content evidence.  This is deliberately
    document/page local so a different book layout does not require changing
    source code.
    """
    cache = {}
    all_lines = []

    for page_num in range(_page_count(pdf_document)):
        lines = _lines_for_page(pdf_document, page_num, cache)
        lines = _exclude_bibliography_noise(pdf_document, page_num, lines)
        all_lines.extend(lines)

    if not all_lines:
        return None

    # Keep text-bearing lines only.  Do not use one fixed X coordinate.
    usable = [li for li in all_lines if _line_text(li)]
    if not usable:
        return None

    body_size = pdf_block_detector.page_body_font_size(usable)
    tol = _auto_x_tolerance(body_size)

    # Cluster the actual left edges found in the PDF.
    xs = [_line_x(li) for li in usable]
    clusters = _cluster_1d(xs, tolerance=tol) or []
    if not clusters:
        clusters = [statistics.median(xs)]

    stats = []
    for center in clusters:
        members = [li for li in usable if abs(_line_x(li) - center) <= tol]
        if not members:
            continue
        scores = [_content_score_for_auto(li) for li in members]
        stats.append((
            float(center),
            len(members),
            sum(scores) / len(scores),
            sum(1 for s in scores if s >= 0.25),
        ))

    if not stats:
        return None

    # Entry-start indentation is the cluster with the strongest combination
    # of bibliography content and repeated use.  This works for numbered
    # references, author/year references, and ordinary unnumbered entries.
    first_x, _, _, _ = max(
        stats,
        key=lambda s: (
            s[2] * 5.0 +
            (s[3] / max(1, s[1])) * 4.0 +
            min(1.0, s[1] / 8.0)
        ),
    )

    # Find a right-shifted cluster that is also repeatedly used by bibliography
    # lines.  If none exists, the page is treated as flush-left and vertical
    # spacing/content signals determine entry boundaries.
    right = [
        s for s in stats
        if s[0] > first_x + tol
        and (s[2] >= 0.18 or s[3] >= 1)
        and s[0] - first_x <= max(45.0, float(body_size or 10.0) * 4.0)
    ]

    continuation_x = first_x
    if right:
        continuation_x = min(
            right,
            key=lambda s: (
                s[0] - first_x,
                -s[2],
                -s[1],
            ),
        )[0]

    hanging = abs(continuation_x - first_x) > tol

    # Calculate normal within-entry leading from all bibliography-like lines.
    bib_lines = [
        li for li in usable
        if _content_score_for_auto(li) >= 0.20
    ]
    leading_values = []
    by_page = {}
    for li in bib_lines:
        p = getattr(li, "page", None)
        by_page.setdefault(p, []).append(li)
    for group in by_page.values():
        group.sort(key=lambda li: (_line_y(li), _line_x(li)))
        for a, b in zip(group, group[1:]):
            d = _line_y(b) - _line_y(a)
            if d > 0:
                leading_values.append(d)

    if not leading_values:
        leading_values = [
            li.bbox[3] - li.bbox[1]
            for li in bib_lines
            if li.bbox[3] > li.bbox[1]
        ]

    line_leading = max(1.0, statistics.median(leading_values) if leading_values else 10.0)

    return BibliographyPattern(
        tag=tag,
        attributes=dict(attributes or {}),
        first_x=first_x,
        continuation_x=continuation_x,
        hanging_indent=hanging,
        tolerance=tol,
        body_line_height=line_leading,
        entry_gap_min=line_leading * ENTRY_GAP_RATIO,
        typical_min_lines=1,
        typical_max_lines=999,
    )


def _content_score_for_auto(line):
    text = _line_text(line)
    if not text:
        return 0.0
    score = 0.0
    try:
        if content.looks_like_entry_start(text):
            score += 0.55
    except Exception:
        pass
    try:
        if content.extract_year(text):
            score += 0.15
    except Exception:
        pass
    try:
        if content.has_page_range(text):
            score += 0.10
    except Exception:
        pass
    try:
        if content.has_url_or_doi(text):
            score += 0.10
    except Exception:
        pass
    # Numbered bibliography/index entries are valid starts even when the
    # content helper does not recognize their exact typography.
    if re.match(r"^\s*(?:\d+|[A-Za-z]{1,4}\d*)[.)]\s+", text):
        score += 0.25
    # Common author/year form.
    if re.search(r"\b(?:19|20)\d{2}[a-z]?\b", text):
        score += 0.10
    return min(1.0, score)


def _exclude_bibliography_noise(pdf_document, page_num, lines):
    """Remove page-number/header/footer noise before automatic learning."""
    if not lines:
        return []
    try:
        width, height = pdf_document.page_size(page_num)
        page_line, header_footer_ids = page_number_detector.detect(
            lines, width, height
        )
        excluded = set(header_footer_ids or [])
        if page_line is not None:
            excluded.add(id(page_line))
    except Exception:
        excluded = set()

    result = []
    for li in lines:
        if id(li) in excluded:
            continue
        try:
            if content.looks_like_bibliography_heading(li.text):
                continue
        except Exception:
            pass
        result.append(li)
    return result


def learn_pattern(pdf_document, sample_zones=None, tag=None, attributes=None):
    """Learn bibliography layout without requiring manual examples.

    `sample_zones` is retained only for backward compatibility with older GUI
    callers.  When it is empty (the normal Auto Zone path), the complete
    document is analyzed automatically.  No "select 3 samples" requirement
    remains.
    """
    sample_zones = sample_zones or []

    if tag is None:
        tag = sample_zones[0].tag if sample_zones else REFD_TAG
    tag = _normalise_reference_tag(tag)
    if attributes is None:
        attributes = dict(sample_zones[0].attributes) if sample_zones else {}

    # NEW DEFAULT: fully automatic discovery.
    if not sample_zones:
        pattern = _auto_bibliography_pattern(
            pdf_document,
            tag=tag,
            attributes=attributes,
        )
        if pattern is None:
            return None, "No bibliography/reference layout could be detected automatically."
        return pattern, None

    # Compatibility mode for callers that still pass manually selected zones.
    page_cache = {}
    per_sample_lines = []
    for z in sample_zones:
        lines = _lines_in_bbox(
            _lines_for_page(pdf_document, z.page, page_cache), z.bbox
        )
        if lines:
            per_sample_lines.append(lines)

    if not per_sample_lines:
        return None, "Could not read any text inside the selected zones."

    all_lines = [li for group in per_sample_lines for li in group]
    body_size = pdf_block_detector.page_body_font_size(all_lines)
    tol = _auto_x_tolerance(body_size)
    first_x = statistics.median([_line_x(g[0]) for g in per_sample_lines if g])
    continuation_values = [
        _line_x(li)
        for group in per_sample_lines
        if len(group) > 1
        for li in group[1:]
    ]
    continuation_x = statistics.median(continuation_values) if continuation_values else first_x
    leading = _line_leading(all_lines)

    return BibliographyPattern(
        tag=_normalise_reference_tag(sample_zones[0].tag),
        attributes=dict(sample_zones[0].attributes),
        first_x=first_x,
        continuation_x=continuation_x,
        hanging_indent=abs(continuation_x - first_x) > tol,
        tolerance=tol,
        body_line_height=leading,
        entry_gap_min=leading * ENTRY_GAP_RATIO,
        typical_min_lines=1,
        typical_max_lines=999,
    )


@dataclass
class DetectionReportEntry:
    """spec 52 (debug/logging report) - one row per detected entry,
    whether or not it was actually zoned (a LOW-confidence entry still
    gets a row here, with created=False, so a reviewer can see WHY
    nothing was created there)."""
    page: int
    bbox: tuple
    first_x: float
    continuation_x: float
    lines: int
    confidence: float
    confidence_tier: str          # "HIGH" | "MEDIUM" | "LOW"
    created: bool
    detected_author: str = ""
    detected_year: str = ""


@dataclass
class BibliographyAutoZoneResult:
    predicted_zones: list = field(default_factory=list)
    entries_detected: int = 0       # zones actually created (HIGH + MEDIUM) - unchanged meaning
    high_confidence: int = 0         # unchanged meaning (>= CONFIDENCE_THRESHOLD)
    review_required: int = 0         # unchanged meaning: MEDIUM-confidence zones that WERE created
                                       # (entries_detected - high_confidence), flagged for the user to review
    low_confidence: int = 0           # NEW (spec 34): detected but confidence fell below
                                        # CONFIDENCE_LOW_FLOOR - never zoned at all, reported instead
    pattern_changed: bool = False
    warnings: list = field(default_factory=list)
    report: list = field(default_factory=list)   # [DetectionReportEntry, ...], every entry seen, zoned or not


def _normalise_reference_tag(tag):
    """Preserve the tag selected by the user.

    In particular, ``ref_n`` and ``ref_d`` are NOT interchangeable:
      ref_n -> number/label only
      ref_d -> reference details only

    Legacy spellings are accepted, but they are canonicalised to the
    project's underscore form.
    """
    value = str(tag or "").strip().lower()

    if value in {"refn", "ref_n", "ref-number", "ref_number", "label"}:
        return REFN_TAG
    if value in {"refd", "ref_d"}:
        return REFD_TAG

    # Keep other explicitly selected tags intact.  Only an empty selection
    # falls back to ref_d.
    return value or REFD_TAG

def _extract_reference_number(text):
    """Return (label, remaining_text) for common numbered references.

    Supported forms include ``1.``, ``1)``, ``[1]``, ``[ 1 ]`` and short
    alphanumeric labels such as ``A1.``.  The function deliberately does not
    invent a number for an unnumbered reference.
    """
    text = str(text or "")
    patterns = (
        r"^(\[\s*[A-Za-z]?\d+\s*\])\s*(.*)$",
        r"^([A-Za-z]{1,4}\d+[.)])\s*(.*)$",
        r"^(\d+[.)])\s*(.*)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group(1), match.group(2)
    return "", text.strip()


def _line_words(line):
    """Best-effort access to word/span geometry exposed by PDF line objects."""
    for attr in ("words", "word_items", "spans"):
        value = getattr(line, attr, None)
        if value:
            try:
                return list(value)
            except Exception:
                pass
    return []


def _word_text(item):
    if isinstance(item, dict):
        return str(item.get("text", item.get("string", "")) or "")
    return str(getattr(item, "text", getattr(item, "string", "")) or "")


def _word_bbox(item):
    if isinstance(item, dict):
        bbox = item.get("bbox")
        if bbox is not None:
            return tuple(bbox)
        if all(k in item for k in ("x0", "y0", "x1", "y1")):
            return (
                float(item["x0"]), float(item["y0"]),
                float(item["x1"]), float(item["y1"]),
            )
    bbox = getattr(item, "bbox", None)
    if bbox is not None:
        return tuple(bbox)
    return None


def _reference_number_bbox(line, label):
    """Find the precise bbox of the leading reference number when possible.

    If the PDF line extractor exposes word/span boxes, use them.  Otherwise
    estimate the label width from the line text.  The fallback is deliberately
    conservative and never changes the text or entry grouping.
    """
    bbox = getattr(line, "bbox", (0, 0, 0, 0))
    x0, y0, x1, y1 = map(float, bbox)
    if not label or x1 <= x0:
        return None

    words = _line_words(line)
    if words:
        wanted = re.sub(r"\s+", "", label).lower()
        consumed = ""
        first_box = None
        last_box = None
        for item in words:
            txt = _word_text(item)
            ib = _word_bbox(item)
            if not txt or not ib:
                continue
            compact = re.sub(r"\s+", "", txt).lower()
            if first_box is None and wanted.startswith(compact):
                first_box = ib
                consumed = compact
                last_box = ib
                if consumed == wanted:
                    break
                continue
            if first_box is not None and len(consumed) < len(wanted):
                consumed += compact
                last_box = ib
                if consumed == wanted:
                    break
        if first_box is not None and last_box is not None:
            return (
                float(first_box[0]),
                y0,
                float(last_box[2]),
                y1,
            )

    # Geometry-only fallback.  PDF extraction can expose only a whole-line
    # bbox; estimate the leading label's width from character count.
    full_text = _line_text(line)
    if not full_text:
        return None
    ratio = min(0.35, max(0.02, len(label) / max(1, len(full_text))))
    label_x1 = x0 + (x1 - x0) * ratio
    return (x0, y0, max(x0 + 1.0, label_x1), y1)


def _reference_detail_boxes(entry_lines, label):
    """Return non-overlapping ref_d rectangles for a complete reference.

    A single bounding rectangle around a multi-line reference can overlap
    ref_n because continuation lines may extend farther left than the first
    line's details.  Therefore ref_d is emitted as one fitted rectangle per
    PDF line.  The first line starts immediately after ref_n; continuation
    lines use their own line boxes.
    """
    if not entry_lines:
        return []

    boxes = []
    first = entry_lines[0]
    first_bbox = tuple(map(float, getattr(first, "bbox", (0, 0, 0, 0))))
    number_box = _reference_number_bbox(first, label)

    if number_box:
        x0, y0, x1, y1 = first_bbox
        detail_x0 = max(x0, float(number_box[2]) + 1.0)
        if detail_x0 < x1:
            boxes.append((detail_x0, y0, x1, y1))
    else:
        boxes.append(first_bbox)

    # Every continuation line is its own ref_d box.  This prevents a large
    # union bbox from crossing over the ref_n rectangle on the first line.
    for line in entry_lines[1:]:
        bbox = tuple(map(float, getattr(line, "bbox", (0, 0, 0, 0))))
        if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
            boxes.append(bbox)

    return boxes


def _reference_detail_bbox(entry_lines, label):
    """Backward-compatible union bbox for callers that need one rectangle."""
    boxes = _reference_detail_boxes(entry_lines, label)
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )

def _reference_number_zone(entry_lines, label, confidence, attrs):
    """Build the refn PredictedZone, or None for an unnumbered entry."""
    if not label or not entry_lines:
        return None
    bbox = _reference_number_bbox(entry_lines[0], label)
    if not bbox:
        return None
    number_attrs = dict(attrs)
    number_attrs["source"] = SOURCE_TAG
    number_attrs["reference_role"] = "number"
    return PredictedZone(
        bbox=bbox,
        tag=REFN_TAG,
        confidence=confidence,
        attributes=number_attrs,
    )


def _reference_details_zones(entry_lines, label, confidence, attrs):
    """Build fitted ref_d PredictedZones, one rectangle per text line."""
    boxes = _reference_detail_boxes(entry_lines, label)
    if not boxes:
        return []

    detail_attrs = dict(attrs)
    detail_attrs["source"] = SOURCE_TAG
    detail_attrs["reference_role"] = "details"

    return [
        PredictedZone(
            bbox=box,
            tag=REFD_TAG,
            confidence=confidence,
            attributes=dict(detail_attrs),
        )
        for box in boxes
    ]


def _reference_details_zone(entry_lines, label, confidence, attrs):
    """Backward-compatible single-zone helper."""
    zones = _reference_details_zones(entry_lines, label, confidence, attrs)
    return zones[0] if zones else None

def detect_bibliography_zones(
    pdf_document,
    page_num: int,
    pattern: BibliographyPattern,
    existing_bboxes: list = None,
    lines: list = None,
) -> BibliographyAutoZoneResult:
    """Detect COMPLETE bibliography entries.

    Structural model:

        first/unindented entry line
            -> every following continuation line
            -> next genuine first/unindented entry line

    A continuation line is never made into its own zone just because it has a
    year, a title-like phrase, or a slightly different extracted x position.
    The learned first_x/continuation_x values are document-local and are not
    hard-coded for any PDF.
    """
    result = BibliographyAutoZoneResult()

    if lines is None:
        lines = pdf_block_detector.detect_lines(pdf_document.get_page(page_num))
    if not lines:
        return result

    # Remove page numbers, headers/footers, and bibliography headings first.
    page_width, page_height = pdf_document.page_size(page_num)
    try:
        page_line, header_footer_ids = page_number_detector.detect(
            lines, page_width, page_height
        )
    except Exception:
        page_line, header_footer_ids = None, []

    excluded = set(header_footer_ids or [])
    if page_line is not None:
        excluded.add(id(page_line))

    usable = []
    for li in lines:
        if id(li) in excluded:
            continue

        txt = _line_text(li)
        if not txt:
            continue

        try:
            if content.looks_like_bibliography_heading(txt):
                continue
        except Exception:
            pass

        usable.append(li)

    if not usable:
        return result

    # Do not re-zone lines already covered by an existing zone.
    existing_bboxes = existing_bboxes or []
    if existing_bboxes:
        remaining = []
        for li in usable:
            cx = (li.bbox[0] + li.bbox[2]) / 2.0
            cy = (li.bbox[1] + li.bbox[3]) / 2.0
            point = (cx, cy, cx, cy)

            if not any(
                _bbox_overlaps(point, bbox, tol=0.0)
                for bbox in existing_bboxes
            ):
                remaining.append(li)

        usable = remaining

    if not usable:
        return result

    usable.sort(key=lambda li: (_line_y(li), _line_x(li)))

    tol = max(2.0, float(pattern.tolerance or 5.0))

    # ---------------------------------------------------------------
    # Current-page local line spacing.
    # ---------------------------------------------------------------
    distances = []
    for a, b in zip(usable, usable[1:]):
        distance = _line_y(b) - _line_y(a)
        if distance > 0:
            distances.append(distance)

    local_leading = max(1.0, float(pattern.body_line_height or 10.0))

    if distances:
        median_distance = statistics.median(distances)

        # Large entry gaps should not determine normal line spacing.
        normal_distances = [
            d for d in distances
            if d <= median_distance * 1.35
        ]

        if normal_distances:
            local_leading = max(
                1.0,
                statistics.median(normal_distances),
            )
        else:
            local_leading = max(1.0, median_distance)

    def near_first(line):
        return abs(_line_x(line) - pattern.first_x) <= tol

    def near_continuation(line):
        return (
            pattern.hanging_indent
            and abs(_line_x(line) - pattern.continuation_x) <= tol
        )

    def looks_like_entry_start(line):
        """Content evidence only; never used alone to split a continuation."""
        txt = _line_text(line)

        try:
            if content.looks_like_entry_start(txt):
                return True
        except Exception:
            pass

        # Numbered bibliography/reference entries.
        if re.match(
            r"^\s*(?:\d+|[A-Za-z]{1,4}\d*)[.)]\s+",
            txt,
        ):
            return True

        # Common author/editor form, including accented Latin letters.
        if re.match(
            r"^\s*[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+"
            r"(?:\s*,|\s+and\s+|\s*&\s+)",
            txt,
        ):
            return True

        return False

    def gap_before(index):
        if index <= 0:
            return 0.0
        return max(
            0.0,
            _line_y(usable[index]) - _line_y(usable[index - 1]),
        )

    # ---------------------------------------------------------------
    # Detect START lines.
    #
    # Hanging-indent bibliography:
    #
    #   FIRST_X      -> new entry
    #   CONT_X       -> continuation
    #
    # Therefore CONT_X can NEVER create a new entry by itself.
    # ---------------------------------------------------------------
    starts = []

    for index, line in enumerate(usable):
        first_x = near_first(line)
        cont_x = near_continuation(line)
        content_start = looks_like_entry_start(line)
        gap = gap_before(index)

        if index == 0:
            if first_x or content_start:
                starts.append(index)
            continue

        if pattern.hanging_indent:
            if first_x and not cont_x:
                # A return to the first-line margin is the main new-entry
                # signal. Content corroborates it; a modest local gap can
                # also corroborate it when text recognition is conservative.
                if (
                    content_start
                    or gap >= local_leading * 1.25
                ):
                    starts.append(index)

            elif (
                content_start
                and not cont_x
                and abs(_line_x(line) - pattern.first_x) <= tol * 1.5
                and gap >= local_leading * 1.15
            ):
                # Small extraction drift around first_x.
                starts.append(index)

        else:
            # Flush-left/no-hanging-indent bibliography.
            if first_x and content_start:
                if gap >= local_leading * 1.15:
                    starts.append(index)

            elif (
                content_start
                and gap >= local_leading * 1.35
            ):
                starts.append(index)

    # Conservative geometry fallback if the content helper is unusually weak.
    if not starts and pattern.hanging_indent:
        starts = [
            index
            for index, line in enumerate(usable)
            if near_first(line)
        ]

    starts = sorted(set(starts))

    if not starts:
        return result

    # ---------------------------------------------------------------
    # GROUP COMPLETE ENTRIES.
    #
    # This is the important part:
    #
    #     START -> ALL following lines -> NEXT START
    #
    # We do NOT classify each extracted line as a separate bibliography.
    # ---------------------------------------------------------------
    entries = []

    for position, start_index in enumerate(starts):
        next_index = (
            starts[position + 1]
            if position + 1 < len(starts)
            else len(usable)
        )

        entry_lines = usable[start_index:next_index]

        if not entry_lines:
            continue

        if position > 0:
            first_line = entry_lines[0]
            previous_line = usable[start_index - 1]

            boundary_gap = max(
                0.0,
                _line_y(first_line) - _line_y(previous_line),
            )

            genuine_start = (
                near_first(first_line)
                and not (
                    pattern.hanging_indent
                    and near_continuation(first_line)
                    and not near_first(first_line)
                )
                and (
                    looks_like_entry_start(first_line)
                    or boundary_gap >= local_leading * 1.25
                )
            )

            if not genuine_start:
                # False start: merge the entire candidate back into the
                # preceding bibliography entry.
                if entries:
                    entries[-1].extend(entry_lines)
                else:
                    entries.append(entry_lines)
                continue

        entries.append(entry_lines)

    # ---------------------------------------------------------------
    # Convert each COMPLETE entry into zones using the tag selected in the Tags panel.
    # ---------------------------------------------------------------
    predicted = []
    report_entries = []

    for entry_lines in entries:
        if not entry_lines:
            continue

        bbox = (
            min(line.bbox[0] for line in entry_lines),
            min(line.bbox[1] for line in entry_lines),
            max(line.bbox[2] for line in entry_lines),
            max(line.bbox[3] for line in entry_lines),
        )

        first_text = _line_text(entry_lines[0])
        line_count = len(entry_lines)

        confidence = CONFIDENCE_BASE

        # Geometry confidence.
        x_error = abs(
            _line_x(entry_lines[0]) - pattern.first_x
        )

        if x_error > tol:
            confidence -= min(
                CONFIDENCE_PENALTY,
                (x_error / max(1.0, tol)) * 5.0,
            )

        # Content corroboration affects confidence only. It does NOT split
        # the entry and therefore cannot turn "London, 1965." into a zone.
        if looks_like_entry_start(entry_lines[0]):
            confidence += 5.0
        else:
            confidence -= 5.0

        confidence = max(
            0.0,
            min(100.0, confidence),
        )

        tier = (
            "HIGH"
            if confidence >= CONFIDENCE_THRESHOLD
            else "MEDIUM"
            if confidence >= CONFIDENCE_LOW_FLOOR
            else "LOW"
        )

        created = tier != "LOW"

        report_entries.append(
            DetectionReportEntry(
                page=page_num,
                bbox=bbox,
                first_x=pattern.first_x,
                continuation_x=pattern.continuation_x,
                lines=line_count,
                confidence=confidence,
                confidence_tier=tier,
                created=created,
                detected_author=content.extract_author(first_text),
                detected_year=content.extract_year(first_text),
            )
        )

        if not created:
            continue

        attrs = dict(pattern.attributes)

        # Respect the tag selected in the Tags panel.
        #
        # ref_n -> ONLY the leading reference number/label.
        # ref_d -> ONLY the reference details, fitted line-by-line so they
        #          cannot overlap ref_n.
        # Other tags retain the legacy one-zone-per-entry behaviour.
        label, _detail_text = _extract_reference_number(first_text)

        selected_tag = _normalise_reference_tag(pattern.tag)

        if selected_tag == REFN_TAG:
            number_zone = _reference_number_zone(
                entry_lines, label, confidence, attrs
            )
            if number_zone is not None:
                predicted.append(number_zone)

        elif selected_tag == REFD_TAG:
            predicted.extend(
                _reference_details_zones(
                    entry_lines, label, confidence, attrs
                )
            )

        else:
            # If the user deliberately selected another tag, honour it.
            entry_attrs = dict(attrs)
            entry_attrs["source"] = SOURCE_TAG
            entry_attrs["reference_role"] = "reference"
            predicted.append(
                PredictedZone(
                    bbox=bbox,
                    tag=selected_tag,
                    confidence=confidence,
                    attributes=entry_attrs,
                )
            )

    result.predicted_zones = predicted

    # predicted_zones contains up to two semantic zones per entry, so entry
    # counters must be calculated from report_entries rather than zone count.
    created_entries = [entry for entry in report_entries if entry.created]
    result.entries_detected = len(created_entries)
    result.high_confidence = sum(
        1
        for entry in created_entries
        if entry.confidence >= CONFIDENCE_THRESHOLD
    )
    result.review_required = (
        result.entries_detected - result.high_confidence
    )
    result.low_confidence = sum(
        1
        for entry in report_entries
        if entry.confidence_tier == "LOW"
    )
    result.report = report_entries

    return result

