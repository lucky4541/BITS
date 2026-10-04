"""Step 8 (spec: "PAGE REFERENCE LINKS") - detects "p. 34", "p. 34.",
"pp. 34-36", "page 34", "pages 34-36" style references in body text and
links them to the REAL pagebreak marker (an existing
epub:type="pagebreak" element, per core.epub_structure.pagebreak_analyzer)
carrying that exact printed label. Never invents a pagebreak - a
reference to a page number no real pagebreak in the book actually carries
is left for REVIEW rather than guessed at, exactly like the chapter/
section/figure/table/reference matchers already in this module.

A mandatory space between the abbreviation and the number ("p. 34", never
"p.34") deliberately excludes common unrelated abbreviations that share
the same leading letters with no following space - "p.m."/"P.M." (time of
day) would otherwise falsely register "m" as a roman-numeral page
citation."""
import posixpath
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.xhtml_parser import XHTML_NS, local_name

_KEYWORD_RE = re.compile(r"\b(pp?\.|pages?)\s+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+|[ivxlcdm]+\b", re.IGNORECASE)
_CONTINUATION_RE = re.compile(r"\s*(?:[-–—]\s*|,\s*(?:and\s+)?|and\s+)", re.IGNORECASE)


@dataclass
class LinkResult:
    detected: int = 0
    created: int = 0
    review: list = field(default_factory=list)   # (doc_path, text, reason)


def _page_index(registry) -> dict:
    """label -> [(doc, Pagebreak), ...] book-wide - a label duplicated
    across the book yields >1 candidates and is reported REVIEW rather
    than guessed at, exactly like chapter/section numbers."""
    by_label = {}
    for doc in registry.documents:
        for pb in doc.pagebreaks:
            if not pb.label or not pb.id:
                continue
            by_label.setdefault(pb.label, []).append((doc, pb))
    return by_label


def _iter_references(text: str):
    """Yields (number, start, end) - same keyword+range+list continuation
    pattern already proven for figure/table and chapter/section wording."""
    results = []
    for km in _KEYWORD_RE.finditer(text):
        pos = km.end()
        nm = _NUMBER_RE.match(text, pos)
        if not nm:
            continue
        results.append((nm.group(0), km.start(), nm.end()))
        pos = nm.end()
        while True:
            cm = _CONTINUATION_RE.match(text, pos)
            if not cm:
                break
            nm2 = _NUMBER_RE.match(text, cm.end())
            if not nm2:
                break
            results.append((nm2.group(0), nm2.start(), nm2.end()))
            pos = nm2.end()
    return results


def apply_page_reference_links(registry) -> LinkResult:
    """THE single entry point. Mutates each document's tree in place and
    marks it dirty; the orchestrator serializes dirty documents exactly
    like every other generation step. Scans both an element's own .text
    AND every child's own .tail, so a reference appearing after an inline
    element (e.g. after a <sup>) in the same paragraph is never silently
    skipped."""
    result = LinkResult()
    by_label = _page_index(registry)

    def _resolve_spans(text: str, doc):
        spans = []   # (start, end, target_doc_path, target_id, text)
        for number, start, end in _iter_references(text):
            result.detected += 1
            candidates = by_label.get(number, [])
            if len(candidates) != 1:
                if not candidates:
                    result.review.append((doc.path, text[start:end],
                                           "no pagebreak with this label exists anywhere in the book"))
                else:
                    result.review.append((doc.path, text[start:end],
                                           f"ambiguous - {len(candidates)} pagebreaks share this label"))
                continue
            target_doc, pb = candidates[0]
            spans.append((start, end, target_doc.path, pb.id, text[start:end]))
        return spans

    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        for el in list(doc.tree.iter()):
            ln = local_name(el.tag)
            if ln not in ("p", "li"):
                continue
            ancestor = el.getparent()
            skip = False
            while ancestor is not None:
                if local_name(ancestor.tag) in ("figure", "figcaption", "a"):
                    skip = True
                    break
                ancestor = ancestor.getparent()
            if skip:
                continue

            # Captured BEFORE el.text is processed below - a NEWLY-inserted
            # anchor's own .tail is exactly the tail end of the SAME
            # el.text string _resolve_spans just fully scanned in one pass
            # (review items included); re-scanning it via a freshly-
            # fetched list(el) would report the same unresolved reference a
            # second time (see figure_table_links.py's own identical fix).
            original_children = list(el)

            if el.text:
                spans = _resolve_spans(el.text, doc)
                if spans:
                    spans.sort(key=lambda s: s[0])
                    _apply_spans(el, doc.path, spans)
                    result.created += len(spans)
                    changed_here = True

            for child in original_children:
                # See reference_links.py's own identical fix: only figure/
                # figcaption hard-block a whole sibling subtree - an <a>
                # child's own TAIL is ordinary, unlinked text and must
                # still be scanned.
                if local_name(child.tag) in ("figure", "figcaption") or not child.tail:
                    continue
                spans = _resolve_spans(child.tail, doc)
                if spans:
                    spans.sort(key=lambda s: s[0])
                    _apply_tail_spans(el, child, doc.path, spans)
                    result.created += len(spans)
                    changed_here = True

        if changed_here:
            doc.dirty = True
    return result


