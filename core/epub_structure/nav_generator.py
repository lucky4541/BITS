"""Steps 8-10/13-14 (spec sections 8-10, "TEMPLATE = FORMAT") - builds a
fresh, valid EPUB 3 navigation document from the GLOBAL DOCUMENT
REGISTRY: <nav epub:type="toc">, <nav epub:type="landmarks">, and <nav
epub:type="page-list">.

Every href this module writes points at a REAL document + a REAL existing
(or freshly id_assigner-assigned) id - never a synthesized/guessed
fragment (spec 6/8/9/10's shared "no {0}, no #unknown_id, no invented
target" rule). Regenerating the nav is always a full, deterministic
rebuild from the CURRENT registry, never an incremental append - running
this twice against the same registry produces byte-identical output
(spec 19: idempotency).

An OPTIONAL core.epub_structure.template_profile.TemplateProfile steers
purely STRUCTURAL choices (role attributes, heading label text, list
class names, whether page-list carries hidden="hidden") to match an
approved sample - never book-specific data, which always comes from
`registry` regardless of what profile is supplied (spec 3/6: "TEMPLATE =
FORMAT, CURRENT EPUB = DATA"). Omitting the profile falls back to this
module's own sensible built-in defaults."""
import posixpath

from lxml import etree

from core.epub_structure.template_profile import TemplateProfile

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
_E = f"{{{EPUB_NS}}}"

# Human-readable fallback label for a document with no heading text at all
# (e.g. an image-only cover) - used for BOTH the toc entry's link text and
# the landmarks label, so the two never disagree.
_DISPLAY_LABELS = {
    "cover": "Cover", "halftitle": "Half Title", "titlepage": "Title Page",
    "copyrightpage": "Copyright", "dedication": "Dedication", "toc": "Contents",
    "preface": "Preface", "foreword": "Foreword", "introduction": "Introduction",
    "frontmatter": "Front Matter", "acknowledgements": "Acknowledgements",
    "chapter": "Chapter", "part": "Part", "section": "Section", "appendix": "Appendix",
    "glossary": "Glossary", "bibliography": "Bibliography", "references": "References",
    "backmatter": "Back Matter", "index": "Index", "name-index": "Name Index",
    "subject-index": "Subject Index", "other": "",
}

# Document types that represent real, linear reading content a "Begin
# Reading" landmark should jump to - deliberately NOT based on this book's
# own body epub:type="bodymatter" attribute (confirmed unreliable against
# a real production EPUB, which stamps "bodymatter" on its half-title and
# title pages too - see doctype_detector's own module docstring).
_BEGIN_READING_TYPES = {"chapter", "part", "preface", "introduction", "foreword"}

# document_type -> (standard EPUB3 landmark epub:type, label, doc-role).
# Only the FIRST document of each of these types becomes a landmark (spec
# 9: one entry per landmark KIND, not one per matching document) - my own
# finer-grained "name-index"/"subject-index" both map to the one standard
# "index" landmark epub:type, keeping their own distinct label text.
_LANDMARK_MAP = {
    "cover": ("cover", "Cover Page", "doc-cover"),
    "toc": ("toc", "Contents", "doc-toc"),
    "bibliography": ("bibliography", "Bibliography", "doc-bibliography"),
    "glossary": ("glossary", "Glossary", "doc-glossary"),
    "index": ("index", "Index", "doc-index"),
    "name-index": ("index", "Name Index", "doc-index"),
    "subject-index": ("index", "Subject Index", "doc-index"),
}

NON_CONTENT_TYPES = {"toc"}   # document types that are pure navigation aids, never given their own TOC/landmark entry


def _el(tag, nsmap=None):
    return etree.Element(f"{{{XHTML_NS}}}{tag}", nsmap=nsmap)


def _sub(parent, tag, **attrs):
    e = etree.SubElement(parent, f"{{{XHTML_NS}}}{tag}")
    for k, v in attrs.items():
        e.set(k, v)
    return e


def display_title(doc) -> str:
    return doc.title.strip() if doc.title.strip() else _DISPLAY_LABELS.get(doc.document_type, doc.document_type.title())


