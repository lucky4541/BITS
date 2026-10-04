"""Finds MISSING printed page numbers in the pagebreak sequence and fills
the gaps that can be placed safely.

Printed page numbers are sequential within each numbering system (roman
front matter, arabic body). When the markers go ... 44, 45 | 47, 48 ...
page 46 certainly exists in the print book - it simply has no marker.
The classic case is the blank verso page at the end of a chapter
(chapter 1 ends on 45, chapter 2 starts on the next recto, 47): the
converter saw no text on page 46, so no marker was produced. Without that
marker the NAV page-list, the NCX pageList and every index / page-reference
link to "46" are broken.

What is filled (an INTERIOR gap - the numbers on both sides exist):
  * gap at a document boundary (the previous page's marker is in an earlier
    file than the next page's marker): the missing page(s) are blank pages
    that close the earlier document, so the marker(s) are appended at the
    end of that document. If documents with NO page markers sit between the
    two (e.g. a part-title file) and their count equals the number of
    missing pages, each gets one marker at its start instead.
    -> confidence HIGH
  * gap inside one document: the page's content cannot be located without
    the PDF, so the marker(s) are inserted immediately before the next
    page's marker -> confidence MEDIUM, listed for review.

What is NOT filled (reported for review, never invented):
  * pages before the first marker of a numbering system (e.g. front matter
    i-vi before vii: usually unnumbered half-title / title / copyright
    pages - which file each belongs to is unknown without the PDF)
  * pages after the last marker (the book's real last page is unknown)
  * a gap larger than MAX_FILL pages, a sequence that runs backwards, or a
    missing label that already exists elsewhere in the book.
With the source PDF, the PDF <-> XHTML QC workspace places every page
marker (including those) from the PDF itself.

New markers copy the tag and attributes of a neighbouring marker of the
same book (so the house style is kept), carry the label in the same
attribute(s) the neighbour uses, follow the neighbour's id pattern
(page_45 -> page_46) and are EMPTY - they add no visible text, so the
content-integrity guard is unaffected.
"""
import copy
import re
from dataclasses import dataclass, field

from core.epub_structure import pagebreak_analyzer
from core.epub_structure.xhtml_parser import XHTML_NS, local_name

MAX_FILL = 12                 # larger gaps are reported, not filled
_ARABIC_RE = re.compile(r"^\d+$")
_ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
_ID_LABEL_RE = re.compile(r"(\d+|[ivxlcdm]+)$", re.IGNORECASE)
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_CONTAINERS = ("section", "div", "article", "main")
_INLINE = ("span", "a")


@dataclass
class FilledPage:
    label: str
    doc_path: str
    placement: str          # "end of document" / "start of document" / "before page N"
    confidence: str         # "HIGH" / "MEDIUM"
    marker_id: str = ""


@dataclass
class UnfilledGap:
    labels: list
    doc_path: str
    reason: str
    kind: str = "gap"       # "leading" (front of a numbering system - informational) / "gap" / "order"


@dataclass
class GapFillResult:
    markers_found: int = 0
    missing_detected: int = 0
    filled: list = field(default_factory=list)        # FilledPage
    unfilled: list = field(default_factory=list)      # UnfilledGap

    @property
    def filled_count(self):
        return len(self.filled)

    @property
    def review_count(self):
        return (sum(len(g.labels) for g in self.unfilled if g.kind != "leading")
                + sum(1 for f in self.filled if f.confidence != "HIGH"))


# ------------------------------------------------------------- labels
def _roman_to_int(s):
    return pagebreak_analyzer._roman_to_int(s.lower())


