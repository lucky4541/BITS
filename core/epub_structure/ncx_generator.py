"""Step 12 (spec section 12) - generates/repairs an NCX (EPUB 2
compatibility, or an EPUB 3 book that still ships one) from the SAME
GLOBAL DOCUMENT REGISTRY the nav generator already uses - one heading
hierarchy, never two independently-built navigation trees that could
disagree. playOrder is always a clean, gapless 1..N sequence in real
document order (spec 12: "no gaps, no duplicate playOrder"), independent
of whatever playOrder values (if any) an existing NCX used to have."""
import posixpath

from lxml import etree

from core.epub_structure.nav_generator import NON_CONTENT_TYPES, display_title, heading_href

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
_N = f"{{{NCX_NS}}}"

def _next_id(counter: dict, prefix: str) -> str:
    counter[prefix] = counter.get(prefix, 0) + 1
    return f"{prefix}{counter[prefix]}"


def _build_nav_points(parent, doc_path: str, headings: list, counter: dict, order_box: list):
    for h in headings:
        nav_point = etree.SubElement(parent, f"{_N}navPoint")
        nav_point.set("id", _next_id(counter, "navpoint"))
        order_box[0] += 1
        nav_point.set("playOrder", str(order_box[0]))
        nav_label = etree.SubElement(nav_point, f"{_N}navLabel")
        text_el = etree.SubElement(nav_label, f"{_N}text")
        text_el.text = h.raw_text or h.text
        content = etree.SubElement(nav_point, f"{_N}content")
        content.set("src", heading_href(doc_path, h))
        _build_nav_points(nav_point, doc_path, h.children, counter, order_box)


def build_ncx_document(registry, title: str = "", identifier: str = "") -> object:
    """Returns a complete <ncx> root Element. Mirrors build_toc_nav's own
    document-then-heading-hierarchy walk exactly (same document filter,
    same per-document fallback label for a document with no headings) -
    the two navigation documents can never disagree about WHICH documents
    or headings exist, only in their own required XML shape."""
    ncx = etree.Element(f"{_N}ncx", nsmap={None: NCX_NS})
    ncx.set("version", "2005-1")

    page_entries = [(doc.path, pb) for doc in registry.documents if doc.tree is not None
                     for pb in doc.pagebreaks if pb.id and pb.label]
    arabic_labels = [int(pb.label) for _p, pb in page_entries if pb.label.isdigit()]
    max_page_number = str(max(arabic_labels)) if arabic_labels else "0"

    head = etree.SubElement(ncx, f"{_N}head")
    for name, content in (("dtb:uid", identifier or "urn:uuid:00000000000000000000000000000000"),
                           ("dtb:depth", "1" if any(d.headings and any(h.children for h in d.headings)
                                                     for d in registry.documents) else "0"),
                           ("dtb:totalPageCount", str(len(page_entries))), ("dtb:maxPageNumber", max_page_number)):
        meta = etree.SubElement(head, f"{_N}meta")
        meta.set("name", name)
        meta.set("content", content)

    doc_title = etree.SubElement(ncx, f"{_N}docTitle")
    text_el = etree.SubElement(doc_title, f"{_N}text")
    text_el.text = title or "Untitled"

    nav_map = etree.SubElement(ncx, f"{_N}navMap")
    counter = {}
    order_box = [0]
    for doc in registry.documents:
        if doc.tree is None or doc.document_type in NON_CONTENT_TYPES:
            continue
        nav_point = etree.SubElement(nav_map, f"{_N}navPoint")
        nav_point.set("id", _next_id(counter, "navpoint"))
        order_box[0] += 1
        nav_point.set("playOrder", str(order_box[0]))
        nav_label = etree.SubElement(nav_point, f"{_N}navLabel")
        text_el = etree.SubElement(nav_label, f"{_N}text")
        if doc.headings:
            text_el.text = doc.headings[0].raw_text or doc.headings[0].text
            content = etree.SubElement(nav_point, f"{_N}content")
            content.set("src", heading_href(doc.path, doc.headings[0]))
            _build_nav_points(nav_point, doc.path, doc.headings[0].children, counter, order_box)
        else:
            text_el.text = display_title(doc)
            content = etree.SubElement(nav_point, f"{_N}content")
            content.set("src", posixpath.basename(doc.path))

    if page_entries:
        page_list = etree.SubElement(ncx, f"{_N}pageList")
        info = etree.SubElement(page_list, f"{_N}navInfo")
        info_text = etree.SubElement(info, f"{_N}text")
        info_text.text = "Pages"
        for doc_path, pb in page_entries:
            order_box[0] += 1
            target = etree.SubElement(page_list, f"{_N}pageTarget")
            target.set("type", "normal")
            target.set("playOrder", str(order_box[0]))
            label = etree.SubElement(target, f"{_N}navLabel")
            label_text = etree.SubElement(label, f"{_N}text")
            label_text.text = pb.label
            content = etree.SubElement(target, f"{_N}content")
            content.set("src", f"{posixpath.basename(doc_path)}#{pb.id}")

    return ncx


def validate_ncx_playorder(ncx_tree) -> list:
    """Returns a list of human-readable problems (empty = clean): gaps,
    duplicates, or non-sequential playOrder values in an EXISTING ncx
    tree - used by the validator step (16) against a project's own
    already-present NCX, independent of whether this module regenerated
    it this run."""
    orders = []
    for el in ncx_tree.iter(f"{_N}navPoint"):
        raw = el.get("playOrder")
        if raw is None:
            continue
        try:
            orders.append(int(raw))
        except ValueError:
            return [f"non-numeric playOrder value: {raw!r}"]
    if not orders:
        return []
    problems = []
    if len(set(orders)) != len(orders):
        problems.append("duplicate playOrder value(s) found")
    expected = list(range(1, len(orders) + 1))
    if sorted(orders) != expected:
        problems.append(f"playOrder values are not a gapless 1..{len(orders)} sequence")
    return problems
