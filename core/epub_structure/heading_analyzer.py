"""Step 4 (spec section 4) - analyzes a document's headings and builds
their hierarchy.

Confirmed against a real production EPUB before writing this: a real book
can emit EVERY numbered heading ("1 Introduction", "1.1 The discovery...",
"1.2 Characteristics...") as a literal <h1> - the raw HTML heading LEVEL
carries no hierarchy information at all in that case. The heading's own
NUMBER (dot-depth: "1" -> depth 1, "1.1" -> depth 2, "1.1.1" -> depth 3) is
therefore the PRIMARY hierarchy signal when a heading has one; the raw
HTML tag level (h1=1 .. h6=6) is only the fallback for headings that carry
no number at all (spec: "must also support books where numbering is
absent") - exactly what this module does, never assuming one signal is
always present.

A whole book's TOC hierarchy is deliberately NOT built here - hierarchy
across chapter N's own un-numbered title and its "N.1"/"N.2" subsections
is built PER DOCUMENT ONLY; the book-level nesting (each document as one
top-level entry, in document order) is core.epub_structure.nav_generator's
job. This avoids ever having to assume chapter N's internal numbering
matches its position among the book's other chapters (confirmed false in a
real book: chapters 7-9 have un-numbered top titles but still-numbered
"7.1"/"9.1" subsections)."""
import re
from dataclasses import dataclass, field

from core.epub_structure.xhtml_parser import local_name

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_NUMBERED_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.:]?\s+(\S.*)$")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class Heading:
    element: object                  # the real lxml Element - callers may still need raw attributes
    id: str = ""
    tag_level: int = 1                 # raw HTML heading level (1-6)
    number: str = ""                   # e.g. "1.1", or "" if this heading isn't numbered
    depth: int = 1                     # the EFFECTIVE hierarchy depth used for nesting (see module docstring)
    text: str = ""                     # the heading's own full text, numbering stripped when present
    raw_text: str = ""                  # the heading's full text exactly as written (numbering included)
    parent: "Heading" = None
    children: list = field(default_factory=list)


def _clean_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def extract_headings(tree) -> list:
    """Returns every heading in DOCUMENT ORDER as a flat list of Heading
    records (each still carrying .parent/.children once build_hierarchy()
    below has run on the result) - never skips a heading just because it
    lacks an id or a number."""
    headings = []
    for el in tree.iter():
        level = _HEADING_TAGS.get(local_name(el.tag))
        if level is None:
            continue
        raw_text = _clean_text("".join(el.itertext()))
        m = _NUMBERED_RE.match(raw_text)
        if m:
            number, text = m.group(1), _clean_text(m.group(2))
            depth = number.count(".") + 1
        else:
            number, text = "", raw_text
            depth = level
        headings.append(Heading(
            element=el, id=el.get("id") or "", tag_level=level,
            number=number, depth=depth, text=text, raw_text=raw_text,
        ))
    return headings


def build_hierarchy(headings: list) -> list:
    """Nests a flat, document-order Heading list by .depth using a simple
    stack (a smaller depth number closes every open heading whose own
    depth is >= the new one, mirroring exactly how h1/h2/h3 nesting would
    work if the raw tag level were reliable - this is the same algorithm,
    just driven by the more reliable effective depth). Returns the list of
    TOP-LEVEL Heading records (depth-1 roots); every other heading is
    reachable through .children. A heading whose depth skips a level from
    its immediate predecessor (e.g. depth 1 directly followed by depth 3)
    is still nested under the nearest preceding shallower heading - never
    dropped, never promoted to a fake root, since spec 4 explicitly warns
    against inventing structure that was not really there."""
    roots = []
    stack = []  # [(depth, Heading), ...] - open ancestors, shallowest first
    for h in headings:
        while stack and stack[-1][0] >= h.depth:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            h.parent = parent
            parent.children.append(h)
        else:
            roots.append(h)
        stack.append((h.depth, h))
    return roots
