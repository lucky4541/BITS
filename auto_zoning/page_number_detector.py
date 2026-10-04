"""Header/footer band exclusion + page-number detection for one page, used
only by the Auto Analyse feature (auto_zoning/page_analyzer.py) - never by
Auto Zone, which has its own reference-template-driven pipeline.

The header/footer band is treated as OFF LIMITS to every other detector
(heading/paragraph/list/table never see a line from it) regardless of
whether a page number is actually found there - this is what stops running
header text ("CHAPTER 4", "Pain Management", ...) from ever becoming a
zone. The ONLY thing ever extracted from that band is, at most, one isolated
page-number line.

Single-page analysis genuinely cannot use "repeated position across pages"
(Auto Analyse is deliberately scoped to the current page only) - this uses
only the signals available on one page: band position, isolation (the
line's ENTIRE text, not a fragment of a longer line), and a plausible
page-number shape (short arabic run or roman numeral)."""
import re

HEADER_BAND_FRACTION = 0.12   # top of page
FOOTER_BAND_FRACTION = 0.10   # bottom of page

_ARABIC_RE = re.compile(r"^\d{1,4}$")
_ROMAN_RE = re.compile(r"^[ivxlcdmIVXLCDM]{1,15}$")


def _looks_like_page_number(text: str) -> bool:
    t = (text or "").strip().strip(".-")
    if not t:
        return False
    if _ARABIC_RE.match(t):
        return True
    # Roman numerals: require a real roman-numeral shape, not just any run
    # of the letters (avoids misreading a stray "i" or "Mix" as a number) -
    # cheap validity check via a canonical round-trip.
    if _ROMAN_RE.match(t):
        return _is_valid_roman(t.upper())
    return False


def _is_valid_roman(s: str) -> bool:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    prev = 0
    for ch in reversed(s):
        v = values.get(ch)
        if v is None:
            return False
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total > 0


def detect(lines: list, page_width: float, page_height: float):
    """Returns (pagenumber_line_or_None, excluded_line_ids). `lines`: this
    page's pdf_block_detector.LineInfo list. excluded_line_ids is the id()
    of EVERY line found in the header or footer band - callers must drop
    all of them from heading/paragraph/list/table detection, whether or not
    a page number was found among them."""
    header_y_max = page_height * HEADER_BAND_FRACTION
    footer_y_min = page_height * (1.0 - FOOTER_BAND_FRACTION)

    header_lines = [li for li in lines if li.bbox[3] <= header_y_max]
    footer_lines = [li for li in lines if li.bbox[1] >= footer_y_min]
    excluded_ids = {id(li) for li in header_lines} | {id(li) for li in footer_lines}

    # Footer is checked first (page numbers live there far more often);
    # header is the fallback. Within a band, take the first isolated
    # number-shaped line - real running headers/footers with a number
    # ("Chapter 4  |  12") are excluded because the CANDIDATE line's own
    # text must be ENTIRELY the number, never a substring of a longer line.
    for band in (footer_lines, header_lines):
        for li in band:
            if _looks_like_page_number(li.text):
                return li, excluded_ids
    return None, excluded_ids
