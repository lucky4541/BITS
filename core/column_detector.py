"""Page-layout column/band detection - a single, generic geometry primitive
shared by three callers: auto_zoning (laying out brand-new candidate zones
from raw PDF text lines, before any zones exist), reading_order.py's
automatic Reading Order recalculation (re-sorting already-drawn zones by
their own bbox after every zoning change), and anything else that needs
"what column is this bbox in." Operates on anything with a `.bbox` =
(x0, y0, x1, y1)-like attribute - a `pdf_block_detector.LineInfo` or a
`core.zone_manager.Zone` work identically here, since only bbox geometry is
ever read.

A page is partitioned into top-to-bottom BANDS at every "full-width" item
(one whose bbox spans the ENTIRE detected multi-column region - from the
first column's own left edge to the last column's own right edge - e.g. a
title, a full-width figure caption, a section break). Within a band that is
NOT interrupted by a full-width item, items are assigned to N columns.
Banding happens BEFORE column assignment, so a full-width heading between
two multi-column regions never silently merges them into one.

Supports any number of columns (2, 3, ...), not just 2 - every strong,
recurring left-edge cluster with a genuine gap from its neighbor becomes its
own column. Built on the same generic 1D-clustering primitive
core.table_extractor already uses for its own (differently-scoped,
cell-level) column detection, applied here to a page-wide/zone-wide
problem instead."""
from dataclasses import dataclass, field

from core.table_extractor import _cluster_1d

def _median_right_edge(members) -> float:
    """The median (not mean) right edge (bbox[2]) across `members` - used
    everywhere this file needs "how far right does this cluster/column
    actually extend" so that one unusually wide item (a full-width
    heading sharing this column's own left margin, or simply one entry
    drawn wider than its neighbors) can never single-handedly drag a
    gutter/boundary calculation far past where the column actually ends.
    A mean is sensitive to exactly that one outlier; a median isn't, as
    long as it isn't the majority of the group - confirmed as the fix for
    a real regression (see _merge_indentation_clusters's own docstring).
    True median (averages the two middle values for an even-sized group) -
    NOT edges[n//2] alone, which for an even count silently picks the
    UPPER of the two middle values (degenerating to the max itself for a
    2-member group) - confirmed as a real, separate bug: a 2-member
    "column" consisting of one full-width item plus one real narrow zone
    still let the full-width item's own far right edge dominate."""
    edges = sorted(m.bbox[2] for m in members)
    n = len(edges)
    mid = n // 2
    if n % 2 == 0:
        return (edges[mid - 1] + edges[mid]) / 2.0
    return edges[mid]


def _exclude_full_width_outliers(items):
    """Excludes items whose bbox spans NEAR BOTH the leftmost AND the
    rightmost edge observed across every item on the page - i.e. a
    genuine full-width item (heading, rule, caption spanning every
    column) - from column-DETECTION's clustering input only. Real,
    confirmed bug this fixes: a full-width item that happens to share its
    LEFT edge with one column's own margin (very common) was still being
    fed into that column's raw x0 cluster; with only 1-2 OTHER real
    members in that cluster, even the median right-edge estimate
    (_median_right_edge) couldn't fully resist being dragged toward the
    full-width item's own far right edge, making a genuinely separate
    column on the other side look like it fell inside the resulting
    (inflated) gap and get silently merged away - collapsing column
    detection entirely on a page with a full-width note/heading above a
    2-column region that had only just started (few zones per column).

    Deliberately an ABSOLUTE, both-sided test - not "wider than the
    typical item" (an earlier version of this function used that relative
    comparison and caused a DIFFERENT regression: a real column whose
    zones are just naturally wider than its neighbor's got excluded
    outright, see _merge_indentation_clusters's own docstring). A column's
    own zone, however wide, only ever reaches ONE side of the page's full
    observed span - a real gutter keeps its far edge well short of the
    OTHER column's own content - so requiring BOTH edges to be near the
    extremes never misclassifies an ordinary (even unusually wide) column
    zone, only a genuine full-width item spanning past every column."""
    if not items:
        return items
    all_x0 = min(it.bbox[0] for it in items)
    all_x2 = max(it.bbox[2] for it in items)
    span = all_x2 - all_x0
    if span <= 0:
        return items
    margin = max(span * 0.1, GUTTER_MIN_GAP)
    typical = [it for it in items
               if not (it.bbox[0] <= all_x0 + margin and it.bbox[2] >= all_x2 - margin)]
    return typical or items


