"""Index (back-of-book) Auto-Zoning engine - detects index entries, their
indentation-derived hierarchy level, and wrapped continuation lines for ONE
PDF page, and proposes IndexPE/IndexSE/IndexTE-equivalent zones for it (spec:
"AUTO ZONE INDEX"). A dedicated, page-scoped detector alongside the existing
auto_zoning package's general Auto Zone/Auto Analyse engines - reuses their
shared primitives where they genuinely fit (auto_zoning.pdf_block_detector.
detect_lines for raw text extraction, core.table_extractor._cluster_1d for
1D coordinate clustering, auto_zoning.hierarchy_builder.PredictedZone/
create_page for actual zone creation) rather than a second, competing
PDF-parsing or zone-creation layer. Column detection is the one exception -
see _detect_index_columns's own docstring for why core.column_detector's
existing general-purpose detector (built for, and correct for, ordinary
paragraph-flow columns) is NOT reused here: it was tried first and
confirmed to misdetect real two-column index layouts specifically because
of their internal indentation hierarchy.

Two-pass design:
  1. Group raw text LINES into logical entries using text-continuity +
     vertical-gap evidence only (never indentation - the "wrapped-line
     protection" the spec insists on: a continuation line is not yet known
     to be one until the entry text itself is examined, and indentation
     clusters can't be computed before entries exist in the first place).
  2. Cluster each logical entry's OWN indentation (relative to its column's
     left edge, never an absolute page X - spec: two-column pages must be
     processed independently) to assign Level 1/2/3/... - smallest cluster
     is always Level 1, regardless of its absolute X value on this
     particular page/PDF (spec: "Do NOT use fixed pixel values").

Returns detection results only - never touches ZoneManager itself (spec:
"Keep the detection engine separate from UI code" / "show a preview before
Apply"). The caller (gui/main_window.py's App.auto_zone_index) is
responsible for actually creating zones via hierarchy_builder.create_page,
and only after the user confirms the preview.
"""
import re
from dataclasses import dataclass, field

from core.table_extractor import _cluster_1d
from core.text_extractor import dehyphenate_join
from auto_zoning import pdf_block_detector
from auto_zoning.hierarchy_builder import PredictedZone

# The level->tag mapping used when the active profile doesn't declare its
# own "index_hierarchy_tags" (see core/profile_manager.py / core/cup_config.
# py) - matches core/epub_xml_generator.py's own EpubXmlGenerator.
# _INDEX_HIERARCHY_LEVELS default exactly (same three tag names both EPUB's
# and CUPEPUB's real tag_buttons already use), so this is a safety-net
# fallback, never the only implementation (spec: "do NOT hard-code Level 1
# = IndexPE... as the only implementation - read the active profile").
DEFAULT_INDEX_HIERARCHY_TAGS = {1: "indexprimary", 2: "indexsecondary", 3: "indexterritory"}

SOURCE_TAG = "auto_index"

# A vertical gap bigger than this many line-heights always starts a new
# entry, regardless of text evidence - protects against greedily merging
# across a genuine paragraph-style gap (e.g. a blank line between
# alphabetical sub-sections). Same ratio auto_zoning.pdf_block_detector
# already uses for its own (unrelated) paragraph-block grouping.
GAP_MAX_RATIO = 1.8
# A logical entry needs at least this many members in its own indentation
# cluster to count as a genuine, distinct level - fewer than this folds
# into the nearest real cluster instead of inventing a spurious deeper
# level from what is more likely an OCR wobble/one-off outlier (spec:
# "OUTLIER HANDLING").
MIN_CLUSTER_MEMBERS = 2

_ENDS_WITH_DIGIT_RE = re.compile(r'\d\s*$')
# A cross-reference entry ("women, see gender" / "education, see also
# universities" - spec: "CROSS-REFERENCE ENTRIES") is a complete, finished
# index entry with NO page number at all - "see"/"see also" is itself the
# terminal content, not a hint that more text (and eventually a page
# number) is still coming. Requires the comma that always precedes it in
# real index style, so an ordinary sentence fragment that merely CONTAINS
# the word "see" (rare in index text, but not impossible) isn't misread as
# a cross-reference terminator.
from core import lang as _lang  # noqa: E402

_CROSS_REF_RE = re.compile(r'[,.]\s*(?:' + "|".join(re.escape(t) for t in _lang.SEE_ALSO + _lang.SEE)
                           + r')(?![^\W\d_])', re.IGNORECASE)
