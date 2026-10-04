"""Candidate TABLE region detection for one page, used only by Auto Analyse
(auto_zoning/page_analyzer.py). This module only decides WHERE a table is
(a bbox) - once a region is chosen, page_analyzer re-runs the normal band/
list/heading pipeline scoped to it for the table's own children, and
core.table_extractor.analyze_table (unchanged, existing) does the actual
cell-by-cell analysis at XML-generation time, exactly as it already does
for a manually-drawn Table zone. This is the one genuinely new/heuristic
piece of Auto Analyse, so it stays deliberately conservative - either
signal below, if weak, is discarded rather than guessed, to avoid
misreading an ordinary multi-column page or a numbered list as a table.

Two independent, deterministic signals:
  1. Ruling lines/rects (page.get_drawings() - not used anywhere else in
     this codebase today) clustered into a region with real evidence of
     BOTH horizontal and vertical rules.
  2. Absent ruling lines, recurring column X-positions (reusing
     core.table_extractor._cluster_1d) populated consistently across
     several compact rows - the classic "borderless table" shape, but
     required to be visibly denser/shorter-row than ordinary paragraph
     flow so a real 2-column prose page is never misread as a table."""
from core.table_extractor import _cluster_1d

MIN_RULE_SEGMENTS_PER_AXIS = 2
LINE_THICKNESS_TOLERANCE = 2.0      # pt - a segment this thin on one axis counts as a "line" on the other
DRAWING_MERGE_GAP = 6.0             # pt - drawings within this gap are treated as one region

TEXT_TABLE_MIN_COLUMNS = 2
TEXT_TABLE_MIN_ROWS = 3
TEXT_TABLE_MAX_HEIGHT_FRACTION = 0.45   # a candidate text-table region can't span more than this much of the page
TEXT_TABLE_COLUMN_TOLERANCE = 6.0
TEXT_TABLE_ROW_GAP_MAX = 3.0            # max row-height multiple between rows to stay "table-tight"


