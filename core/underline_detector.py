"""Character-level text-decoration (underline / strike-through) detection
from real page geometry.

Underlines are almost never a font property in a PDF - they are separate
vector objects (a stroked line, a thin filled rectangle, a quad) or an
Underline/StrikeOut annotation, drawn independently of the glyphs they sit
under. This module finds every such candidate segment on a page ONCE
(cached), then maps each segment onto the EXACT characters whose own glyph
boxes it actually runs beneath:

  * a character is decorated only if the segment covers the majority of
    that character's own advance width - never because the segment touches
    "the word" somewhere;
  * a space is decorated only when the same continuous segment runs through
    it AND decorates real characters on both sides (so an underline
    interrupted at a space stays two separate ranges);
  * a segment whose length is mostly NOT under text (a table rule, a
    footnote separator, a heading rule spanning the column) is rejected as a
    rule, not an underline;
  * the segment's vertical distance from the line's baseline decides
    underline (just below the baseline) vs strike-through (through the
    x-height) vs unrelated (anything else).

Scanned pages (no vector text) use the raster path at the bottom of this
module: underline ink runs are found in the rendered line image and mapped to
glyph columns obtained from the same image.

The module never changes extracted characters - it only answers "which
character indices carry which decoration", plus the evidence for the debug
overlay."""
from collections import OrderedDict
from dataclasses import dataclass, field

# A decoration segment thicker than this fraction of the font size is a
# box/rule/graphic, not a text decoration.
MAX_THICKNESS_RATIO = 0.22
MAX_THICKNESS_PT = 3.5
# Shortest segment considered at all (a single narrow glyph such as "i" or
# "l" underlined on its own is ~2pt wide at body size).
MIN_SEGMENT_LENGTH = 1.0
# Max |dy| for a stroked line to count as horizontal.
HORIZONTAL_TOLERANCE = 0.6
# Two collinear pieces closer than this are the same visual segment (PDF
# producers often draw one underline per glyph run or per word).
MERGE_GAP = 0.75
MERGE_DY = 0.8
# Vertical windows, as a fraction of font size, measured as
# (segment_y - baseline_y) - positive is BELOW the baseline (PDF y grows down).
UNDERLINE_BAND = (-0.06, 0.42)
STRIKE_BAND = (-0.50, -0.14)
# A character is decorated when the segment covers at least this fraction
# of the character's own advance width.
CHAR_COVERAGE_MIN = 0.5
# A segment is a text decoration only if at least this much of its length
# lies under decorated characters; anything sparser is a rule.
SEGMENT_TEXT_COVERAGE_MIN = 0.55
# How far (fraction of font size) a segment may overshoot the first/last
# decorated character before it starts counting against coverage.
OVERSHOOT_ALLOWANCE = 0.35

# Glyphs that ARE horizontal strokes - a segment that only lines up with
# these is the glyph's own ink (raster path) or a rule beside it, never a
# decoration of it.
LINE_LIKE_GLYPHS = set("-_\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u203E\u2500")

_SEGMENT_CACHE_MAXSIZE = 16
_segment_cache = OrderedDict()


@dataclass
class DecorationSegment:
    x0: float
    x1: float
    y: float
    thickness: float
    source: str  # "line" | "rect" | "quad" | "annot-underline" | "annot-strike" | "raster"

    @property
    def length(self) -> float:
        return self.x1 - self.x0

    def to_dict(self) -> dict:
        return {"x0": self.x0, "x1": self.x1, "y": self.y, "thickness": self.thickness, "source": self.source}


@dataclass
class DecorationEvidence:
    """One accepted segment and exactly the character indices it was mapped
    to - what the debug overlay draws and what the regression tests assert."""
    kind: str                      # "underline" | "strike"
    segment: DecorationSegment
    char_indices: list = field(default_factory=list)
    baseline_distance: float = 0.0
    text_coverage: float = 0.0
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "segment": self.segment.to_dict(), "char_indices": list(self.char_indices),
                "baseline_distance": round(self.baseline_distance, 3),
                "text_coverage": round(self.text_coverage, 3), "confidence": round(self.confidence, 3)}


# --------------------------------------------------------------- page scan
def _page_key(page):
    parent = getattr(page, "parent", None)
    return ((getattr(parent, "name", None) or None) or id(parent), getattr(page, "number", None))


def clear_cache():
    _segment_cache.clear()


