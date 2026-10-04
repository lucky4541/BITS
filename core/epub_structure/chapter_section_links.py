"""Step 4 (spec: "CHAPTER / SECTION CROSS-LINKS") - detects references to
chapters/sections in body text and links them to the real heading they
name, using the SAME global heading data core.epub_structure.registry
already built (never a second, independent heading scan).

Explicit numbered references ("Chapter 3", "Section 1.2", "Chapters 2 and
3", "Sections 1.1-1.3") are matched with HIGH confidence whenever exactly
one real heading has that number. Relative references ("see next
section", "see previous chapter") are resolved relative to the CITING
paragraph's own nearest preceding heading, WITHIN THE SAME DOCUMENT ONLY -
never guessed across a document boundary, since "the next section" from
the very last section of a chapter would otherwise require assuming which
document comes next, a judgment call this module deliberately leaves for
REVIEW rather than making. "see above"/"see below" are always REVIEW -
genuinely too vague (no specific heading is named) to resolve safely."""
import posixpath
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.xhtml_parser import XHTML_NS, local_name

_KEYWORD_RE = re.compile(r"\b(Chapters?|Sections?|Secs?\.)\s*", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)+|\d+")
_CONTINUATION_RE = re.compile(r"\s*(?:[-–—]\s*|,\s*(?:and\s+)?|and\s+)", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"\bsee\s+(above|below|(?:the\s+)?next\s+(section|chapter)|(?:the\s+)?previous\s+(section|chapter))\b",
    re.IGNORECASE)


def _kind_of(word: str) -> str:
    return "chapter" if word.lower().startswith("chap") else "section"


@dataclass
class LinkResult:
    detected: int = 0
    created: int = 0
    already_valid: int = 0
    review: list = field(default_factory=list)   # (doc_path, text, reason)


def _iter_explicit(text: str):
    """Yields (kind, number, start, end) - same keyword+range+list pattern
    already proven for figure/table citations, applied here to Chapter/
    Section wording."""
    results = []
    for km in _KEYWORD_RE.finditer(text):
        kind = _kind_of(km.group(1))
        pos = km.end()
        nm = _NUMBER_RE.match(text, pos)
        if not nm:
            continue
        results.append((kind, nm.group(0), km.start(), nm.end()))
        pos = nm.end()
        while True:
            cm = _CONTINUATION_RE.match(text, pos)
            if not cm:
                break
            nm2 = _NUMBER_RE.match(text, cm.end())
            if not nm2:
                break
            results.append((kind, nm2.group(0), nm2.start(), nm2.end()))
            pos = nm2.end()
    return results


def _heading_index(registry):
    """(kind, number) -> [(doc, heading), ...] book-wide - "chapter" means
    a depth-1 (undotted) heading number; "section" means anything else."""
    by_key = {}
    for doc in registry.documents:
        for h in doc.headings:
            if not h.number or not h.id:
                continue
            kind = "chapter" if "." not in h.number else "section"
            by_key.setdefault((kind, h.number), []).append((doc, h))
    return by_key


def _block_index_map(tree) -> dict:
    return {el: i for i, el in enumerate(tree.iter())}


def _nearest_preceding_heading(doc, index_map, element_index):
    best = None
    for h in doc.headings:
        h_index = index_map.get(h.element, -1)
        if h_index <= element_index and (best is None or h_index > index_map.get(best.element, -1)):
            best = h
    return best


def _resolve_relative(doc, index_map, element_index, phrase: str):
    """Returns a Heading or None. `phrase`: the matched relative-reference
    text, lowercased. Only "next"/"previous" section/chapter are ever
    resolved; "above"/"below" always return None (caller reports REVIEW)."""
    phrase = phrase.lower()
    if "above" in phrase or "below" in phrase:
        return None
    current = _nearest_preceding_heading(doc, index_map, element_index)
    if current is None:
        return None
    current_pos = index_map.get(current.element, -1)
    ordered = sorted(doc.headings, key=lambda h: index_map.get(h.element, -1))
    is_chapter = "chapter" in phrase
    candidates = [h for h in ordered if h.number and "." not in h.number] if is_chapter else ordered
    if "next" in phrase:
        for h in candidates:
            if index_map.get(h.element, -1) > current_pos:
                return h
        return None
    if "previous" in phrase:
        prior = [h for h in candidates if index_map.get(h.element, -1) < current_pos]
        return prior[-1] if prior else None
    return None


