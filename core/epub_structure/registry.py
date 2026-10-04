"""Step 2/3 combined (spec section 2's "GLOBAL DOCUMENT REGISTRY") - the
ONE data model every later step (TOC resolver, NAV generator, link
resolver, validator) reads from. Built once per project scan; nothing
downstream re-parses an XHTML file a second time."""
import posixpath
import re
from dataclasses import dataclass, field

from core.epub_structure import doctype_detector, heading_analyzer, pagebreak_analyzer
from core.epub_structure.xhtml_parser import epub_type, iter_by_local_name, local_name, parse_xhtml_file

_OPF_TITLE_RE = re.compile(r"<dc:title[^>]*>(.*?)</dc:title>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


@dataclass
class FigureRecord:
    id: str
    kind: str            # "figure" or "table" (a <figure class="...Table...">-style table is still <figure> in this pipeline)
    caption: str = ""


@dataclass
class NoteRecord:
    id: str
    kind: str            # "footnote" / "endnote" / "biblioentry"


@dataclass
class IndexEntryRecord:
    id: str
    text: str = ""


@dataclass
class LinkRecord:
    element: object
    attr: str             # "href", "src", "data", or an {xlink}href-style qualified name
    value: str             # the raw attribute value, exactly as written
    line: int = -1


@dataclass
class DocumentRecord:
    path: str                    # project-relative POSIX path
    tree: object = None            # the real, live lxml root Element - kept alive for the whole
                                     # orchestration run so later steps (TOC/NAV generation, id
                                     # assignment, serialization) mutate and write back the SAME
                                     # tree every Heading/Pagebreak/LinkRecord.element already
                                     # points into, never a second, disconnected re-parse.
    order: int = 0
    document_type: str = "other"
    body_epub_type: str = ""
    section_epub_types: list = field(default_factory=list)
    title: str = ""                # this document's own first-heading text (numbering stripped)
    headings: list = field(default_factory=list)     # flat, document-order list of heading_analyzer.Heading
    heading_roots: list = field(default_factory=list)  # nested, see heading_analyzer.build_hierarchy
    pagebreaks: list = field(default_factory=list)    # pagebreak_analyzer.Pagebreak
    figures: list = field(default_factory=list)       # FigureRecord
    notes: list = field(default_factory=list)          # NoteRecord
    index_entries: list = field(default_factory=list)  # IndexEntryRecord
    fragment_ids: set = field(default_factory=set)     # EVERY real id in this document, any element
    links: list = field(default_factory=list)          # LinkRecord, every href/src/data-bearing attribute
    parse_error: str = ""
    doctype: str = ""             # e.g. "<!DOCTYPE html>" - preserved from the source file, see xhtml_parser.py
    dirty: bool = False           # set by a later step (id_assigner/link_resolver/toc_resolver) when
                                    # its in-memory .tree was actually mutated - orchestrator only
                                    # writes documents with dirty=True back to disk.


@dataclass
class GlobalRegistry:
    scan: object
    book_title: str = ""
    documents: list = field(default_factory=list)     # DocumentRecord, in document order
    by_path: dict = field(default_factory=dict)          # path -> DocumentRecord
    id_index: dict = field(default_factory=dict)          # id -> [path, path, ...] - a real duplicate has len() > 1

    def document_for(self, path: str):
        return self.by_path.get(path)

    def resolve_fragment(self, path: str, fragment: str) -> bool:
        doc = self.by_path.get(path)
        return bool(doc and fragment in doc.fragment_ids)


def _read_opf_title(scan) -> str:
    if not scan.opf_path:
        return ""
    try:
        with open(scan.abspath(scan.opf_path), "r", encoding="utf-8-sig", errors="replace") as f:
            content = f.read()
    except OSError:
        return ""
    m = _OPF_TITLE_RE.search(content)
    if not m:
        return ""
    return _TAG_STRIP_RE.sub("", m.group(1)).strip()


_LINK_ATTRS = ("href", "src", "data", "{http://www.w3.org/1999/xlink}href")


def _figure_kind(el) -> str:
    class_attr = (el.get("class") or "").lower()
    return "table" if "table" in class_attr else "figure"


def build_document_record(scan, path: str, order: int, book_title: str) -> DocumentRecord:
    parsed = parse_xhtml_file(scan, path)
    if parsed.tree is None:
        return DocumentRecord(path=path, order=order, parse_error=parsed.error)

    tree = parsed.tree
    record = DocumentRecord(path=path, tree=tree, order=order, doctype=parsed.doctype)
    record.document_type = doctype_detector.detect_document_type(tree, path, order, book_title)

    body = next((e for e in tree.iter() if local_name(e.tag) == "body"), None)
    record.body_epub_type = epub_type(body) if body is not None else ""
    record.section_epub_types = [epub_type(s) for s in iter_by_local_name(tree, "section")]

    record.headings = heading_analyzer.extract_headings(tree)
    record.heading_roots = heading_analyzer.build_hierarchy(record.headings)
    record.title = record.headings[0].text if record.headings else ""

    record.pagebreaks = pagebreak_analyzer.extract_pagebreaks(tree)

    for el in tree.iter():
        el_id = el.get("id")
        if el_id:
            record.fragment_ids.add(el_id)
        ln = local_name(el.tag)
        if ln == "figure":
            caption = ""
            for child in el:
                if local_name(child.tag) == "figcaption":
                    caption = "".join(child.itertext()).strip()
            record.figures.append(FigureRecord(id=el_id or "", kind=_figure_kind(el), caption=caption))
        elif ln == "li":
            et = epub_type(el)
            if et in ("footnote", "endnote", "biblioentry"):
                record.notes.append(NoteRecord(id=el_id or "", kind=et))
            elif et == "index-entry":
                record.index_entries.append(IndexEntryRecord(id=el_id or "", text="".join(el.itertext()).strip()))
        for attr in _LINK_ATTRS:
            value = el.get(attr)
            if value:
                record.links.append(LinkRecord(element=el, attr=attr, value=value))

    return record


def build_registry(scan, progress_cb=None) -> GlobalRegistry:
    """THE single entry point. `scan`: a core.epub_structure.scanner.
    ProjectScan. Builds one DocumentRecord per XHTML file (document order
    = scan.xhtml_files' own natural order, overridden by the real OPF
    spine order when a valid, resolvable spine already exists - see
    _spine_order below), plus the cross-document id_index used to detect
    real duplicate ids (the SAME id appearing in more than one document is
    harmless - ids are only unique WITHIN a document per the XML spec -
    but the SAME id appearing twice in the SAME document is a real
    conflict, already visible per-document via len(fragment_ids) shrinking
    below len(headings)+... - this index exists for informational/landmark
    purposes, not as its own duplicate-id validator)."""
    registry = GlobalRegistry(scan=scan)
    registry.book_title = _read_opf_title(scan)

    ordered_paths = _spine_order(scan) or scan.xhtml_files
    total = len(ordered_paths)
    for order, path in enumerate(ordered_paths):
        record = build_document_record(scan, path, order, registry.book_title)
        registry.documents.append(record)
        registry.by_path[path] = record
        for frag_id in record.fragment_ids:
            registry.id_index.setdefault(frag_id, []).append(path)
        if progress_cb:
            progress_cb(order + 1, total, path)

    return registry


_SPINE_IDREF_RE = re.compile(r'<itemref[^>]*\bidref\s*=\s*"([^"]+)"', re.IGNORECASE)
_MANIFEST_ITEM_RE = re.compile(r'<item\b(?=[^>]*\bid\s*=\s*"([^"]+)")(?=[^>]*\bhref\s*=\s*"([^"]+)")', re.IGNORECASE)


def _spine_order(scan) -> list:
    """When a real, already-valid OPF is present, its <spine> is the book's
    OWN authoritative reading order - more trustworthy than a filename
    sort for a book whose filenames don't happen to be numbered (spec:
    "must work for other books with completely different filenames and
    numbers"). Returns [] (never raises) when there is no OPF, or its
    manifest/spine cannot resolve every entry back to a REAL scanned
    xhtml file - falling back to the scanner's own natural filename order
    is always safe and never worse than a partially-wrong spine order."""
    if not scan.opf_path:
        return []
    try:
        with open(scan.abspath(scan.opf_path), "r", encoding="utf-8-sig", errors="replace") as f:
            opf_text = f.read()
    except OSError:
        return []

    id_to_href = {m.group(1): m.group(2) for m in _MANIFEST_ITEM_RE.finditer(opf_text)}
    opf_dir = posixpath.dirname(scan.opf_path)
    xhtml_set = set(scan.xhtml_files)
    ordered = []
    for idref in _SPINE_IDREF_RE.findall(opf_text):
        href = id_to_href.get(idref)
        if not href:
            return []
        resolved = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else posixpath.normpath(href)
        if resolved not in xhtml_set:
            return []
        ordered.append(resolved)

    if set(ordered) != xhtml_set:
        return []  # the spine doesn't account for every real xhtml file - don't trust a partial order
    return ordered