GUTTER_MIN_GAP = 4.0             # min horizontal gap (pt) between two adjacent columns' text edges to be real
# Was 18.0, then 8.0 - each confirmed too strict for real documents. Traced directly against the user's own
# actual project (a manually-split two-column INDEX page: zones z00054/z00059, split into z00055-058 left /
# z00060-067 right) - that page's real, physical gutter measures exactly 6.4557pt (right column x0=216.264 minus
# left column x2=209.809), which the 8.0pt threshold still rejected entirely (_detect_columns returned ZERO
# columns, confirmed via direct tracing - not inferred), falling back to flat Y-sort and reproducing the exact
# reported interleaving on that real page. 4.0 leaves a safety margin below that real, measured value while
# staying meaningfully above zero/noise. Indentation-level "gaps" (indexsecondary nested under indexprimary,
# etc.) are unaffected regardless of how low this constant goes: because an entry typically extends across most
# of its column's own width, the computed gap for a same-column indent step is almost always strongly NEGATIVE
# (deeply overlapping, e.g. an indented entry's own x0 sitting well inside the previous entry's already-larger
# x2) rather than a small positive number near this threshold - only a GENUINE cross-column gutter is a small
# positive number in the first place, since by definition no content exists between two real columns.
GUTTER_MIN_MEMBERS = 2           # a candidate column's left-edge cluster must be respected by at least this many
# items outright - guards against a lone stray item (e.g. a label next to a page number) being misread as a real
# recurring column.
#
# Deliberately NOT also a proportional/fraction-of-total-items requirement (an earlier version of this file had
# one, GUTTER_MIN_LINE_FRACTION - confirmed as a real bug: a genuine two-column INDEX/BIBLIOGRAPHY page where one
# column is short - e.g. 10 left-column entries and only 1-2 in the right column, exactly the reported real-world
# case - has its minority column's true membership fraction fall below any reasonable threshold, so the fraction
# check silently rejected the column entirely and the WHOLE PAGE fell back to flat Y-sort, reproducing the exact
# "right column jumps ahead of a lower left column" bug. The real column/not-column discriminator is the GUTTER_
# MIN_GAP check below (a genuine physical gutter), not how many items happen to be on each side of it.

MINORITY_COLUMN_Y_MARGIN = 30.0  # pt - see _detect_columns' minority-column Y-overlap check just below: two
# columns on a real page rarely start at EXACTLY the same Y (a drop cap, a slightly taller first entry, or simply
# the user zoning the second column's first line a little higher/lower than the first column's own top edge) - a
# strict, zero-tolerance Y-RANGE OVERLAP test wrongly excluded a brand-new column's very first zone merely because
# it sat a few points above/below the other column's own topmost/bottommost content (confirmed: a real reported
# workflow had a new column's first zone at y0=50 sitting just 10pt above an existing column starting at y0=100,
# correctly NOT a header/footer - those sit much further away, well beyond this margin, in the page's own margins).


@dataclass
class Band:
    y0: float
    y1: float
    columns: int              # 1, 2, 3, ...
    column_bounds: list        # [(x0, x1), ...] one per column, left-to-right
    items: list = field(default_factory=list)      # LineInfo or Zone objects whose bbox falls in this band
    full_width_item: object = None                   # the item that opened this band as full-width, or None


