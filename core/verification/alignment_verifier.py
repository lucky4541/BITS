"""Alignment verification - detects LEFT/CENTER/RIGHT/JUSTIFIED from the
zone's own real line geometry, reusing core.text_extractor.extract_lines'
own per-line bbox output directly (never re-deriving line positions from
scratch, never touching a raw fitz rawdict itself).

Real production case this targets (a title page): "CAMBRIDGE LATIN
AMERICAN STUDIES" genuinely centered in the source PDF but generated
left-aligned because nothing in this app previously computed or stored
a per-zone alignment property at all. This module ADDS that missing
detection; it does not yet wire an "alignment" CSS/attribute all the way
into the XHTML generator (a separate, larger change) - the fix recorded
here is `zone.attributes["alignment"]`, available for a future generator
hook, applied per zone/paragraph as the spec requires (never a blanket
global CSS rule)."""

_TOLERANCE_RATIO = 0.03  # gap-vs-zone-width fraction within which two gaps are considered "equal"


def _gaps(zone_bbox, line_bbox):
    left_gap = line_bbox[0] - zone_bbox[0]
    right_gap = zone_bbox[2] - line_bbox[2]
    return max(0.0, left_gap), max(0.0, right_gap)


def detect_alignment(zone_bbox, lines: list) -> str:
    """`lines`: the exact list[(line_bbox, text)] core.text_extractor.
    extract_lines already returns for this zone. Returns one of "left",
    "center", "right", "justified", or "unknown" (too little evidence -
    e.g. no lines, or a zone too narrow to measure meaningfully)."""
    width = zone_bbox[2] - zone_bbox[0]
    if width <= 0 or not lines:
        return "unknown"
    tolerance = max(2.0, width * _TOLERANCE_RATIO)

    measurements = []
    for line_bbox, text in lines:
        if not text or not text.strip():
            continue
        left_gap, right_gap = _gaps(zone_bbox, line_bbox)
        measurements.append((left_gap, right_gap))
    if not measurements:
        return "unknown"

    left_flush = sum(1 for l, r in measurements if l <= tolerance)
    right_flush = sum(1 for l, r in measurements if r <= tolerance)
    centered = sum(1 for l, r in measurements if abs(l - r) <= tolerance and l > tolerance)
    both_flush = sum(1 for l, r in measurements if l <= tolerance and r <= tolerance)
    n = len(measurements)

    # JUSTIFIED: every line except (optionally) the last one - a wrapped
    # paragraph's own final line is naturally short, exactly like this
    # project's own paragraph-boundary work elsewhere already accounts
    # for - reaches close to both margins.
    if n >= 2 and both_flush >= n - 1:
        return "justified"
    if n == 1:
        l, r = measurements[0]
        if l <= tolerance and r > tolerance:
            return "left"
        if r <= tolerance and l > tolerance:
            return "right"
        if abs(l - r) <= tolerance:
            return "center"
        return "left"
    if centered == n:
        return "center"
    if left_flush == n and right_flush < n:
        return "left"
    if right_flush == n and left_flush < n:
        return "right"
    return "unknown"


_ALIGNMENT_DISPLAY = {"left": "LEFT", "center": "CENTER", "right": "RIGHT",
                      "justified": "JUSTIFIED", "unknown": "UNKNOWN"}


def display_name(alignment: str) -> str:
    return _ALIGNMENT_DISPLAY.get(alignment, "UNKNOWN")


def check_alignment(zone, lines: list):
    """Returns None when there's nothing to flag (no stored expectation
    differs from the detected geometry, or detection is inconclusive).
    Otherwise returns {"detected": str, "current": str}. `current` is
    whatever zone.attributes.get("alignment") already records (defaults
    to "left", matching ordinary prose and this app's own existing,
    unstated default rendering)."""
    detected = detect_alignment(zone.bbox, lines)
    if detected == "unknown":
        return None
    current = zone.attributes.get("alignment", "left")
    if detected == current:
        return None
    return {"detected": detected, "current": current}


def apply_alignment(zone, alignment: str):
    zone.attributes["alignment"] = alignment