def _segments_from_drawings(page) -> list:
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        width = d.get("width") or 0.0
        for item in d.get("items", []) or []:
            if not item:
                continue
            op = item[0]
            try:
                if op == "l" and len(item) >= 3:
                    p0, p1 = item[1], item[2]
                    if abs(p1.y - p0.y) <= HORIZONTAL_TOLERANCE and abs(p1.x - p0.x) >= MIN_SEGMENT_LENGTH:
                        x0, x1 = sorted((float(p0.x), float(p1.x)))
                        out.append(DecorationSegment(x0, x1, (p0.y + p1.y) / 2.0, float(width or 0.5), "line"))
                elif op == "re" and len(item) >= 2:
                    r = item[1]
                    h = abs(r.y1 - r.y0)
                    w = abs(r.x1 - r.x0)
                    if w >= MIN_SEGMENT_LENGTH and h <= MAX_THICKNESS_PT and w > h * 1.5:
                        thickness = max(h, float(width or 0.0)) if d.get("type") in ("s", "fs") else h
                        out.append(DecorationSegment(min(r.x0, r.x1), max(r.x0, r.x1), (r.y0 + r.y1) / 2.0,
                                                     max(thickness, 0.1), "rect"))
                elif op == "qu" and len(item) >= 2:
                    q = item[1]
                    r = q.rect
                    if r.width >= MIN_SEGMENT_LENGTH and r.height <= MAX_THICKNESS_PT and r.width > r.height * 1.5:
                        out.append(DecorationSegment(r.x0, r.x1, (r.y0 + r.y1) / 2.0, max(r.height, 0.1), "quad"))
            except Exception:
                continue
    return out


def _segments_from_annotations(page) -> list:
    out = []
    try:
        annots = list(page.annots() or [])
    except Exception:
        return out
    for annot in annots:
        try:
            kind = (annot.type[1] or "").lower()
        except Exception:
            continue
        if kind not in ("underline", "strikeout"):
            continue
        verts = annot.vertices or []
        for i in range(0, len(verts) - 3, 4):
            quad = verts[i:i + 4]
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            top, bottom = min(ys), max(ys)
            x0, x1 = min(xs), max(xs)
            if kind == "underline":
                # Viewers draw the annotation line ~1/7 of the quad height
                # above its bottom edge.
                y = bottom - (bottom - top) / 7.0
                out.append(DecorationSegment(x0, x1, y, 1.0, "annot-underline"))
            else:
                out.append(DecorationSegment(x0, x1, (top + bottom) / 2.0, 1.0, "annot-strike"))
    return out


def merge_segments(segments: list) -> list:
    """Joins collinear, touching pieces into one visual segment and
    removes exact duplicates (an annotation's own appearance stream shows
    up in get_drawings too)."""
    segs = sorted(segments, key=lambda s: (round(s.y, 1), s.x0))
    merged = []
    for s in segs:
        target = None
        for m in merged:
            if abs(m.y - s.y) <= MERGE_DY and s.x0 <= m.x1 + MERGE_GAP and s.x1 >= m.x0 - MERGE_GAP:
                target = m
                break
        if target is None:
            merged.append(DecorationSegment(s.x0, s.x1, s.y, s.thickness, s.source))
        else:
            target.x0 = min(target.x0, s.x0)
            target.x1 = max(target.x1, s.x1)
            target.thickness = max(target.thickness, s.thickness)
            if target.source != s.source and not target.source.startswith("annot"):
                target.source = s.source if s.source.startswith("annot") else target.source
    return merged


def page_decoration_segments(page) -> list:
    """Every candidate decoration segment on the page (cached per page)."""
    key = _page_key(page)
    cached = _segment_cache.get(key)
    if cached is not None:
        _segment_cache.move_to_end(key)
        return cached
    segs = merge_segments(_segments_from_drawings(page) + _segments_from_annotations(page))
    # Searchable scan: the visible underlines live in the page image, while
    # the (invisible) native text layer still supplies exact glyph boxes.
    try:
        from core.ocr.style_detector import page_is_image_dominated
        if page_is_image_dominated(page) and _text_is_mostly_invisible(page):
            r = page.rect
            segs = segs + raster_segments_for_region(page, (r.x0, r.y0, r.x1, r.y1))
    except Exception:
        pass
    _segment_cache[key] = segs
    while len(_segment_cache) > _SEGMENT_CACHE_MAXSIZE:
        _segment_cache.popitem(last=False)
    return segs


