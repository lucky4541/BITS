"""Step 5 (spec section 10) - finds every real pagebreak marker in a
document. Matches on the STRUCTURAL SIGNAL (an element carrying
epub:type="pagebreak", regardless of its own tag name or exactly which of
role="doc-pagebreak"/aria-label/id it also carries) - never assumes the
element is a <span>, and never invents a page number: a document with no
pagebreak markers at all simply contributes zero entries, and a gap in the
printed numbering (page 8 missing between 7 and 9) is preserved exactly as
found, never filled in."""
import re
from dataclasses import dataclass

from core.epub_structure.xhtml_parser import epub_type

_ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
_ARABIC_RE = re.compile(r"^\d+$")


@dataclass
class Pagebreak:
    element: object
    id: str = ""
    label: str = ""            # aria-label / printed page number, exactly as written


def extract_pagebreaks(tree) -> list:
    breaks = []
    for el in tree.iter():
        if epub_type(el) != "pagebreak":
            continue
        label = el.get("aria-label") or el.get("title") or ""
        breaks.append(Pagebreak(element=el, id=el.get("id") or "", label=label))
    return breaks


def page_sort_key(label: str):
    """Roman numerals sort before Arabic numbers (front matter is always
    numbered i/ii/iii.. before the book switches to 1/2/3..), and within
    each numbering system by real numeric value - never a plain string
    sort, which would put "10" before "2". A label that is neither (or
    empty) sorts last, in original order, rather than crashing or being
    silently dropped."""
    if _ARABIC_RE.match(label or ""):
        return (1, int(label))
    if _ROMAN_RE.match(label or ""):
        return (0, _roman_to_int(label.lower()))
    return (2, 0)


_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _roman_to_int(s: str) -> int:
    total = 0
    prev = 0
    for ch in reversed(s):
        val = _ROMAN_VALUES.get(ch, 0)
        total += -val if val < prev else val
        prev = max(prev, val)
    return total
