"""Reads an EPUB package directly (spec section 5) - OPF/manifest/spine,
every spine XHTML document, NAV/NCX if present - into the SAME
document_model.Document shape pdf_reader.py produces, so every downstream
comparator (content/structure detectors) treats a PDF-derived Document
and an EPUB-derived Document identically wherever geometry isn't
involved.

READ-ONLY (spec section 5/71): only opens the .epub as a zip archive and
parses its XML with lxml.etree.parse - never writes to it, never touches
core/epub_xml_generator.py, core/xml_generator.py, or any other existing
generation code. EPUB is reflowable, so every Block here carries a dummy
zero-area BBox and each Page here corresponds to one SPINE ITEM (not a
physical page) - layout-based comparators (alignment/indentation/margin/
etc.) recognize this (page_width == 0) and skip themselves for the EPUB
side; the fidelity_compare engine only ever runs layout comparison
between the two PDFs (Original vs Converted), never against the EPUB
(see core/fidelity_compare/engine.py)."""
import os
import posixpath
import re
import zipfile

from lxml import etree

from core.fidelity_compare import text_forms
from core.fidelity_compare.document_model import (
    BBox, Block, Document, Figure, Footnote, IndexEntry, Link, Page, Reference, Table, TableCell,
)

_XHTML_NS = "http://www.w3.org/1999/xhtml"
_OPF_NS = "http://www.idpf.org/2007/opf"
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_EPUB_NS = "http://www.idpf.org/2007/ops"
_ZERO_BBOX = BBox(0, 0, 0, 0)

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_FOOTNOTE_CLASS_RE = re.compile(r"\bfn\b", re.I)
_BIBLIO_CLASS_RE = re.compile(r"\bbiblioentry\b", re.I)
_INDEX_CLASS_RE = re.compile(r"\bindex\b", re.I)
_CAPTION_CLASS_RE = re.compile(r"\b(figcaption|tblcaption|caption)\b", re.I)


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _class_of(el) -> str:
    return el.get("class", "") or ""


def _epub_type_of(el) -> str:
    return el.get(f"{{{_EPUB_NS}}}type", "") or ""


def _text_of(el) -> str:
    return "".join(el.itertext())


def read_epub(path: str) -> Document:
    document = Document(source_kind="epub", source_path=path)
    with zipfile.ZipFile(path, "r") as zf:
        opf_path, manifest, spine_ids = _read_opf(zf)
        opf_dir = posixpath.dirname(opf_path)
        footnote_order = 0
        reference_order = 0
        index_order = 0
        for page_number, item_id in enumerate(spine_ids, start=1):
            href = manifest.get(item_id)
            if not href:
                continue
            full_path = posixpath.normpath(posixpath.join(opf_dir, href))
            try:
                raw_bytes = zf.read(full_path)
            except KeyError:
                continue
            page, foots, refs, idxs, links = _parse_xhtml(raw_bytes, page_number, full_path,
                                                            footnote_order, reference_order, index_order)
            footnote_order += len(foots)
            reference_order += len(refs)
            index_order += len(idxs)
            document.pages.append(page)
            document.footnotes.extend(foots)
            document.references.extend(refs)
            document.index_entries.extend(idxs)
            document.links.extend(links)
    return document


def _read_opf(zf: zipfile.ZipFile):
    container_xml = zf.read("META-INF/container.xml")
    container_root = etree.fromstring(container_xml)
    rootfile = container_root.find(f".//{{{_CONTAINER_NS}}}rootfile")
    opf_path = rootfile.get("full-path")
    opf_root = etree.fromstring(zf.read(opf_path))
    manifest = {}
    for item in opf_root.iter(f"{{{_OPF_NS}}}item"):
        manifest[item.get("id")] = item.get("href")
    spine_ids = [ref.get("idref") for ref in opf_root.iter(f"{{{_OPF_NS}}}itemref")
                 if ref.get("linear", "yes") != "no"]
    return opf_path, manifest, spine_ids


def _list_depth(el, root) -> int:
    depth = 0
    node = el.getparent()
    while node is not None and node is not root:
        if _local(node.tag) in ("ul", "ol"):
            depth += 1
        node = node.getparent()
    return depth