# Glyphs whose own ink below / at the baseline (serif feet, descender
# tails) looks like a short underline in a page image.
SERIF_FOOT_GLYPHS = set("ilrtfhkmnpqxyzIJLTEFPRDBHKMNXYZ1j")


def _single_glyph_underline(c, box, seg) -> bool:
    """An image ink run under exactly ONE glyph is an underline only when it
    spans the whole glyph (a serif foot or descender tail is narrower) and
    the glyph has no foot / descender of its own."""
    if not c or c in DESCENDER_GLYPHS or c in SERIF_FOOT_GLYPHS or box is None:
        return False
    width = max(box[2] - box[0], 0.01)
    return _overlap(box[0], box[2], seg.x0, seg.x1) / width >= 0.9


def _text_is_mostly_invisible(page) -> bool:
    """True for a searchable scan: the page's text layer is invisible (render
    mode 3 / zero opacity) and the visible letters are in the image. A page
    whose glyphs are really drawn (a digital page with a full-page
    background, a large figure or a tinted panel) has its underlines as
    vector objects - scanning its image would read serif feet and descenders
    of single letters as underlines."""
    try:
        trace = page.get_texttrace()
    except Exception:
        return True
    hidden = shown = 0
    for t in trace:
        n = len(t.get("chars") or ())
        if t.get("type") == 3 or (t.get("opacity") is not None and t.get("opacity") <= 0.01):
            hidden += n
        else:
            shown += n
    if hidden + shown == 0:
        return True
    return hidden >= 0.5 * (hidden + shown)


# ------------------------------------------------------- char assignment
def _median(values):
    vals = sorted(values)
    if not vals:
        return 0.0
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _char_box(ch):
    b = ch.get("bbox")
    if not b or len(b) < 4:
        return None
    return float(b[0]), float(b[1]), float(b[2]), float(b[3])


