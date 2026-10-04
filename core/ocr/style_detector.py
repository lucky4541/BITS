"""Image-based italic/bold/superscript/subscript detection for OCR zones
(spec: "OCR ZONES - Image-based italic/bold/sup/sub detection").

A scanned PDF page's own "text" (zone.text, core.text_extractor.
_zone_prefers_stored_text) comes from an OCR text layer that carries NO
font/glyph metadata at all - core/formatting_detector.py's font-flag-based
detect_bold_italic/classify_position genuinely has nothing to read for
such a zone (confirmed directly against a real scanned page: the
invisible OCR text layer reports a generic placeholder font with all
style flags off, even though the SCANNED IMAGE itself visibly shows
italic/bold text). This module detects styling from the scanned PIXELS
instead, reusing the exact same "measure against this zone's own dominant
value, never an absolute/hardcoded threshold" philosophy
core/formatting_detector.py already uses for size-ratio/baseline-shift
based sup/sub/small-caps detection - just applied to pixel geometry
(ink density, row-centroid slant, word bounding-box height/position)
instead of font size/origin.

Never re-runs OCR text recognition - zone.text (already recognized,
already trusted, already what the user sees) is the single source of
truth for WHAT the text says. This module only determines WHERE each
already-known word sits in the zone's own cropped image and what its
pixels look like, then wraps that exact word's own text (never a
re-recognized substitute) in the same <bold>/<italic>/<sup>/<sub>
markup vocabulary core.text_extractor's digital-PDF path already emits.

Every ambiguous case (a line/word count that doesn't cleanly match what's
already known from zone.text) falls back to plain, unmarked text for
that line - never a guessed pairing that could misattribute Formatting to
the wrong word."""
import fitz
import numpy as np
from PIL import Image
import cv2

from core.ocr.text_quality import SCANNED_IMAGE_COVERAGE
# core.text_extractor imports THIS module (see its own extract_zone_
# formatted_text), so _wrap_run is imported lazily inside
# detect_ocr_zone_lines below rather than at module level, to avoid a
# circular import.

# Genuine italic slant is typically 10-15 degrees; upright text measures
# near 0 with some pixel/anti-aliasing noise. This fixed cutoff mirrors
# core/formatting_detector.py's own existing fixed-but-reasoned constants
# (SIZE_RATIO_THRESHOLD/BASELINE_SHIFT_RATIO) - a literature-grounded
# threshold, not a per-word/per-book hardcoded guess.
SLANT_THRESHOLD_DEGREES = 9.0
# A word's own mean stroke width (see _stroke_width) this much above the
# zone's own dominant (median) stroke width is bold - a RATIO, so it
# scales with scan DPI/quality/font instead of an absolute pixel-count
# cutoff. Stroke width (not raw ink-density-of-bbox) is the measurement
# here specifically because it was tried both ways and confirmed directly:
# raw ink density is dominated by which LETTERS happen to be in a word
# (a short, round, open letter like "a" can out-density a longer bold
# word), while distance-transform-based stroke width isolates line
# THICKNESS itself, giving a clean, reliable separation between a real
# bold word (~1.3x the zone's own median) and ordinary letter-shape noise
# (never higher than ~1.12x in testing) - see _stroke_width.
BOLD_STROKE_RATIO = 1.25
# A word narrower than this fraction of the zone's own median word width
# is too small a sample for stroke-width-based bold detection to trust
# (see the bold-detection loop's own comment for the confirmed failure
# case) - relative to the zone's own words, never an absolute pixel count.
MIN_BOLD_WIDTH_RATIO = 0.5
# Mirrors core/formatting_detector.py's own SIZE_RATIO_THRESHOLD/
# BASELINE_SHIFT_RATIO in spirit, applied to word bounding-box height/
# vertical position instead of font size/origin_y - but held to a
# CONSIDERABLY stricter bar than font-flag-based detection can be,
# because there is no ground truth here to fall back on if it's wrong.
# Confirmed directly as a real, generalizable failure mode (not specific
# to one PDF/word): an ordinary word's own tight ink height/vertical
# center varies by 20-50% purely from which of its OWN letters happen to
# have an ascender/descender (e.g. a genuinely plain word with no
# descender reads "shorter" than a neighboring word that has one, with
# zero actual repositioning) - a multi-word fully-italic phrase, where
# several different words in a row each lack a descender/ascender for
# unrelated reasons, could trip the OLD, looser thresholds (0.85 / 0.12)
# on several consecutive ordinary words at once, and get merged by this
# module's own downstream same-tag-run coalescing into one large,
# entirely wrong <sup>/<sub> span over what was really just an ordinary
# stretch of italic prose. Raised specifically so ordinary letter-shape
# variance no longer clears the bar, while a genuine, deliberately-
# shrunk-and-repositioned sup/sub character (which shows a MUCH larger
# height/position swing - confirmed against real synthetic sup/sub
# fixtures, comfortably clearing 0.65/0.20 with a wide margin) still
# does. Sup/sub is exactly the direction this module should be most
# conservative in: a missed sup/sub degrades to plain text (still
# correct prose, just unstyled), while an invented one corrupts the
# actual words a reader sees.
SIZE_RATIO_THRESHOLD = 0.65
BASELINE_SHIFT_RATIO = 0.20
# A second, independent signal required in addition to size+position: a
# genuine superscript/subscript is, in ordinary typesetting, always a
# SHORT marker (a footnote digit, a math exponent, an ordinal suffix,
# a chemical subscript) - never a multi-word phrase or an ordinary long
# word. This is a structural/linguistic constraint on what a sup/sub
# marker actually IS, not a resolution-dependent pixel measurement, so a
# fixed character count (not a DPI/zone-relative ratio) is the correct
# kind of constant here - it does not need to scale with scan quality or
# font size the way a geometric threshold would.
MAX_SUPSUB_CHARS = 6
_MIN_INK_FRACTION = 0.004  # a row/column this empty (of its own max) counts as a gap