def apply_chapter_section_links(registry) -> LinkResult:
    """THE single entry point. Mutates each document's tree in place and
    marks it dirty; the orchestrator serializes dirty documents exactly
    like every other generation step. Never touches text inside a heading
    itself, a figure/figcaption, or an already-existing <a> (spec 10:
    "Before creating a new link check whether the element is already
    inside <a>")."""
    result = LinkResult()
    by_key = _heading_index(registry)

    def _resolve_text(text: str, doc, el, citing_element_index: int):
        resolved_spans = []   # (start, end, target_doc, target_id, text)
        for kind, number, start, end in _iter_explicit(text):
            result.detected += 1
            candidates = by_key.get((kind, number), [])
            if len(candidates) != 1:
                if not candidates:
                    result.review.append((doc.path, text[start:end],
                                           "no heading with this number exists anywhere in the book"))
                else:
                    result.review.append((doc.path, text[start:end],
                                           f"ambiguous - {len(candidates)} headings share this number"))
                continue
            target_doc, heading = candidates[0]
            resolved_spans.append((start, end, target_doc.path, heading.id, text[start:end]))

        for m in _RELATIVE_RE.finditer(text):
            result.detected += 1
            phrase = m.group(1)
            if "above" in phrase.lower() or "below" in phrase.lower():
                result.review.append((doc.path, m.group(0), "'see above/below' is too vague to resolve safely"))
                continue
            heading = _resolve_relative(doc, index_map, citing_element_index, phrase)
            if heading is None:
                result.review.append((doc.path, m.group(0),
                                       "no next/previous heading exists within this same document"))
                continue
            resolved_spans.append((m.start(1), m.end(1), doc.path, heading.id, m.group(1)))
        return resolved_spans

    for doc in registry.documents:
        if doc.tree is None:
            continue
        index_map = _block_index_map(doc.tree)
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

            # Relative references ("see next section") always resolve
            # against THIS block's own nearest-preceding-heading, whether
            # the matched text sits in el.text or in a child's own tail.
            citing_element_index = index_map.get(el, -1)

            # Captured BEFORE el.text is processed below - a NEWLY-inserted
            # anchor's own .tail is exactly the tail end of the SAME
            # el.text string _resolve_text just fully scanned in one pass
            # (review items included); re-scanning it via a freshly-
            # fetched list(el) would report the same unresolved reference
            # a second time (confirmed real bug: "Chapter 2 ... Section
            # 1.1 ... see above" duplicated "see above" into review twice).
            original_children = list(el)

            if el.text:
                resolved_spans = _resolve_text(el.text, doc, el, citing_element_index)
                if resolved_spans:
                    resolved_spans.sort(key=lambda s: s[0])
                    _apply_spans(el, resolved_spans)
                    result.created += len(resolved_spans)
                    changed_here = True

            for child in original_children:
                # See reference_links.py's own identical fix: only figure/
                # figcaption hard-block a whole sibling subtree - an <a>
                # child's own TAIL is ordinary, unlinked text and must
                # still be scanned.
                if local_name(child.tag) in ("figure", "figcaption") or not child.tail:
                    continue
                resolved_spans = _resolve_text(child.tail, doc, el, citing_element_index)
                if resolved_spans:
                    resolved_spans.sort(key=lambda s: s[0])
                    _apply_tail_spans(el, child, resolved_spans)
                    result.created += len(resolved_spans)
                    changed_here = True

        if changed_here:
            doc.dirty = True
    return result


def _apply_spans(el, spans):
    """spans: [(start, end, target_doc_path, target_id, text), ...] sorted
    by start, all within el.text - rewrites el.text into
    [leading][<a>]...[<a>][trailing], exactly like figure_table_links'
    own _split_and_link (never removes/adds a visible character)."""
    text = el.text
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id, span_text) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        anchor.set("href", f"{posixpath.basename(target_doc_path)}#{target_id}")
        anchor.text = span_text
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
    el.text = leading
    for offset, anchor in enumerate(nodes):
        el.insert(offset, anchor)


def _apply_tail_spans(el, child, spans):
    """Same as _apply_spans but for spans found in `child.tail` rather than
    `el.text` - the new anchors are inserted into `el` right after `child`,
    never touching `child` itself (mirrors figure_table_links' own tail-
    handling)."""
    text = child.tail
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id, span_text) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        anchor.set("href", f"{posixpath.basename(target_doc_path)}#{target_id}")
        anchor.text = span_text
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
    child.tail = leading
    insert_at = el.index(child) + 1
    for offset, anchor in enumerate(nodes):
        el.insert(insert_at + offset, anchor)