def _is_space(ch) -> bool:
    c = ch.get("c", "")
    return not c or c.isspace()


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_decorations(chars: list, segments: list, font_size: float = None):
    """chars: one visual line's character dicts in reading order (rawdict
    shape: {"c", "bbox", "origin"}). segments: candidate DecorationSegments
    (already page-scoped; filtered here by geometry).

    Returns (underline_flags, strike_flags, evidence_list)."""
    n = len(chars)
    underline = [False] * n
    strike = [False] * n
    evidence = []
    if not chars or not segments:
        return underline, strike, evidence

    boxes = [_char_box(ch) for ch in chars]
    ink = [i for i in range(n) if boxes[i] is not None and not _is_space(chars[i])]
    if not ink:
        return underline, strike, evidence
    heights = [boxes[i][3] - boxes[i][1] for i in ink]
    size = float(font_size) if font_size else _median(heights) / 1.15 if heights else 10.0
    size = max(size, 2.0)
    baselines = []
    for i in ink:
        origin = chars[i].get("origin")
        baselines.append(float(origin[1]) if origin and len(origin) >= 2 else boxes[i][3] - 0.2 * size)
    baseline = _median(baselines)
    line_x0 = min(boxes[i][0] for i in ink)
    line_x1 = max(boxes[i][2] for i in ink)
    max_thickness = min(MAX_THICKNESS_PT, max(0.8, size * MAX_THICKNESS_RATIO))

    for seg in segments:
        trusted = seg.source.startswith(("annot", "raster"))
        if seg.thickness > max_thickness and not trusted:
            continue
        if seg.x1 <= line_x0 - 0.5 or seg.x0 >= line_x1 + 0.5:
            continue
        dy = (seg.y - baseline) / size
        if seg.source == "annot-strike":
            kind = "strike"
        elif seg.source == "annot-underline":
            kind = "underline"
        elif seg.source == "raster-underline":
            # Image-derived: kind is known, but it must still sit just under
            # THIS line (not under the line above it).
            if not (-0.15 <= dy <= 0.65):
                continue
            kind = "underline"
        elif seg.source == "raster-strike":
            if not (-0.65 <= dy <= -0.05):
                continue
            kind = "strike"
        elif UNDERLINE_BAND[0] <= dy <= UNDERLINE_BAND[1]:
            kind = "underline"
        elif STRIKE_BAND[0] <= dy <= STRIKE_BAND[1]:
            kind = "strike"
        else:
            continue

        hit = []
        covered = 0.0
        for i in ink:
            bx0, _, bx1, _ = boxes[i]
            width = max(bx1 - bx0, 0.01)
            ov = _overlap(bx0, bx1, seg.x0, seg.x1)
            if ov / width >= CHAR_COVERAGE_MIN:
                hit.append(i)
                covered += ov
        if not hit:
            continue
        # Spaces strictly between two decorated chars, fully under the segment.
        hit_set = set(hit)
        first, last = min(hit), max(hit)
        for i in range(first + 1, last):
            if i in hit_set or boxes[i] is None:
                continue
            if _is_space(chars[i]):
                bx0, _, bx1, _ = boxes[i]
                width = max(bx1 - bx0, 0.01)
                if _overlap(bx0, bx1, seg.x0, seg.x1) / width >= CHAR_COVERAGE_MIN:
                    hit_set.add(i)
                    covered += _overlap(bx0, bx1, seg.x0, seg.x1)
        hit = sorted(hit_set)
        solo = [i for i in hit if not _is_space(chars[i])]
        if seg.source.startswith("raster") and len(solo) == 1 and \
                not _single_glyph_underline(chars[solo[0]].get("c", ""), boxes[solo[0]], seg):
            continue
        if all(chars[i].get("c", "") in LINE_LIKE_GLYPHS for i in hit if not _is_space(chars[i])):
            # The "segment" is the glyph itself (an em dash, an underscore).
            continue

        # Rule rejection: how much of the segment lies under the decorated
        # characters (allowing a small overshoot at either end, which real
        # underlines routinely have).
        allowance = OVERSHOOT_ALLOWANCE * size
        span_x0 = min(boxes[i][0] for i in hit) - allowance
        span_x1 = max(boxes[i][2] for i in hit) + allowance
        inside = _overlap(seg.x0, seg.x1, span_x0, span_x1)
        text_coverage = covered / max(seg.length, 0.01)
        effective = inside / max(seg.length, 0.01)
        if not seg.source.startswith("annot") and effective < SEGMENT_TEXT_COVERAGE_MIN:
            continue

        ideal = 0.12 if kind == "underline" else -0.3
        conf = 0.98 if seg.source.startswith("annot") else 0.95
        conf -= min(0.3, abs(dy - ideal) * 0.8)
        conf -= max(0.0, 0.9 - effective) * 0.5
        evidence.append(DecorationEvidence(kind=kind, segment=seg, char_indices=hit, baseline_distance=dy,
                                           text_coverage=min(1.0, text_coverage), confidence=max(0.05, conf)))
        flags = underline if kind == "underline" else strike
        for i in hit:
            flags[i] = True
    return underline, strike, evidence


def line_decorations(page, chars: list, font_size: float = None):
    """Convenience wrapper used by core.text_extractor.extract_lines: only
    segments vertically near this line are considered."""
    segs = page_decoration_segments(page) if page is not None else []
    if not segs or not chars:
        n = len(chars or [])
        return [False] * n, [False] * n, []
    boxes = [_char_box(ch) for ch in chars if _char_box(ch)]
    if not boxes:
        n = len(chars)
        return [False] * n, [False] * n, []
    y0 = min(b[1] for b in boxes)
    y1 = max(b[3] for b in boxes)
    pad = (y1 - y0) * 0.5
    near = [s for s in segs if y0 - pad <= s.y <= y1 + pad]
    return assign_decorations(chars, near, font_size)


def flags_to_ranges(flags: list) -> list:
    """[(start, end), ...] half-open runs of True - separate runs stay
    separate."""
    ranges, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            ranges.append((start, i))
            start = None
    if start is not None:
        ranges.append((start, len(flags)))
    return ranges


def debug_page_decorations(page, bbox=None) -> list:
    """Every accepted decoration on the page (or inside bbox) with the
    exact character boxes it was assigned to - for the debug overlay."""
    from core.text_extractor import _get_rawdict
    out = []
    raw = _get_rawdict(page)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                chars.extend(span.get("chars", []))
            if bbox is not None:
                chars = [c for c in chars if _center_in(c.get("bbox"), bbox)]
            if not chars:
                continue
            sizes = [s.get("size", 0) for s in line.get("spans", []) if s.get("size")]
            _, _, ev = line_decorations(page, chars, _median(sizes) if sizes else None)
            for e in ev:
                out.append({"kind": e.kind, "segment": e.segment.to_dict(), "confidence": e.confidence,
                            "chars": [{"c": chars[i].get("c", ""), "bbox": list(chars[i]["bbox"])}
                                      for i in e.char_indices]})
    return out


