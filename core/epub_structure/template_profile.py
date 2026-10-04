"""Steps 5-7 (spec: "APPROVED SAMPLE FILES" / "TEMPLATE = FORMAT" /
"TEMPLATE PROFILE") - reads approved sample nav.xhtml/content.opf/toc.ncx
files and extracts their STRUCTURAL conventions (namespaces, attribute
patterns, ID/metadata style, whether NCX/guide/hidden-page-list are used)
into a reusable TemplateProfile.

ABSOLUTE RULE (spec 3/6), enforced by construction: this module extracts
ONLY structural facts (booleans, attribute names, class names, namespace
URIs) - it never reads or stores the sample's own book-specific VALUES
(title, author, filenames, ids, chapter names, ISBNs). There is no field
on TemplateProfile a caller could accidentally populate with sample book
content even by mistake."""
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.xhtml_parser import epub_type, iter_by_local_name, local_name

_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)


@dataclass
class TemplateProfile:
    # --- NAV structure ---
    nav_has_doctype_html: bool = True
    nav_extra_namespaces: dict = field(default_factory=dict)   # prefix -> URI, e.g. {"svg": "..."}
    nav_epub_prefix: str = ""                                    # e.g. "index: http://www.index.com/"
    nav_uses_role_attributes: bool = True                          # role="doc-toc"/"doc-pagelist"/"doc-chapter" alongside epub:type
    nav_toc_heading_text: str = "Table of Contents"
    nav_toc_class: str = "none"
    nav_landmarks_heading_text: str = "Book Landmarks"
    nav_pagelist_hidden: bool = False                               # hidden="hidden" on the page-list <nav> itself
    nav_pagelist_class: str = ""
    nav_stylesheet_present: bool = False

    # --- OPF structure ---
    opf_version: str = "3.0"
    # defaults = the client's (CUPEPUB) OPF format: refines (display-seq,
    # role, file-as), schema accessibility metadata, a guide, and the print
    # ISBN as <dc:source> - a supplied sample OPF still decides
    opf_uses_refines_metadata: bool = True         # title-type/display-seq/file-as/role refinement pattern
    opf_includes_accessibility_metadata: bool = True
    opf_includes_guide: bool = True
    opf_includes_source_identifier: bool = True     # a <dc:source> alongside <dc:identifier> (pagination provenance)

    # --- NCX ---
    ncx_present_in_sample: bool = False
    ncx_includes_page_list: bool = False

    source_files: dict = field(default_factory=dict)   # {"nav": path, "opf": path, "ncx": path} - for the report only


def _parse(path: str):
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return etree.fromstring(raw), raw
    except (OSError, etree.XMLSyntaxError):
        return None, b""


def _read_nav_sample(profile: TemplateProfile, path: str):
    tree, raw = _parse(path)
    if tree is None:
        return
    profile.source_files["nav"] = path
    try:
        text_head = raw[:500].decode("utf-8", errors="replace")
    except Exception:
        text_head = ""
    profile.nav_has_doctype_html = bool(_DOCTYPE_RE.search(text_head))

    root_nsmap = tree.nsmap if hasattr(tree, "nsmap") else {}
    for prefix, uri in (root_nsmap or {}).items():
        if prefix and prefix not in ("epub",) and "1999/xhtml" not in uri:
            profile.nav_extra_namespaces[prefix] = uri

    epub_prefix_attr = tree.get("{http://www.idpf.org/2007/ops}prefix")
    if epub_prefix_attr:
        profile.nav_epub_prefix = epub_prefix_attr

    toc_nav = next((n for n in iter_by_local_name(tree, "nav") if epub_type(n) == "toc"), None)
    if toc_nav is not None:
        profile.nav_uses_role_attributes = bool(toc_nav.get("role"))
        h1 = next((e for e in toc_nav if local_name(e.tag) in ("h1", "h2")), None)
        if h1 is not None and "".join(h1.itertext()).strip():
            profile.nav_toc_heading_text = "".join(h1.itertext()).strip()
        ol = next((e for e in toc_nav if local_name(e.tag) == "ol"), None)
        if ol is not None and ol.get("class"):
            profile.nav_toc_class = ol.get("class")

    landmarks_nav = next((n for n in iter_by_local_name(tree, "nav") if epub_type(n) == "landmarks"), None)
    if landmarks_nav is not None:
        h = next((e for e in landmarks_nav if local_name(e.tag) in ("h1", "h2")), None)
        if h is not None and "".join(h.itertext()).strip():
            profile.nav_landmarks_heading_text = "".join(h.itertext()).strip()

    pagelist_nav = next((n for n in iter_by_local_name(tree, "nav") if epub_type(n) == "page-list"), None)
    if pagelist_nav is not None:
        profile.nav_pagelist_hidden = pagelist_nav.get("hidden") is not None
        if pagelist_nav.get("class"):
            profile.nav_pagelist_class = pagelist_nav.get("class")

    profile.nav_stylesheet_present = bool(next((e for e in tree.iter() if local_name(e.tag) == "link"
                                                 and (e.get("rel") or "") == "stylesheet"), None))


def _read_opf_sample(profile: TemplateProfile, path: str):
    tree, _raw = _parse(path)
    if tree is None:
        return
    profile.source_files["opf"] = path
    profile.opf_version = tree.get("version") or "3.0"

    metadata_el = next((e for e in tree.iter() if local_name(e.tag) == "metadata"), None)
    if metadata_el is not None:
        profile.opf_uses_refines_metadata = any(
            local_name(e.tag) == "meta" and e.get("refines") for e in metadata_el)
        profile.opf_includes_accessibility_metadata = any(
            local_name(e.tag) == "meta" and (e.get("property") or "").startswith("schema:accessibility")
            for e in metadata_el)
        profile.opf_includes_source_identifier = any(local_name(e.tag) == "source" for e in metadata_el)

    profile.opf_includes_guide = any(local_name(e.tag) == "guide" for e in tree.iter())


def _read_ncx_sample(profile: TemplateProfile, path: str):
    tree, _raw = _parse(path)
    if tree is None:
        return
    profile.source_files["ncx"] = path
    profile.ncx_present_in_sample = True
    profile.ncx_includes_page_list = any(local_name(e.tag) == "pageList" for e in tree.iter())


def load_template_profile(nav_path: str = "", opf_path: str = "", ncx_path: str = "") -> TemplateProfile:
    """THE single entry point. Any of the three sample paths may be
    omitted/blank; a missing sample simply leaves that section of the
    profile at its own sensible built-in default (spec's own acceptance
    criteria: the module must still work when no samples are supplied at
    all)."""
    profile = TemplateProfile()
    if nav_path:
        _read_nav_sample(profile, nav_path)
    if opf_path:
        _read_opf_sample(profile, opf_path)
    if ncx_path:
        _read_ncx_sample(profile, ncx_path)
    return profile
