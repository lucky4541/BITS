"""Step 6 (spec: "FOOTNOTE / ENDNOTE LINKS") - repairs/creates bidirectional
wiring between an ALREADY-EXISTING footnote/endnote body marker (an
<a href="#fn1">1</a>-style reference some earlier authoring/OCR step
already placed in running text) and its own footnote/endnote content
element, and repairs the note's own existing return-link back to that
marker.

Deliberately conservative (spec: "never mistake C<sup>60</sup> or math
superscripts for footnotes"): this module NEVER invents a footnote marker
from a bare superscript number, and NEVER fabricates new visible text (a
literal "back arrow" glyph would violate the zero-content-loss guarantee -
content_integrity_guard compares character MULTISETS before/after, so any
added visible character shows up as a real mismatch). It only ever:
  1. Repairs an EXISTING marker <a>'s href when it is broken but a real,
     unambiguous footnote/endnote target can be found (spec 10: "repair
     broken-but-resolvable hrefs").
  2. Repairs an EXISTING return-link <a> already inside the note's own
     content (also broken-but-resolvable) so it correctly points back to
     the marker.
If a note has no return-link markup to repair at all, that is left as
REVIEW rather than inventing new visible content - the SAME "repair
existing, never fabricate" discipline already used for TOC/chapter/
section/figure/reference links throughout this module."""
import posixpath
import re
from dataclasses import dataclass, field

from core.epub_structure.id_assigner import derive_content_prefix
from core.epub_structure.xhtml_parser import epub_type, local_name

_NOTE_ID_RE = re.compile(r"^(fn|footnote|note|endnote)[-_]?(\d+)$", re.IGNORECASE)


def _is_note_target(el) -> bool:
    et = epub_type(el)
    if et in ("footnote", "endnote"):
        return True
    role = (el.get("role") or "").lower()
    if role in ("doc-footnote", "doc-endnote"):
        return True
    el_id = el.get("id") or ""
    return bool(_NOTE_ID_RE.match(el_id))


def _note_kind(el) -> str:
    et = epub_type(el)
    if et in ("footnote", "endnote"):
        return et
    role = (el.get("role") or "").lower()
    if role == "doc-endnote":
        return "endnote"
    if role == "doc-footnote":
        return "footnote"
    el_id = el.get("id") or ""
    m = _NOTE_ID_RE.match(el_id)
    if m and m.group(1).lower() == "endnote":
        return "endnote"
    return "footnote"


@dataclass
class NoteEntry:
    id: str
    kind: str
    doc_path: str
    element: object


@dataclass
class LinkResult:
    footnotes_detected: int = 0
    forward_links: int = 0
    back_links: int = 0
    broken: list = field(default_factory=list)   # (doc_path, text, reason)


def build_note_registry(registry) -> dict:
    """id -> NoteEntry, book-wide. Only elements that look like a REAL
    footnote/endnote target per _is_note_target - a plain id="something"
    with no epub:type/role/recognized-id-shape is never claimed as a note
    target, so unrelated elements can never be mistaken for one."""
    by_id = {}
    for doc in registry.documents:
        if doc.tree is None:
            continue
        for el in doc.tree.iter():
            el_id = el.get("id")
            if not el_id or el_id in by_id:
                continue
            if _is_note_target(el):
                by_id[el_id] = NoteEntry(id=el_id, kind=_note_kind(el), doc_path=doc.path, element=el)
    return by_id


def _is_marker(el) -> bool:
    """An existing <a> some earlier step already placed as a footnote/
    endnote reference - identified either by its own epub:type/role, or by
    its href fragment already matching the note-id shape. Never a bare,
    unlinked superscript (spec: no <a> means no marker, no guessing)."""
    if local_name(el.tag) != "a":
        return False
    et = epub_type(el)
    role = (el.get("role") or "").lower()
    if et == "noteref" or role == "doc-noteref":
        return True
    href = el.get("href") or ""
    frag = href.split("#", 1)[1] if "#" in href else ""
    return bool(frag) and bool(_NOTE_ID_RE.match(frag))


def apply_footnote_links(registry, note_registry: dict) -> LinkResult:
    """THE single entry point. Mutates only existing <a> elements' href
    attributes (never inserts new elements or text) and marks affected
    documents dirty."""
    result = LinkResult()
    result.footnotes_detected = len(note_registry)

    # marker id -> (doc, marker element) - assigned on demand, only to a
    # marker whose forward link actually resolved, so a return-link has
    # somewhere real to point back to.
    marker_ids_by_note = {}   # note.id -> (marker_doc_path, marker_id)

    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        for el in doc.tree.iter():
            if not _is_marker(el):
                continue
            href = el.get("href") or ""
            frag = href.split("#", 1)[1] if "#" in href else ""
            entry = note_registry.get(frag)
            if entry is None:
                result.broken.append((doc.path, href, "marker href does not resolve to any known footnote/endnote"))
                continue

            correct_href = ("" if entry.doc_path == doc.path else posixpath.basename(entry.doc_path)) + f"#{entry.id}"
            if href != correct_href:
                el.set("href", correct_href)
                changed_here = True
            result.forward_links += 1

            marker_id = el.get("id")
            if not marker_id:
                prefix = derive_content_prefix(doc.path)
                marker_id = f"{prefix}_fnref_{entry.id}"
                el.set("id", marker_id)
                changed_here = True
            marker_ids_by_note[entry.id] = (doc.path, marker_id)

        if changed_here:
            doc.dirty = True

    # Second pass: repair each note's OWN existing return-link (an <a>
    # already present inside the note element) to point back at its
    # marker - never inserted fresh, only repaired if already there.
    for note_id, entry in note_registry.items():
        target = marker_ids_by_note.get(note_id)
        if target is None:
            continue   # no marker ever resolved to this note - nothing to link back to
        marker_doc_path, marker_id = target
        return_anchor = None
        for child in entry.element.iter():
            if child is entry.element or local_name(child.tag) != "a":
                continue
            return_anchor = child
            break
        if return_anchor is None:
            result.broken.append((entry.doc_path, entry.id,
                                   "no existing return-link markup inside this note to repair"))
            continue
        correct_href = ("" if marker_doc_path == entry.doc_path else posixpath.basename(marker_doc_path)) + f"#{marker_id}"
        if return_anchor.get("href") != correct_href:
            return_anchor.set("href", correct_href)
            target_doc = registry.by_path.get(entry.doc_path)
            if target_doc is not None:
                target_doc.dirty = True
        result.back_links += 1

    return result
