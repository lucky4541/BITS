"""Role-aware superscript/subscript + page-number classification.

Explicitly NOT "if small: sup" / "if numeric: sup" heuristics (the exact
anti-pattern the spec this was built from prohibits) - role comes FIRST,
from signals the zoning stage already got right:

  - A page-number zone is already tagged core.constants.TAG_PAGENUMBER
    ("pagenumber").
  - A footnote/endnote zone is already tagged "fn"/"en" (NOTE_TAGS).
  - Whether a nested "en"/"fn" zone (or an inline digit-only run inside
    an ordinary paragraph) is a genuine citation marker reuses the exact
    same evidence _gen_endnote_marker already applies: the candidate's
    OWN text must be nothing but a bare number (re.fullmatch(r"\\s*(\\d+)
    \\s*", text)) - never inferred from size/position alone.

Only ONCE role is known does formatting verification even ask "should
this be superscript?" - a PAGE_NUMBER is never eligible for a sup/sub
issue at all, regardless of what the PDF's own font flags/geometry say,
matching the spec's own explicit priority (PAGE_NUMBER > BODY_FOOTNOTE_
CALLOUT > NORMAL_TEXT)."""
import re

from core.constants import TAG_PAGENUMBER

# footnote / endnote zone tags (BITS / JATS profiles: both become <fn>)
NOTE_TAGS = ("fn", "en")

PAGE_NUMBER = "PAGE_NUMBER"
BODY_FOOTNOTE_CALLOUT = "BODY_FOOTNOTE_CALLOUT"
FOOTNOTE_NUMBER = "FOOTNOTE_NUMBER"
NORMAL_TEXT = "NORMAL_TEXT"
OTHER = "OTHER"

_BARE_NUMBER_RE = re.compile(r"\s*(\d+)\s*")
_SENTENCE_END_CHARS = ".!?"


def is_page_number_zone(zone) -> bool:
    return zone.tag == TAG_PAGENUMBER


def note_zone_kind(zone):
    """Returns "fn"/"en" if this zone is a footnote/endnote zone (whether
    top-level content or a nested marker), else None."""
    return zone.tag if zone.tag in NOTE_TAGS else None


def classify_zone_role(zone) -> str:
    """Zone-level role - the FIRST, primary classification (spec: "First
    identify role... then determine formatting"). A zone nested inside
    another zone (zone.parent_id is not None) is treated as an inline
    marker candidate within its parent; a top-level fn/en zone is that
    note's own printed content."""
    if is_page_number_zone(zone):
        return PAGE_NUMBER
    note_tag = note_zone_kind(zone)
    if note_tag:
        if zone.parent_id is not None and (zone.text or "").strip() and \
                _BARE_NUMBER_RE.fullmatch(zone.text.strip()):
            return BODY_FOOTNOTE_CALLOUT
        return FOOTNOTE_NUMBER
    return NORMAL_TEXT


def classify_inline_candidate(candidate_text: str, preceding_char: str) -> str:
    """For a superscript/subscript-shaped run found WITHIN an ordinary
    paragraph zone's own text (no separate nested zone at all - the
    common shape for a digitally-typeset footnote callout, e.g.
    "...year.<sup>1</sup> Such a triumph..."): a short, bare digit run
    immediately after sentence-ending punctuation is a genuine body
    footnote callout; anything else (a math exponent, a chemical formula
    subscript, an ordinal suffix, a genuinely different superscript) is
    left as NORMAL_TEXT - never guessed into a footnote role just
    because it happens to be small/raised."""
    stripped = (candidate_text or "").strip()
    if stripped.isdigit() and 1 <= len(stripped) <= 3 and preceding_char in _SENTENCE_END_CHARS:
        return BODY_FOOTNOTE_CALLOUT
    return NORMAL_TEXT


def eligible_for_supsub_issue(role: str) -> bool:
    """PAGE_NUMBER is never eligible for a superscript/subscript issue at
    all (spec: "page numbers must never automatically become
    superscript... even if small/isolated/vertically offset"). Every
    other role may still genuinely need sup/sub (a footnote's own
    printed number is often itself typeset as an ordinary-sized digit,
    not raised - so this only EXCLUDES page numbers, it doesn't force
    any other role to require sup/sub either)."""
    return role != PAGE_NUMBER


# ============================================================
# Page-number OCR-confusion correction (role-gated, evidence-based)
# ============================================================

# A real, hand-verified confusion table (spec section 25's own explicit
# list) - applied ONLY after role == PAGE_NUMBER is already established
# from the zone's own tag (never inferred from the text's shape), and
# only ever OFFERED (never auto-applied) unless sequential-neighbor
# evidence confirms it (see suggest_page_number_correction below).
_CONFUSION_MAP = {
    "I": "1", "l": "1", "i": "1",
    "O": "0", "o": "0", "°": "0",  # degree sign
    "S": "5", "B": "8", "G": "6", "Z": "2",
}


def _apply_confusion_map(text: str) -> str:
    return "".join(_CONFUSION_MAP.get(ch, ch) for ch in text)


def is_roman_numeral(text: str) -> bool:
    from auto_zoning.page_number_detector import _ROMAN_RE, _is_valid_roman
    stripped = (text or "").strip()
    return bool(stripped) and bool(_ROMAN_RE.match(stripped)) and _is_valid_roman(stripped.upper())


def suggest_page_number_correction(zone_page: int, zone_text: str, neighboring_page_numbers: list):
    """`neighboring_page_numbers`: [(page_number: int, value: str), ...]
    for every OTHER PageNum zone already in the project (any classifiable
    digit value) - used purely as SEQUENTIAL evidence, never to invent a
    value out of nothing. Returns None when there's nothing to correct
    (already clean, or a genuine Roman numeral) or {"suggested": str,
    "confidence": float, "reason": str} otherwise - confidence is HIGH
    (0.9) only with a real sequential match, LOW (0.3) when the
    confusion-map substitution is the only evidence (never silently
    auto-applied at that confidence - see verification_engine.py's own
    auto_fixable gating)."""
    stripped = (zone_text or "").strip()
    if not stripped or stripped.isdigit():
        return None  # nothing to correct, or already a clean Arabic number
    if is_roman_numeral(stripped):
        return None  # Roman numerals are never digit-confusion-corrected

    candidate = _apply_confusion_map(stripped)
    if not candidate.isdigit():
        return None  # even after substitution, not a clean number - insufficient evidence

    candidate_int = int(candidate)
    for other_page, other_value in neighboring_page_numbers:
        other_value = (other_value or "").strip()
        if not other_value.isdigit() or other_page == zone_page:
            continue
        expected = int(other_value) + (zone_page - other_page)
        if expected == candidate_int:
            return {"suggested": candidate, "confidence": 0.9,
                    "reason": f"sequential match with page {other_page}'s own page number "
                              f"({other_value!r} -> {candidate!r})"}

    return {"suggested": candidate, "confidence": 0.3,
            "reason": "character-confusion pattern matched, but no sequential-neighbor evidence found"}