# A trailing page-number/page-range token - "84", "228-9", "60-1, 190",
# "100-1, 190" (spec: "DETECT PAGE NUMBERS" / en dash and hyphen both
# valid range separators, never normalized between the two - spec 7/23).
_PAGE_REF_RE = re.compile(r'\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*\s*$')
# The WHOLE line is nothing BUT a page-number/range token (spec 10: "PAGE
# NUMBER RECOGNITION" - a wrapped continuation line often ends up being
# just the trailing page reference on its own physical line, with the
# entry's actual text on the line above it).
_PAGE_REF_ONLY_RE = re.compile(r'^\s*\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*\s*$')
# A lone alphabetic section heading ("A", "B", "C" - spec: "ALPHABETIC
# INDEX SECTIONS") - a single letter, optionally with trailing punctuation
# a house style might add ("A.", "A -"). Deliberately narrow (exactly one
# letter) so a genuine one-letter index TERM (rare, but not impossible)
# is never accidentally swallowed by this check.
_ALPHA_HEADING_RE = re.compile(r'^[A-Za-z][.\-\s]*$')


def _looks_complete(text: str) -> bool:
    """A genuine, finished index entry almost always ends with a page
    number or page range ("...79-80", "...12, 45") OR is a complete
    cross-reference ("...see also X") - the single most reliable "this
    entry is done" signal available, far more reliable than geometry alone
    (spec's own worked example: "...women lack strategy" has NO trailing
    digit and must NOT be treated as a finished entry). Deliberately the
    ONLY text-evidence signals used (core.paragraph_merge's own analogous
    heuristic for ordinary prose ALSO checks "does the next fragment start
    lowercase" - that signal was tried here too and confirmed WRONG for
    index content specifically: unlike prose, both parent AND child index
    terms are routinely lowercase by house style ("women" / "academic
    careers" - neither is a sentence, so capitalization carries no
    structural meaning), which caused a real false-positive merge of a
    legitimate parentless term into the entry before it)."""
    text = (text or "").strip()
    return bool(_ENDS_WITH_DIGIT_RE.search(text)) or bool(_CROSS_REF_RE.search(text))


def is_alphabetic_section_heading(text: str) -> bool:
    """Spec 11 ("ALPHABETIC INDEX SECTIONS"): a lone "A"/"B"/"C"-style
    section divider is not an index entry at all - including it in
    indentation clustering would corrupt the real levels (a single
    letter's own X position is meaningless), and creating an indexprimary/
    secondary/territory zone for it would misrepresent it as index
    content. No CUPEPUB tag for this concept was found in profiles/
    CUPEPUB/CUPLookup.xml or CUPEPUB_Zoning.xml (confirmed by inspection),
    so - per spec's own fallback ("otherwise preserve it as the
    appropriate heading/tag already defined by the profile") - the
    conservative, correct behavior is to exclude it from auto-zoning
    entirely rather than guess a tag that doesn't exist; the user is left
    to zone it manually with whatever heading tag their profile actually
    offers."""
    return bool(_ALPHA_HEADING_RE.match((text or "").strip()))


# A short, ALL-CAPS running header/title ("INDEX", "GLOSSARY", "APPENDIX A"
# - spec 14: "Do not include: INDEX header, running headers... inside
# index entry zones") sitting in the page's own top margin. Deliberately
# NOT specific to the literal word "INDEX" (spec 24: never hard-code the
# example content) - any short all-caps line up there is treated the same
# way, since that combination (short + all-caps + top-margin) is what
# actually identifies a running page header structurally, regardless of
# which specific word a given book happens to print there.
_RUNNING_HEADER_RE = re.compile(r'^[A-Z][A-Z0-9\s.\-]{0,24}$')
_HEADER_MARGIN_FRACTION = 0.08


def is_running_header(text: str, y0: float, page_height: float) -> bool:
    text = (text or "").strip()
    if not text or not _RUNNING_HEADER_RE.match(text) or page_height <= 0:
        return False
    return y0 <= page_height * _HEADER_MARGIN_FRACTION


def has_page_reference(text: str) -> bool:
    return bool(_PAGE_REF_RE.search((text or "").strip()))


def is_cross_reference(text: str) -> bool:
    return bool(_CROSS_REF_RE.search((text or "").strip()))