def _parse_xhtml(raw_bytes: bytes, page_number: int, source_href: str,
                  footnote_start: int, reference_start: int, index_start: int):
    parser = etree.XMLParser(recover=True, resolve_entities=False)
    root = etree.fromstring(raw_bytes, parser=parser)
    body = None
    for el in root.iter():
        if _local(el.tag) == "body":
            body = el
            break
    blocks = []
    figures = []
    tables = []
    footnotes = []
    references = []
    index_entries = []
    links = []
    if body is None:
        return Page(number=page_number, blocks=blocks, figures=figures, tables=tables), \
            footnotes, references, index_entries, links

    in_footnotes_container = False
    in_bibliography_container = False
    in_index_container = False

    for el in body.iter():
        tag = _local(el.tag)
        cls = _class_of(el)
        etype = _epub_type_of(el)

        if tag in ("div", "section", "aside"):
            if "footnotes" in cls or "endnotes" in cls or etype in ("footnotes",):
                in_footnotes_container = True
            if "bibliography" in cls or etype == "bibliography":
                in_bibliography_container = True
            if _INDEX_CLASS_RE.search(cls) or etype == "index":
                in_index_container = True

        if tag in _HEADING_TAGS and el.getparent() is not None:
            text = _text_of(el)
            if text.strip():
                blocks.append(Block(text=text_forms.build(text), bbox=_ZERO_BBOX,
                                     kind="heading", heading_level=int(tag[1]), block_id=el.get("id")))
            continue

        if tag == "li":
            text = _text_of(el)
            if not text.strip():
                continue
            if in_bibliography_container or _BIBLIO_CLASS_RE.search(cls) or etype == "biblioentry":
                references.append(Reference(text=text_forms.build(text), order=reference_start + len(references)))
                continue
            if in_index_container:
                depth = _list_depth(el, body)
                index_entries.append(IndexEntry(text=text_forms.build(text),
                                                  order=index_start + len(index_entries), depth=depth))
                continue
            parent_list = el.getparent()
            list_kind = "ordered" if parent_list is not None and _local(parent_list.tag) == "ol" else "unordered"
            depth = _list_depth(el, body)
            blocks.append(Block(text=text_forms.build(text), bbox=_ZERO_BBOX, kind="list_item",
                                 list_kind=list_kind, list_depth=depth, block_id=el.get("id")))
            continue

        if tag == "table":
            tables.append(_parse_table(el, page_number))
            continue

        if tag == "figure" or tag == "img":
            figures.append(_parse_figure(el))
            continue

        if (_FOOTNOTE_CLASS_RE.search(cls) or etype in ("footnote", "note") or in_footnotes_container) \
                and tag in ("p", "div", "aside", "li"):
            text = _text_of(el)
            if text.strip():
                marker_match = re.match(r"^\s*(\d+|\*+|[a-z])\s*[.)]?\s*", text)
                marker = marker_match.group(1) if marker_match else ""
                footnotes.append(Footnote(marker=marker, text=text_forms.build(text), page=page_number,
                                           target_id=el.get("id"), order=footnote_start + len(footnotes)))
            continue

        if tag == "p" and not in_bibliography_container and not in_index_container:
            text = _text_of(el)
            if text.strip():
                if _CAPTION_CLASS_RE.search(cls):
                    kind = "caption"
                else:
                    kind = "paragraph"
                blocks.append(Block(text=text_forms.build(text), bbox=_ZERO_BBOX, kind=kind,
                                     block_id=el.get("id")))
            continue

        if tag == "a" and el.get("href"):
            href = el.get("href")
            links.append(Link(text=text_forms.build(_text_of(el)), href=href,
                               kind=_classify_link(href), source_page=page_number,
                               target_id=href.split("#", 1)[1] if "#" in href else None))

    page = Page(number=page_number, blocks=blocks, figures=figures, tables=tables)
    return page, footnotes, references, index_entries, links


def _classify_link(href: str) -> str:
    low = href.lower()
    if "fn" in low or "footnote" in low or "note" in low:
        return "footnote"
    if "biblio" in low or "ref" in low:
        return "reference"
    if "index" in low:
        return "index"
    if "table" in low or "tbl" in low:
        return "table"
    if "fig" in low:
        return "figure"
    return "chapter" if href and not href.startswith("#") else "generic"


def _parse_table(table_el, page_number: int) -> Table:
    cells = []
    caption_text = None
    max_cols = 0
    row_idx = 0
    for child in table_el.iter():
        tag = _local(child.tag)
        if tag == "caption":
            caption_text = _text_of(child)
        elif tag == "tr":
            col_idx = 0
            for cell in child:
                ctag = _local(cell.tag)
                if ctag not in ("td", "th"):
                    continue
                rowspan = int(cell.get("rowspan", "1") or "1")
                colspan = int(cell.get("colspan", "1") or "1")
                cells.append(TableCell(text=text_forms.build(_text_of(cell)), row=row_idx, col=col_idx,
                                        rowspan=rowspan, colspan=colspan, is_header=(ctag == "th")))
                col_idx += colspan
            max_cols = max(max_cols, col_idx)
            row_idx += 1
    return Table(bbox=_ZERO_BBOX, page=page_number, rows=row_idx, cols=max_cols, cells=cells,
                 caption=text_forms.build(caption_text) if caption_text else None)


def _parse_figure(el) -> Figure:
    caption_text = None
    if _local(el.tag) == "figure":
        for child in el.iter():
            if _local(child.tag) == "figcaption":
                caption_text = _text_of(child)
                break
    return Figure(bbox=_ZERO_BBOX, page=0, caption=text_forms.build(caption_text) if caption_text else None)
