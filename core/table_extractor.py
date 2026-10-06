"""Detects table rows/columns/cells from PDF LAYOUT (word positions, gaps,
ruling lines - never DTD/BITS rules, which only validate the XML that this
module's output is later turned into) within a Table zone's bbox.

Reuses the existing project pipeline wherever possible instead of building a
second one: PyMuPDF (core.pdf_loader's own library) for word-level geometry,
and core.text_extractor.extract_formatted_text for actual cell TEXT content
- which means bold/italic detection, superscript/subscript detection, and
line-break-hyphen normalization are all reused completely unchanged, not
reimplemented here. This module only ever determines WHERE the rows/columns/
cells are; core/xml_generator.py turns that geometry into BITS/JATS XML
using its own existing ID/element-building conventions.
"""
import re

import fitz

from core import text_extractor, debug_log, image_extractor
from core.constants import (
    TABLE_COLUMN_X_TOLERANCE, TABLE_ROW_Y_TOLERANCE,
    TABLE_CELL_COORD_TOLERANCE, TABLE_HEADER_BOLD_RATIO,
    TABLE_COLUMN_GAP_THRESHOLD, TABLE_COLUMN_MIN_OCCURRENCES,
    TABLE_EXTRACTION_BOUNDARY_TOLERANCE,
    TABLE_LIST_BULLET_CHARS, TABLE_LIST_MIN_ITEMS, TABLE_LIST_INDENT_TOLERANCE,
)
from core.formatting_detector import detect_bold_italic

_TABLE_BULLET_LINE_RE = re.compile(rf"^\s*[{re.escape(TABLE_LIST_BULLET_CHARS)}]\s*")


def _detect_bullet_list_items(page, cell_bbox, keep_hyphen_at=None):
    """Returns a list of per-item formatted text strings, or None - see
    _bullet_items_from_lines for the full evidence contract. This is the
    ordinary (non-rotated) entry point: gets the cell's own lines via the
    unmodified text_extractor.extract_lines (page-space order, exactly
    as every other zone type already uses it) and hands them to the
    shared detector. A rotated table's cells instead call
    _bullet_items_from_lines directly with lines already re-ordered into
    normalized reading order (see _extract_cell_lines_ordered) - the
    detection RULES are identical either way, only how the lines were
    obtained differs."""
    lines = text_extractor.extract_lines(page, cell_bbox)
    return _bullet_items_from_lines(lines, keep_hyphen_at)


def _bullet_items_from_lines(lines, keep_hyphen_at=None):
    """Returns a list of per-item formatted text strings (bullet marker
    stripped, wrapped/continuation lines joined via the existing
    dehyphenate_join - so hyphenation/formatting are reused exactly like
    ordinary cell text) if `lines` ([(bbox, formatted_text), ...], already
    in correct reading order) shows real layout evidence of a bullet
    list, or None otherwise, in which case the caller falls back to
    ordinary paragraph-style cell extraction.

    Deliberately conservative (spec: "Do NOT automatically convert
    ordinary multi-line table text into <list>") - ALL of the following
    must hold, not just a lone leading dash on one line:
      - at least TABLE_LIST_MIN_ITEMS physical lines start with one of
        the recognized bullet characters (TABLE_LIST_BULLET_CHARS - a
        narrower, more conservative set than the existing standalone-
        List-zone marker patterns, since this function is what decides
        whether something is a list at all, not just how to strip an
        already-user-confirmed one);
      - those marker lines all start at very nearly the same leading
        position (TABLE_LIST_INDENT_TOLERANCE) - real list items align
        left (or, for a rotated table, along whatever axis `bbox[0]`
        represents in the caller's own coordinate space - see
        _extract_cell_lines_ordered);
      - the CELL'S OWN FIRST line is itself a marker line, so a cell
        that starts with ordinary lead-in prose before some bulleted
        content is never misread as starting a list on line 0.
    A non-marker line between two marker lines is treated as a WRAPPED
    CONTINUATION of the immediately preceding item (spec: "visual PDF
    line wrapping must NOT create a new list-item"), never a new item on
    its own."""
    if len(lines) < TABLE_LIST_MIN_ITEMS:
        return None

    marker_idxs = []
    marker_x0s = []
    for i, (line_bbox, text) in enumerate(lines):
        plain = text_extractor.strip_tags_to_plain(text)
        if _TABLE_BULLET_LINE_RE.match(plain):
            marker_idxs.append(i)
            marker_x0s.append(line_bbox[0])

    if len(marker_idxs) < TABLE_LIST_MIN_ITEMS:
        return None
    if marker_idxs[0] != 0:
        return None
    if max(marker_x0s) - min(marker_x0s) > TABLE_LIST_INDENT_TOLERANCE:
        return None

    item_line_groups = []
    for i, (_, text) in enumerate(lines):
        if i in marker_idxs:
            item_line_groups.append([text])
        else:
            item_line_groups[-1].append(text)

    items = []
    for parts in item_line_groups:
        first = _TABLE_BULLET_LINE_RE.sub("", parts[0], count=1)
        items.append(text_extractor.dehyphenate_join([first] + parts[1:], keep_hyphen_at))
    return items


def _get_words_in_bbox(page, bbox):
    """Word-level (x0, y0, x1, y1, text) geometry for exactly the words
    PyMuPDF's own tokenizer finds within bbox (page.get_text("words",
    clip=...) - the project's existing PDF library, not a second/competing
    text-extraction pipeline). Used ONLY for table GEOMETRY (row/column/
    cell boundaries) - actual cell text content always goes through
    text_extractor.extract_formatted_text once a cell's bbox is known, so
    formatting/hyphenation/sup-sub detection is reused, never duplicated."""
    words = page.get_text("words", clip=fitz.Rect(*bbox))
    out = [{"bbox": (w[0], w[1], w[2], w[3]), "text": w[4]} for w in words]
    return _attach_bullets(out)