def _merge_indentation_clusters(raw_clusters):
    """Merges adjacent raw left-edge clusters that sit close together into
    one combined column GROUP, before the member-count/fraction strength
    filter runs - fixes a real bug: a hierarchical multi-level list (Index
    Primary/Secondary/Territory entries, nested list items, etc.) has its
    OWN column fragmented into several distinct x0 clusters, one per
    indent level, each individually too small a minority to pass
    GUTTER_MIN_MEMBERS/GUTTER_MIN_LINE_FRACTION - which previously made
    _detect_columns() see "not enough strong clusters" and fall back to
    treating the whole page as single-column (a right-column zone with a
    smaller Y then sorted before a lower left-column zone - the exact
    reported bug on two-column INDEX/BIBLIOGRAPHY pages).

    Reuses GUTTER_MIN_GAP - the SAME constant/semantic _detect_columns's
    own final gap check already uses ("a real inter-column gutter") -
    rather than inventing a second magic number: two raw clusters merge
    into the same group whenever the next cluster's x0 is closer than a
    genuine gutter to the group's own current right extent (the MEDIAN
    right edge across every individual member seen in the group so far -
    not a mean/average, and not a per-cluster average - since index/list
    entries of any indent level typically extend to roughly the same
    column-relative right edge regardless of level, so the majority's own
    median is a stable, honest "how far right does this column reach"
    estimate). Only a gap that is itself at least a real gutter keeps two
    clusters as separate columns - a plain 2-column page (already one
    clean cluster per side) is completely unaffected, since there is
    nothing close enough to merge.

    Deliberately a MEDIAN, not a mean: an earlier version of this
    function averaged member right edges directly, which let a SINGLE
    unusually wide item (a full-width heading/rule sharing this column's
    own left margin, or simply one entry drawn wider than its neighbors)
    drag the computed right edge far past where the column actually ends,
    making a genuinely separate second column look like it falls inside
    that inflated "gap" and wrongly fusing two real columns into one -
    confirmed as a real regression (a heading spanning both columns, and
    separately a page where the two columns' zones simply weren't drawn
    to identical widths, both reproduced the original "columns
    interleaved" bug through this exact averaging weakness). The median
    of the WHOLE group's individual members (not an average of per-
    cluster averages) is insensitive to that one outlier as long as it
    isn't the majority of the group, which a real heading/rule never is."""
    groups = [[raw_clusters[0]]]
    for cluster in raw_clusters[1:]:
        prev_group = groups[-1]
        prev_members = [it for rc in prev_group for it in rc["members"]]
        prev_right = _median_right_edge(prev_members)
        if cluster["x0"] - prev_right < GUTTER_MIN_GAP:
            prev_group.append(cluster)
        else:
            groups.append([cluster])
    merged = []
    for group in groups:
        members = [it for rc in group for it in rc["members"]]
        merged.append({"x0": min(rc["x0"] for rc in group), "members": members})
    return merged


def _detect_columns(items):
    """Returns a list of column descriptors (each {"x0": cluster center,
    "members": [...]},  left-to-right) if the items show real evidence of
    2+ genuine columns, or [] for a single-column page. Clusters every
    item's OWN left edge (x0) via _cluster_1d - a real N-column layout
    produces N strong, recurring left-edge clusters, each separated from
    its neighbor by a real gap (that neighbor's own median right edge to
    this cluster's x0 - NOT the raw midpoint between two left edges, which
    would fall inside the left column's own content for anything but
    equal-width columns). Adjacent raw clusters representing indentation
    levels WITHIN one visual column (not a real second column) are merged
    first - see _merge_indentation_clusters. Genuine full-width items
    (see _exclude_full_width_outliers) are excluded from this clustering
    input entirely first - detect_bands still evaluates them for full-
    width band-opening against the ORIGINAL, unfiltered item list."""
    if not items:
        return []
    typical_items = _exclude_full_width_outliers(items)
    x0s = [it.bbox[0] for it in typical_items]
    clusters = _cluster_1d(x0s, tolerance=6.0)
    if len(clusters) < 2:
        return []
    raw_clusters = []
    for c in clusters:
        members = [it for it in typical_items if abs(it.bbox[0] - c) <= 6.0]
        if members:
            raw_clusters.append({"x0": c, "members": members})
    raw_clusters.sort(key=lambda d: d["x0"])
    candidates = _merge_indentation_clusters(raw_clusters) if len(raw_clusters) >= 2 else raw_clusters
    strong = [c for c in candidates if len(c["members"]) >= GUTTER_MIN_MEMBERS]
    # A column that has only just started (or is short by nature - e.g. a
    # bibliography's final, half-empty second column) can genuinely have
    # just ONE member. Real bug this fixes: an INDEX page with 10 left-
    # column entries and exactly 1 right-column entry (the spec's own
    # worked example) had that lone right-column zone excluded here,
    # leaving only 1 "strong" candidate, so the whole page fell back to
    # flat Y-sort. A too-small cluster is still admitted if it vertically
    # OVERLAPS the page's main (largest) cluster - i.e. it genuinely sits
    # BESIDE real column content, not merely a lone header/running-title/
    # footer/page-number zone sitting entirely above or below all body
    # content (spec: "do not accidentally place page number/running
    # header/footer into the wrong column order" - such zones' own Y-range
    # never overlaps the body column's, so they correctly stay excluded
    # and keep sorting by plain Y like today).
    if len(strong) < 2 and candidates:
        anchor = max(candidates, key=lambda c: len(c["members"]))
        anchor_y0 = min(it.bbox[1] for it in anchor["members"])
        anchor_y1 = max(it.bbox[3] for it in anchor["members"])
        strong_ids = {id(c) for c in strong}
        for c in candidates:
            if id(c) in strong_ids or c is anchor:
                continue
            c_y0 = min(it.bbox[1] for it in c["members"])
            c_y1 = max(it.bbox[3] for it in c["members"])
            if c_y0 < anchor_y1 + MINORITY_COLUMN_Y_MARGIN and anchor_y0 < c_y1 + MINORITY_COLUMN_Y_MARGIN:
                strong.append(c)
        if anchor not in strong:
            strong.append(anchor)
    if len(strong) < 2:
        return []
    strong.sort(key=lambda d: d["x0"])
    for i in range(len(strong) - 1):
        left_right_edge = _median_right_edge(strong[i]["members"])
        if strong[i + 1]["x0"] - left_right_edge < GUTTER_MIN_GAP:
            return []  # at least one adjacent pair isn't a real gap - too ambiguous, fall back to single-column
    return strong


