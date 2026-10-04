"""Steps 17-21 (spec: "FIGURE / TABLE ANALYSIS" / "FIGURE/TABLE
FIRST-CITATION PLACEMENT" / "FIGURE/TABLE MATCHING" / "CROSS-FILE
REFERENCES") - builds a whole-book registry of numbered figures/tables
and every place they are cited in body text, book-wide.

Token-aware number matching (spec 21/"FIGURE/TABLE NUMBER MATCHING"):
"Figure 2.1" and "Figure 2.10" are two clean, DIFFERENT matches of
\\d+(?:[.\\-]\\d+)* - the trailing digits are captured whole (greedy,
word-bounded), never accidentally split ("2.1" is never a prefix match
against "2.10"'s own "2.1" substring).

Citation matching supports "Fig."/"Figure"/"Figs."/"FIGURE", "Table",
"Plate", and "Diagram" as independent keyword families, each its own
citation-kind NAMESPACE (a "Plate 1" citation can never match a "Figure 1"
entry, even though both happen to be numbered "1") - confirmed as a real
risk: real books commonly restart plate/diagram numbering independently
of figure numbering. Also supports a range ("Figures 2.1-2.3") and a list
("Figs. 1.1 and 1.2") by yielding one citation per WRITTEN endpoint/item -
a range's own unwritten interior numbers are never invented as citations.

iter_citations() operates on an element's FLATTENED text (.itertext()
joined) - this already correctly detects a citation split across inline
markup ("Fig. <i>1.1</i>") for counting/first-citation-placement purposes,
since itertext() flattens across tag boundaries by construction; only
actually INSERTING a link for such a split citation needs extra care (see
core.epub_structure.figure_table_links, which handles that case
separately)."""
import re
from dataclasses import dataclass, field

from core.epub_structure.xhtml_parser import local_name

_KEYWORD_RE = re.compile(r"\b(Fig(?:ure|s|\.)?|Table|Plate|Diagram)s?\.?\s*", re.IGNORECASE)
# Dot-chained numbers ("2", "2.1", "2.3.1") can go arbitrarily deep; a
# hyphen ("2-1", spec's own example for "Figure 2-1") is accepted as an
# ALTERNATE 2-segment form only, tried after the dot-chain fails, so a
# hyphen used as a RANGE separator ("Figures 2.1-2.3") is never swallowed
# into the number itself - _CONTINUATION_RE below is what picks up a
# genuine range's second endpoint.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)+|\d+-\d+|\d+")
_CONTINUATION_RE = re.compile(r"\s*(?:[-–—]\s*|,\s*(?:and\s+)?|and\s+)", re.IGNORECASE)
# "Fig. 1.3(a)", "Fig. 1.8(b)" - a sub-part letter immediately (no
# whitespace) following the number is part of the VISIBLE citation label
# to link, but never part of the number itself used for registry lookup
# (spec: "The target must be determined from Fig. 1.3, not from fig4").
_LETTER_SUFFIX_RE = re.compile(r"\([a-zA-Z]\)")


def _extend_with_suffix(text: str, end: int) -> int:
    sm = _LETTER_SUFFIX_RE.match(text, end)
    return sm.end() if sm else end


def _citation_kind(word: str) -> str:
    w = word.lower()
    if w.startswith("table"):
        return "table"
    if w.startswith("plate"):
        return "plate"
    if w.startswith("diagram"):
        return "diagram"
    return "figure"


def report_kind(citation_kind: str) -> str:
    """The 2-way bucket (spec 21's own report sections: FIGURES / TABLES) -
    "plate"/"diagram" are real, independent citation-kind NAMESPACES for
    matching purposes, but are still conceptually figures for reporting/
    placement purposes."""
    return "table" if citation_kind == "table" else "figure"


def _normalize_number(raw: str) -> str:
    return raw.replace("-", ".")


def iter_citations(text: str):
    """Yields (citation_kind, number, start, end) for every citation found
    in `text`, in left-to-right order. `start`/`end` bound the FULL match
    (keyword word through the number) for the first item of a range/list;
    a continuation item's own span covers just its own number (the shared
    keyword is never re-included)."""
    results = []
    for km in _KEYWORD_RE.finditer(text):
        kind = _citation_kind(km.group(1))
        pos = km.end()
        nm = _NUMBER_RE.match(text, pos)
        if not nm:
            continue
        pos = _extend_with_suffix(text, nm.end())
        results.append((kind, _normalize_number(nm.group(0)), km.start(), pos))
        while True:
            cm = _CONTINUATION_RE.match(text, pos)
            if not cm:
                break
            nm2 = _NUMBER_RE.match(text, cm.end())
            if not nm2:
                break
            pos = _extend_with_suffix(text, nm2.end())
            results.append((kind, _normalize_number(nm2.group(0)), nm2.start(), pos))
    return results