def _center_in(cb, bbox, tol=1.0) -> bool:
    if not cb:
        return False
    cx, cy = (cb[0] + cb[2]) / 2.0, (cb[1] + cb[3]) / 2.0
    return bbox[0] - tol <= cx <= bbox[2] + tol and bbox[1] - tol <= cy <= bbox[3] + tol


# ------------------------------------------------------------- raster path
def _runs(mask_1d):
    runs, start = [], None
    for x, v in enumerate(mask_1d):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(mask_1d)))
    return runs


def _gaps_bridged(ink, g, x_top, baseline) -> int:
    """Number of separate empty-column gaps (no ink in the x-height band
    apart from the candidate's own rows) the candidate run crosses."""
    band = ink[x_top:baseline + 1, g["x0"]:g["x1"]].copy()
    r0 = max(0, g["y0"] - x_top)
    r1 = max(0, g["y1"] - x_top + 1)
    band[r0:r1, :] = False
    empty = band.sum(axis=0) == 0
    gaps, prev = 0, False
    for k, e in enumerate(empty):
        if e and not prev and 0 < k:
            gaps += 1
        prev = e
    # A gap touching the right end is not "between" glyphs.
    if len(empty) and empty[-1]:
        gaps -= 1
    return gaps


def raster_segments_in_line(ink, scale: float = 1.0, origin=(0.0, 0.0)):
    """Underline/strike ink runs inside ONE rendered text line.

    ink: 2-D boolean numpy array (True = ink) covering one line.
    Returns (segments_pdf, baseline_row, top_row, segments_px). A candidate
    is a horizontal ink run that is (a) long relative to the line height,
    (b) thin, and (c) sits at/below the baseline (underline) or in the
    x-height band (strike) - glyph strokes such as a 'T' bar or an 'e'
    crossbar fail (a) or (b), descenders fail (a)."""
    import numpy as np
    if ink is None or ink.size == 0:
        return [], 0, 0, []
    rows = ink.sum(axis=1)
    if rows.max() == 0:
        return [], 0, 0, []
    h, _w = ink.shape
    ink_rows = np.nonzero(rows)[0]
    top, bottom = int(ink_rows[0]), int(ink_rows[-1])
    line_h = max(1, bottom - top + 1)
    run_counts = [len(_runs(ink[y])) for y in range(h)]
    # The x-height body: the contiguous block of well-inked rows around the
    # row crossing the MOST glyph strokes (an underline row crosses few).
    peak_row = max(range(h), key=lambda y: (run_counts[y], rows[y]))
    floor = 0.3 * rows[peak_row]
    x_top = peak_row
    while x_top - 1 >= 0 and rows[x_top - 1] >= floor:
        x_top -= 1
    baseline = peak_row
    while baseline + 1 < h and rows[baseline + 1] >= floor:
        baseline += 1
    x_height = max(2, baseline - x_top + 1)
    min_run = max(3, int(0.35 * x_height))
    max_thick = max(2, int(0.2 * x_height))
    # An underline drawn touching the baseline extends the block: peel off
    # trailing rows that are dominated by one long run.
    peel = 0
    while peel < max_thick and baseline - 1 > x_top:
        longest = max((b - a for a, b in _runs(ink[baseline])), default=0)
        if longest >= max(min_run, int(0.6 * x_height)) and run_counts[baseline] <= max(3, run_counts[peak_row] // 3):
            baseline -= 1
            peel += 1
        else:
            break

    cand = []
    for y in range(top, h):
        for x0, x1 in _runs(ink[y]):
            if x1 - x0 >= min_run:
                cand.append((y, x0, x1))
    groups = []
    for y, x0, x1 in sorted(cand):
        for g in groups:
            ov = min(x1, g["x1"]) - max(x0, g["x0"])
            if y - g["y1"] <= 1 and ov >= 0.6 * max(x1 - x0, g["x1"] - g["x0"]):
                g["y1"] = y
                g["x0"] = min(g["x0"], x0)
                g["x1"] = max(g["x1"], x1)
                g["lens"].append(x1 - x0)
                break
        else:
            groups.append({"y0": y, "y1": y, "x0": x0, "x1": x1, "lens": [x1 - x0]})
    seg_px = []
    for g in groups:
        longest = max(g["lens"])
        core = [k for k, ln in enumerate(g["lens"]) if ln >= 0.8 * longest]
        g["core_y0"] = g["y0"] + core[0]
        g["core_y1"] = g["y0"] + core[-1]
        thick = core[-1] - core[0] + 1
        mid = (g["core_y0"] + g["core_y1"]) / 2.0
        if thick > max_thick:
            continue
        if mid > baseline:
            # A real underline is a separate stroke: the row directly above
            # it is mostly empty (or only glyph bottoms when it touches the
            # baseline). The bottom of a descender bowl is not - the curve
            # above it is solid ink over most of the same columns.
            # Rectangular stroke: every row of it has ~the same length, and
            # nothing tapers off below it (a bowl bottom does both).
            if len(g["lens"]) >= 2 and min(g["lens"]) < 0.8 * max(g["lens"]):
                continue
            below = g["y1"] + 1
            if below < h and g["x1"] > g["x0"] and ink[below, g["x0"]:g["x1"]].mean() >= 0.3:
                continue
            above = g["y0"] - 1
            if above >= 0:
                frac = ink[above, g["x0"]:g["x1"]].mean() if g["x1"] > g["x0"] else 1.0
                if frac >= 0.7:
                    continue
            g["kind"] = "underline"
        elif x_top + 0.3 * x_height < mid < baseline - 0.2 * x_height and longest >= 1.1 * x_height \
                and _gaps_bridged(ink, g, x_top, baseline) >= 2:
            # A glyph's own bar (H, e, t, f, serifs) never bridges two
            # empty inter-glyph columns; a strike through text does.
            g["kind"] = "strike"
        else:
            continue
        seg_px.append(g)
    ox, oy = origin
    segs = [DecorationSegment(ox + g["x0"] / scale, ox + g["x1"] / scale,
                              oy + (g["core_y0"] + g["core_y1"] + 1) / 2.0 / scale,
                              (g["core_y1"] - g["core_y0"] + 1) / scale, "raster-" + g["kind"])
            for g in seg_px]
    return segs, baseline, top, seg_px


def raster_segments_for_region(page, bbox, dpi: int = 300) -> list:
    """Decoration segments for a page region whose typography lives in an
    image (a searchable scan). Splits the region into text-line bands by
    row projection and scans each band with raster_segments_in_line."""
    import numpy as np
    try:
        from core.ocr.style_detector import _crop_binary
        binary = _crop_binary(page, bbox, dpi=dpi)
    except Exception:
        return []
    if binary is None or binary.size == 0:
        return []
    ink = binary > 0
    scale = dpi / 72.0
    rows = ink.sum(axis=1)
    bands = _runs(rows > max(1, 0.002 * ink.shape[1]))
    out = []
    for y0, y1 in bands:
        segs, _, _, _ = raster_segments_in_line(ink[y0:y1], scale, (bbox[0], bbox[1] + y0 / scale))
        out.extend(segs)
    return out


def _expected_widths(word: str) -> list:
    """Relative advance widths for the characters of `word` - a generic
    proportional-font width table (PyMuPDF's built-in Helvetica metrics),
    used only to split a run of touching glyphs between its characters."""
    try:
        import fitz
        return [max(fitz.get_text_length(c, fontname="helv", fontsize=10.0), 1.5) for c in word]
    except Exception:
        return [5.0] * len(word)


def align_glyph_runs(word: str, runs: list, monospace: bool = False) -> list:
    """Distributes a word's characters over its ink column runs.

    runs: [(x0, x1), ...] glyph column runs of ONE word (left to right).
    Touching glyphs (serif fonts, bold, low resolution) merge into one run,
    so a run may hold several characters; a broken glyph may split into
    several runs. Dynamic programming assigns consecutive character groups
    to consecutive run groups, minimising the difference between each
    group's measured width and its expected (relative) width; a run group
    holding several characters is then split proportionally to those
    characters' expected widths. Returns one (x0, x1) per character."""
    m = len(word)
    if m == 0 or not runs:
        return [None] * m
    total_px = runs[-1][1] - runs[0][0]
    widths = [1.0] * m if monospace else _expected_widths(word)
    unit = total_px / max(sum(widths), 0.01)
    k = len(runs)
    INF = float("inf")
    # cost[i][j]: best cost assigning first i chars to first j runs
    cost = [[INF] * (k + 1) for _ in range(m + 1)]
    back = [[None] * (k + 1) for _ in range(m + 1)]
    cost[0][0] = 0.0
    prefix = [0.0]
    for w in widths:
        prefix.append(prefix[-1] + w)
    for i in range(1, m + 1):
        for j in range(1, k + 1):
            for pi in range(max(0, i - 6), i):
                for pj in range(max(0, j - 6), j):
                    if cost[pi][pj] == INF:
                        continue
                    # chars pi..i-1 <-> runs pj..j-1 ; many-to-many only if one side is 1
                    if (i - pi) > 1 and (j - pj) > 1:
                        continue
                    measured = runs[j - 1][1] - runs[pj][0]
                    expected = (prefix[i] - prefix[pi]) * unit
                    c = cost[pi][pj] + abs(measured - expected) / max(expected, 1.0)
                    if c < cost[i][j]:
                        cost[i][j] = c
                        back[i][j] = (pi, pj)
    if cost[m][k] == INF:
        x0, x1 = runs[0][0], runs[-1][1]
        out, acc = [], x0
        for w in widths:
            nxt = acc + w * unit
            out.append((acc, nxt))
            acc = nxt
        return out
    pieces = []
    i, j = m, k
    while i > 0:
        pi, pj = back[i][j]
        pieces.append((pi, i, pj, j))
        i, j = pi, pj
    out = [None] * m
    for pi, i, pj, j in reversed(pieces):
        x0, x1 = runs[pj][0], runs[j - 1][1]
        span_w = sum(widths[pi:i])
        acc = x0
        for c in range(pi, i):
            nxt = acc + (x1 - x0) * widths[c] / max(span_w, 0.01)
            out[c] = (acc, nxt)
            acc = nxt
    return out


def _looks_monospaced(word_spans) -> bool:
    """True when glyph runs inside words start at a constant pitch."""
    deltas = []
    for runs in word_spans:
        for a, b in zip(runs, runs[1:]):
            deltas.append(b[0] - a[0])
    if len(deltas) < 6:
        return False
    deltas.sort()
    med = deltas[len(deltas) // 2]
    if med <= 0:
        return False
    p10 = deltas[len(deltas) // 10]
    p90 = deltas[(len(deltas) * 9) // 10]
    return (p90 - p10) / med < 0.3


def _line_is_monospaced(glyph_runs) -> bool:
    starts = [r[0] for r in glyph_runs]
    deltas = sorted(b - a for a, b in zip(starts, starts[1:]))
    if len(deltas) < 6:
        return False
    med = deltas[len(deltas) // 2]
    if med <= 0:
        return False
    # Most consecutive glyphs one pitch apart (word spaces are multiples).
    near = sum(1 for d in deltas if abs(d - med) <= 0.12 * med)
    return near >= 0.75 * len(deltas)


def split_runs_into_words(glyph_runs, text: str, words: list) -> list:
    """Groups glyph column runs into the text's words. Word boundaries are
    placed at the inter-run gaps closest to where the text's own expected
    (relative-width) layout puts each space, weighted toward wider gaps -
    never simply "the N largest gaps", which fails for monospaced fonts
    (gaps between narrow letters there are as wide as word spaces)."""
    if len(words) <= 1 or len(glyph_runs) <= 1:
        return [glyph_runs] + [[] for _ in range(len(words) - 1)]
    mono = _line_is_monospaced(glyph_runs)
    stripped = text.strip()
    widths = [1.0] * len(stripped) if mono else _expected_widths(stripped)
    total_expected = sum(widths) or 1.0
    x_start, x_end = glyph_runs[0][0], glyph_runs[-1][1]
    scale = (x_end - x_start) / total_expected
    # expected x of each space that separates two words
    expected_cuts, acc, in_space = [], 0.0, False
    for ch, w in zip(stripped, widths):
        if ch.isspace():
            if not in_space:
                expected_cuts.append(x_start + (acc + w / 2.0) * scale)
            in_space = True
        else:
            in_space = False
        acc += w
    gaps = [((glyph_runs[i][1] + glyph_runs[i + 1][0]) / 2.0, glyph_runs[i + 1][0] - glyph_runs[i][1], i)
            for i in range(len(glyph_runs) - 1)]
    max_gap = max(g[1] for g in gaps) or 1.0
    used, cut_after = set(), []
    for ex in expected_cuts[: len(words) - 1]:
        best, best_score = None, None
        for center, size, i in gaps:
            if i in used:
                continue
            score = abs(center - ex) / max(scale, 0.01) - 2.0 * (size / max_gap)
            if best_score is None or score < best_score:
                best, best_score = i, score
        if best is not None:
            used.add(best)
            cut_after.append(best)
    cut_after.sort()
    spans, start = [], 0
    for c in cut_after + [len(glyph_runs) - 1]:
        spans.append(glyph_runs[start:c + 1])
        start = c + 1
    while len(spans) < len(words):
        spans.append([])
    return spans


# Letters whose descender tail can itself be a flat horizontal stroke in
# some typefaces (Courier "g", "j" ...). In the IMAGE path a segment that
# lines up with nothing but one of these is ambiguous and is not reported.
DESCENDER_GLYPHS = set("gjpqyçŋ")


def raster_line_decorations(ink, text: str, scale: float = 1.0, origin=(0.0, 0.0)):
    """Scanned-page (image-only, OCR text) decoration detection for ONE
    line: no native glyph boxes exist, so glyph columns are recovered from
    the same image after the decoration ink is erased, distributed over the
    text's characters word by word, and each decoration run is assigned to
    exactly the glyph boxes it covers.

    ink: 2-D boolean array (True = ink) of the line band.
    Returns (char_boxes_pdf, underline_flags, strike_flags, segments_pdf)."""
    n = len(text)
    underline = [False] * n
    strike = [False] * n
    if ink is None or ink.size == 0 or n == 0:
        return [None] * n, underline, strike, []
    segs, baseline, top, seg_px = raster_segments_in_line(ink, scale, origin)
    clean = ink.copy()
    for g in seg_px:
        clean[g["core_y0"]:g["core_y1"] + 1, g["x0"]:g["x1"]] = False
    glyph_runs = _runs(clean[: baseline + 1].sum(axis=0) > 0)

    words = text.split()
    char_boxes_px = [None] * n
    if glyph_runs and words:
        word_spans = split_runs_into_words(glyph_runs, text, words)
        positions, idx = [], 0
        for word in words:
            idx = text.index(word, idx)
            positions.append(idx)
            idx += len(word)
        monospace = _looks_monospaced(word_spans) or _line_is_monospaced(glyph_runs)
        for word, pos, runs in zip(words, positions, word_spans):
            for k, box in enumerate(align_glyph_runs(word, runs, monospace)):
                char_boxes_px[pos + k] = box
        for i, c in enumerate(text):
            if c.isspace() and 0 < i < n - 1 and char_boxes_px[i - 1] and char_boxes_px[i + 1]:
                char_boxes_px[i] = (char_boxes_px[i - 1][1], char_boxes_px[i + 1][0])

    ox, oy = origin
    chars = []
    for i, b in enumerate(char_boxes_px):
        if b is None:
            chars.append({"c": text[i], "bbox": None})
        else:
            box = (ox + b[0] / scale, oy + top / scale, ox + b[1] / scale, oy + (baseline + 1) / scale)
            chars.append({"c": text[i], "bbox": box, "origin": (box[0], box[3])})
    for seg in segs:
        flags = underline if seg.source == "raster-underline" else strike
        hits = []
        for i, ch in enumerate(chars):
            b = ch["bbox"]
            if b is None or text[i].isspace():
                continue
            if _overlap(b[0], b[2], seg.x0, seg.x1) / max(b[2] - b[0], 0.01) >= CHAR_COVERAGE_MIN:
                hits.append(i)
        if hits and all(text[i] in LINE_LIKE_GLYPHS for i in hits):
            continue
        if len(hits) == 1 and not _single_glyph_underline(text[hits[0]], chars[hits[0]]["bbox"], seg):
            continue
        for i in hits:
            flags[i] = True
        if hits:
            for i in range(min(hits) + 1, max(hits)):
                b = chars[i]["bbox"]
                if text[i].isspace() and b is not None and \
                        _overlap(b[0], b[2], seg.x0, seg.x1) / max(b[2] - b[0], 0.01) >= CHAR_COVERAGE_MIN:
                    flags[i] = True
    return [c["bbox"] for c in chars], underline, strike, segs