def _column_bounds(columns, page_width):
    def gutter_between(left_col, right_col):
        left_right_edge = _median_right_edge(left_col["members"])
        return (left_right_edge + right_col["x0"]) / 2.0

    bounds = []
    for i, col in enumerate(columns):
        left = 0.0 if i == 0 else gutter_between(columns[i - 1], col)
        right = page_width if i == len(columns) - 1 else gutter_between(col, columns[i + 1])
        bounds.append((left, right))
    return bounds


def _is_full_width(bbox, columns, margin=4.0):
    """True if bbox spans the ENTIRE multi-column region - from the first
    column's own left edge to the last column's own right extent - not
    just one column's width."""
    first_x0 = columns[0]["x0"]
    last_right = max(it.bbox[2] for it in columns[-1]["members"])
    return bbox[0] <= first_x0 + margin and bbox[2] >= last_right - margin


def detect_bands(page_width, page_height, items) -> list:
    """Partitions `items` (already sorted top-to-bottom by the caller - e.g.
    pdf_block_detector.detect_lines's own output order, or a zone list
    sorted by (bbox[1], bbox[0])) into top-to-bottom Bands. Single-column
    page (or no detectable columns): one Band, one column, spanning the
    whole page width. Multi-column page: a full-width item closes any open
    multi-column band and starts a new 1-column full-width band by itself;
    between full-width items, an N-column band is opened using the page's
    own detected columns."""
    columns = _detect_columns(items)
    if not columns or not items:
        return [Band(y0=0.0, y1=page_height, columns=1,
                      column_bounds=[(0.0, page_width)], items=list(items))]

    column_bounds = _column_bounds(columns, page_width)
    n_cols = len(columns)
    bands = []
    current = None
    for it in items:
        if _is_full_width(it.bbox, columns):
            if current is not None:
                bands.append(current)
                current = None
            bands.append(Band(y0=it.bbox[1], y1=it.bbox[3], columns=1,
                               column_bounds=[(0.0, page_width)], items=[it], full_width_item=it))
        else:
            if current is None:
                current = Band(y0=it.bbox[1], y1=it.bbox[3], columns=n_cols,
                                column_bounds=column_bounds, items=[])
            current.items.append(it)
            current.y1 = max(current.y1, it.bbox[3])
    if current is not None:
        bands.append(current)
    bands.sort(key=lambda b: b.y0)
    return bands


def column_index_for_bbox(band: Band, bbox) -> int:
    """Which of `band`'s columns `bbox` (an item's own bbox, or any
    candidate bbox) belongs to, by center-x. Full-width bands always have
    exactly one column (index 0)."""
    if band.columns <= 1:
        return 0
    cx = (bbox[0] + bbox[2]) / 2.0
    best_i, best_d = 0, None
    for i, (cx0, cx1) in enumerate(band.column_bounds):
        col_center = (cx0 + cx1) / 2.0
        d = abs(cx - col_center)
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    return best_i


def column_bounds_for_bbox(band: Band, bbox) -> tuple:
    return band.column_bounds[column_index_for_bbox(band, bbox)]