def heading_href(doc_path: str, heading) -> str:
    return f"{posixpath.basename(doc_path)}#{heading.id}" if heading.id else posixpath.basename(doc_path)


def _build_heading_ol(doc_path: str, headings: list, list_class: str):
    """headings: a list of heading_analyzer.Heading (already nested via
    build_hierarchy) - builds ONE <ol> covering just these siblings and
    their own descendants, or returns None for an empty list (spec 8:
    never emit an empty <ol>)."""
    if not headings:
        return None
    ol = _sub_ol(list_class)
    for h in headings:
        li = _sub(ol, "li")
        a = _sub(li, "a", href=heading_href(doc_path, h))
        a.text = h.raw_text or h.text
        child_ol = _build_heading_ol(doc_path, h.children, list_class)
        if child_ol is not None:
            li.append(child_ol)
    return ol


def _sub_ol(list_class: str):
    ol = etree.Element(f"{{{XHTML_NS}}}ol")
    if list_class:
        ol.set("class", list_class)
    return ol


def build_toc_nav(registry, profile: TemplateProfile = None) -> object:
    """Returns a <nav epub:type="toc"> Element. ONE top-level <li> per
    real content document, in document/spine order, nested with that
    document's OWN internal heading hierarchy (spec 8: "Do not flatten
    hierarchical content")."""
    profile = profile or TemplateProfile()
    nav = _el("nav")
    nav.set(f"{_E}type", "toc")
    if profile.nav_uses_role_attributes:
        nav.set("role", "doc-toc")
    heading = _sub(nav, "h1")
    heading.text = profile.nav_toc_heading_text
    ol = _sub_ol(profile.nav_toc_class)
    nav.append(ol)

    for doc in registry.documents:
        if doc.tree is None or doc.document_type in NON_CONTENT_TYPES:
            continue
        li = _sub(ol, "li")
        if doc.headings:
            a = _sub(li, "a", href=heading_href(doc.path, doc.headings[0]))
            a.text = doc.headings[0].raw_text or doc.headings[0].text
            child_ol = _build_heading_ol(doc.path, doc.headings[0].children, profile.nav_toc_class)
        else:
            a = _sub(li, "a", href=posixpath.basename(doc.path))
            a.text = display_title(doc)
            child_ol = None
        if child_ol is not None:
            li.append(child_ol)
    return nav


def build_landmarks_nav(registry, profile: TemplateProfile = None) -> object:
    """Returns a <nav epub:type="landmarks"> Element, or None if the book
    has NOTHING landmark-worthy at all (never an empty <nav>). One entry
    per landmark KIND (spec 9), plus a "Begin Reading" entry at the first
    real bodymatter document with actual heading content - every href is
    guaranteed to point at a document that is really in this registry
    (spec 9: "Every landmark target must exist")."""
    profile = profile or TemplateProfile()
    nav = _el("nav")
    nav.set(f"{_E}type", "landmarks")
    # the client's (CUPEPUB) landmarks format: <h2 id="landmarks">Book
    # Landmarks</h2>, <ol class="none">, Cover Page / Contents / Begin
    # Reading (epub:type="part", as the client's validation tool reads it) /
    # Index - EPUB-035 / EPUB-037
    heading = _sub(nav, "h2")
    heading.set("id", "landmarks")
    heading.text = profile.nav_landmarks_heading_text
    ol = _sub_ol(profile.nav_toc_class or "none")
    nav.append(ol)

    entries = []                      # (order, href, epub:type, label)
    order = {"cover": 0, "toc": 1, "part": 2, "index": 9}
    seen_kinds = set()
    for doc in registry.documents:
        if doc.tree is None:
            continue
        mapped = _LANDMARK_MAP.get(doc.document_type)
        if mapped and doc.document_type not in seen_kinds:
            seen_kinds.add(doc.document_type)
            landmark_type, label, role = mapped
            # no role on the <a>: DPUB roles such as doc-chapter are not allowed on
            # links (EPUBCheck RSC-005) - epub:type already carries the landmark
            _ = role
            href = heading_href(doc.path, doc.headings[0]) if doc.headings else posixpath.basename(doc.path)
            entries.append((order.get(landmark_type, 5), href, landmark_type, label))

    begin = next((d for d in registry.documents
                  if d.tree is not None and d.document_type in _BEGIN_READING_TYPES and d.headings), None)
    if begin is not None:
        entries.append((order["part"], heading_href(begin.path, begin.headings[0]), "part", "Begin Reading"))
    for _o, href, typ, label in sorted(entries, key=lambda e: e[0]):
        li = _sub(ol, "li")
        a = _sub(li, "a", href=href)
        a.set(f"{_E}type", typ)
        a.text = label

    return nav if entries else None


