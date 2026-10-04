"""Formatting verification - cross-checks the CURRENT extraction's own
formatting decision (core.text_extractor.extract_zone_formatted_text,
which is exactly what would land in the final XHTML today) against an
INDEPENDENT second signal: core.ocr.style_detector's pixel/geometry-based
visual detector (detect_ocr_zone_lines), reused as-is.

Re-running the SAME native-font-flag logic against itself would never
catch a bug in that logic, so a meaningful cross-check needs a genuinely
different signal - the visual/OCR path is exactly that (it measures
pixel slant/stroke-width/size/position, never PDF font flags). Two
important consequences of this, both confirmed against core/text_
extractor.py's own routing logic (_zone_wants_image_formatting_detection):

  1. A zone that ALREADY routes through the visual/OCR path today (a
     scanned page, or explicitly source=="ocr") has NO native/font-flag
     alternative to compare it against at all - calling
     detect_ocr_zone_lines a second time would just repeat the exact
     same pixel computation, not provide independent evidence. Such
     zones are therefore never automatically cross-checked here.
  2. The genuinely independent, worth-checking-automatically case is the
     OPPOSITE: a zone that is using the native FONT-FLAG path today
     (_zone_wants_image_formatting_detection is False) on a page that
     ISN'T cleanly, purely digital (MIXED/SCANNED/OCR_REQUIRED/UNKNOWN,
     or genuinely image-dominated per page_is_image_dominated) - a
     "digital text over/near a scan-like page" case, exactly the spec's
     own "for mixed/searchable pages: use native extraction + OCR as
     evidence" instruction. This is a narrow, uncommon category, not
     "every digital PDF's every zone" - an ordinary, cleanly digital
     page never triggers it, so this never reintroduces the "expensive
     per-word image analysis on every digital page" this project's own
     OCR work earlier already ruled out.

`force=True` (the operator's own explicit "Check Formatting"/"Re-OCR
Zone" action, per spec) bypasses this gate entirely for a one-off,
deliberate re-check even on an ordinary digital zone - a bounded,
single-zone cost the operator asked for, not an automatic blanket pass.

core.formatting_detector.is_small_caps has no equivalent in core.
fidelity_compare's own font_analyzer.py (which has no smallcaps field at
all) - this module reuses the Zoning app's own formatting stack
throughout, never fidelity_compare's, for exactly that reason."""
import re

_TAG_PATTERNS = {
    "bold": re.compile(r"<b>|<bold>"),
    "italic": re.compile(r"<i>|<italic>"),
    "underline": re.compile(r"<u>|<underline>"),
    "superscript": re.compile(r"<sup>"),
    "subscript": re.compile(r"<sub>"),
    "smallcaps": re.compile(r"<smallcaps>"),
}


def _tag_presence(tagged_text: str) -> dict:
    text = tagged_text or ""
    return {name: bool(pattern.search(text)) for name, pattern in _TAG_PATTERNS.items()}


def has_independent_evidence(page, zone) -> bool:
    """True only for the narrow "digital text on a not-cleanly-digital
    page" case (see module docstring) - never for a zone already using
    the visual/OCR path itself, and never for an ordinary, purely
    digital page (where there is nothing independent to check against,
    and where re-rendering/analyzing pixels would be the exact
    unnecessary per-zone image-analysis cost this project's own OCR work
    already ruled out for digital PDFs)."""
    from core.text_extractor import _zone_wants_image_formatting_detection
    from core.ocr.style_detector import page_is_image_dominated
    from core.ocr.text_quality import analyze_page_object, classify_page

    if _zone_wants_image_formatting_detection(page, zone):
        return False  # already visual/OCR-based - no independent alternative exists
    metrics = analyze_page_object(page, zone.page)
    if classify_page(metrics) in ("SCANNED", "MIXED", "OCR_REQUIRED", "UNKNOWN"):
        return True
    return page_is_image_dominated(page, zone.bbox)


def check_formatting(page, zone, force: bool = False):
    """Returns None when there is no independent second source to check
    against and `force` wasn't requested. Otherwise returns:
        {"native": {tag: bool, ...}, "visual": {tag: bool, ...},
         "agrees": bool, "mismatches": [tag, ...]}
    `native` reflects core.text_extractor's own current decision (what
    would actually be generated today); `visual` reflects the
    independent OCR/pixel-based re-check."""
    from core.ocr.style_detector import detect_ocr_zone_lines
    from core.text_extractor import extract_zone_formatted_text

    if not force and not has_independent_evidence(page, zone):
        return None

    native_text = extract_zone_formatted_text(page, zone)
    visual_lines = detect_ocr_zone_lines(page, zone)
    visual_text = "\n".join(visual_lines)

    native_tags = _tag_presence(native_text)
    visual_tags = _tag_presence(visual_text)
    mismatches = [tag for tag in native_tags if native_tags[tag] != visual_tags.get(tag, False)]
    return {"native": native_tags, "visual": visual_tags, "agrees": not mismatches, "mismatches": mismatches}


_TAG_TO_ISSUE_TYPE = {
    "bold": "BOLD", "italic": "ITALIC", "underline": "UNDERLINE",
    "superscript": "SUPERSCRIPT", "subscript": "SUBSCRIPT", "smallcaps": "SMALLCAPS",
}


def issue_type_for_tag(tag: str) -> str:
    return _TAG_TO_ISSUE_TYPE.get(tag, "INLINE_STYLE")
