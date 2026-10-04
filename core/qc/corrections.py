"""Applying corrections from the unified mapping to the XHTML package.

Every correction runs through EpubPackageManager.edit() - one undoable
step, locked splits refused - and locates its target either by the element
XPath recorded by the XHTML model or by a word position, mapped back into the
live DOM (the exact text node and offset), so page markers and inline images
can be inserted / moved in the middle of a paragraph without touching the
surrounding formatting.

New page markers are created from the project's OWN convention: an existing
marker element is cloned (tag, attributes, id pattern); only when the
package has none is the Zoning generator's own marker element used
(core.epub_xml_generator.EpubXmlGenerator._page_break_span).
"""
import copy
import re

from lxml import etree

from core.qc.xhtml_model import BLOCK_NAMES, SKIP_NAMES

XHTML_NS = "http://www.w3.org/1999/xhtml"


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _is_marker(el):
    from core.epub_structure.xhtml_parser import epub_type
    return isinstance(el.tag, str) and (epub_type(el) == "pagebreak" or (el.get("role") or "") == "doc-pagebreak")


# ------------------------------------------------------- text positions
def text_segments(block_el):
    """[(node, 'text'|'tail', start, end)] - the block's own text in the same
    order core.qc.xhtml_model counted it (nested blocks, markers and images
    excluded), with block-text offsets."""
    segs = []
    pos = [0]

    def add(node, attr, text):
        if text:
            segs.append((node, attr, pos[0], pos[0] + len(text)))
            pos[0] += len(text)

    def walk(el, own):
        add(el, "text", el.text if own else None)
        for child in el:
            name = _local(child.tag)
            skip = (not isinstance(child.tag, str) or name in SKIP_NAMES or child.get("hidden") is not None
                    or _is_marker(child) or name in ("img", "image") or name in BLOCK_NAMES)
            if not skip:
                walk(child, True)
            add(child, "tail", child.tail)
    walk(block_el, True)
    return segs


def locate(block_el, offset):
    """(node, attr, index) of a character offset inside the block's text."""
    segs = text_segments(block_el)
    for node, attr, s, e in segs:
        if s <= offset < e or (offset == e and (node, attr, s, e) == segs[-1]):
            return node, attr, offset - s
    if segs:
        node, attr, s, e = segs[-1]
        return node, attr, e - s
    return block_el, "text", len(block_el.text or "")


def insert_at(node, attr, index, new_el):
    """Inserts new_el at a text position (splitting that text node)."""
    if attr == "text":
        text = node.text or ""
        node.text = text[:index]
        new_el.tail = text[index:] + (new_el.tail or "")
        node.insert(0, new_el)
    else:
        text = node.tail or ""
        node.tail = text[:index]
        new_el.tail = text[index:] + (new_el.tail or "")
        node.addnext(new_el)


