"""Step 7 (spec: "BIBLIOGRAPHY / REFERENCE LINKS") - detects numbered
citations in body text ("(1.1)", "(1.1, 1.2)", "(1.1–1.5)", "[1]", "[1,2]",
"[1-5]") and links them to the real bibliography/reference entry they
name.

Confirmed against a real production EPUB before writing this: bibliography
entries (<li epub:type="biblioentry">) commonly carry NO id at all - their
own identifying number lives only in their own leading text, e.g. "(1.1)
H. W. Kroto, ...". This module therefore (a) parses each entry's own
leading number to build the citation registry, and (b) assigns a fresh,
deterministic id ONLY to an entry a real citation actually needs to link
to (spec 13: "If a target has no ID and an ID is genuinely required:
generate one deterministically" - never unconditionally id-ifying every
entry regardless of whether anything cites it).

Chemical/mathematical notation safety (spec 11) is satisfied by
construction: a citation is only ever recognized INSIDE a real "(...)" or
"[...]" delimiter pair, so "C<sup>60</sup>" (which flattens to the bare,
undelimited text "C60") can never match this module's own citation
pattern at all - and even a genuinely delimited but coincidental
parenthetical number (e.g. "(60)" meaning something else entirely, not a
citation) is only ever linked when a real bibliography entry with that
exact number actually exists; if none does, it is correctly left alone."""
import posixpath
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.id_assigner import derive_content_prefix
from core.epub_structure.xhtml_parser import XHTML_NS, epub_type, local_name

_ENTRY_LEADING_RE = re.compile(r"^\s*[\[(]?\s*(\d+(?:\.\d+)*)\s*[\])]?")
_PAREN_GROUP_RE = re.compile(r"\((\d+(?:\.\d+)*(?:\s*[,–—-]\s*(?:and\s+)?\d+(?:\.\d+)*)*)\)")
_BRACKET_GROUP_RE = re.compile(r"\[(\d+(?:\.\d+)*(?:\s*[,–—-]\s*\d+(?:\.\d+)*)*)\]")
_ITEM_RE = re.compile(r"\d+(?:\.\d+)*")


@dataclass
class ReferenceEntry:
    id: str
    number: str
    doc_path: str
    element: object
    needs_id: bool = False   # True until a citation actually requires assigning one


@dataclass
class LinkResult:
    detected: int = 0
    created: int = 0
    review: list = field(default_factory=list)   # (doc_path, text, reason)
    ids_assigned: int = 0
    cross_file_created: int = 0


def _is_biblioentry(el) -> bool:
    if local_name(el.tag) != "li":
        return False
    return epub_type(el) in ("biblioentry", "reference") or "biblioentry" in (el.get("class") or "").lower()


def build_reference_registry(registry) -> dict:
    """(number) -> ReferenceEntry, book-wide - a plain number key (not
    namespaced by kind, unlike figures/tables/chapters) since bibliography
    numbering is its own single sequence in every real book seen so far;
    the first entry found for a given number wins if a book ever somehow
    duplicates one (never silently overwritten by a later duplicate)."""
    by_number = {}
    for doc in registry.documents:
        if doc.tree is None:
            continue
        for el in doc.tree.iter():
            if not _is_biblioentry(el):
                continue
            text = "".join(el.itertext())
            m = _ENTRY_LEADING_RE.match(text)
            if not m:
                continue
            number = m.group(1)
            if number in by_number:
                continue
            by_number[number] = ReferenceEntry(id=el.get("id") or "", number=number, doc_path=doc.path,
                                                element=el, needs_id=not el.get("id"))
    return by_number


def _iter_citation_groups(text: str):
    """Yields (group_start, group_end, [(item_start, item_end, number), ...])
    for every "(...)"/"[...]" citation-shaped group in `text`."""
    groups = []
    for pattern in (_PAREN_GROUP_RE, _BRACKET_GROUP_RE):
        for gm in pattern.finditer(text):
            inner = gm.group(1)
            inner_start = gm.start(1)
            items = []
            for im in _ITEM_RE.finditer(inner):
                items.append((inner_start + im.start(), inner_start + im.end(), im.group(0)))
            groups.append((gm.start(), gm.end(), items))
    groups.sort(key=lambda g: g[0])
    return groups


def apply_reference_links(registry, ref_registry: dict) -> LinkResult:
    """THE single entry point. Mutates each document's tree in place
    (including, when needed, assigning a fresh id to a bibliography entry
    a real citation resolves to) and marks affected documents dirty; the
    orchestrator serializes dirty documents exactly like every other
    generation step."""
    result = LinkResult()

    def _resolve_spans(text: str, doc_path: str):
        spans = []
        for _gstart, _gend, items in _iter_citation_groups(text):
            # Every number actually WRITTEN in the group is linked -
            # _ITEM_RE never invents an unwritten intermediate member of a
            # range, so "(1.1-1.5)" already yields just its two real
            # endpoints without any special-casing here.
            for start, end, number in items:
                result.detected += 1
                entry = ref_registry.get(number)
                if entry is None:
                    result.review.append((doc_path, text[start:end],
                                           "no bibliography/reference entry with this number exists"))
                    continue
                if entry.needs_id:
                    prefix = derive_content_prefix(entry.doc_path)
                    entry.id = f"{prefix}_ref_{entry.number.replace('.', '_')}"
                    entry.element.set("id", entry.id)
                    entry.needs_id = False
                    result.ids_assigned += 1
                    target_doc_record = registry.by_path.get(entry.doc_path)
                    if target_doc_record is not None:
                        target_doc_record.dirty = True
                if entry.doc_path != doc_path:
                    result.cross_file_created += 1
                spans.append((start, end, entry.doc_path, entry.id, text[start:end]))
        return spans

    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        for el in list(doc.tree.iter()):
            ln = local_name(el.tag)
            if ln not in ("p", "li"):
                continue
            if _is_biblioentry(el):
                continue   # a bibliography entry's own leading "(1.1)" is its OWN label, not a citation of itself
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
            # fetched list(el) would report the same unresolved citation a
            # second time (see figure_table_links.py's own identical fix).
            original_children = list(el)

            if el.text:
                spans = _resolve_spans(el.text, doc.path)
                if spans:
                    spans.sort(key=lambda s: s[0])
                    _apply_spans(el, doc.path, spans)
                    result.created += len(spans)
                    changed_here = True

            for child in original_children:
                # Only figure/figcaption hard-block a WHOLE sibling subtree
                # from ever being scannable text. An <a> child is NOT
                # excluded here - its own TAIL (text after the closing
                # </a>, still flowing in the same paragraph) is completely
                # ordinary, unlinked text that must still be scanned - a
                # real bug found via this exact reasoning: "See <a
                # href='#ch2'>Chapter 2</a> and reference (1.9)..." was
                # silently never even attempted, since child.tail was
                # skipped merely because the PRECEDING sibling was a link.
                if local_name(child.tag) in ("figure", "figcaption") or not child.tail:
                    continue
                spans = _resolve_spans(child.tail, doc.path)
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
    """Same as _apply_spans but for spans found in `child.tail` rather than
    `el.text` - the new anchors are inserted into `el` right after `child`
    (mirroring figure_table_links.apply_figure_table_links' own tail-
    handling), never touching `child` itself."""
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