def build_page_list_nav(registry, profile: TemplateProfile = None) -> object:
    """Returns a <nav epub:type="page-list"> Element, or None if the book
    has no pagebreak markers at all. Built purely from the ACTUAL
    pagebreak registry, in document order - never assumes continuity, and
    a document that has no pagebreaks simply contributes none (spec 10:
    "If page 8 is absent, do not invent page 8")."""
    profile = profile or TemplateProfile()
    entries = []
    for doc in registry.documents:
        if doc.tree is None:
            continue
        for pb in doc.pagebreaks:
            if pb.id and pb.label:
                entries.append((doc.path, pb))
    if not entries:
        return None

    nav = _el("nav")
    nav.set(f"{_E}type", "page-list")
    if profile.nav_uses_role_attributes:
        nav.set("role", "doc-pagelist")
    if profile.nav_pagelist_hidden:
        nav.set("hidden", "hidden")
    if profile.nav_pagelist_class:
        nav.set("class", profile.nav_pagelist_class)
    heading = _sub(nav, "h1")
    heading.text = "Page List"
    ol = _sub_ol(profile.nav_toc_class)
    nav.append(ol)
    for doc_path, pb in entries:
        li = _sub(ol, "li")
        a = _sub(li, "a", href=f"{posixpath.basename(doc_path)}#{pb.id}")
        a.text = pb.label
    return nav


def build_nav_document(registry, title: str = "Navigation", profile: TemplateProfile = None,
                        stylesheet_href: str = "") -> object:
    """Assembles a COMPLETE, standalone nav.xhtml document (<html><head>/
    <body>) from the three nav sections above - the single function the
    orchestrator calls to (re)generate the whole file. Regenerating is
    always a full rebuild from the CURRENT registry (spec 19: running this
    twice produces byte-identical output, never a duplicate/appended
    entry) - the caller is responsible for replacing whatever nav document
    previously existed, never merging into it.

    `stylesheet_href`: the CURRENT project's own real, existing stylesheet
    path (relative to the nav document) - never the sample's own css
    filename (spec: "TEMPLATE = FORMAT, CURRENT EPUB = DATA"). Left blank
    when the project has no CSS file to reference."""
    profile = profile or TemplateProfile()
    nsmap = {None: XHTML_NS, "epub": EPUB_NS}
    for prefix, uri in profile.nav_extra_namespaces.items():
        nsmap[prefix] = uri
    html = _el("html", nsmap=nsmap)
    if profile.nav_epub_prefix:
        html.set(f"{_E}prefix", profile.nav_epub_prefix)

    head = etree.SubElement(html, f"{{{XHTML_NS}}}head")
    title_el = etree.SubElement(head, f"{{{XHTML_NS}}}title")
    title_el.text = title
    if stylesheet_href:
        link = etree.SubElement(head, f"{{{XHTML_NS}}}link")
        link.set("rel", "stylesheet")
        link.set("type", "text/css")
        link.set("href", stylesheet_href)

    body = etree.SubElement(html, f"{{{XHTML_NS}}}body")
    body.set(f"{_E}type", "frontmatter")

    body.append(build_toc_nav(registry, profile))
    landmarks = build_landmarks_nav(registry, profile)
    if landmarks is not None:
        body.append(landmarks)
    page_list = build_page_list_nav(registry, profile)
    if page_list is not None:
        body.append(page_list)
    return html