def _drawing_bboxes(page):
    """Every individual ruling-line/rect PRIMITIVE on the page, with its own
    bbox and how many horizontal/vertical edges it counts as. A table's
    gridlines are typically drawn as ONE Shape (one page.get_drawings()
    entry) containing several 'l' (line) and/or 're' (rect) sub-items in
    its own "items" list - the drawing's own overall bbox spans the WHOLE
    table region, which is neither thin-horizontal nor thin-vertical, so
    each sub-item must be inspected individually rather than judging the
    parent drawing's bbox as a whole (confirmed by direct inspection: a
    rect + 3 internal lines drawn as one Shape produces exactly one
    page.get_drawings() entry whose own "rect" is the full table bbox)."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    out = []
    for d in drawings:
        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                x0, x1 = sorted((p1.x, p2.x))
                y0, y1 = sorted((p1.y, p2.y))
                w, h = x1 - x0, y1 - y0
                is_h = h <= LINE_THICKNESS_TOLERANCE and w > LINE_THICKNESS_TOLERANCE
                is_v = w <= LINE_THICKNESS_TOLERANCE and h > LINE_THICKNESS_TOLERANCE
                if is_h or is_v:
                    out.append({"bbox": (x0, y0, x1, y1), "h": is_h, "v": is_v})
            elif kind == "re":
                rect = item[1]
                x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
                if x1 - x0 > 0 and y1 - y0 > 0:
                    # A stroked rectangle inherently has 2 horizontal AND 2
                    # vertical edges of its own - counted directly rather
                    # than judged by its (necessarily non-thin) own bbox,
                    # so a single bordered box with no internal gridlines
                    # still satisfies the >=2-per-axis evidence threshold.
                    out.append({"bbox": (x0, y0, x1, y1), "h": True, "v": True, "weight": 2})
    return out


def _merge_into_regions(entries):
    """Greedy spatial merge: any two entries whose (gap-expanded) bboxes
    overlap belong to the same region. Returns a list of
    {"bbox": union_bbox, "h_count": n, "v_count": n}."""
    regions = []
    for e in entries:
        weight = e.get("weight", 1)
        x0, y0, x1, y1 = e["bbox"]
        merged = False
        for r in regions:
            rx0, ry0, rx1, ry1 = r["bbox"]
            if (x0 - DRAWING_MERGE_GAP <= rx1 and rx0 - DRAWING_MERGE_GAP <= x1
                    and y0 - DRAWING_MERGE_GAP <= ry1 and ry0 - DRAWING_MERGE_GAP <= y1):
                r["bbox"] = (min(rx0, x0), min(ry0, y0), max(rx1, x1), max(ry1, y1))
                r["h_count"] += weight if e["h"] else 0
                r["v_count"] += weight if e["v"] else 0
                merged = True
                break
        if not merged:
            regions.append({"bbox": e["bbox"], "h_count": weight if e["h"] else 0,
                             "v_count": weight if e["v"] else 0})
    # A newly merged region can now touch an EARLIER region that didn't
    # overlap the original single entry - one more pass collapses those.
    changed = True
    while changed and len(regions) > 1:
        changed = False
        for i in range(len(regions)):
            for j in range(i + 1, len(regions)):
                ax0, ay0, ax1, ay1 = regions[i]["bbox"]
                bx0, by0, bx1, by1 = regions[j]["bbox"]
                if (ax0 - DRAWING_MERGE_GAP <= bx1 and bx0 - DRAWING_MERGE_GAP <= ax1
                        and ay0 - DRAWING_MERGE_GAP <= by1 and by0 - DRAWING_MERGE_GAP <= ay1):
                    regions[i]["bbox"] = (min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1))
                    regions[i]["h_count"] += regions[j]["h_count"]
                    regions[i]["v_count"] += regions[j]["v_count"]
                    regions.pop(j)
                    changed = True
                    break
            if changed:
                break
    return regions


def _ruling_line_tables(page):
    entries = _drawing_bboxes(page)
    if not entries:
        return []
    regions = _merge_into_regions(entries)
    return [r["bbox"] for r in regions
            if r["h_count"] >= MIN_RULE_SEGMENTS_PER_AXIS and r["v_count"] >= MIN_RULE_SEGMENTS_PER_AXIS]


def _text_geometry_tables(lines, page_height):
    """Fallback for a borderless table: a compact, multi-row region where
    >=2 distinct left-x columns are each populated in every row. Scans the
    page top-to-bottom in row-bands (grouping lines whose Y-center is
    close together) and grows a candidate region while consecutive rows
    keep showing the same >=2-column shape; a row that breaks the shape
    (or a big vertical gap) ends the candidate."""
    if not lines:
        return []
    sorted_lines = sorted(lines, key=lambda li: (li.bbox[1], li.bbox[0]))
    heights = [li.bbox[3] - li.bbox[1] for li in sorted_lines if li.bbox[3] > li.bbox[1]]
    median_h = sorted(heights)[len(heights) // 2] if heights else 10.0

    rows = []
    current_row = []
    prev_bottom = None
    for li in sorted_lines:
        if current_row and li.bbox[1] - prev_bottom > median_h * 0.6:
            rows.append(current_row)
            current_row = []
        current_row.append(li)
        prev_bottom = max(prev_bottom or li.bbox[3], li.bbox[3])
    if current_row:
        rows.append(current_row)

    tables = []
    i = 0
    while i < len(rows):
        run = [rows[i]]
        j = i + 1
        while j < len(rows):
            gap = rows[j][0].bbox[1] - max(li.bbox[3] for li in rows[j - 1])
            if gap > median_h * TEXT_TABLE_ROW_GAP_MAX:
                break
            run.append(rows[j])
            j += 1
        if len(run) >= TEXT_TABLE_MIN_ROWS:
            x0s = [li.bbox[0] for row in run for li in row]
            columns = _cluster_1d(x0s, TEXT_TABLE_COLUMN_TOLERANCE)
            if len(columns) >= TEXT_TABLE_MIN_COLUMNS:
                populated_rows = 0
                for row in run:
                    row_cols = {min(range(len(columns)), key=lambda k: abs(columns[k] - li.bbox[0]))
                                 for li in row}
                    if len(row_cols) >= TEXT_TABLE_MIN_COLUMNS:
                        populated_rows += 1
                if populated_rows >= TEXT_TABLE_MIN_ROWS:
                    all_lines = [li for row in run for li in row]
                    bbox = (min(li.bbox[0] for li in all_lines), min(li.bbox[1] for li in all_lines),
                            max(li.bbox[2] for li in all_lines), max(li.bbox[3] for li in all_lines))
                    if (bbox[3] - bbox[1]) <= page_height * TEXT_TABLE_MAX_HEIGHT_FRACTION:
                        tables.append(bbox)
        i = j if j > i else i + 1
    return tables


def detect_tables(page, lines, page_height) -> list:
    """Returns candidate table bboxes for this page, ruling-line evidence
    first (stronger signal), falling back to text-geometry evidence only
    where no ruling-line table already covers that region."""
    ruled = _ruling_line_tables(page)
    text_based = []
    for bbox in _text_geometry_tables(lines, page_height):
        covered = any(bbox[0] >= r[0] - 4 and bbox[1] >= r[1] - 4 and bbox[2] <= r[2] + 4 and bbox[3] <= r[3] + 4
                       for r in ruled)
        if not covered:
            text_based.append(bbox)
    return ruled + text_based