def _crop_binary(page, bbox, dpi=300):
    """Crops zone.bbox to a binarized (ink=255, background=0) numpy array.
    Mirrors core.pdf_loader.PDFDocument.crop_region's own crop logic and
    core/ocr/preprocessing.py's own Otsu binarization convention (already
    used by deskew/detect_orientation) exactly, rather than inventing a
    third variant.

    Reads the underlying full page through core.ocr.page_image_cache
    (a persistent, high-resolution, on-disk cache keyed off this exact
    `page`'s own owning PDF/page-number - spec: "HIGH-RESOLUTION PDF PAGE
    CACHE / THUMB SYSTEM") instead of re-rendering/clipping directly from
    `page` on every call, then crops the equivalent pixel rectangle out of
    that cached full-page image. `bbox` scaled by `zoom` and rounded via
    fitz.Rect.irect matches PyMuPDF's own internal clip-rect expansion, so
    the resulting pixels are identical to the previous direct-clip render
    for the same bbox/dpi (verified in tests/test_page_image_cache.py)."""
    from core.ocr.page_image_cache import get_page_image
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    irect = (fitz.Rect(*bbox) * matrix).irect
    if irect.width <= 0 or irect.height <= 0:
        return np.zeros((0, 0), dtype=np.uint8)
    full_img = get_page_image(page, dpi)
    crop = full_img.crop((irect.x0, irect.y0, irect.x1, irect.y1))
    if crop.mode != "L":
        crop = crop.convert("L")
    gray = np.array(crop)
    if gray.size == 0:
        return np.zeros((0, 0), dtype=np.uint8)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return binary