@dataclass
class Citation:
    doc_path: str
    element: object            # the real <p> (or similar) element containing the citation text
    match_text: str
    doc_order: int
    element_index: int          # this element's position among ITS OWN document's citable elements, for ordering


@dataclass
class FigureTableEntry:
    id: str
    kind: str                  # "figure" or "table" (from <figure class="...Table..."> markup) - drives placement/reporting
    citation_kind: str = ""      # "figure"/"table"/"plate"/"diagram" (from the caption's OWN keyword) - drives citation matching
    number: str = ""             # e.g. "1.1", "" if the caption has no parseable number
    doc_path: str = ""
    doc_order: int = 0
    element: object = None         # the real <figure> Element
    element_index: int = 0          # this figure's own position among its document's block-level children
    caption_text: str = ""
    citations: list = field(default_factory=list)   # Citation, in whole-book reading order


def _caption_text(figure_el) -> str:
    for child in figure_el:
        if local_name(child.tag) == "figcaption":
            return "".join(child.itertext()).strip()
    return ""


def _parse_leading_citation(caption: str):
    """Returns (citation_kind, number) from the caption's OWN first
    citation-shaped token, or ("", "") if the caption has none."""
    found = iter_citations(caption)
    if not found:
        return "", ""
    kind, number, _start, _end = found[0]
    return kind, number


def _block_index_map(tree) -> dict:
    """Maps every Element to its own 0-based position among ALL elements
    in document order - a cheap, real (not guessed) "how far into the
    document is this" measure used to order same-document citations and
    figures relative to each other."""
    return {el: i for i, el in enumerate(tree.iter())}


def build_figure_table_registry(registry) -> list:
    """THE single entry point. Returns [FigureTableEntry, ...] in whole-
    book (document order, then in-document order). Citation search
    deliberately walks <p> AND heading elements but explicitly EXCLUDES
    text inside <figure>/<figcaption> - a caption labeling its own figure
    ("Fig. 1.1. C60: buckminsterfullerene.") is not a CITATION of that
    figure from body text, and counting it as one would make every figure
    with a numbered caption falsely appear to be "cited" at its own
    current position."""
    entries = []
    all_citations_by_key = {}   # (citation_kind, number) -> [Citation, ...], book-wide, in reading order

    for doc in registry.documents:
        if doc.tree is None:
            continue
        index_map = _block_index_map(doc.tree)
        for el in doc.tree.iter():
            ln = local_name(el.tag)
            if ln == "figure":
                kind = "table" if "table" in (el.get("class") or "").lower() else "figure"
                caption = _caption_text(el)
                citation_kind, number = _parse_leading_citation(caption)
                entries.append(FigureTableEntry(
                    id=el.get("id") or "", kind=kind, citation_kind=citation_kind, number=number,
                    doc_path=doc.path, doc_order=doc.order, element=el, element_index=index_map.get(el, -1),
                    caption_text=caption))
            elif ln in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
                # Skip anything nested inside a figure/figcaption - see docstring.
                ancestor = el.getparent()
                inside_figure = False
                while ancestor is not None:
                    if local_name(ancestor.tag) in ("figure", "figcaption"):
                        inside_figure = True
                        break
                    ancestor = ancestor.getparent()
                if inside_figure:
                    continue
                text = "".join(el.itertext())
                for kind, number, start, end in iter_citations(text):
                    citation = Citation(doc_path=doc.path, element=el, match_text=text[start:end],
                                         doc_order=doc.order, element_index=index_map.get(el, -1))
                    all_citations_by_key.setdefault((kind, number), []).append(citation)

    for entry in entries:
        if not entry.number:
            continue
        candidates = all_citations_by_key.get((entry.citation_kind, entry.number), [])
        entry.citations = sorted(candidates, key=lambda c: (c.doc_order, c.element_index))

    return entries


def first_citation(entry: FigureTableEntry):
    """Returns the Citation that appears FIRST in whole-book reading order,
    or None if this figure/table has no real citation anywhere (spec 23:
    "If no citation is found: do not randomly move it")."""
    return entry.citations[0] if entry.citations else None
