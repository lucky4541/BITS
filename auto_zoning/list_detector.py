"""Page-wide bullet/number/alpha list-run detection for the auto-zoning
engine - reuses core.xml_generator's own marker-pattern definitions
(_LIST_MARKER_PATTERNS) so a run's classified list_type matches EXACTLY what
the existing List zone attribute already expects (no new marker vocabulary).

Unlike core.table_extractor's _bullet_items_from_lines (scoped to one
already-known table-cell bbox, and a narrower/more conservative bullet-only
character set), this operates on a whole page/band/column's lines and
explicitly supports number/alpha-upper/alpha-lower/simple - and, critically,
BREAKS a run the moment a non-matching line interrupts it, so separate list
regions on the same page are never merged into one (per spec)."""
from dataclasses import dataclass, field

from core.xml_generator import _LIST_MARKER_PATTERNS
from core.text_extractor import dehyphenate_join

LIST_MIN_ITEMS = 2
LIST_INDENT_TOLERANCE = 4.0
LIST_ITEM_GAP_MAX_RATIO = 2.2   # a vertical gap more than this many median line-heights ends a run
# A marker line indented at least this much further right than the CURRENT
# run's own x0 is a nested SUB-list under the item directly above it, never
# a sibling top-level run of its own (spec: "EPUBForge - Complete Semantic
# Auto-Zone" Part T - "Detect indentation levels... must preserve nesting.
# Do not flatten nested lists"). Deliberately larger than LIST_INDENT_
# TOLERANCE (an ordinary wrapped-continuation-line indent is only a few
# points beyond the marker's own x0; a genuine sub-list level is a much
# bigger, deliberate indent step - the same kind of gap index_auto_zone.py's
# own indentation-level clustering already relies on for an analogous
# "is this really a deeper level, or just noise" question).
NESTING_INDENT_MIN = 12.0


@dataclass
class ListItemSpan:
    lines: list                  # [LineInfo, ...] marker line + any wrapped continuation lines
    text: str = ""                 # marker-stripped, dehyphenated item text
    children: list = field(default_factory=list)   # nested ListRun(s) directly under this item, if any
    _nested_lines: list = field(default_factory=list, repr=False)  # staging buffer, consumed by detect_list_runs

    @property
    def bbox(self):
        x0 = min(li.bbox[0] for li in self.lines)
        y0 = min(li.bbox[1] for li in self.lines)
        x1 = max(li.bbox[2] for li in self.lines)
        y1 = max(li.bbox[3] for li in self.lines)
        return (x0, y0, x1, y1)


@dataclass
class ListRun:
    list_type: str
    items: list = field(default_factory=list)   # [ListItemSpan, ...]

    @property
    def bbox(self):
        x0 = min(it.bbox[0] for it in self.items)
        y0 = min(it.bbox[1] for it in self.items)
        x1 = max(it.bbox[2] for it in self.items)
        y1 = max(it.bbox[3] for it in self.items)
        return (x0, y0, x1, y1)


def _classify_marker(text: str):
    for list_type, pat in _LIST_MARKER_PATTERNS.items():
        if pat.match(text):
            return list_type
    return None


def _start_item(li, list_type):
    pat = _LIST_MARKER_PATTERNS[list_type]
    m = pat.match(li.text)
    stripped = li.text[m.end():] if m else li.text
    return ListItemSpan(lines=[li], text=stripped)


def detect_list_runs(lines: list) -> list:
    """`lines`: one band/column's LineInfo objects, already in top-to-bottom
    reading order. Returns every maximal run of consistently-marked,
    same-indent lines as a separate ListRun - a non-marker line breaks the
    current run (becomes ordinary paragraph material for the caller
    instead), and a marker of a DIFFERENT list_type also starts a new run
    rather than extending the current one (spec: "If there are separate
    list regions... create separate list zones").

    A marker line indented at least NESTING_INDENT_MIN beyond the CURRENT
    run's own x0 is buffered onto the current item's own `_nested_lines`
    instead of breaking/starting a new top-level run, then this SAME
    function is called recursively on each item's buffered lines (at their
    own new indentation baseline) to build that item's `.children` -
    supporting nesting to any depth, never flattened (spec Part T)."""
    if not lines:
        return []
    line_heights = [li.bbox[3] - li.bbox[1] for li in lines if li.bbox[3] > li.bbox[1]]
    median_h = sorted(line_heights)[len(line_heights) // 2] if line_heights else 10.0

    runs = []
    current_items = None
    current_type = None
    current_x0 = None
    prev_line = None

    def flush():
        nonlocal current_items, current_type, current_x0
        if current_items and len(current_items) >= LIST_MIN_ITEMS:
            runs.append(ListRun(list_type=current_type, items=current_items))
        current_items, current_type, current_x0 = None, None, None

    for li in lines:
        marker_type = _classify_marker(li.text)
        gap_too_big = (prev_line is not None and (li.bbox[1] - prev_line.bbox[3]) > median_h * LIST_ITEM_GAP_MAX_RATIO)

        extends_run = (marker_type is not None and current_items is not None
                       and current_type == marker_type
                       and abs(li.bbox[0] - current_x0) <= LIST_INDENT_TOLERANCE
                       and not gap_too_big)
        is_nested_marker = (marker_type is not None and current_items is not None and not extends_run
                             and not gap_too_big and li.bbox[0] > current_x0 + NESTING_INDENT_MIN)
        is_continuation = (marker_type is None and current_items is not None and not gap_too_big
                            and li.bbox[0] > current_x0 + LIST_INDENT_TOLERANCE)

        if is_nested_marker:
            current_items[-1]._nested_lines.append(li)
        elif extends_run:
            current_items.append(_start_item(li, marker_type))
        elif is_continuation:
            current_items[-1].lines.append(li)
        elif marker_type is not None:
            flush()
            current_items = [_start_item(li, marker_type)]
            current_type = marker_type
            current_x0 = li.bbox[0]
        else:
            flush()
        prev_line = li
    flush()

    for run in runs:
        for item in run.items:
            item.text = dehyphenate_join([l.text for l in item.lines])
            if item._nested_lines:
                item.children = detect_list_runs(item._nested_lines)
                item._nested_lines = []
    return runs