def detach(el):
    """Removes an element keeping its tail text in place."""
    parent = el.getparent()
    tail = el.tail or ""
    prev = el.getprevious()
    if prev is not None:
        prev.tail = (prev.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail
    parent.remove(el)
    el.tail = None
    return el


def word_location(session, j):
    """(split, block element, char offset) for XHTML word j (or the end of
    the last word when j is past the end)."""
    x = session.xhtml
    if not x.words:
        return None
    end = j >= len(x.words)
    w = x.words[min(j, len(x.words) - 1)]
    blk = x.blocks[w.block]
    tree = session.mgr.doc(w.split)
    found = tree.xpath(blk.xpath)
    if not found:
        return None
    return w.split, found[0], (w.end if end else w.start)


# ---------------------------------------------------------- page markers
def marker_template(session, label):
    """A new page-marker element following the project's own convention."""
    mgr = session.mgr
    for m in session.xhtml.markers:
        try:
            found = mgr.doc(m.split).xpath(m.xpath)
        except Exception:
            continue
        if not found:
            continue
        el = copy.deepcopy(found[0])
        el.tail = None
        old = m.label
        for attr in ("aria-label", "title"):
            if el.get(attr) == old:
                el.set(attr, label)
        if (el.text or "").strip() == old:
            el.text = label
        if el.get("id"):
            el.set("id", _relabel_id(el.get("id"), old, label))
        return el
    # no marker anywhere: the Zoning generator's own element
    from core.epub_xml_generator import EpubXmlGenerator
    from core.xhtml_writer import _apply_epub_namespace
    el = EpubXmlGenerator._page_break_span(label)
    _apply_epub_namespace(el)
    for e in el.iter():
        if isinstance(e.tag, str) and not e.tag.startswith("{"):
            e.tag = f"{{{XHTML_NS}}}{e.tag}"
    return el


def _relabel_id(old_id, old_label, new_label):
    if old_label and old_label in old_id:
        return re.sub(re.escape(old_label) + r"(?!.*" + re.escape(old_label) + ")", new_label, old_id)
    return f"{old_id}-{new_label}"


def _unique_id(mgr, wanted):
    taken = set()
    for ids in mgr.all_ids().values():
        taken |= ids
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


def _add_page_list_entry(mgr, split, marker_id, label):
    """Keeps the nav page-list in step with an inserted marker."""
    if not mgr.nav_path or not mgr.exists(mgr.nav_path):
        return False
    from core.epub_structure.pagebreak_analyzer import page_sort_key
    from core.qc.package_manager import EPUB_NS
    root = mgr.doc(mgr.nav_path).getroot()
    for nav in root.iter():
        if _local(nav.tag) != "nav" or "page-list" not in (nav.get(f"{{{EPUB_NS}}}type") or ""):
            continue
        ol = next((c for c in nav if _local(c.tag) == "ol"), None)
        if ol is None:
            return False
        href = mgr.make_href(mgr.nav_path, split, marker_id)
        for li in ol:
            a = next((x for x in li.iter() if _local(x.tag) == "a"), None)
            if a is not None and a.get("href") == href:
                return False
        items = [li for li in ol if _local(li.tag) == "li"]
        new = copy.deepcopy(items[0]) if items else etree.SubElement(ol, f"{{{XHTML_NS}}}li")
        a = next((x for x in new.iter() if _local(x.tag) == "a"), None)
        if a is None:
            a = etree.SubElement(new, f"{{{XHTML_NS}}}a")
        a.set("href", href)
        a.text = label
        key = page_sort_key(label)
        pos = len(ol)
        for k, li in enumerate(ol):
            la = next((x for x in li.iter() if _local(x.tag) == "a"), None)
            if la is not None and page_sort_key((la.text or "").strip()) > key:
                pos = k
                break
        if new.getparent() is ol:
            ol.remove(new)
        ol.insert(pos, new)
        return True
    return False


# ---------------------------------------------------------------- apply
def files_for(session, corr):
    p = corr.params
    files = set()
    if "split" in p:
        files.add(p["split"])
    if "x_pos" in p or "x_start" in p:
        j = p.get("x_pos", p.get("x_start"))
        loc = word_location(session, j)
        if loc:
            files.add(loc[0])
    if corr.action == "fix_link":
        files.add(p["file"])
    if corr.action in ("insert_page_marker", "relabel_page_marker", "remove_page_marker") and session.mgr.nav_path:
        files.add(session.mgr.nav_path)
    if corr.action in ("package_fix", "reorder_splits") or corr.action.endswith("_page_marker"):
        if session.mgr.opf_path:
            files.add(session.mgr.opf_path)
    # links to a relabelled/removed marker may live anywhere
    if corr.action in ("relabel_page_marker", "remove_page_marker", "move_page_marker"):
        files |= set(session.mgr.content_documents())
    return sorted(files)


def apply(session, diff, edit_text=None):
    """Applies diff.correction (or a user edit) as one undoable step.
    Returns the OperationResult."""
    corr = diff.correction
    mgr = session.mgr
    if corr is None and edit_text is None:
        raise ValueError("This difference has no correction - use EDIT")
    if edit_text is not None:
        from core.qc.mapping import Correction
        if diff.x_start is not None and diff.x_end is not None and diff.x_end > diff.x_start:
            corr = Correction("replace_text", {"x_start": diff.x_start, "x_end": diff.x_end, "text": edit_text}, 1.0)
        else:
            corr = Correction("insert_text", {"x_pos": diff.x_start or 0, "text": edit_text}, 1.0)
    if corr.action == "package_fix":
        return mgr.fix_package_problem(tuple(corr.params["fix"]))
    if corr.action == "reorder_splits":
        return mgr.reorder(corr.params["order"])
    fn = ACTIONS[corr.action]
    return mgr.edit(f"{corr.action.replace('_', ' ').title()}: {diff.label}",
                    lambda m: fn(session, m, corr.params), files_for(session, corr))


def _insert_page_marker(session, mgr, p):
    loc = word_location(session, p["x_pos"])
    if loc is None:
        return ["no XHTML text to anchor the marker"]
    split, block, offset = loc
    el = marker_template(session, p["label"])
    if el.get("id"):
        el.set("id", _unique_id(mgr, el.get("id")))
    node, attr, idx = locate(block, offset)
    insert_at(node, attr, idx, el)
    if el.get("id"):
        _add_page_list_entry(mgr, split, el.get("id"), p["label"])
    return []


def _find(mgr, split, xpath):
    found = mgr.doc(split).xpath(xpath)
    if not found:
        raise ValueError(f"element {xpath} no longer exists in {split} - re-run the comparison")
    return found[0]


def _relabel_page_marker(session, mgr, p):
    el = _find(mgr, p["split"], p["xpath"])
    old = el.get("aria-label") or el.get("title") or (el.text or "").strip()
    new = p["label"]
    for attr in ("aria-label", "title"):
        if el.get(attr) is not None:
            el.set(attr, new)
    if (el.text or "").strip() == old:
        el.text = new
    review = []
    if el.get("id") and old and old in el.get("id"):
        # the id encodes the label (project convention) -> follows it; any
        # other id is preserved unchanged
        old_id = el.get("id")
        new_id = _unique_id(mgr, _relabel_id(old_id, old, new))
        el.set("id", new_id)
        from core.qc.package_manager import OperationResult
        res = OperationResult(name="relabel")
        mgr._rewrite_links(lambda t, f: (t, new_id) if t == p["split"] and f == old_id else None, res)
        # page-list entry text follows the label
        if mgr.nav_path and mgr.exists(mgr.nav_path):
            for a in mgr.doc(mgr.nav_path).getroot().iter():
                if _local(a.tag) == "a" and (a.get("href") or "").endswith(f"#{new_id}") and \
                        (a.text or "").strip() == old:
                    a.text = new
    return review


def _move_page_marker(session, mgr, p):
    el = _find(mgr, p["split"], p["xpath"])
    loc = word_location(session, p["x_pos"])
    if loc is None:
        return ["target position not found"]
    split, block, offset = loc
    # marker text is never content, so block offsets are unchanged by removing it
    detach(el)
    node, attr, idx = locate(block, offset)
    insert_at(node, attr, idx, el)
    if split != p["split"] and el.get("id"):
        from core.qc.package_manager import OperationResult
        res = OperationResult(name="move marker")
        mid = el.get("id")
        mgr._rewrite_links(lambda t, f: (split, f) if t == p["split"] and f == mid else None, res)
    return []


def _remove_page_marker(session, mgr, p):
    el = _find(mgr, p["split"], p["xpath"])
    mid = el.get("id")
    label = el.get("aria-label") or el.get("title") or (el.text or "").strip()
    detach(el)
    if mid:
        # links to the removed duplicate go to the surviving marker with the same label
        survivor = next((m for m in session.xhtml.markers if m.label == label and m.xpath != p["xpath"] and m.id),
                        None)
        if survivor:
            from core.qc.package_manager import OperationResult
            res = OperationResult(name="remove marker")
            mgr._rewrite_links(lambda t, f: (survivor.split, survivor.id) if t == p["split"] and f == mid else None,
                               res)
    return []


def _remap_image(session, mgr, p):
    el = _find(mgr, p["split"], p["xpath"])
    href = mgr.make_href(p["split"], p["file"])
    for attr in ("src", "{http://www.w3.org/1999/xlink}href", "href"):
        if el.get(attr) is not None:
            el.set(attr, href)
            break
    return []


def _movable_unit(img):
    """The image, or its figure-like wrapper when that wrapper holds nothing
    but this image (+ caption)."""
    parent = img.getparent()
    if parent is not None and _local(parent.tag) in ("figure", "div"):
        others = [c for c in parent if c is not img and _local(c.tag) not in ("figcaption", "caption", "p")]
        if not others:
            return parent
    return img


def _move_image(session, mgr, p):
    img = _find(mgr, p["split"], p["xpath"])
    unit = _movable_unit(img)
    loc = word_location(session, p["x_pos"])
    if loc is None:
        return ["target position not found"]
    split, block, offset = loc
    detach(unit)
    caption = block
    while caption is not None and _local(caption.tag) not in ("figcaption", "caption"):
        caption = caption.getparent()
    if caption is not None and offset == 0:
        # the image belongs directly before its caption (inside its figure)
        caption.addprevious(unit)
        return []
    if offset == 0:
        # PDF: TEXT / IMAGE / TEXT between paragraphs -> between the blocks
        block.addprevious(unit)
        return []
    if _local(unit.tag) in BLOCK_NAMES:
        # block-level figure: before the block whose first word it precedes,
        # otherwise after that block (a figure never goes mid-paragraph)
        if offset == 0:
            block.addprevious(unit)
        else:
            block.addnext(unit)
    else:
        node, attr, idx = locate(block, offset)
        insert_at(node, attr, idx, unit)
    return []


def _insert_image(session, mgr, p):
    template = None
    for xi in session.xhtml.images:
        found = mgr.doc(xi.split).xpath(xi.xpath)
        if found:
            template = _movable_unit(found[0])
            break
    loc = word_location(session, p["x_pos"])
    if loc is None:
        return ["target position not found"]
    split, block, _offset = loc
    if template is None:
        el = etree.Element(f"{{{XHTML_NS}}}img", src=mgr.make_href(split, p["file"]), alt="")
    else:
        el = copy.deepcopy(template)
        el.tail = None
        for d in el.iter():
            if isinstance(d.tag, str) and d.get("id"):
                d.set("id", _unique_id(mgr, d.get("id")))
            if _local(d.tag) in ("img", "image"):
                for attr in ("src", "{http://www.w3.org/1999/xlink}href", "href"):
                    if d.get(attr) is not None:
                        d.set(attr, mgr.make_href(split, p["file"]))
            if _local(d.tag) in ("figcaption", "caption"):
                for c in list(d):
                    d.remove(c)
                d.text = ""
    block.addprevious(el)
    return ["inserted image: check its caption and attributes"]


def _fix_link(session, mgr, p):
    n = 0
    for el, attr in list(mgr._href_elements(p["file"])):
        if el.get(attr) == p["old"]:
            el.set(attr, p["new"])
            n += 1
    return [] if n else [f"link {p['old']} not found in {p['file']}"]


def _replace_range(session, mgr, x_start, x_end, new_text):
    x = session.xhtml
    w0, w1 = x.words[x_start], x.words[x_end - 1]
    if w0.block != w1.block:
        raise ValueError("The text spans several XHTML blocks - edit it in the source view")
    blk = x.blocks[w0.block]
    block = _find(mgr, blk.split, blk.xpath)
    start, end = w0.start, w1.end
    first = True
    for node, attr, s, e in text_segments(block):
        if e <= start or s >= end:
            continue
        text = getattr(node, attr) or ""
        a, b = max(start, s) - s, min(end, e) - s
        repl = new_text if first else ""
        first = False
        text = text[:a] + repl + text[b:]
        setattr(node, attr, text)
    return []


def _replace_text(session, mgr, p):
    return _replace_range(session, mgr, p["x_start"], p["x_end"], p["text"])


def _remove_text(session, mgr, p):
    _replace_range(session, mgr, p["x_start"], p["x_end"], "")
    # collapse the double space left behind
    x = session.xhtml
    blk = x.blocks[x.words[p["x_start"]].block]
    block = _find(mgr, blk.split, blk.xpath)
    for node, attr, _s, _e in text_segments(block):
        t = getattr(node, attr)
        if t and "  " in t:
            setattr(node, attr, re.sub(r"  +", " ", t))
    return []


def _insert_text(session, mgr, p):
    loc = word_location(session, p["x_pos"])
    if loc is None:
        return ["no XHTML position"]
    _split, block, offset = loc
    node, attr, idx = locate(block, offset)
    text = getattr(node, attr) or ""
    ins = p["text"]
    if idx > 0 and not text[idx - 1:idx].isspace():
        ins = " " + ins
    if idx < len(text) and not text[idx:idx + 1].isspace():
        ins = ins + " "
    setattr(node, attr, text[:idx] + ins + text[idx:])
    return []


ACTIONS = {
    "insert_page_marker": _insert_page_marker,
    "relabel_page_marker": _relabel_page_marker,
    "move_page_marker": _move_page_marker,
    "remove_page_marker": _remove_page_marker,
    "remap_image": _remap_image,
    "move_image": _move_image,
    "insert_image": _insert_image,
    "fix_link": _fix_link,
    "replace_text": _replace_text,
    "remove_text": _remove_text,
    "insert_text": _insert_text,
}