def _bands(profile, gap_merge_ratio=0.35):
    """Given a 1D ink-count profile (row or column sums), returns
    [(start, end), ...] contiguous ink bands (end exclusive), merging
    adjacent bands separated by a gap smaller than gap_merge_ratio times
    the median band height/width seen so far - collapses a stray blank
    row/column inside one physical line/word (e.g. between an ascender
    and the x-height body) without needing a fixed pixel constant."""
    if profile.size == 0:
        return []
    threshold = max(1.0, profile.max() * _MIN_INK_FRACTION)
    is_ink = profile > threshold
    raw = []
    start = None
    for i, v in enumerate(is_ink):
        if v and start is None:
            start = i
        elif not v and start is not None:
            raw.append((start, i))
            start = None
    if start is not None:
        raw.append((start, len(is_ink)))
    if len(raw) <= 1:
        return raw
    sizes = sorted((b - a) for a, b in raw)
    median_size = sizes[len(sizes) // 2] or 1
    merged = [raw[0]]
    for a, b in raw[1:]:
        pa, pb = merged[-1]
        if (a - pb) < gap_merge_ratio * median_size:
            merged[-1] = (pa, b)
        else:
            merged.append((a, b))
    return merged


def _word_bands(col_profile, expected_word_count):
    """Word-level column-ink-band segmentation for ONE text line, using
    the word count ALREADY KNOWN from zone.text (never guessed) - unlike
    _bands' own fixed-ratio-vs-median-width merge (tuned for LINE/row
    segmentation, where band SIZE is a stable reference), a line's word
    WIDTHS vary too much (a short function word like "in"/"of" sitting
    next to a long word like "Bargaining") for a fixed ratio to reliably
    tell a genuine word-gap from a letter-internal gap - confirmed
    directly: "...Shadow of the Law" merged "of"+"the" into one band
    under that ratio, since the line's median band WIDTH was pulled up by
    "Bargaining"/"Shadow", making "of the"'s own genuine word-gap look
    small by comparison.

    Instead: start from the fully raw (unmerged) per-glyph-run ink bands,
    then repeatedly merge whichever adjacent PAIR is separated by the
    SMALLEST remaining gap, until exactly `expected_word_count` bands
    remain. A genuine word-separating gap is, in every ordinary typeface
    (including tightly-set italics), never smaller than any single
    letter's own internal/inter-letter gap - so collapsing the smallest
    gaps first recovers the correct grouping regardless of how narrow an
    individual word's own gap happens to be. Returns fewer than
    expected_word_count bands unchanged if there aren't enough distinct
    ink concentrations to reach it - still genuinely ambiguous, left for
    the caller's own strict count-match check (never a guessed split)."""
    bands = list(_bands(col_profile, gap_merge_ratio=0.0))
    while len(bands) > expected_word_count:
        gaps = [bands[i + 1][0] - bands[i][1] for i in range(len(bands) - 1)]
        i = int(np.argmin(gaps))
        bands[i] = (bands[i][0], bands[i + 1][1])
        del bands[i + 1]
    return bands


_SHEAR_SEARCH_DEGREES = np.arange(-24.0, 24.01, 2.0)


def _shear_search_angle(shape_binary) -> float:
    """Shear-search slant estimation for ONE connected shape - a standard,
    well-established document-image-analysis technique (related to the
    "cumulative angular function" method used for de-slanting cursive/
    italic text), independent of font/glyph metadata (there is none here,
    only pixels). Tries a range of candidate shear angles; for each,
    shifts every row horizontally by an amount proportional to the angle
    and the row's own distance from the shape's vertical center (an
    exact, no-interpolation shear for a binary image), then measures how
    PEAKY the resulting column-projection profile is (variance). At the
    shape's own TRUE slant angle, its vertical-ish strokes line up into
    narrow columns, maximizing that variance - the angle that scores
    highest is the estimated slant. (An earlier per-row ink-centroid-
    regression approach was tried and rejected: a row's overall ink
    centroid is dominated by WHICH letters have ink in that row - e.g.
    only ascenders near the top of a word - not by the strokes' own
    slant, and produced meaningless angles on real test fixtures,
    confirmed directly.) Returns 0.0 for a shape too small to measure
    reliably. Also returns 0.0 when the winning angle sits AT the search
    range's own edge (+/-25 degrees) - confirmed directly this matters for
    a small glyph (e.g. a deliberately-shrunk superscript/subscript
    digit): with too few distinguishing pixels, the variance-maximization
    can find no genuine peak and instead keeps climbing to the boundary
    of the searched range, which is a sign the search ran out of room
    rather than found a real slant - no ordinary italic typeface is
    actually slanted that extremely, so a result pinned to the edge is
    treated as "no reliable measurement" (0.0) rather than trusted."""
    h, w = shape_binary.shape
    if h < 6 or w < 3:
        return 0.0
    best_angle, best_score = 0.0, -1.0
    max_shift = int(np.tan(np.radians(_SHEAR_SEARCH_DEGREES.max())) * h) + 2
    for angle_deg in _SHEAR_SEARCH_DEGREES:
        shear = np.tan(np.radians(angle_deg))
        col_counts = np.zeros(w + 2 * max_shift, dtype=np.int64)
        for r in range(h):
            xs = np.nonzero(shape_binary[r])[0]
            if xs.size == 0:
                continue
            shift = int(round(shear * (r - h / 2.0)))
            idx = xs + shift + max_shift
            col_counts[idx] += 1
        score = float(np.var(col_counts))
        if score > best_score:
            best_score, best_angle = score, float(angle_deg)
    if abs(best_angle) >= _SHEAR_SEARCH_DEGREES.max() - 0.5:
        return 0.0
    return best_angle


def _slant_degrees(word_binary) -> float:
    """A word's own slant, as the MEDIAN of each of its INDIVIDUAL
    letters' own shear-search angle (_shear_search_angle) - never a
    single whole-word search. Confirmed directly this is necessary: a
    whole multi-letter word's own column-projection profile can have a
    spurious secondary peak at a plausible-looking angle (e.g. a wide
    uppercase letter's own diagonal strokes coincidentally aligning at
    some shear unrelated to the word's real, upright slant), which a
    single whole-word search has no way to distinguish from a genuine
    italic slant - a real upright test word measured a false 14 degrees
    this way. Segmenting into individual letters (cv2.connectedComponents)
    and taking the MEDIAN of their own independent angles means one
    letter's own quirk gets outvoted by the rest, exactly like this
    module's other measurements are resistant to a minority outlier.

    A letter whose OWN connected component is much shorter than this same
    word's own median component height is excluded from that vote first -
    confirmed directly this matters: some glyphs (e.g. italic "w") can
    threshold/connect into more than one component (a small disconnected
    fragment plus the glyph's own main body), and that fragment's own
    tiny, atypically-shaped shear-search result is not a second, genuine
    letter - it is noise from ONE letter counted twice. Sized relative to
    THIS word's own letters (never an absolute pixel constant), exactly
    like every other measurement in this module."""
    if word_binary.shape[0] < 6 or word_binary.shape[1] < 6:
        return 0.0
    n_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(word_binary, connectivity=8)
    candidates = []
    for label in range(1, n_labels):
        x, y, w, h, area = stats[label]
        if area < 4 or h < 6 or w < 3:
            continue
        candidates.append((label, x, y, w, h))
    if not candidates:
        return _shear_search_angle(word_binary)
    median_h = float(np.median([h for _, _, _, _, h in candidates]))
    angles = []
    for label, x, y, w, h in candidates:
        if h < 0.6 * median_h:
            continue  # a fragment of another letter, not a genuine letter of its own
        shape = (labels[y:y + h, x:x + w] == label).astype(np.uint8) * 255
        angles.append(_shear_search_angle(shape))
    if not angles:
        return _shear_search_angle(word_binary)
    return float(np.median(angles))


def _ink_row_extent(word_binary):
    """(tight_height, row_center) from the ROWS that actually have ink,
    within word_binary's own coordinate frame - NOT the raw array height,
    which (since every word on one line shares the same outer row-band
    slice) is identical for every word on a line regardless of how small
    or vertically shifted its own glyphs actually are. A superscript/
    subscript word's own ink occupies only a SUBSET of those shared rows;
    this is what makes its true size and position visible. Returns
    (0, shape[0]/2.0) for a completely blank slice."""
    ink_rows = np.nonzero(word_binary.sum(axis=1))[0]
    if ink_rows.size == 0:
        return 0, word_binary.shape[0] / 2.0
    return int(ink_rows.max() - ink_rows.min() + 1), float(ink_rows.mean())


def _stroke_width(word_binary) -> float:
    """Mean distance-transform value at ink pixels - a standard proxy for
    average stroke THICKNESS, independent of a particular letter's own
    shape/openness (unlike raw ink-density-of-bounding-box, which was
    tried first and rejected: a short, round, open letter like "a" can
    out-density a genuinely bold word purely by shape, confirmed directly
    against real test fixtures - see BOLD_STROKE_RATIO's own comment).
    Each ink pixel's distance-transform value is roughly half the local
    stroke width at that point; a thicker (bolder) stroke pushes interior
    pixels farther from the nearest background pixel, raising the mean."""
    if word_binary.size == 0:
        return 0.0
    dt = cv2.distanceTransform(word_binary, cv2.DIST_L2, 5)
    ink_values = dt[word_binary > 0]
    return float(ink_values.mean()) if ink_values.size else 0.0


def page_is_image_dominated(page, zone_bbox=None, coverage_threshold: float = SCANNED_IMAGE_COVERAGE,
                             overlap_threshold: float = 0.8) -> bool:
    """True when there's a dominant scanned-page-like image on `page` -
    reuses core.ocr.text_quality's own SCANNED_IMAGE_COVERAGE threshold
    for consistency (never a second, independently-tuned magic number),
    checked in ISOLATION from the rest of that module's own
    classify_page: that function's job is "does this page need
    (re-)OCR", a genuinely different question - a real searchable-scan
    PDF's invisible OCR text layer is often already clean, readable text
    (as confirmed directly on the user's own real example file), so
    classify_page() correctly calls such a page DIGITAL (it doesn't need
    OCR again) even though the underlying image means that same text
    carries NO real font metadata to analyze for italic/bold/sup/sub
    styling.

    When `zone_bbox` is given, ALSO requires that specific zone's own
    bbox to sit substantially (>= overlap_threshold of its own area) on
    top of that dominant image - never just "this page happens to
    contain a large image somewhere" (a real, confirmed distinction: a
    genuinely digital PDF page can legitimately have one large photo/
    figure covering most of the page AND a separate, genuinely digital-
    font caption or body paragraph positioned elsewhere on that SAME
    page - treating the whole page as "image-dominated" would incorrectly
    route that caption's own real font-rendered text through image
    analysis too, instead of the already-correct live font/glyph path)."""
    page_area = max(page.rect.width * page.rect.height, 1.0)
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001 - a malformed image xref must never break text extraction
        return False
    zx0 = zy0 = zx1 = zy1 = zone_area = None
    if zone_bbox is not None:
        zx0, zy0, zx1, zy1 = zone_bbox
        zone_area = max((zx1 - zx0) * (zy1 - zy0), 1.0)
    for info in infos:
        bbox = info.get("bbox", [0, 0, 0, 0])
        ix0, iy0, ix1, iy1 = bbox
        w, h = ix1 - ix0, iy1 - iy0
        if w <= 0 or h <= 0 or (w * h) / page_area < coverage_threshold:
            continue
        if zone_bbox is None:
            return True
        ox0, oy0 = max(zx0, ix0), max(zy0, iy0)
        ox1, oy1 = min(zx1, ix1), min(zy1, iy1)
        if ox1 <= ox0 or oy1 <= oy0:
            continue
        overlap_area = (ox1 - ox0) * (oy1 - oy0)
        if overlap_area / zone_area >= overlap_threshold:
            return True
    return False


def _word_vertical_features(word_binary, line_height):
    """Return tighter vertical measurements for small raised/lowered glyphs.

    The old detector compares the word center against word_bin.shape[0]/2.
    That is unreliable because the crop is the shared line band: a normal
    word with ascenders/descenders can have a different ink center without
    actually being raised. This helper uses the actual ink bounds and also
    returns the ink top/bottom relative to the line band.
    """
    if word_binary.size == 0:
        return 0, 0.0, 0, 0

    ys = np.nonzero(word_binary.sum(axis=1))[0]
    if ys.size == 0:
        return 0, 0.0, 0, 0

    top = int(ys.min())
    bottom = int(ys.max())
    height = bottom - top + 1
    center = float((top + bottom) / 2.0)
    return height, center, top, bottom


def _classify_supsub(word_text, word_binary, dominant_height, line_height):
    """Conservative but more sensitive OCR-image sup/sub classification."""
    if not word_text or len(word_text.strip()) > MAX_SUPSUB_CHARS:
        return "normal"

    # Sup/sub markers in scanned books are overwhelmingly digits, ordinal
    # letters, footnote marks, or mathematical symbols. Avoid turning a short
    # ordinary word into superscript just because it is small.
    stripped = word_text.strip()
    # Ordinary alphabetic words are not superscript candidates. In this
    # scanned-bibliography path, reliable markers are digits and typographic
    # footnote/math marks; size + vertical displacement still must agree.
    if not all(ch.isdigit() or ch in "*†‡§¶" for ch in stripped):
        return "normal"

    tight_h, center, top, bottom = _word_vertical_features(word_binary, line_height)
    if not tight_h or not dominant_height:
        return "normal"

    ratio = tight_h / dominant_height

    # Use a slightly less rigid size floor than the old <= 0.65 rule. A scan
    # can enlarge a small superscript enough to land around 0.68-0.78.
    if ratio > 0.78:
        return "normal"

    mid = line_height / 2.0
    # Require a meaningful vertical displacement as well as small size.
    shift = abs(center - mid) / max(dominant_height, 1.0)

    if shift >= 0.16:
        return "sup" if center < mid else "sub"

    return "normal"


def _native_line_char_records(page, zone):
    """Return rawdict text lines whose character boxes intersect `zone`.

    Searchable-scan PDFs have a useful OCR text layer for WHAT was recognized,
    but often no useful font metadata for HOW it was printed.  The character
    boxes are still useful: they give us the exact physical position of each
    already-recognized character on the scanned page.  This helper deliberately
    uses only those boxes; it never performs OCR or rewrites text.
    """
    try:
        rawdict = page.get_text("rawdict")
    except Exception:
        return []
    zx0, zy0, zx1, zy1 = zone.bbox
    result = []
    for block in rawdict.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    box = ch.get("bbox") or ()
                    if len(box) < 4:
                        continue
                    cx = (float(box[0]) + float(box[2])) / 2.0
                    cy = (float(box[1]) + float(box[3])) / 2.0
                    if zx0 <= cx <= zx1 and zy0 <= cy <= zy1:
                        chars.append(ch)
            if chars:
                chars.sort(key=lambda ch: (float(ch["bbox"][0]), float(ch["bbox"][1])))
                result.append(chars)
    result.sort(key=lambda chars: (min(float(c["bbox"][1]) for c in chars),
                                   min(float(c["bbox"][0]) for c in chars)))
    return result


def _native_word_style_map(page, chars, fixed_chars):
    """Measure visual style from native character boxes + rendered pixels.

    Native character boxes define the word boundaries and also provide the
    reliable geometry needed for a superscript/subscript character.  The
    rendered pixels are used only for the word's italic/bold appearance.
    """
    if not chars:
        return [None] * len(chars)

    words = []
    current = []
    for i, ch in enumerate(chars):
        c = (ch.get("c", "") or "")
        fc = fixed_chars[i] if i < len(fixed_chars) else c
        if c.isspace() or (fc or "").isspace():
            if current:
                words.append(current)
                current = []
        else:
            current.append(i)
    if current:
        words.append(current)
    if not words:
        return [None] * len(chars)

    # Native PDF coordinates are enough for relative vertical positioning;
    # no OCR is performed here.
    char_heights = []
    char_centers = []
    for ch in chars:
        b = ch.get("bbox") or ()
        if len(b) >= 4:
            char_heights.append(max(0.1, float(b[3]) - float(b[1])))
            char_centers.append((float(b[1]) + float(b[3])) / 2.0)
    line_height_pt = float(np.median(char_heights)) if char_heights else 0.0
    line_center_pt = float(np.median(char_centers)) if char_centers else 0.0

    dpi = 200
    try:
        from core.ocr.page_image_cache import get_page_image
        image = get_page_image(page, dpi)
        gray = np.array(image.convert("L"))
    except Exception:
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        gray = np.array(image.convert("L"))

    measurements = []
    for wi, indices in enumerate(words):
        x0 = min(float(chars[i]["bbox"][0]) for i in indices)
        y0 = min(float(chars[i]["bbox"][1]) for i in indices)
        x1 = max(float(chars[i]["bbox"][2]) for i in indices)
        y1 = max(float(chars[i]["bbox"][3]) for i in indices)
        rect = (fitz.Rect(x0, y0, x1, y1) * (dpi / 72.0)).irect
        px0, py0, px1, py1 = rect.x0, rect.y0, rect.x1, rect.y1
        if px1 <= px0 or py1 <= py0:
            measurements.append((wi, np.zeros((0, 0), dtype=np.uint8), 0, 0.0, 0.0, 0.0, False, indices))
            continue
        pad = max(1, int(round(1.0 * dpi / 72.0)))
        px0 = max(0, px0 - pad); py0 = max(0, py0 - pad)
        px1 = min(gray.shape[1], px1 + pad); py1 = min(gray.shape[0], py1 + pad)
        crop = gray[py0:py1, px0:px1]
        if crop.size:
            _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        else:
            binary = np.zeros((0, 0), dtype=np.uint8)
        tight_h, center = _ink_row_extent(binary)
        stroke = _stroke_width(binary)
        angle = _slant_degrees(binary)
        italic = abs(angle) >= SLANT_THRESHOLD_DEGREES
        measurements.append((wi, binary, tight_h, center, stroke, angle, italic, indices))

    heights = [m[2] for m in measurements if m[2] > 0]
    widths = []
    for m in measurements:
        inds = m[7]
        if inds:
            widths.append(max(float(chars[i]["bbox"][2]) for i in inds) -
                          min(float(chars[i]["bbox"][0]) for i in inds))
    dominant_height = float(np.median(heights)) if heights else 0.0
    dominant_width = float(np.median(widths)) if widths else 0.0

    # A one-/two-letter word is too little pixel evidence for the connected
    # component shear estimator (a standalone `W.` can otherwise produce a
    # spurious extreme angle). Do not classify such a word from its own angle.
    # It may still inherit italic only when BOTH adjacent, longer words are
    # already italic. Longer words can have the same isolated-hole repair: a
    # single unstable estimate such as `Paris` is surrounded by two reliable
    # italic words in the real source PDF.
    alpha_counts = [sum(ch.isalpha() for ch in ("".join(chars[i]["c"] for i in m[7]))) for m in measurements]
    # Short words are still measurable when their rendered slant is strong.
    # Do not discard them outright: real titles contain short italic words
    # such as "De", "Le", "of", "in", and "the". A higher threshold for
    # short words keeps unstable single-glyph measurements such as "W." and
    # "H." from becoming italic.
    SHORT_WORD_ITALIC_THRESHOLD = 13.0
    word_texts = [
        "".join(chars[i]["c"] for i in m[7])
        for m in measurements
    ]
    visual_italic = [
        (
            False
            if (
                (alpha_counts[k] == 0 and len(word_texts[k].strip()) <= 2)
                or (
                    alpha_counts[k] == 1
                    and any(not ch.isalpha() for ch in word_texts[k])
                )
            )
            else (
                bool(m[6])
                if alpha_counts[k] >= 3
                else abs(float(m[5])) >= SHORT_WORD_ITALIC_THRESHOLD
            )
        )
        for k, m in enumerate(measurements)
    ]

    # A very short word at a title boundary can have an unstable own angle
    # (the real PDF's leading "De" is one example). If the next TWO words are
    # independently strong italic measurements, inherit that style. This is
    # deliberately local and geometric; it does not use vocabulary or spelling.
    for k in range(len(visual_italic) - 2):
        if alpha_counts[k] <= 2 and not visual_italic[k]:
            previous_is_italic = (k > 0 and visual_italic[k - 1])
            at_line_start = (k == 0)
            if (at_line_start or previous_is_italic) and visual_italic[k + 1] and visual_italic[k + 2]:
                visual_italic[k] = True
    for k in range(1, len(visual_italic) - 1):
        if not visual_italic[k] and visual_italic[k - 1] and visual_italic[k + 1] and alpha_counts[k] >= 1:
            visual_italic[k] = True

    stroke_records = [(m[0], m[4], visual_italic[k], widths[k])
                      for k, m in enumerate(measurements)
                      if m[4] > 0 and (dominant_width <= 0 or widths[k] >= MIN_BOLD_WIDTH_RATIO * dominant_width)]

    def dominant_stroke(idx, italic):
        vals = [st for i, st, it, _w in stroke_records if i != idx and it == italic]
        if vals:
            return float(np.median(vals))
        vals = [st for i, st, _it, _w in stroke_records if i != idx]
        return float(np.median(vals)) if vals else 0.0

    styles = [None] * len(chars)
    sup_chars = {"*", "†", "‡", "§", "¶"}
    zoom = dpi / 72.0
    # Pixel-space equivalents for the line-level reference values.
    line_height_px = line_height_pt * zoom
    line_center_px = line_center_pt * zoom

    for k, m in enumerate(measurements):
        wi, binary, tight_h, center, stroke, angle, _measured_italic, indices = m
        italic = visual_italic[k]
        ds = dominant_stroke(wi, italic)
        width_reliable = dominant_width <= 0 or widths[k] >= MIN_BOLD_WIDTH_RATIO * dominant_width
        bold = bool(ds > 0 and width_reliable and (stroke / ds) > BOLD_STROKE_RATIO)

        for i in indices:
            c = fixed_chars[i] if i < len(fixed_chars) else chars[i].get("c", "")
            pos = "normal"
            b = chars[i].get("bbox") or ()
            if len(b) >= 4 and line_height_pt > 0:
                ch_h = float(b[3]) - float(b[1])
                ch_center = (float(b[1]) + float(b[3])) / 2.0
                is_marker = bool((c or "").isdigit() or c in sup_chars)
                # Sup/sub detection: PDF bbox-based primary, pixel-based fallback.
                # Primary: bbox height ≤ 82 % of line median AND center shift ≥ 16 %.
                small_pdf = ch_h <= line_height_pt * 0.82
                shift_pdf = abs(ch_center - line_center_pt) / line_height_pt if line_height_pt else 0.0
                detected = is_marker and small_pdf and shift_pdf >= 0.16
                if is_marker and not detected and line_height_px > 0:
                    # Fallback: measure actual ink pixels.  Some PDFs encode the
                    # character bbox at full body size even when the glyph itself
                    # renders at superscript size (OpenType superscript variants,
                    # or an ArialMT single-font PDF that visually shows a raised,
                    # smaller marker without metadata to match).
                    try:
                        r = fitz.Rect(*b) * zoom
                        px0 = max(0, int(r.x0))
                        py0 = max(0, int(r.y0))
                        px1 = min(gray.shape[1], int(r.x1) + 1)
                        py1 = min(gray.shape[0], int(r.y1) + 1)
                        ch_crop = gray[py0:py1, px0:px1]
                        if ch_crop.size > 0:
                            _, ch_bin = cv2.threshold(
                                ch_crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
                            )
                            ink_rows = np.where(ch_bin.any(axis=1))[0]
                            if len(ink_rows) >= 2:
                                ink_h_px = float(ink_rows[-1] - ink_rows[0] + 1)
                                ink_center_px = (ink_rows[0] + ink_rows[-1]) / 2.0 + py0
                                small_px = ink_h_px <= line_height_px * 0.80
                                shift_px = abs(ink_center_px - line_center_px) / line_height_px
                                detected = small_px and shift_px >= 0.12
                    except Exception:
                        pass
                if detected:
                    pos = "sup" if ch_center < line_center_pt else "sub"
            styles[i] = (bold, italic, pos, False)
    return styles


def _apply_native_visual_styles(page, zone, raw_lines):
    """Style `zone.text` lines from native character positions + page pixels."""
    from core.text_extractor import _wrap_run
    native_lines = _native_line_char_records(page, zone)
    if len(native_lines) != len(raw_lines):
        return None
    output = []
    for line_text, chars in zip(raw_lines, native_lines):
        fixed = [ch.get("c", "") or "" for ch in chars]
        styles = _native_word_style_map(page, chars, fixed)
        # Require a basic text/character correspondence.  If it does not hold,
        # fall back rather than guessing a visual-to-text alignment.
        native_plain = "".join(fixed)
        # The native line may contain a verified Unicode repair (for example
        # one raw glyph becoming a different Unicode character, or a broken
        # ligature expanding from one raw character to two output characters).
        # Align the already-extracted output text to the raw character stream
        # rather than rejecting the whole line.  If the alignment is genuinely
        # poor, return None and let the existing conservative fallback run.
        import difflib
        matcher = difflib.SequenceMatcher(
            None, native_plain, line_text, autojunk=False
        )
        if matcher.quick_ratio() < 0.75:
            return None
        mapped_styles = [None] * len(line_text)
        for tag, a0, a1, b0, b1 in matcher.get_opcodes():
            if tag == "equal":
                for ai, bi in zip(range(a0, a1), range(b0, b1)):
                    if ai < len(styles) and bi < len(mapped_styles):
                        mapped_styles[bi] = styles[ai]
            elif tag in ("replace", "delete") and b0 < b1:
                source = [styles[ai] for ai in range(a0, a1) if ai < len(styles)]
                chosen = next((st for st in source if st is not None), None)
                for bi in range(b0, b1):
                    mapped_styles[bi] = chosen
        # Coalesce adjacent characters/words with the same style.  Spaces are
        # deliberately kept INSIDE a styled run when the style continues on
        # both sides, so a real title becomes one <italic>...</italic> span
        # rather than one tag per word.
        pieces = []
        current_style = None
        current_text = []
        normal = (False, False, "normal", False)

        def flush():
            nonlocal current_style, current_text
            if current_text:
                pieces.append(_wrap_run(current_style or normal, "".join(current_text)))
            current_text = []
            current_style = None

        nline = len(line_text)
        for char_pos, c in enumerate(line_text):
            if c.isspace():
                j = char_pos + 1
                while j < nline and line_text[j].isspace():
                    j += 1
                next_style = mapped_styles[j] if j < len(mapped_styles) else None
                if next_style is None:
                    next_style = normal
                # Keep whitespace in the current run only when the next real
                # character continues exactly the same style.
                if current_text and current_style == next_style:
                    current_text.append(c)
                else:
                    flush()
                    pieces.append(c)
                continue

            style = mapped_styles[char_pos] if char_pos < len(mapped_styles) else None
            if style is None:
                style = normal
            if current_style == style:
                current_text.append(c)
            else:
                flush()
                current_style = style
                current_text = [c]
        flush()
        assembled = "".join(pieces)
        # Diagnostics: report any visual italic/bold/sup/sub runs.
        if any(t in assembled for t in ("<i>", "<b>", "<sup>", "<sub>")):
            _pgno = getattr(page, 'number', -1) + 1
            _lno = len(output) + 1
            import re as _re
            for _m in _re.finditer(r"<(i|b|sup|sub)>([^<]*)</\1>", assembled):
                _tag, _txt = _m.group(1), _m.group(2)
                if _txt.strip():
                    if _tag in ("sup", "sub"):
                        print(f"[VISUAL SUP] page={_pgno} line={_lno} text={_txt!r} {_tag}=True")
                    else:
                        print(f"[VISUAL STYLE] page={_pgno} line={_lno} text={_txt!r} tag={_tag}")
        output.append(assembled)
    return output


def detect_ocr_zone_lines(page, zone) -> list:
    """Main entry point (spec: "OCR ZONES - Image-based italic/bold/sup/
    sub detection"). Returns one marked-up (<bold>/<italic>/<sup>/<sub> -
    the SAME vocabulary core.text_extractor's digital-PDF path already
    emits) string per line of zone.text, for the caller to join exactly
    like the digital path already does (dehyphenate_join/
    dehyphenate_join_with_breaks - both pure-text, zone-type-agnostic).
    Never touches zone.bbox/zone.text/any other saved zoning data - reads
    them only."""
    from core.text_extractor import _wrap_run  # deferred - see the module-level import comment
    raw_lines = (zone.text or "").split("\n")
    native_visual = _apply_native_visual_styles(page, zone, raw_lines)
    if native_visual is not None:
        return native_visual
    if not any(line.strip() for line in raw_lines):
        return raw_lines
    binary = _crop_binary(page, zone.bbox)
    if binary.size == 0:
        return raw_lines
    row_profile = binary.sum(axis=1).astype(np.float64)
    row_bands = _bands(row_profile)
    if len(row_bands) != len(raw_lines):
        # Ambiguous line segmentation - never guess which detected band
        # belongs to which saved text line. Plain text for the whole
        # zone is the same output this zone already produced before this
        # feature existed - a safe, honest degradation.
        return raw_lines

    # Pass 1: measure every cleanly-matched word in the WHOLE zone first,
    # so stroke-width/height comparisons are against the zone's own
    # dominant value (mirrors core/formatting_detector.py's own dominant-
    # size convention), not just one line's own local average. Slant is
    # measured here too (not just stroke/height) because bold detection
    # below needs to know each word's own italic status before it can
    # pick the right stroke baseline to compare that word against. Each
    # word gets a unique running `idx` so its own stroke can be excluded
    # from its own comparison baseline in Pass 2 (see _dominant_stroke_for).
    per_line_words = []  # [(words_text, word_measurements_or_None), ...]
    # word_measurements: [(idx, word_bin, tight_h, center, stroke, italic, width), ...]
    all_heights, all_widths = [], []
    idx_counter = 0
    for (y0, y1), line_text in zip(row_bands, raw_lines):
        words_text = line_text.split(" ")
        words_text = [w for w in words_text if w != ""]
        if not words_text:
            per_line_words.append(([], []))
            continue
        col_profile = binary[y0:y1, :].sum(axis=0).astype(np.float64)
        col_bands = _word_bands(col_profile, len(words_text))
        if len(col_bands) != len(words_text):
            per_line_words.append((words_text, None))
            continue
        measurements = []
        for x0, x1 in col_bands:
            word_bin = binary[y0:y1, x0:x1]
            width = x1 - x0
            tight_h, center = _ink_row_extent(word_bin)
            stroke = _stroke_width(word_bin)
            angle = _slant_degrees(word_bin)
            italic = abs(angle) >= SLANT_THRESHOLD_DEGREES
            measurements.append((idx_counter, word_bin, tight_h, center, stroke, italic, width))
            idx_counter += 1
            if tight_h:
                all_heights.append(tight_h)
            all_widths.append(width)
        per_line_words.append((words_text, measurements))

    if not all_heights:
        return raw_lines

    # The MEDIAN (never a mean) of the zone's own words, exactly like
    # core/formatting_detector.py's own dominant-size convention needs to
    # resist a MINORITY of outlier words - a mean would let even one
    # genuinely bold/sup/sub word pull the very baseline it's being
    # compared against toward itself, diluting the contrast.
    dominant_height = float(np.median(all_heights))
    dominant_width = float(np.median(all_widths)) if all_widths else 0.0

    # Stroke-width baseline records EXCLUDE any word narrower than
    # MIN_BOLD_WIDTH_RATIO of the zone's own median word width - confirmed
    # directly this matters even for words that are NOT themselves being
    # tested for bold: a deliberately tiny word (e.g. a superscript digit)
    # has an unreliable stroke-width reading (see the bold-detection loop's
    # own comment), and letting that unreliable value sit in the baseline
    # POLLUTES the comparison for OTHER, ordinary-sized words - a real,
    # confirmed case: a thin superscript "2" dragged the zone's own
    # leave-one-out median down far enough that an ordinary plain word
    # measured as falsely bold next to it. Excluding it from the baseline
    # entirely (not just from being a bold CANDIDATE) protects both
    # directions with one guard.
    stroke_records = [
        (idx, stroke, italic)
        for _words_text, ms in per_line_words if ms
        for (idx, _word_bin, _tight_h, _center, stroke, italic, width) in ms
        if dominant_width <= 0 or width >= MIN_BOLD_WIDTH_RATIO * dominant_width
    ]

    def _dominant_stroke_for(idx: int, italic: bool) -> float:
        """This word's own comparison baseline, EXCLUDING its own stroke
        value (a leave-one-out median) - confirmed directly this matters
        for a small zone: with only 2-3 words of a given style, including
        the very word being tested in its own "dominant" baseline pulls
        that baseline toward the word itself, diluting the contrast (the
        same minority-outlier problem this module's median convention is
        otherwise built to resist - it just needs the CANDIDATE excluded,
        not merely the CROWD kept small). Also grouped by each word's OWN
        italic status first (never one dominant mixing both): an italic
        typeface's own glyphs measure systematically thinner via axis-
        aligned distance-transform than an upright typeface at the "same"
        nominal weight, confirmed directly against a real bold+italic word
        compared against a dominant blended from upright words, which
        undershot BOLD_STROKE_RATIO despite being visibly, clearly bolder
        than its own italic, non-bold neighbor. Falls back to the OTHER
        style's own strokes (still zone-specific, never a hardcoded
        constant) when this word's own style has no other member to
        compare against - e.g. a 2-word zone with one upright and one
        italic word each still gets a real, if imperfect, baseline."""
        same_style = [s for i, s, it in stroke_records if it == italic and i != idx]
        if same_style:
            return float(np.median(same_style))
        other_style = [s for i, s, it in stroke_records if it != italic]
        if other_style:
            return float(np.median(other_style))
        return 0.0  # no other word anywhere in the zone to compare against

    # Character-level underline/strike from the line image (CUPEPUB only -
    # see core.text_extractor.set_decoration_detection_enabled). Computed
    # per line band; None means "no decoration evidence for this line".
    line_decorations = [None] * len(raw_lines)
    try:
        from core.text_extractor import decoration_detection_enabled
        if decoration_detection_enabled():
            from core.underline_detector import raster_line_decorations
            scale = 300 / 72.0
            for li, ((y0, y1), line_text) in enumerate(zip(row_bands, raw_lines)):
                _boxes, u_flags, s_flags, segs = raster_line_decorations(
                    binary[y0:y1, :] > 0, line_text, scale, (zone.bbox[0], zone.bbox[1] + y0 / scale))
                if segs and (any(u_flags) or any(s_flags)):
                    line_decorations[li] = (u_flags, s_flags)
    except Exception:
        line_decorations = [None] * len(raw_lines)

    out_lines = []
    for line_no, ((words_text, measurements), line_text) in enumerate(zip(per_line_words, raw_lines)):
        if measurements is None:
            # Word-level ambiguity for THIS line only - other lines in the
            # same zone are unaffected.
            out_lines.append(line_text)
            continue
        if not words_text:
            out_lines.append(line_text)
            continue
        pieces = []
        for word_text, (idx, word_bin, tight_h, center, stroke, italic, width) in zip(words_text, measurements):
            dominant_stroke = _dominant_stroke_for(idx, italic)
            # A word much narrower than the zone's own typical word (e.g. a
            # single short/round letter like "a") is too small a sample for
            # this distance-transform stroke measurement to trust for bold -
            # confirmed directly: a plain "a" measured a higher raw stroke
            # value than several genuinely longer plain words purely because
            # a small closed bowl shape's own distance-transform reads
            # "thick" relative to its own tiny size, unrelated to actual
            # font weight. Sized relative to THIS zone's own median word
            # width (never an absolute pixel constant) - mirrors this
            # module's existing minimum-size reliability floors elsewhere
            # (e.g. _shear_search_angle's own h<6/w<3 guard). A false
            # negative (missing bold on a genuinely bold tiny word) is the
            # safe direction to err in - never a guessed positive.
            width_reliable = dominant_width <= 0 or width >= MIN_BOLD_WIDTH_RATIO * dominant_width
            bold = width_reliable and dominant_stroke > 0 and (stroke / dominant_stroke) > BOLD_STROKE_RATIO
            pos = _classify_supsub(
                word_text,
                word_bin,
                dominant_height,
                word_bin.shape[0],
            )
            pieces.append(((bold, italic, pos), word_text))
        deco = line_decorations[line_no] if line_no < len(line_decorations) else None
        if deco is None:
            out_lines.append(" ".join(_wrap_run((b, it, p, False), w) for (b, it, p), w in pieces))
            continue
        # Decorated line: rebuild it character by character so each
        # underline/strike range covers exactly the characters it covers on
        # the page, then wrap maximal equal-style runs.
        u_flags, s_flags = deco
        runs = []
        cursor = 0
        prev_base = None
        for (base, word_text) in pieces:
            start = line_text.find(word_text, cursor)
            if start < 0:
                start = cursor
            if runs:
                for k in range(cursor, start):
                    deco_space = (k < len(u_flags) and u_flags[k]) or (k < len(s_flags) and s_flags[k])
                    style = (prev_base[0], prev_base[1], "normal", False,
                             bool(u_flags[k]) if k < len(u_flags) else False,
                             bool(s_flags[k]) if k < len(s_flags) else False) if deco_space \
                        else (False, False, "normal")
                    runs.append([style, line_text[k] if k < len(line_text) else " "])
            for k, ch in enumerate(word_text):
                pos_k = start + k
                u = bool(u_flags[pos_k]) if pos_k < len(u_flags) else False
                st = bool(s_flags[pos_k]) if pos_k < len(s_flags) else False
                style = (base[0], base[1], base[2], False, u, st) if (u or st) else base
                runs.append([style, ch])
            cursor = start + len(word_text)
            prev_base = base
        merged = []
        for style, ch in runs:
            if merged and merged[-1][0] == style:
                merged[-1][1] += ch
            else:
                merged.append([style, ch])
        out_lines.append("".join(_wrap_run(style, text) for style, text in merged))
    return out_lines