def _int_to_roman(n, upper=False):
    out = []
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                       (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out.append(sym)
            n -= value
    s = "".join(out)
    return s.upper() if upper else s


def effective_label(pb) -> str:
    """The printed label of a marker: aria-label/title, else its own text,
    else the trailing number of its id (page_46 -> 46)."""
    if pb.label:
        return pb.label.strip()
    text = "".join(pb.element.itertext()).strip() if pb.element is not None else ""
    if text and (_ARABIC_RE.match(text) or _ROMAN_RE.match(text)):
        return text
    m = _ID_LABEL_RE.search(pb.id or "")
    return m.group(1) if m else ""


def _system_value(label):
    """('arabic'|'roman', int value) or (None, None)."""
    if _ARABIC_RE.match(label):
        return "arabic", int(label)
    if _ROMAN_RE.match(label):
        value = _roman_to_int(label)
        # reject strings that are not canonical roman numerals ("iiii", "vx" ...)
        if value and _int_to_roman(value) == label.lower():
            return "roman", value
    return None, None


def _format(system, value, like):
    if system == "arabic":
        return str(value)
    return _int_to_roman(value, upper=like.isupper())


# ------------------------------------------------------------ markers
def _new_marker(template_pb, template_label, new_label, existing_ids):
    src = template_pb.element
    el = copy.deepcopy(src)
    for child in list(el):
        el.remove(child)
    el.text = None
    el.tail = None
    if src.get("aria-label") is not None or src.get("title") is None:
        el.set("aria-label", new_label)
    if src.get("title") is not None:
        el.set("title", new_label)
    old_id = src.get("id") or ""
    if old_id and template_label and old_id.endswith(_ID_SAFE_RE.sub("", template_label)):
        base = old_id[: len(old_id) - len(_ID_SAFE_RE.sub("", template_label))] + _ID_SAFE_RE.sub("", new_label)
    else:
        base = f"page_{_ID_SAFE_RE.sub('', new_label)}"
    candidate, n = base, 2
    while candidate in existing_ids:
        candidate = f"{base}_{n}"
        n += 1
    el.set("id", candidate)
    existing_ids.add(candidate)
    return el


def _body(tree):
    return next((e for e in tree.iter() if isinstance(e.tag, str) and local_name(e.tag) == "body"), None)


def _last_container(body):
    """Innermost trailing section/div wrapper of the body (markers are
    appended inside the chapter's own wrapper, not after it)."""
    cur = body
    while True:
        kids = [c for c in cur if isinstance(c.tag, str)]
        if kids and local_name(kids[-1].tag) in _CONTAINERS:
            cur = kids[-1]
            continue
        return cur


def _first_container(body):
    cur = body
    while True:
        kids = [c for c in cur if isinstance(c.tag, str)]
        if kids and local_name(kids[0].tag) in _CONTAINERS:
            cur = kids[0]
            continue
        return cur


def _wrap_if_needed(doc, el):
    """XHTML 1.x (EPUB 2) does not allow inline elements directly in
    <body>/<div>; such a marker is wrapped in an empty <div>."""
    if local_name(el.tag) in _INLINE and "XHTML 1." in (doc.doctype or ""):
        div = el.makeelement(f"{{{XHTML_NS}}}div", {})
        div.append(el)
        return div
    return el


def _append_end(doc, markers):
    body = _body(doc.tree)
    if body is None:
        return False
    target = _last_container(body)
    for m in markers:
        target.append(_wrap_if_needed(doc, m))
    return True


def _insert_start(doc, markers):
    body = _body(doc.tree)
    if body is None:
        return False
    target = _first_container(body)
    for k, m in enumerate(markers):
        target.insert(k, _wrap_if_needed(doc, m))
    return True


# ---------------------------------------------------------------- main
def fill_missing_pagebreaks(registry, existing_ids: set, apply: bool = True) -> GapFillResult:
    """Detects missing page numbers across the whole book (reading order)
    and, when `apply`, inserts the safe ones into the live document trees
    (documents are marked dirty; their .pagebreaks lists are refreshed so
    the page-list / NCX / page-link steps that run afterwards see them)."""
    res = GapFillResult()
    docs = [d for d in registry.documents if d.tree is not None]
    seq = []                                        # (doc_index, pb, label, system, value)
    for di, doc in enumerate(docs):
        for pb in doc.pagebreaks:
            label = effective_label(pb)
            system, value = _system_value(label)
            seq.append((di, pb, label, system, value))
    res.markers_found = len(seq)
    if not seq:
        return res
    all_labels = {(s[3], s[4]) for s in seq if s[3]}
    touched = set()
    for system in ("roman", "arabic"):
        items = [s for s in seq if s[3] == system]
        if not items:
            continue
        first = items[0]
        if first[4] > 1:
            # leading gap: only reported (front matter pages are usually unnumbered)
            labels = [_format(system, v, first[2]) for v in range(1, first[4])]
            res.missing_detected += len(labels)
            res.unfilled.append(UnfilledGap(
                labels, docs[first[0]].path,
                f"pages before the first {system} page marker ({first[2]}) - usually unnumbered pages "
                "(half title, title, copyright...); their files cannot be determined without the PDF. "
                "Use the PDF <-> XHTML QC workspace to place them.", kind="leading"))
        for a, b in zip(items, items[1:]):
            gap = b[4] - a[4] - 1
            if gap <= 0:
                if b[4] <= a[4]:
                    res.unfilled.append(UnfilledGap([b[2]], docs[b[0]].path,
                                                    f"page {b[2]} follows page {a[2]} - out of order or "
                                                    "duplicate marker; nothing filled", kind="order"))
                continue
            labels = [_format(system, v, a[2]) for v in range(a[4] + 1, b[4])]
            res.missing_detected += len(labels)
            if gap > MAX_FILL:
                res.unfilled.append(UnfilledGap(labels, docs[a[0]].path,
                                                f"{gap} consecutive pages missing between {a[2]} and {b[2]} - "
                                                "too many to place safely; check the source"))
                continue
            clash = [lab for v, lab in zip(range(a[4] + 1, b[4]), labels) if (system, v) in all_labels]
            if clash:
                res.unfilled.append(UnfilledGap(labels, docs[a[0]].path,
                                                f"label(s) {', '.join(clash)} already exist elsewhere in the "
                                                "book (out-of-order markers) - not filled"))
                continue
            if not apply:
                continue
            new = [_new_marker(b[1], b[2], lab, existing_ids) for lab in labels]
            if a[0] != b[0]:
                between = [d for d in docs[a[0] + 1:b[0]] if not d.pagebreaks]
                if between and len(between) == len(new):
                    for doc, m, lab in zip(between, new, labels):
                        _insert_start(doc, [m])
                        touched.add(doc.path)
                        res.filled.append(FilledPage(lab, doc.path, "start of document (no markers of its own)",
                                                     "HIGH", m.get("id")))
                else:
                    doc = docs[a[0]]
                    if _append_end(doc, new):
                        touched.add(doc.path)
                        for m, lab in zip(new, labels):
                            res.filled.append(FilledPage(lab, doc.path, f"end of document (blank page after "
                                                                        f"{a[2]})", "HIGH", m.get("id")))
            else:
                anchor = b[1].element
                for m in new:
                    anchor.addprevious(m)
                doc = docs[b[0]]
                touched.add(doc.path)
                for m, lab in zip(new, labels):
                    res.filled.append(FilledPage(lab, doc.path, f"before page {b[2]} (position approximate - "
                                                                f"check against the PDF)", "MEDIUM", m.get("id")))
    for doc in docs:
        if doc.path in touched:
            doc.dirty = True
            doc.pagebreaks = pagebreak_analyzer.extract_pagebreaks(doc.tree)
            for pb in doc.pagebreaks:
                if pb.id:
                    doc.fragment_ids.add(pb.id)
                    registry.id_index.setdefault(pb.id, [])
                    if doc.path not in registry.id_index[pb.id]:
                        registry.id_index[pb.id].append(doc.path)
    return res


def describe(res: GapFillResult) -> list:
    lines = [f"Page markers found: {res.markers_found}",
             f"Missing page numbers detected: {res.missing_detected}",
             f"Auto-filled: {res.filled_count}",
             f"Review: {res.review_count}"]
    for f in res.filled:
        lines.append(f"  + page {f.label} -> {f.doc_path}, {f.placement} [{f.confidence}]")
    for g in res.unfilled:
        shown = ", ".join(g.labels[:12]) + (" ..." if len(g.labels) > 12 else "")
        lines.append(f"  ! not filled: {shown} ({g.doc_path}) - {g.reason}")
    return lines