def is_page_number_only(text: str) -> bool:
    """Spec 10: "Page-number-only lines normally belong to the preceding
    logical entry. Do not create separate zones for page-number-only
    continuation lines." A bare page number/range is essentially NEVER a
    genuine standalone index entry by itself - used in
    _group_lines_into_entries to force continuation UNCONDITIONALLY
    (skipping the normal level-based child/continuation decision
    entirely), since such a line's own indentation is not a reliable
    signal here: many books hang-indent a wrapped page number at the SAME
    position used elsewhere for a genuine deeper entry level, which would
    otherwise make it look exactly like a real Level-2/3 child."""
    return bool(_PAGE_REF_ONLY_RE.match((text or "").strip()))


@dataclass
class LogicalEntry:
    lines: list                # pdf_block_detector.LineInfo, in order
    bbox: tuple
    text: str
    x0: float                  # the entry's OWN indentation anchor - its first line's x0, never a later wrapped line's
    wrapped: bool = False       # True once a second (or later) line has been folded in
    level: int = 1
    outlier: bool = False       # True if its indentation cluster was too weak to trust (see MIN_CLUSTER_MEMBERS)


def _union_bbox(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _join_text(a: str, b: str) -> str:
    """Spec 11 ("HYPHENATION"): reuses core.text_extractor.dehyphenate_join
    - the SAME single source of truth every other zone-text-joining path in
    this app already uses (Merge Previous, Title zones, ordinary paragraph
    extraction) - rather than a second, independently-written hyphen rule.
    A genuine PDF line-break hyphen ("decision-" + "making 229") collapses
    directly to "decision-making 229"; anything else (including a real
    same-line compound hyphen, which dehyphenate_join's own boundary check
    never touches - see _is_linebreak_hyphen_boundary) gets a plain space,
    with every other character - Unicode, en dash, em dash, accents,
    quotes - passed through completely unaltered (spec 12)."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    return dehyphenate_join([a, b])


def _indentation_tolerance(body_font_size: float) -> float:
    """Adaptive clustering tolerance (spec 16): a fixed floor (matching
    core.column_detector's own 6.0pt convention for the identical "is this
    really a different column/indent, or just coordinate noise" question),
    scaled up slightly for a larger body font so proportionally larger
    inter-level indentation still clusters correctly - never a bare fixed
    pixel constant on its own."""
    return max(6.0, 0.35 * (body_font_size or 0.0))


def _cluster_line_levels(lines: list, column_left: float, tolerance: float) -> tuple:
    """Spec 3's own literal algorithm ("INDENTATION IS THE PRIMARY LEVEL
    DETECTOR"): clusters every LINE's own relative_x = x0 - column_left
    (spec 5 - never an absolute page X) directly - not just the first line
    of an already-known entry, since entry boundaries aren't known yet at
    this point (this function runs BEFORE _group_lines_into_entries, which
    consumes its output). Smallest cluster = Level 1, next = Level 2, and
    so on, for however many real clusters are found (spec: "Support any
    number of levels"). A cluster with too few members (spec 17: "OUTLIER
    HANDLING") is folded into its nearest real neighbor rather than
    manufacturing a spurious extra level from a likely OCR wobble/one-off -
    but is still reported back as an outlier, since _group_lines_into_
    entries treats "lands on a real, recurring level" very differently from
    "lands on an isolated, unique-to-this-line position" when deciding
    wrapped-continuation vs. genuine-child (see that function's own
    docstring).

    Page-number-only lines (spec 10) are EXCLUDED from deciding which
    clusters count as "real" (though every line, including these, still
    gets a level looked up, for dict completeness) - confirmed real bug
    otherwise: a wrapped page number is routinely hang-indented at the
    SAME position used elsewhere for a genuine deeper level (e.g. many
    entries wrap their trailing page number to one consistent indent),
    which would make that position look like a strong, recurring level
    purely from page-number wobble, wrongly preventing an unrelated
    ordinary wrapped line that happens to share that exact indent from
    being recognized as a continuation of ITS OWN entry - page-number-only
    lines already merge unconditionally regardless of the level assigned
    to them here (see _group_lines_into_entries), so their own indentation
    contributing to what counts as "real" for OTHER lines was never
    meaningful signal, only noise. Returns ({id(line): level},
    {id(line): is_outlier})."""
    if not lines:
        return {}, {}
    clusterable = [li for li in lines if not is_page_number_only(li.text)]
    rel_xs = [li.bbox[0] - column_left for li in clusterable]
    centers = _cluster_1d(rel_xs, tolerance=tolerance) if rel_xs else []
    counts = {c: sum(1 for x in rel_xs if abs(x - c) <= tolerance) for c in centers}
    strong = sorted(c for c in centers if counts[c] >= MIN_CLUSTER_MEMBERS)
    if not strong:
        strong = sorted(centers)  # every cluster is small (a short index column) - trust them all rather than collapse to nothing
    rank = {c: i + 1 for i, c in enumerate(strong)}
    level_by_id, outlier_by_id = {}, {}
    for li in lines:
        if not centers:
            level_by_id[id(li)] = 1
            outlier_by_id[id(li)] = True
            continue
        x = li.bbox[0] - column_left
        nearest = min(centers, key=lambda c: abs(c - x))
        is_outlier = nearest not in rank
        # An outlier (weak/singleton cluster) is folded into whichever REAL
        # level cluster is geometrically nearest to it, never invents a new
        # level of its own.
        target = nearest if nearest in rank else min(strong, key=lambda c: abs(c - nearest))
        level_by_id[id(li)] = rank[target]
        outlier_by_id[id(li)] = is_outlier
    return level_by_id, outlier_by_id


def _group_lines_into_entries(lines: list, line_level: dict, line_outlier: dict) -> list:
    """Pass 2 (spec 6/7): builds LogicalEntry objects from raw same-column,
    top-to-bottom LineInfo objects, using the PER-LINE indentation level
    computed by _cluster_line_levels as the primary signal (spec 3), with
    text-continuity/vertical-gap evidence resolving only the genuinely
    ambiguous cases:

    - A line at a level SHALLOWER than the current entry's own level is
      NEVER that entry's continuation - it is always at least a sibling
      (or higher), full stop (spec 8's hierarchy only ever nests DEEPER,
      never re-attaches something already proven shallower).
    - A line at the SAME level as the current entry could be a genuine new
      sibling entry, or a "flush" wrap with no extra indent at all - text
      evidence (does the current entry's text look finished? does this
      line start lowercase?) decides.
    - A line DEEPER than the current entry is the one truly hard case the
      spec itself calls out (Part 7's "women -> academic careers" MUST
      nest, but "admissions procedure... -> to deal with 79-80" MUST NOT):
      if that deeper indentation is a REAL, recurring level elsewhere on
      this column (not an outlier - see _cluster_line_levels), it is
      trusted as a genuine child EVEN IF the current entry's text doesn't
      "look finished" (a parent term like "women" legitimately has no page
      number of its own - that alone must never make its first real child
      look like a continuation). Only when the deeper indentation is an
      OUTLIER (an isolated, one-off position that doesn't recur anywhere
      else as a real level) does text evidence get a vote - exactly the
      shape of a genuine wrapped continuation's own ad-hoc hanging indent,
      as opposed to a deliberately chosen, consistently-reused sub-entry
      indentation."""
    if not lines:
        return []
    heights = [li.bbox[3] - li.bbox[1] for li in lines if li.bbox[3] > li.bbox[1]]
    median_height = sorted(heights)[len(heights) // 2] if heights else 10.0

    first = lines[0]
    entries = [LogicalEntry(lines=[first], bbox=first.bbox, text=first.text, x0=first.bbox[0],
                             level=line_level[id(first)], outlier=line_outlier[id(first)])]
    for li in lines[1:]:
        current = entries[-1]
        prev_line = current.lines[-1]
        gap = li.bbox[1] - prev_line.bbox[3]
        large_gap = median_height > 0 and gap > median_height * GAP_MAX_RATIO
        lvl = line_level[id(li)]

        is_continuation = False
        if not large_gap:
            if is_page_number_only(li.text):
                # Spec 10 - a bare page number folds into the preceding
                # entry UNCONDITIONALLY (skips the level-based decision
                # entirely, checked first): its own indentation is not a
                # trustworthy signal, since a wrapped page number is
                # routinely hang-indented at the exact same position a
                # genuine deeper entry level uses elsewhere on the page.
                is_continuation = True
            elif lvl == current.level or (lvl > current.level and line_outlier[id(li)]):
                is_continuation = not _looks_complete(current.text)
            # lvl < current.level, or a REAL (non-outlier) deeper level:
            # never a continuation - see docstring.

        if is_continuation:
            current.lines.append(li)
            current.text = _join_text(current.text, li.text)
            current.bbox = _union_bbox(current.bbox, li.bbox)
            current.wrapped = True
            # current.level/.outlier stay as the ENTRY's own (first line's)
            # - a wrapped continuation's own indentation never redefines
            # its entry's level.
        else:
            entries.append(LogicalEntry(lines=[li], bbox=li.bbox, text=li.text, x0=li.bbox[0],
                                         level=lvl, outlier=line_outlier[id(li)]))
    return entries


def _build_predicted_tree(entries: list, tag_for_level: dict) -> list:
    """Spec 8 ("HIERARCHY"): walks entries in column (top-to-bottom) order
    and, for each one, finds the NEAREST PREVIOUS entry with a LOWER level
    as its parent (a plain outline stack - identical in spirit to core.
    epub_xml_generator.EpubXmlGenerator._build_index_hierarchy's own
    generation-time reconstruction, so both places agree on the same
    "nearest previous entry with a lower level" rule). Builds real nested
    PredictedZone.children (spec: "create parent relationships"), which
    auto_zoning.hierarchy_builder.create_page then turns into real zone_
    manager parent_id links - reading order (spec 9) is a SEPARATE concern,
    decided later by the caller from column-major position, never from this
    level nesting."""
    deepest_tag = tag_for_level[max(tag_for_level)]
    top_level = []
    stack = []  # [(level, PredictedZone)], innermost/most-recent last
    for entry in entries:
        tag = tag_for_level.get(entry.level, deepest_tag)
        confidence = 96.0 if not entry.wrapped else 90.0
        if entry.outlier:
            confidence -= 15.0
        pz = PredictedZone(
            bbox=entry.bbox, tag=tag, confidence=confidence, text=entry.text,
            attributes={
                "source": SOURCE_TAG,
                "index_level": entry.level,
                "index_wrapped": entry.wrapped,
            })
        while stack and stack[-1][0] >= entry.level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(pz)
        else:
            top_level.append(pz)
        stack.append((entry.level, pz))
    return top_level


@dataclass
class IndexAutoZoneResult:
    predicted_zones: list = field(default_factory=list)     # top-level PredictedZone list, column-major order
    columns_detected: int = 0
    entries_by_level: dict = field(default_factory=dict)     # {level: count}
    wrapped_count: int = 0
    outlier_count: int = 0
    page_reference_count: int = 0
    cross_reference_count: int = 0
    alphabetic_headings_skipped: int = 0
    page_number_only_lines_excluded: int = 0
    avg_confidence: float = 0.0
    indentation_by_level: dict = field(default_factory=dict)  # {level: approx PAGE-absolute x0, first column}
    warnings: list = field(default_factory=list)

    @property
    def total_entries(self) -> int:
        return sum(self.entries_by_level.values())


def _all_predicted(zones: list):
    for pz in zones:
        yield pz
        yield from _all_predicted(pz.children)


# A genuine column gutter in a two-up (or more) index layout is typically
# hundreds of points wide - far bigger than an indentation step between
# hierarchy levels (tens of points). core.column_detector's own general
# column detector (built for, and correct for, ordinary paragraph-flow
# columns) validates a "real gutter" by checking each candidate column's
# own average text RIGHT edge against the next column's left edge - which
# fails specifically for index content: a Level-1 entry's own text
# routinely extends past the X position where Level 2/3 entries start
# indenting from, and whichever level happens to have the most entries can
# just as easily be Level 2 as Level 1 (confirmed directly: an index
# column with more Level-2 than Level-1 entries makes that detector
# collapse the whole page to a single column, completely missing a real
# two-column layout).
#
# A previous version of this function used a single fixed absolute-point
# tolerance (150.0) to greedily chain every line's raw x0 into groups.
# Reproduced directly (spec: "reproduce first"): single-linkage chaining
# on RAW, unfiltered x0 values is bridgeable by exactly one line - a real,
# ordinary deep index sub-entry (e.g. a Level-3 term indented well into a
# narrow column) can sit close enough to BOTH the end of column 1's own
# indentation spread and the start of column 2 to chain them into one
# group, collapsing a genuine two-column page to one. It was also a bare
# page-space constant, never adapting to page width or font size.
#
# Fixed with a two-stage, self-calibrating approach:
#   1. Cluster x0 values the SAME way indentation levels already are
#      (_cluster_1d at this page's own adaptive tolerance), and keep only
#      clusters with real, recurring support (>= MIN_CLUSTER_MEMBERS
#      lines) - a one-off deep indentation never becomes its own anchor,
#      so it can never bridge two real columns the way a raw single-
#      linkage chain could.
#   2. A column boundary exists only at a gap between two consecutive
#      REAL anchors that is both (a) the dominant gap on the page -
#      clearly wider than ordinary indentation steps elsewhere, not just
#      larger than zero - and (b) large in absolute terms relative to
#      this page's own body font size and page width, never a fixed
#      pixel constant. A page with only ordinary indentation-scale gaps
#      between its anchors never has one that clears both bars, so a
#      single, deeply-indented column is never falsely split.
MIN_GUTTER_WIDTH_FRACTION = 0.03  # a real gutter is a meaningful fraction of the page itself
MIN_GUTTER_FONT_MULTIPLE = 4.0    # ...and categorically wider than a font-sized indentation step
GUTTER_DOMINANCE_RATIO = 3.0      # ...and clearly the odd one out among this page's own gaps


def _detect_index_columns(lines: list, page_width: float, body_font_size: float = 0.0) -> list:
    """Returns [(col_left, col_right), ...], left to right, spanning
    [0, page_width] with no gaps - one tuple for a single-column page.
    See the module-level comment above for the algorithm and why the
    previous single-linkage-chain approach was replaced."""
    if not lines:
        return [(0.0, page_width)]
    tol = _indentation_tolerance(body_font_size)
    x0s = sorted(li.bbox[0] for li in lines)
    anchors = _cluster_1d(x0s, tolerance=tol)
    real_anchors = sorted(
        a for a in anchors
        if sum(1 for x in x0s if abs(x - a) <= tol) >= MIN_CLUSTER_MEMBERS
    )
    if len(real_anchors) < 2:
        return [(0.0, page_width)]

    gaps = [real_anchors[i + 1] - real_anchors[i] for i in range(len(real_anchors) - 1)]
    min_gutter = max(page_width * MIN_GUTTER_WIDTH_FRACTION, body_font_size * MIN_GUTTER_FONT_MULTIPLE)
    median_gap = sorted(gaps)[len(gaps) // 2]
    split_after = [
        i for i, g in enumerate(gaps)
        if g >= min_gutter and (len(gaps) == 1 or g >= median_gap * GUTTER_DOMINANCE_RATIO)
    ]
    if not split_after:
        return [(0.0, page_width)]

    groups, current = [], [real_anchors[0]]
    for i, a in enumerate(real_anchors[1:]):
        if i in split_after:
            groups.append(current)
            current = [a]
        else:
            current.append(a)
    groups.append(current)

    bounds = []
    for i, g in enumerate(groups):
        left = 0.0 if i == 0 else (max(groups[i - 1]) + min(g)) / 2.0
        right = page_width if i == len(groups) - 1 else (max(g) + min(groups[i + 1])) / 2.0
        bounds.append((left, right))
    return bounds


def _column_index_for_x0(bounds: list, x0: float) -> int:
    for i, (left, right) in enumerate(bounds):
        if left <= x0 < right:
            return i
    return len(bounds) - 1  # right of the last boundary (shouldn't normally happen) - clamp rather than crash


def detect_index_zones(pdf_document, page_num: int, index_hierarchy_tags: dict = None,
                        tolerance: float = None, lines: list = None) -> IndexAutoZoneResult:
    """THE detection entry point (spec 2's pipeline, steps 1-9 - stops
    short of actual zone creation, see module docstring). index_hierarchy_
    tags: {level:int -> tag:str}, normally profile.get("index_hierarchy_
    tags") (spec 4 - read from the active profile, never hardcoded as the
    only implementation); falls back to DEFAULT_INDEX_HIERARCHY_TAGS when
    the active profile doesn't declare one. `tolerance`: an explicit
    override (spec 20's "Indentation tolerance: Auto / Custom" setting);
    None (the normal case) computes an adaptive one from this page's own
    body font size (spec 16). `lines`: an explicit pre-extracted line list
    (spec: "OCR COMPATIBILITY" - the caller has already decided digital vs.
    OCR-derived text for this page, e.g. via core.ocr.ocr_service.
    _ocr_blocks_to_lines on a cached OCRResult - see gui/main_window.py's
    App.auto_zone_index); None (the normal case) extracts from the
    document's own digital text layer, exactly as before this parameter
    existed."""
    tag_for_level = {int(k): v for k, v in (index_hierarchy_tags or DEFAULT_INDEX_HIERARCHY_TAGS).items()} \
        or DEFAULT_INDEX_HIERARCHY_TAGS
    result = IndexAutoZoneResult()

    page_width, page_height = pdf_document.page_size(page_num)
    if lines is None:
        page = pdf_document.get_page(page_num)
        lines = pdf_block_detector.detect_lines(page)
    if not lines:
        result.warnings.append("No index entries could be detected on this page.")
        return result

    # Alphabetic section dividers ("A"/"B"/"C" - spec 11) and running
    # headers ("INDEX" - spec 14) are excluded BEFORE column/indentation
    # detection - neither is genuine index-entry geometry, and including
    # either would corrupt the real indentation clusters (spec 29 -
    # geometry-first only works if the geometry fed into it is trustworthy).
    excluded_lines = [li for li in lines if is_alphabetic_section_heading(li.text)
                       or is_running_header(li.text, li.bbox[1], page_height)]
    lines = [li for li in lines if li not in excluded_lines]
    result.alphabetic_headings_skipped = len(excluded_lines)
    if not lines:
        result.warnings.append("No index entries could be detected on this page.")
        return result

    body_size = pdf_block_detector.page_body_font_size(lines)
    tol = tolerance if tolerance is not None else _indentation_tolerance(body_size)

    column_bounds = _detect_index_columns(lines, page_width, body_size)
    if not column_bounds:
        result.warnings.append("Unable to reliably detect index columns.")
        return result
    result.columns_detected = len(column_bounds)

    columns_lines = [[] for _ in column_bounds]
    for li in lines:
        columns_lines[_column_index_for_x0(column_bounds, li.bbox[0])].append(li)

    top_level_all = []
    first_col_indent_x0 = {}   # level -> page-absolute x0, first column only (spec 13's preview table)
    for col_idx, (col_left, _col_right) in enumerate(column_bounds):
        col_lines = sorted(columns_lines[col_idx], key=lambda li: (li.bbox[1], li.bbox[0]))
        if not col_lines:
            continue
        line_level, line_outlier = _cluster_line_levels(col_lines, col_left, tol)
        entries = _group_lines_into_entries(col_lines, line_level, line_outlier)
        # Spec 10/28: a logical entry that is STILL nothing but a bare page
        # number after continuation-merging has already had every chance
        # to attach to a preceding entry - a well-formed index entry is
        # never JUST a page number, so one that survives to here is
        # orphaned noise (a running page-number box at the very top of a
        # column, or similar), never real content (spec: "no page-number-
        # only zones").
        orphaned = [e for e in entries if is_page_number_only(e.text)]
        if orphaned:
            entries = [e for e in entries if e not in orphaned]
            result.page_number_only_lines_excluded += len(orphaned)
        tree = _build_predicted_tree(entries, tag_for_level)
        top_level_all.extend(tree)  # column-major: this whole column's tree before the next column's (spec 9)

        for e in entries:
            result.entries_by_level[e.level] = result.entries_by_level.get(e.level, 0) + 1
            if e.wrapped:
                result.wrapped_count += 1
            if e.outlier:
                result.outlier_count += 1
            if is_cross_reference(e.text):
                result.cross_reference_count += 1
            elif has_page_reference(e.text):
                result.page_reference_count += 1
            if e.level not in first_col_indent_x0:
                first_col_indent_x0[e.level] = e.x0

    if not top_level_all:
        result.warnings.append("No index entries could be detected on this page.")
        return result

    result.predicted_zones = top_level_all
    result.indentation_by_level = dict(sorted(first_col_indent_x0.items()))
    all_conf = [pz.confidence for pz in _all_predicted(top_level_all)]
    result.avg_confidence = sum(all_conf) / len(all_conf) if all_conf else 0.0
    return result


_INDEX_TITLE_RE = re.compile(r'^\s*index\s*$', re.IGNORECASE)


@dataclass
class IndexPageClassification:
    confidence: str   # "high" | "medium" | "low"
    reason: str


def classify_index_page(pdf_document, page_num: int, index_hierarchy_tags: dict = None,
                         zone_manager=None, lines: list = None) -> IndexPageClassification:
    """Spec 2 ("DETECT INDEX PAGE"): a lightweight, page-scoped signal for
    "does this look like an index page" - used to decide whether to
    surface Auto Zone Index prominently (spec: "If automatic page
    classification identifies Index, show the Auto Zone Index action
    prominently"), NEVER to auto-run anything - explicit user action (the
    button/menu click) is always required regardless of this result (spec:
    "Allow Auto Zone -> Index to explicitly force the operation... Do not
    prevent manual execution because page classification has low
    confidence").

    Signals, highest confidence first:
      - the page already has real zones tagged with the active profile's
        own index-hierarchy tags (spec: "existing zone tags include
        IndexPE/SE/TE") - the strongest possible signal, no guessing needed.
      - a short, standalone "Index" heading line near the top of the page.
      - multi-column layout combined with a high proportion of short lines
        that look like finished index entries (page-number/cross-reference
        terminated) - real paragraph text essentially never looks like
        this at scale.
    zone_manager is optional (None skips the first, strongest check - e.g.
    when called before a project/page has any zones yet)."""
    tags = set((index_hierarchy_tags or DEFAULT_INDEX_HIERARCHY_TAGS).values())
    if zone_manager is not None:
        existing = zone_manager.zones_on_page(page_num) if hasattr(zone_manager, "zones_on_page") else []
        if any(z.tag in tags for z in existing):
            return IndexPageClassification("high", "page already contains Index zones")

    if lines is None:
        lines = pdf_block_detector.detect_lines(pdf_document.get_page(page_num))
    if not lines:
        return IndexPageClassification("low", "no text detected on this page")

    for li in lines[:5]:
        if _INDEX_TITLE_RE.match(li.text) or _lang.is_heading(li.text, "index"):
            return IndexPageClassification("high", "page heading reads 'Index'")

    page_width, _page_height = pdf_document.page_size(page_num)
    column_bounds = _detect_index_columns(lines, page_width, pdf_block_detector.page_body_font_size(lines))
    multi_column = len(column_bounds) >= 2
    finished = sum(1 for li in lines if len(li.text) < 80 and
                   (has_page_reference(li.text) or is_cross_reference(li.text)))
    density = finished / len(lines) if lines else 0.0
    if multi_column and density >= 0.4:
        return IndexPageClassification(
            "medium", f"multi-column layout with {density:.0%} short, page-referenced lines")
    if density >= 0.6:
        return IndexPageClassification("medium", f"{density:.0%} of lines look like finished index entries")
    return IndexPageClassification("low", "no strong index signals detected")


def _bbox_overlaps(a, b, tol: float = 1.0) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 + tol or bx1 <= ax0 + tol or ay1 <= by0 + tol or by1 <= ay0 + tol)


def _count_all(zones: list) -> int:
    return sum(1 + _count_all(pz.children) for pz in zones)


def prune_protected_zones(predicted_zones: list, protected_bboxes: list) -> tuple:
    """Spec 11 ("MANUAL ZONE PROTECTION"): removes any PredictedZone (and,
    recursively, its whole subtree, since an orphaned child with no parent
    makes no sense to keep on its own) whose bbox overlaps one of
    `protected_bboxes` - the caller's own already-computed set of manually-
    created/corrected zone bboxes on this page (see gui/main_window.py's
    App.auto_zone_index). A zone that does NOT overlap anything protected
    is kept even if a SIBLING elsewhere in the tree was dropped - this is
    partial, per-entry protection, never an all-or-nothing page-level
    refusal. Returns (kept_top_level_zones, removed_entry_count) - never
    mutates `predicted_zones` itself (returns a new list; children lists
    ARE reassigned on the kept PredictedZone objects, the only mutation)."""
    kept, removed = [], 0
    for pz in predicted_zones:
        if any(_bbox_overlaps(pz.bbox, b) for b in protected_bboxes):
            removed += 1 + _count_all(pz.children)
            continue
        pz.children, child_removed = prune_protected_zones(pz.children, protected_bboxes)
        removed += child_removed
        kept.append(pz)
    return kept, removed
