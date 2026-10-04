"""Content-based signals for Bibliography Auto Zone (spec: "Intelligent
Bibliography Auto-Zone Using Indentation + Content" sections 3/7/8/9/21-27).
Pure text-pattern heuristics over a single extracted line's own plain
text - never a second OCR/extraction engine (auto_zoning.pdf_block_
detector's own LineInfo.text is the only input), and never used ALONE:
auto_zoning.bibliography_auto_zone combines these with the existing
geometry (indentation/vertical-gap) signals into one score, exactly as
spec 13 requires ("Do NOT rely on only one signal").

Deliberately heuristic, not a bibliographic-citation parser: every
function here answers "does this look like X", never "this IS X" -
matching the spec's own framing ("use as SUPPORTING evidence", "signals",
never a hard classification rule)."""
import re

# Surname-first author-start patterns (spec 21): "Smith, John.", "Smith, J.",
# "Smith J." (no comma), "van der Waals, John.", "de Silva, A.", "O'Connor, J.",
# "McDonald, A." - a leading lowercase "particle" (van/der/de/von/da/dos) is
# allowed before the capitalized surname; apostrophes/hyphens are allowed
# inside the surname itself.
_SURNAME = r"(?:(?:[a-z]+\s+){0,2})?[A-Z][A-Za-z'’\-]*"
_AUTHOR_COMMA_RE = re.compile(rf"^{_SURNAME}\s*,\s*[A-Z]")
_AUTHOR_NOCOMMA_RE = re.compile(rf"^{_SURNAME}\s+[A-Z]\.?\s")
_MULTI_AUTHOR_RE = re.compile(rf"^{_SURNAME}\s*,\s*[A-Z][\w.'’\-]*\s+(and|&)\s+{_SURNAME}", re.I)
_ET_AL_RE = re.compile(rf"^{_SURNAME}(\s*,\s*[A-Z][\w.'’\-]*)?\s+et\s+al\.?", re.I)

# Organization/corporate authors (spec 22): a capitalized multi-word phrase
# ending in a recognizable institutional noun, or a well-known all-caps
# acronym - never an exhaustive list, just common, high-confidence cases.
_ORG_SUFFIX_RE = re.compile(
    r"^[A-Z][\w&.,'’\- ]*\b(Organization|Organisation|University|Institute|Association|"
    r"Society|Foundation|Committee|Agency|Council|Union|Nations|Ministry|Department)\b")
_ORG_ACRONYM_RE = re.compile(r"^(WHO|UNESCO|UNICEF|NASA|NATO|UNDP|OECD|EU|UN)\b[.,]?\s")

# Publication year (spec 8): a plausible 4-digit year (1500-2099, a
# generous real-world range), optionally parenthesized/followed by
# terminal punctuation - "do not assume every four-digit number is a
# publication year" is handled by callers checking POSITION (start of
# line / right after an author) rather than scanning the whole line.
_YEAR_RE = re.compile(r"\(?((?:1[5-9]\d{2})|(?:20\d{2}))\)?\s*[.,;)]?")
_YEAR_START_RE = re.compile(r"^\(?((?:1[5-9]\d{2})|(?:20\d{2}))\)?\s*[.,;]")

# Page ranges (spec 25) and DOI/URL (spec 24) - content that must NEVER be
# mistaken for an entry-start, regardless of where it happens to sit.
_PAGE_RANGE_RE = re.compile(r"\bS?\d+\s*[-–—]\s*S?\d+\b")
_URL_OR_DOI_RE = re.compile(r"(https?://|www\.|doi\.org/|10\.\d{4,9}/)", re.I)

# A title that happens to START with a number (spec 26, e.g. "1984: A
# Study...") must never be confused with a year-only entry-start: a real
# year-start is followed by '.'/','/':'/';' immediately (no further title
# words glued on with no separator) - "1984:" is intentionally excluded
# from _YEAR_START_RE's own punctuation class for exactly this reason,
# but is double-checked explicitly here for clarity/robustness.
_TITLE_LEADING_NUMBER_RE = re.compile(r"^\d{4}:\S")


def looks_like_author_start(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    return bool(_AUTHOR_COMMA_RE.match(text) or _AUTHOR_NOCOMMA_RE.match(text)
                or _MULTI_AUTHOR_RE.match(text) or _ET_AL_RE.match(text))


def looks_like_organization_start(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    return bool(_ORG_SUFFIX_RE.match(text) or _ORG_ACRONYM_RE.match(text))


def looks_like_year_only_start(text: str) -> bool:
    """spec 9/27: 'ditto-style' entries where the repeated author is
    omitted and the entry starts directly with the year, e.g. '1989.
    Discussion: What's Wrong with...'."""
    text = text.strip()
    if _TITLE_LEADING_NUMBER_RE.match(text):
        return False
    return bool(_YEAR_START_RE.match(text))


def has_year_near_start(text: str, window: int = 40) -> bool:
    """A plausible year appearing early in the line (e.g. right after an
    author name: 'Smith, John. 2019. Title...') - supporting evidence for
    an entry-start, distinct from looks_like_year_only_start's stricter
    'the line IS just the year' case."""
    text = text.strip()
    return bool(_YEAR_RE.search(text[:window]))


def looks_like_entry_start(text: str) -> bool:
    """Combined "this line's own content looks like where a NEW
    bibliography reference begins" signal (spec 7's worked pattern list) -
    author, organization, or a bare/ditto-style year opening."""
    return (looks_like_author_start(text) or looks_like_organization_start(text)
            or looks_like_year_only_start(text))


def has_page_range(text: str) -> bool:
    return bool(_PAGE_RANGE_RE.search(text))


def has_url_or_doi(text: str) -> bool:
    return bool(_URL_OR_DOI_RE.search(text))


def looks_like_continuation_content(prev_text: str, text: str) -> bool:
    """spec 12: supporting evidence a line CONTINUES the previous one
    rather than starting a new entry - the previous line has no terminal
    sentence punctuation (mid-title/mid-sentence wrap), OR this line is a
    page-range/URL/DOI continuation, OR this line starts with a lowercase
    letter (grammatical continuation)."""
    prev_text = prev_text.rstrip()
    text = text.strip()
    if not text:
        return False
    if has_page_range(text) or has_url_or_doi(text) or has_url_or_doi(prev_text):
        return True
    if text[0].islower():
        return True
    if prev_text and prev_text[-1] not in ".!?":
        return True
    return False


def extract_author(text: str) -> str:
    """Best-effort author-name snippet for spec 52's detection report -
    "" when no author-like pattern is found at all (never a guess)."""
    text = text.strip()
    m = _AUTHOR_COMMA_RE.match(text) or _AUTHOR_NOCOMMA_RE.match(text)
    return m.group(0).rstrip(", ") if m else ""


def extract_year(text: str) -> str:
    """Best-effort publication-year snippet for spec 52's detection
    report - "" when no plausible year is found (never a guess)."""
    m = _YEAR_RE.search(text)
    return m.group(1) if m else ""


def looks_like_bibliography_heading(text: str) -> bool:
    """spec 20: 'Bibliography'/'References'/'Works Cited'/'References and
    Notes' (and common minor variants) must never be zoned as an entry -
    the heading itself remains governed by the existing heading system,
    never this feature. Exact-ish match (case-insensitive, punctuation-
    stripped) rather than a substring check, so a real reference that
    merely CONTAINS the word "References" (e.g. a journal titled
    "References in..." ) is never wrongly excluded."""
    normalized = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    return normalized in {
        "bibliography", "references", "works cited", "references and notes",
        "reference list", "cited works", "literature cited",
    }