_BULLET_ONLY = set("•●▪◦■□◆♦‣⁃–-")


def _attach_bullets(words):
    """A list bullet set apart by a tab ("•<tab>text") is a separate word
    with a wide gap after it - for column detection it would become a
    column of bullets beside a column of text. Geometrically it belongs to
    the word it introduces, so the two are measured as one."""
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        if w["text"] and all(c in _BULLET_ONLY for c in w["text"]):
            x0, y0, x1, y1 = w["bbox"]
            nxt = min((v for v in words if v is not w and abs(v["bbox"][1] - y0) < 2.5
                       and 0 <= v["bbox"][0] - x1 < 25), key=lambda v: v["bbox"][0], default=None)
            if nxt is not None:
                nb = nxt["bbox"]
                nxt["bbox"] = (min(x0, nb[0]), min(y0, nb[1]), max(x1, nb[2]), max(y1, nb[3]))
                nxt["text"] = w["text"] + " " + nxt["text"]
                i += 1
                continue
        out.append(w)
        i += 1
    return out


# ---------- rotated table support ----------
# (R, D) orthonormal basis per rotation key, R = the content's own
# "reading" direction expressed in PAGE space, D = its own "next line"
# direction (R rotated 90 CW as viewed on screen, matching normal
# top-to-bottom line flow within the content's own frame). R here is
# exactly image_extractor's own verified _DIR_TO_ROTATION_KEY vectors
# (the same PDF text "dir" convention already relied on for Figure
# rotation detection), just keyed the other way around (key -> vector
# instead of vector -> key) - not a second, independently-guessed
# convention.
_ROTATION_BASIS = {
    0: ((1.0, 0.0), (0.0, 1.0)),
    90: ((0.0, -1.0), (1.0, 0.0)),
    180: ((-1.0, 0.0), (0.0, -1.0)),
    270: ((0.0, 1.0), (-1.0, 0.0)),
}


def _normalize_point(px, py, origin, basis):
    R, D = basis
    dx, dy = px - origin[0], py - origin[1]
    return dx * R[0] + dy * R[1], dx * D[0] + dy * D[1]


def _denormalize_point(nx, ny, origin, basis):
    R, D = basis
    return origin[0] + nx * R[0] + ny * D[0], origin[1] + nx * R[1] + ny * D[1]


def _normalize_bbox(bbox, origin, basis):
    """Transforms a PAGE-space axis-aligned bbox into the NORMALIZED
    (upright-reading) frame defined by basis/origin - the 4 corners are
    transformed individually and re-bounded, since a page-space
    axis-aligned rect only stays axis-aligned after an exact 90-degree-
    multiple rotation, which is the only kind this module ever detects
    or acts on."""
    x0, y0, x1, y1 = bbox
    corners = [_normalize_point(x, y, origin, basis) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _denormalize_bbox(bbox, origin, basis):
    """Inverse of _normalize_bbox - converts a NORMALIZED bbox back to
    real PAGE-space coordinates, the only space text_extractor's
    functions (and the rawdict character bboxes they filter against)
    understand."""
    nx0, ny0, nx1, ny1 = bbox
    corners = [_denormalize_point(nx, ny, origin, basis) for nx, ny in ((nx0, ny0), (nx1, ny0), (nx0, ny1), (nx1, ny1))]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _extract_cell_lines_ordered(page, cell_bbox_page, origin, basis):
    """Returns [(normalized_line_bbox, formatted_text), ...] for
    cell_bbox_page (a PAGE-space bbox), in correct READING order for a
    table rotated per `basis` - reuses text_extractor.extract_lines
    completely unchanged (so bold/italic/sup/sub detection and per-
    character geometry are exactly what every other zone type already
    gets) for the actual line extraction, but does NOT trust its
    returned order: extract_lines sorts by raw PAGE-space Y, which is
    only "reading order" for upright (0-degree) text - for a rotated
    table this would be page/physical order, not reading order (spec:
    "Do NOT sort original PDF spans using only x/y... that will produce
    incorrect vertical order"). Re-sorting here, by each line's own
    NORMALIZED position, is a purely internal re-ordering local to this
    module - text_extractor's own sort and every other zone type that
    calls it are completely untouched."""
    lines = text_extractor.extract_lines(page, cell_bbox_page)

    def sort_key(item):
        lb, _ = item
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        nx, ny = _normalize_point(cx, cy, origin, basis)
        return (ny, nx)

    ordered = []
    for lb, text in sorted(lines, key=sort_key):
        ordered.append((_normalize_bbox(lb, origin, basis), text))
    return ordered


def _cluster_1d(values, tolerance):
    """Clusters a list of numbers into groups where consecutive sorted
    values are within `tolerance` of each other; returns the sorted list
    of cluster centers (mean of each group). Used both for column-start
    X positions and physical-line Y positions - one small, reusable
    piece of geometry logic instead of writing the same clustering twice."""
    if not values:
        return []
    values = sorted(values)
    clusters = [[values[0]]]
    for v in values[1:]:
        if v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]