def _apply_spans(el, doc_path: str, spans):
    text = el.text
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id, span_text) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        rel_href = ("" if target_doc_path == doc_path else posixpath.basename(target_doc_path)) + f"#{target_id}"
        anchor.set("href", rel_href)
        anchor.text = span_text
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
    el.text = leading
    for offset, anchor in enumerate(nodes):
        el.insert(offset, anchor)


def _apply_tail_spans(el, child, doc_path: str, spans):
    text = child.tail
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id, span_text) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        rel_href = ("" if target_doc_path == doc_path else posixpath.basename(target_doc_path)) + f"#{target_id}"
        anchor.set("href", rel_href)
        anchor.text = span_text
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
    child.tail = leading
    insert_at = el.index(child) + 1
    for offset, anchor in enumerate(nodes):
        el.insert(insert_at + offset, anchor)


_LINK_TEXT_RE = re.compile(r"^\s*(pp?\.|pages?)\s+(\d+|[ivxlcdm]+)\s*$", re.IGNORECASE)
_NUMBER_ONLY_RE = re.compile(r"^\s*(\d+|[ivxlcdm]+)\s*$", re.IGNORECASE)
_CONTINUATION_FULL_RE = re.compile(r"^\s*(?:[-–—]\s*|,\s*(?:and\s+)?|and\s+)$", re.IGNORECASE)


def _in_index(el) -> bool:
    cur = el
    while cur is not None:
        etype = (cur.get("{http://www.idpf.org/2007/ops}type") or cur.get("epub:type") or "")
        if "index" in etype.split() or "index" in (cur.get("class") or "").split():
            return True
        cur = cur.getparent()
    return False


def _unwrap(anchor):
    """Replace <a>text</a> with its text, keeping the surrounding text."""
    parent = anchor.getparent()
    if len(anchor):  # never expected for these links - leave anything unusual alone
        return False
    text = (anchor.text or "") + (anchor.tail or "")
    prev = anchor.getprevious()
    if prev is not None:
        prev.tail = (prev.tail or "") + text
    else:
        parent.text = (parent.text or "") + text
    parent.remove(anchor)
    return True


def remove_page_reference_links(registry) -> int:
    """Undo links an EARLIER packaging run added to running-text page
    references ("p. 60", "pp. 184-8"), so re-packaging an already
    packaged folder also ends up without them. Only removes <a> elements
    that (1) point at a real pagebreak id, (2) sit in a <p>/<li> outside
    any index, and (3) read "p./pp./page/pages N" - or are the plain
    number right after such a link joined by "-", ",", "and" (the second
    half of a range). Index locator links and every other link are kept.
    Returns the number of links removed."""
    pb_ids = {pb.id for doc in registry.documents for pb in doc.pagebreaks if pb.id}
    removed = 0
    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed = False
        for el in list(doc.tree.iter()):
            if not isinstance(el.tag, str) or local_name(el.tag) not in ("p", "li") or _in_index(el):
                continue
            prev_was_page_link = False
            for child in list(el):
                is_page_link = False
                if isinstance(child.tag, str) and local_name(child.tag) == "a" and len(child) == 0:
                    frag = (child.get("href") or "").rsplit("#", 1)[-1]
                    if frag in pb_ids:
                        text = child.text or ""
                        if _LINK_TEXT_RE.match(text):
                            is_page_link = True
                        elif prev_was_page_link and _NUMBER_ONLY_RE.match(text):
                            # previous sibling was a page link whose tail is
                            # just "-", ",", "and" (checked when it was seen)
                            is_page_link = True
                if is_page_link:
                    tail = child.tail or ""
                    if _unwrap(child):
                        removed += 1
                        changed = True
                    # a following range number must be joined by this link's own tail text
                    prev_was_page_link = bool(_CONTINUATION_FULL_RE.match(tail))
                else:
                    prev_was_page_link = False
        if changed:
            doc.dirty = True
    return removed