def _group_into_physical_lines(words, y_tolerance=TABLE_ROW_Y_TOLERANCE):
    """Groups words into physical PDF lines by Y-center proximity (the
    same "same visual line" concept core.text_extractor.extract_lines
    relies on MuPDF's own line grouping for - reimplemented here at the
    WORD level, rather than the character level, because table geometry
    needs individual word bounding boxes to find column gaps, which
    MuPDF's line/span grouping alone doesn't expose). Returns a list of
    {"bbox": (x0,y0,x1,y1), "words": [...]} sorted top-to-bottom, words
    within a line sorted left-to-right."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: ((w["bbox"][1] + w["bbox"][3]) / 2, w["bbox"][0]))
    lines = []
    for w in ordered:
        cy = (w["bbox"][1] + w["bbox"][3]) / 2
        if lines and abs(cy - lines[-1]["_cy"]) <= y_tolerance:
            lines[-1]["words"].append(w)
            n = len(lines[-1]["words"])
            lines[-1]["_cy"] = (lines[-1]["_cy"] * (n - 1) + cy) / n
        else:
            lines.append({"words": [w], "_cy": cy})
    result = []
    for line in lines:
        ws = sorted(line["words"], key=lambda w: w["bbox"][0])
        x0 = min(w["bbox"][0] for w in ws)
        y0 = min(w["bbox"][1] for w in ws)
        x1 = max(w["bbox"][2] for w in ws)
        y1 = max(w["bbox"][3] for w in ws)
        result.append({"bbox": (x0, y0, x1, y1), "words": ws})
    result.sort(key=lambda l: l["bbox"][1])
    return result


def _split_words_into_runs(words, gap_threshold=TABLE_COLUMN_GAP_THRESHOLD):
    """Splits a set of words (already sorted left-to-right) into "runs":
    consecutive words stay in the same run while the horizontal gap to
    the next word is no wider than gap_threshold (ordinary inter-word
    spacing); a wider gap starts a new run. Each run's own leftmost x0 is
    a column-start CANDIDATE - this is what lets column detection work
    even when an entire table row's cells are perfectly Y-aligned onto
    ONE row band (the normal/common case for a real table), instead of
    only working by accident when columns happen to render on slightly
    different Y positions."""
    ordered = sorted(words, key=lambda w: w["bbox"][0])
    runs = []
    for w in ordered:
        if runs and w["bbox"][0] - runs[-1][-1]["bbox"][2] <= gap_threshold:
            runs[-1].append(w)
        else:
            runs.append([w])
    return runs


def _detect_column_boundaries(physical_lines, x_tolerance=TABLE_COLUMN_X_TOLERANCE,
                               gap_threshold=TABLE_COLUMN_GAP_THRESHOLD,
                               min_occurrences=TABLE_COLUMN_MIN_OCCURRENCES):
    """Column start positions, inferred from repeated horizontal alignment
    across MULTIPLE row bands (spec: "infer columns from repeated
    alignment across multiple rows" for borderless tables).

    A row band is first split into "runs" of words separated by a
    horizontal gap wider than ordinary inter-word spacing
    (_split_words_into_runs) - each run's own start-x is a column-start
    CANDIDATE. This is deliberately NOT based on a whole row band's own
    leftmost edge (a confirmed real bug in an earlier version: whenever a
    row's cells happened to be perfectly Y-aligned into ONE row band -
    the ordinary case for a real, aligned table - only the leftmost
    cell's x ever became a clustering seed, silently collapsing every
    other column into the first one).

    Candidates are clustered (x_tolerance) and kept as real columns only
    when they recur across at least `min_occurrences` DISTINCT row bands
    (spec: "do not create a new column for one stray-X-position span") -
    except when the zone has too few row bands overall for that to be
    satisfiable (e.g. a single-row table), in which case the requirement
    is relaxed so columns still get detected rather than collapsing to
    one. Deliberately does NOT force in the zone's own drawn left edge as
    a synthetic extra seed point: real zoning always has a little slack
    around the actual text, and forcing that edge in as its own cluster
    candidate would fragment column 0's real, recurring start position
    into two near-identical "columns" whenever that slack exceeds
    x_tolerance. Returns boundaries sorted ascending; column i spans
    [boundaries[i], boundaries[i+1)) with the zone's right edge closing
    the last column."""
    per_line_candidates = [
        [run[0]["bbox"][0] for run in _split_words_into_runs(line["words"], gap_threshold)]
        for line in physical_lines
    ]
    flat = [x for cands in per_line_candidates for x in cands]
    clusters = _cluster_1d(flat, x_tolerance)
    if not clusters:
        return []

    counts = [0] * len(clusters)
    for cands in per_line_candidates:
        hit_clusters = {min(range(len(clusters)), key=lambda i: abs(clusters[i] - x)) for x in cands}
        for idx in hit_clusters:
            counts[idx] += 1

    kept = [c for c, n in zip(clusters, counts) if n >= min_occurrences]
    if not kept:
        kept = clusters
    return sorted(kept)


def _column_index_for_x(x, boundaries):
    idx = 0
    for i, b in enumerate(boundaries):
        if x >= b - TABLE_CELL_COORD_TOLERANCE:
            idx = i
        else:
            break
    return idx


def _line_starts_with_bullet(line):
    """True if this row band's own FIRST WORD is itself one of the
    recognized bullet characters (PyMuPDF's word tokenizer splits
    "• Masaje" into two separate words, "•" and "Masaje", at the
    whitespace) - used only as a ROW-GROUPING signal (see
    _group_into_logical_rows below), never as the list-vs-not-a-list
    DECISION itself - that happens later, per cell, against the cell's
    own formatted text, in _detect_bullet_list_items."""
    words = line["words"]
    if not words:
        return False
    first = words[0]["text"]
    return bool(first) and first[0] in TABLE_LIST_BULLET_CHARS


def _group_into_logical_rows(physical_lines, boundaries, gap_threshold=TABLE_COLUMN_GAP_THRESHOLD):
    """Row-grouping dispatcher. Bulleted tables need the "content starts
    in column 0 = new row" heuristic PLUS its bullet-run exception (see
    _group_into_logical_rows_bullet_aware) - that mechanism specifically
    relies on a bullet character being a strong, unambiguous per-line
    signal, which an ANCHOR-COLUMN approach (see
    _group_rows_by_anchor_column) cannot reproduce: two columns that are
    BOTH independently bulleted with the same item count look, to anchor
    selection, like "every column already has one line per row" and
    would get split into one row PER bullet instead of one row holding
    both complete lists (confirmed while testing this dispatch). Any
    zone with no bullet-marked line anywhere uses the anchor-column
    approach instead, since it correctly handles the far more common
    case a bullet-run never covers: an ordinary (non-bulleted) column
    that wraps across multiple physical lines per row while ANOTHER
    column in the same row does not (e.g. a short "age range" cell next
    to a long wrapped description) - the old column-0-only heuristic
    cannot distinguish that wrap from a genuinely new row, since both
    start in column 0 identically."""
    if any(_line_starts_with_bullet(line) for line in physical_lines):
        return _group_into_logical_rows_bullet_aware(physical_lines, boundaries, gap_threshold)
    return _group_rows_by_anchor_column(physical_lines, boundaries, gap_threshold)


def _group_rows_by_anchor_column(physical_lines, boundaries, gap_threshold=TABLE_COLUMN_GAP_THRESHOLD):
    """Row detection using the LEAST-WRAPPED column as an anchor: the
    column with the fewest total row bands (most likely to hold exactly
    one physical line per logical table row - e.g. a short "age range"
    or "duration" value) defines the actual ROW boundaries; every other
    column's own row bands (including a heavily-wrapped description
    column's many lines) are bucketed into whichever anchor-defined row
    they fall within: everything from one anchor line's own top down to
    (but not including) the NEXT anchor line's own top belongs to that
    row. This is what lets a real, common table layout - one short
    single-line column beside another column whose text wraps across
    several lines per row - reconstruct correctly: nothing about a
    wrapped description's OWN lines distinguishes "still this row" from
    "a new row", but the anchor column's own single-line-per-row rhythm
    does. Deliberately uses the NEXT anchor's own top edge as the cutoff,
    not the midpoint between consecutive anchor centers: a confirmed real
    bug in an earlier version used the midpoint, which incorrectly
    split a wrapped cell's own LAST line into the following row whenever
    that wrapped column's total height reached past the halfway point
    between two anchor rows (very easy to hit for a 3-4 line wrapped
    cell sitting beside a single-line anchor value) - the next anchor
    line's own top is the one Y position guaranteed to sit AFTER
    everything that visually belongs to the current row and BEFORE
    anything that belongs to the next, regardless of how tall the
    wrapped content is. Falls back to treating every physical line as
    its own row if there are no columns at all."""
    if not boundaries:
        return [[line] for line in physical_lines]
    counts = [0] * len(boundaries)
    per_col_lines = [[] for _ in boundaries]
    for line in physical_lines:
        runs = _split_words_into_runs(line["words"], gap_threshold)
        cols_here = {_column_index_for_x(r[0]["bbox"][0], boundaries) for r in runs}
        for c in cols_here:
            counts[c] += 1
            per_col_lines[c].append(line)

    nonzero = [c for c in range(len(boundaries)) if counts[c] > 0]
    if not nonzero:
        return []
    anchor_col = min(nonzero, key=lambda c: counts[c])
    anchor_lines = sorted(per_col_lines[anchor_col], key=lambda l: l["bbox"][1])

    cutoffs = [l["bbox"][1] for l in anchor_lines[1:]]

    def row_index_for_y(cy):
        idx = 0
        for cutoff in cutoffs:
            if cy >= cutoff:
                idx += 1
            else:
                break
        return min(idx, len(anchor_lines) - 1)

    rows = [[] for _ in anchor_lines]
    for line in physical_lines:
        rows[row_index_for_y(line["bbox"][1])].append(line)
    for row in rows:
        row.sort(key=lambda l: l["bbox"][1])
    return rows


def _group_into_logical_rows_bullet_aware(physical_lines, boundaries, gap_threshold=TABLE_COLUMN_GAP_THRESHOLD):
    """Groups physical row bands into logical table ROWS: a row band that
    has a run of words falling in COLUMN 0 starts a NEW row (spec: a PDF
    visual line is not automatically a table row, but the presence of
    content in the table's own first/leading column is what actually
    marks where one entry ends and the next begins - the same signal a
    human reader uses). A row band with NO run in column 0 (its content
    starts in a LATER column) is a WRAPPED CONTINUATION of the
    currently-open row's cell in whichever column its words land in -
    never a new <tr>, matching the "multi-line cell" requirement exactly.
    Uses the same run-splitting as column detection so a row band that
    spans multiple columns (the normal, Y-aligned case) is still
    correctly recognized as starting in column 0, not just a row band
    that happens to hold only one column's words.

    EXCEPTION: once a row band starting with a bullet character in
    column 0 has opened a row (a "bullet run"), EVERY subsequent row band
    that ALSO starts in column 0 - bulleted or not - is treated as
    belonging to that SAME row, never a new one, until a row band that
    has content in a LATER column WITHOUT itself starting a new bullet in
    column 0 appears (clear evidence of a genuinely unrelated new row,
    which always closes the run first, before that row band is placed).
    Without the first part, a multi-item bullet list living entirely
    inside one table cell (every item flush against the same column-0 X,
    exactly the "content in column 0" signal used everywhere else to
    detect a new row) would get chopped into one fake single-line "row"
    per bullet, AND a bullet item that wraps onto a second physical line
    with no marker of its own (spec: "visual PDF line wrapping must NOT
    create a new list-item") would also incorrectly start yet another
    fake row. Without the second part (closing the run), EVERY row for
    the rest of the table after any bulleted cell would incorrectly keep
    merging into it - both confirmed while testing the internal-list
    feature. The row band that opens a run (e.g. a bulleted column-0 cell
    sharing its own row with an ordinary one-line comment in a later
    column) is itself still correctly treated as starting a fresh row.

    Known limitation (documented, not silently wrong, flagged in this
    module's own debug output so a human can review it): unlike the
    anchor-column strategy this function's own caller falls back to for
    non-bulleted tables (_group_rows_by_anchor_column), a BULLETED
    column-0 cell's own row still can't use another column as an anchor
    for its boundaries - a row whose column-0 bullet run happens to share
    its own row with an ALSO heavily-wrapped (non-bulleted) later column
    could still be mis-split, since bullets themselves remain the primary
    signal here."""
    rows = []
    in_bullet_run = False
    for line in physical_lines:
        runs = _split_words_into_runs(line["words"], gap_threshold)
        run_cols = {_column_index_for_x(run[0]["bbox"][0], boundaries) for run in runs}
        first_col = min(run_cols) if run_cols else 0
        has_other_column = any(c > 0 for c in run_cols)
        col0_is_bullet = first_col == 0 and _line_starts_with_bullet(line)
        closes_run = has_other_column and not col0_is_bullet

        if first_col == 0 and rows and in_bullet_run and not closes_run:
            rows[-1].append(line)
        elif first_col == 0 or not rows:
            rows.append([line])
        else:
            rows[-1].append(line)

        if closes_run:
            in_bullet_run = False
        elif col0_is_bullet:
            in_bullet_run = True
    return rows


def _assign_words_to_columns(lines, boundaries):
    """For a set of physical lines (already grouped into one logical row),
    buckets every word by which column its horizontal center falls into."""
    buckets = {i: [] for i in range(len(boundaries))}
    for line in lines:
        for w in line["words"]:
            cx = (w["bbox"][0] + w["bbox"][2]) / 2
            col = _column_index_for_x(cx, boundaries)
            buckets[col].append(w)
    return buckets


def _bold_ratio(page, bbox):
    """Fraction of characters within bbox that are bold (per the existing
    formatting_detector.detect_bold_italic, the same font-flag/name check
    used everywhere else in the project) - used only as header-row
    evidence, never to alter extracted text."""
    raw = text_extractor._get_rawdict(page)
    bold_count = total = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bold, _ = detect_bold_italic(span.get("flags", 0), span.get("font", ""))
                for ch in span.get("chars", []):
                    if text_extractor._char_in_bbox(ch["bbox"], bbox):
                        total += 1
                        if bold:
                            bold_count += 1
    return (bold_count / total) if total else 0.0


class TableCell:
    __slots__ = ("row", "col", "colspan", "rowspan", "bbox", "text", "is_list", "list_items")

    def __init__(self, row, col, bbox):
        self.row = row
        self.col = col
        self.colspan = 1
        self.rowspan = 1
        self.bbox = bbox
        self.text = ""
        self.is_list = False
        self.list_items = []


class TableStructure:
    """Pure-data result of analyze_table(): rows of TableCell (None for a
    position consumed by a preceding cell's colspan), a header row count,
    and the column boundaries actually used - everything
    xml_generator.py's table-wrap builder needs, and everything the
    Table Debug log prints, with no PDF/lxml objects involved."""
    def __init__(self, rows, header_row_count, column_boundaries):
        self.rows = rows  # list of list[TableCell or None]
        self.header_row_count = header_row_count
        self.column_boundaries = column_boundaries

    @property
    def num_columns(self):
        return len(self.column_boundaries)

    @property
    def num_rows(self):
        return len(self.rows)


def _manual_column_boundaries(manual_column_splits, zone_bbox):
    """Converts a user-drawn list of vertical column-boundary X positions
    (gui.pdf_viewer's Column Split Mode - each line marks the boundary
    BETWEEN two columns, not a column's own start) into the same
    "column start positions" shape _detect_column_boundaries returns -
    the zone's own left edge is always column 0's start, then each
    sorted manual boundary starts the next column. K manual boundaries
    always produce exactly K+1 columns."""
    return [zone_bbox[0]] + sorted(manual_column_splits)


def _manual_row_boundaries(manual_row_splits, zone_bbox):
    """Y-axis counterpart of _manual_column_boundaries: the full fence of
    row-band Y edges (zone top, every sorted manual boundary, zone
    bottom) - row i spans [result[i], result[i+1]). Used to give every
    manual-grid row - INCLUDING one with no text in it at all - its own
    correct, non-degenerate Y-range for building placeholder empty
    cells, rather than falling back to the whole zone's Y-span."""
    return [zone_bbox[1]] + sorted(manual_row_splits) + [zone_bbox[3]]


def _group_rows_by_manual_boundaries(physical_lines, manual_row_splits):
    """Converts a user-drawn list of horizontal row-boundary Y positions
    (gui.pdf_viewer's Row Split Mode) directly into logical rows - K
    manual boundaries always produce exactly K+1 rows, each row band
    holding every physical line whose own CENTER point falls before the
    next boundary (matching the same center-point-containment rule
    _assign_words_to_columns already uses on the column axis - a manual
    boundary is an exact line, and a line belongs to whichever band
    contains its center, never a text-block heuristic). This is the
    manual-override counterpart of _group_into_logical_rows/
    _group_rows_by_anchor_column - used INSTEAD of either whenever the
    user has drawn explicit row boundaries for this Table zone (spec:
    "manual row and column splits override automatic table detection"),
    never blended with them."""
    cutoffs = sorted(manual_row_splits)
    rows = [[] for _ in range(len(cutoffs) + 1)]

    def row_index_for_y(center_y):
        idx = 0
        for cutoff in cutoffs:
            if center_y >= cutoff:
                idx += 1
            else:
                break
        return min(idx, len(rows) - 1)

    for line in physical_lines:
        center_y = (line["bbox"][1] + line["bbox"][3]) / 2
        rows[row_index_for_y(center_y)].append(line)
    for row in rows:
        row.sort(key=lambda l: l["bbox"][1])
    return rows


def analyze_table(page, zone_bbox, keep_hyphen_at=None, manual_row_splits=None, manual_column_splits=None):
    """The single entry point: detects rows/columns/cells/merges/header
    for the Table zone at zone_bbox on `page`, and returns a
    TableStructure whose cell text has ALREADY been extracted via
    text_extractor.extract_formatted_text (so bold/italic/sup/sub/
    hyphenation are all correctly applied per cell, reusing the existing
    pipeline exactly as every other tag does). Only PDF objects whose
    center point falls inside zone_bbox (expanded by the small, fixed
    TABLE_EXTRACTION_BOUNDARY_TOLERANCE - never nearby paragraphs,
    captions, or page numbers outside it, and never the zone's own
    displayed/stored bbox, only this internal extraction pass) are ever
    considered.

    Rotated tables (text placed at 90/180/270 degrees relative to
    upright, e.g. a wide table turned sideways to fit a portrait page -
    the PAGE itself is never rotated, only this content): rotation is
    detected via image_extractor.detect_text_rotation_key, the EXACT
    same PDF text "dir"-vector mechanism the project already uses to fix
    a rotated Figure - not a second, independently-invented detector.
    When rotation is detected, every word's geometry is transformed into
    a NORMALIZED (upright) coordinate frame before column/row/cell
    detection runs - those algorithms are already purely geometric
    (relative X/Y positions, gaps, clustering) and need no rotation-
    specific logic of their own once fed normalized coordinates. Only
    the ORIGINAL PDF page.get_text('words', clip=zone_bbox) query, and
    every text_extractor call that ultimately reads real character
    bboxes from the page's own rawdict, still needs genuine PAGE-space
    coordinates - those are converted back (denormalized) exactly where
    needed, per cell. The zone's own stored/displayed bbox is never
    touched; normalization is purely internal to this function. A
    non-rotated table (the overwhelming common case) takes the exact
    same code path as before this feature existed - see the `if
    rotation_key:` branches below - so nothing changes for it.

    manual_row_splits / manual_column_splits (gui.pdf_viewer's Row/
    Column Split Mode, Ctrl+Shift+R / Ctrl+Shift+C, stored on the Table
    zone's own attributes["row_splits"]/["column_splits"] - PDF page-
    space Y/X positions the user drew as row/column boundary GUIDES,
    never separate XML zones): when given, these are the ONLY source of
    structure for that axis - no automatic detection, no post-hoc
    "repair" of any kind runs against it. K manual boundaries on an axis
    ALWAYS produce exactly K+1 rows/columns, unconditionally - including
    a row/column that ends up with no text in it at all (rendered as a
    real, empty-text cell, never silently dropped or merged into a
    neighbor - spec: "row_count x column_count cells, PERIOD"). This
    also means the colspan/rowspan-merge inference below (extending a
    cell across an empty neighboring position when its own rendered text
    visually crosses that boundary) is used ONLY for AUTOMATICALLY
    detected columns, where it is genuine statistical evidence about a
    clustered, approximate boundary - a MANUAL column split is an
    explicit, absolute user decision and must never be second-guessed or
    merged away by anything the extracted text happens to do. Each axis
    is independent: the user can override only columns, only rows, or
    both - whichever axis has no manual boundaries keeps using automatic
    detection exactly as before this feature existed. Known, disclosed
    limitation: manual guides are only honored for an UNROTATED table
    (rotation_key == 0) - a page-space horizontal/vertical line the user
    draws doesn't correspond to a clean single coordinate in the
    NORMALIZED frame a rotated table uses internally, so manual guides
    are silently ignored (falling back to automatic detection, logged)
    rather than doing something geometrically undefined for that
    combination."""
    rotation_key = image_extractor.detect_text_rotation_key(page, zone_bbox) or 0
    if rotation_key not in (90, 180, 270):
        rotation_key = 0
    origin = (zone_bbox[0], zone_bbox[1])
    basis = _ROTATION_BASIS[rotation_key]
    page_zone_bbox = zone_bbox

    page_words = _get_words_in_bbox(page, page_zone_bbox)
    if rotation_key:
        words = [{"bbox": _normalize_bbox(w["bbox"], origin, basis), "text": w["text"]} for w in page_words]
        zone_bbox = list(_normalize_bbox(page_zone_bbox, origin, basis))
    else:
        words = page_words

    manual_axes_honored = rotation_key == 0
    manual_cols_active = manual_axes_honored and bool(manual_column_splits)
    manual_rows_active = manual_axes_honored and bool(manual_row_splits)

    physical_lines = _group_into_physical_lines(words)
    # A manual grid must produce its exact (rows+1)x(cols+1) cell count
    # EVEN when the zone has little/no extractable text (e.g. a sparse
    # or partly-blank table) - so the empty-zone early-return only
    # applies when NEITHER axis is manually defined.
    if not physical_lines and not manual_cols_active and not manual_rows_active:
        return TableStructure([], 0, [])

    if manual_cols_active:
        boundaries = _manual_column_boundaries(manual_column_splits, zone_bbox)
    elif physical_lines:
        boundaries = _detect_column_boundaries(physical_lines)
    else:
        boundaries = [zone_bbox[0]]

    row_y_boundaries = _manual_row_boundaries(manual_row_splits, zone_bbox) if manual_rows_active else None
    if manual_rows_active:
        logical_rows = _group_rows_by_manual_boundaries(physical_lines, manual_row_splits)
    elif physical_lines:
        logical_rows = _group_into_logical_rows(physical_lines, boundaries)
    else:
        logical_rows = [[]]

    manual_grid_active = manual_cols_active or manual_rows_active
    grid = []
    for row_idx, lines in enumerate(logical_rows):
        buckets = _assign_words_to_columns(lines, boundaries)
        row_top, row_bottom = (row_y_boundaries[row_idx], row_y_boundaries[row_idx + 1]) \
            if row_y_boundaries else (zone_bbox[1], zone_bbox[3])
        row_cells = [None] * len(boundaries)
        col = 0
        while col < len(boundaries):
            words_here = buckets.get(col, [])
            if not words_here:
                # Empty column - if columns were AUTOMATICALLY detected
                # and the PRECEDING cell's own content visually extends
                # past this column's left boundary (no word ever started
                # here, and the previous cell's text crosses into this
                # column's X range), this is a merged (colspan) cell, not
                # a genuinely empty one - see spec part 14. Extend the
                # previous cell instead of emitting a bare <td/>. NEVER
                # done when columns are MANUALLY split: a manual boundary
                # is an explicit, absolute user decision and must never
                # be second-guessed/merged away regardless of how far a
                # neighboring cell's own text happens to render.
                if not manual_cols_active and col > 0 and row_cells[col - 1] is not None:
                    prev = row_cells[col - 1]
                    if prev.bbox[2] > boundaries[col] + TABLE_CELL_COORD_TOLERANCE:
                        prev.colspan += 1
                        col += 1
                        continue
                if manual_grid_active:
                    # Manual grid mode: every grid position is a REAL
                    # cell, even with no text at all - never silently
                    # omitted (spec: "create exactly row_count x
                    # column_count cells" / an empty cell still renders
                    # as <td></td>, not a missing <td>).
                    x_end = boundaries[col + 1] if col + 1 < len(boundaries) else zone_bbox[2]
                    row_cells[col] = TableCell(row_idx, col, [boundaries[col], row_top, x_end, row_bottom])
                col += 1
                continue
            x0 = min(w["bbox"][0] for w in words_here)
            y0 = min(w["bbox"][1] for w in words_here)
            x1 = max(w["bbox"][2] for w in words_here)
            y1 = max(w["bbox"][3] for w in words_here)
            cell = TableCell(row_idx, col, [x0, y0, x1, y1])
            row_cells[col] = cell
            col += 1
        grid.append(row_cells)

    # Re-extract each surviving cell's text through the EXISTING formatted-
    # text pipeline (bold/italic/sup/sub/hyphenation all reused unchanged),
    # using a bbox built from the CELL'S OWN ALREADY-ASSIGNED WORDS (set
    # in the first pass above, [x0, y0, x1, y1] - the tight union of every
    # word _assign_words_to_columns bucketed into this column, across
    # every physical line of this row), expanded only by the small,
    # fixed TABLE_CELL_COORD_TOLERANCE - NOT a bbox derived from the
    # clustered column BOUNDARY line.
    #
    # This replaces an earlier, confirmed-broken design that built the
    # extraction bbox from boundaries[col]/boundaries[end_col] instead: a
    # column boundary is itself an AVERAGE of the very content sitting on
    # both sides of it, so real text is essentially guaranteed to start/
    # end almost exactly AT that line - any fixed tolerance there either
    # clips the cell's own first/last character (when its glyph sits a
    # hair on the far side of the average) or bleeds a neighboring cell's
    # character in (when tolerance is added to compensate) - both were
    # reproduced directly ("Escala"/"Notas" losing their first letters
    # while the PRECEDING cell simultaneously gained a stray extra
    # character from them). A tight bbox around the cell's OWN measured
    # words has no such ambiguity: a neighboring cell's words simply live
    # outside it, regardless of how close the columns are spaced. Still
    # clamped to the zone's own extraction bounds (± the outer
    # TABLE_EXTRACTION_BOUNDARY_TOLERANCE) purely as a defensive backstop,
    # not as the driving edge anymore.
    for row_idx, row_cells in enumerate(grid):
        for col, cell in enumerate(row_cells):
            if cell is None:
                continue
            own_x0, own_y0, own_x1, own_y1 = cell.bbox
            extract_bbox = [
                max(zone_bbox[0] - TABLE_EXTRACTION_BOUNDARY_TOLERANCE, own_x0 - TABLE_CELL_COORD_TOLERANCE),
                max(zone_bbox[1] - TABLE_EXTRACTION_BOUNDARY_TOLERANCE, own_y0 - TABLE_CELL_COORD_TOLERANCE),
                min(zone_bbox[2] + TABLE_EXTRACTION_BOUNDARY_TOLERANCE, own_x1 + TABLE_CELL_COORD_TOLERANCE),
                min(zone_bbox[3] + TABLE_EXTRACTION_BOUNDARY_TOLERANCE, own_y1 + TABLE_CELL_COORD_TOLERANCE),
            ]
            cell.bbox = extract_bbox
            # Bullet-list detection is tried FIRST, against this exact
            # same cell bbox, so it sees exactly the characters that
            # would otherwise become this cell's plain text - never a
            # separate/wider region. Falls back to ordinary formatted-
            # text extraction (unchanged) whenever the cell's own layout
            # doesn't show real list evidence (see
            # _detect_bullet_list_items's own docstring for exactly what
            # counts as evidence) - so this can never turn ordinary
            # multi-line cell text into a <list>.
            if rotation_key:
                # extract_bbox is in NORMALIZED space here - convert back
                # to real PAGE coordinates before touching text_extractor
                # (which reads actual rawdict character bboxes), then
                # re-order the cell's own physical lines by their
                # NORMALIZED position (never trusting extract_lines' own
                # page-Y sort, which is only "reading order" for upright
                # text) before joining/list-detecting - see
                # _extract_cell_lines_ordered.
                extract_bbox_page = _denormalize_bbox(extract_bbox, origin, basis)
                ordered_lines = _extract_cell_lines_ordered(page, extract_bbox_page, origin, basis)
                list_items = _bullet_items_from_lines(ordered_lines, keep_hyphen_at)
                if list_items:
                    cell.is_list = True
                    cell.list_items = list_items
                else:
                    cell.text = text_extractor.dehyphenate_join(
                        [t for _, t in ordered_lines], keep_hyphen_at).strip()
            else:
                list_items = _detect_bullet_list_items(page, extract_bbox, keep_hyphen_at)
                if list_items:
                    cell.is_list = True
                    cell.list_items = list_items
                else:
                    cell.text = text_extractor.extract_formatted_text(page, extract_bbox, keep_hyphen_at)

    # Drop any row that ended up with NO content in ANY column - ONLY
    # ever for AUTOMATICALLY-detected rows, where a fully-empty row can
    # only be a geometry-clustering artifact (every automatic row band
    # is, by construction, seeded from physical lines that DO have
    # content - see _group_into_logical_rows/_group_rows_by_anchor_column
    # - so an all-empty automatic row should never legitimately occur;
    # pruning it is a pure safety net). NEVER done when rows are
    # MANUALLY split: the user's row boundaries are the table's exact,
    # absolute structure - K manual row-split boundaries must ALWAYS
    # produce exactly K+1 rows, including a genuinely empty one, which
    # renders as a real row with empty <td>/<th> cells rather than being
    # silently dropped (spec: "row_count = len(row_splits) + 1, PERIOD").
    # This no longer needs to double as a header-detection safety net
    # either: a manual-grid row's cells are now ALWAYS real TableCell
    # objects (see manual_grid_active above), even when empty, so
    # header-bold-ratio detection below already gets a correct,
    # non-degenerate row-0 bbox without any pruning.
    empty_rows_dropped = 0
    if not manual_rows_active:
        rows_before_prune = len(grid)
        grid = [row for row in grid if any(
            (c is not None) and (c.is_list or (c.text or "").strip()) for c in row)]
        empty_rows_dropped = rows_before_prune - len(grid)

    # Header detection: row 0 counts as a header when its own bold ratio
    # meets the configurable threshold - never blindly assumed - spec
    # part 13. Only ever considers a SINGLE leading header row for now
    # (multiple consecutive bold leading rows are rare in real tables and
    # add detection risk beyond what's warranted here); still leaves room
    # for a genuinely non-bold, borderless small table to correctly get
    # NO <thead> at all.
    header_row_count = 0
    if grid:
        header_bbox = zone_bbox if not grid[0] else \
            [zone_bbox[0], grid[0][0].bbox[1] if grid[0][0] else zone_bbox[1],
             zone_bbox[2], max((c.bbox[3] for c in grid[0] if c), default=zone_bbox[1])]
        # header_bbox is in NORMALIZED space when rotated - _bold_ratio
        # reads real rawdict character bboxes, so it always needs genuine
        # PAGE-space coordinates.
        header_bbox_page = _denormalize_bbox(header_bbox, origin, basis) if rotation_key else header_bbox
        ratio = _bold_ratio(page, header_bbox_page)
        if ratio >= TABLE_HEADER_BOLD_RATIO:
            header_row_count = 1

    structure = TableStructure(grid, header_row_count, boundaries)
    if debug_log.is_enabled():
        _log_debug(page_zone_bbox, structure, rotation_key,
                   manual_row_splits or [], manual_column_splits or [],
                   manual_rows_active, manual_cols_active, empty_rows_dropped)
    return structure


def _log_debug(zone_bbox, structure: TableStructure, rotation_key: int = 0,
                manual_row_splits=None, manual_column_splits=None,
                manual_rows_used: bool = False, manual_cols_used: bool = False,
                empty_rows_dropped: int = 0):
    """Labeled debug dump used to verify, by eye, that Generate XML is
    actually using the manual split coordinates and not any automatic
    detection: TABLE BBOX / COLUMN SPLITS / ROW SPLITS / GENERATED GRID
    (every cell's own bbox) / CELL TEXT (every cell's extracted text)."""
    manual_row_splits = manual_row_splits or []
    manual_column_splits = manual_column_splits or []
    debug_log.log("TABLE", "TABLE DEBUG",
                   f"TABLE BBOX: x0={zone_bbox[0]:.1f} y0={zone_bbox[1]:.1f} "
                   f"x1={zone_bbox[2]:.1f} y1={zone_bbox[3]:.1f}",
                   f"Detected rotation: {rotation_key} deg",
                   f"COLUMN SPLITS ({len(manual_column_splits)}, used={manual_cols_used}): "
                   + (", ".join(f"{v:.1f}" for v in sorted(manual_column_splits)) or "(none)"),
                   f"ROW SPLITS ({len(manual_row_splits)}, used={manual_rows_used}): "
                   + (", ".join(f"{v:.1f}" for v in sorted(manual_row_splits)) or "(none)"),
                   f"empty rows dropped: {empty_rows_dropped}"
                   + (" - a manual row-split boundary may be positioned slightly off from the "
                      "real content it was meant to bound" if empty_rows_dropped else ""),
                   f"Detected columns: {structure.num_columns}, rows: {structure.num_rows}, "
                   f"header rows: {structure.header_row_count}")
    for i, b in enumerate(structure.column_boundaries):
        debug_log.log("TABLE", f"C{i + 1}: x_start={b:.1f}")
    debug_log.log("TABLE", "GENERATED GRID:")
    for r, row in enumerate(structure.rows):
        for c, cell in enumerate(row):
            if cell is None:
                debug_log.log("TABLE", f"  Row {r} Cell {c}: (consumed by colspan)")
                continue
            span_note = f" colspan={cell.colspan}" if cell.colspan > 1 else ""
            bbox = ", ".join(f"{v:.1f}" for v in cell.bbox)
            debug_log.log("TABLE", f"  Row {r} Cell {c}{span_note} bbox = [{bbox}]")
    debug_log.log("TABLE", "CELL TEXT:")
    for r, row in enumerate(structure.rows):
        for c, cell in enumerate(row):
            if cell is None:
                continue
            if cell.is_list:
                debug_log.log("TABLE", f"  Row {r} Cell {c} = LIST[{len(cell.list_items)} items]")
                for n, item in enumerate(cell.list_items):
                    debug_log.log("TABLE", f"    item {n + 1}: {item!r}")
            else:
                debug_log.log("TABLE", f"  Row {r} Cell {c} = {cell.text!r}")
