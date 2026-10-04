"""Step 11 (spec section 11) - analyzes every real resource the scanner
found and repairs the existing OPF's manifest/spine (or synthesizes a
complete new one when none exists at all). Never deletes a real,
unreferenced resource outright (spec: "Never delete a valid resource just
because it is not currently referenced" - it is reported as an orphan
instead, see OpfAnalysis.orphan_resources)."""
import posixpath
import re
from dataclasses import dataclass, field

from lxml import etree

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
_OPF = f"{{{OPF_NS}}}"
_DC = f"{{{DC_NS}}}"

# A fixed, deterministic extension -> media-type table (spec 11: "media-
# types are correct") - independent, small, literal copy of the same
# mapping core.epub.repair_strategies already uses for the zip-based
# repair engine (kept separate rather than imported - this module reads a
# loose directory, that one a zip's MutablePackage; sharing code across
# that boundary isn't worth the coupling for one static dict).
_MEDIA_TYPE_BY_EXT = {
    ".xhtml": "application/xhtml+xml", ".html": "application/xhtml+xml", ".htm": "application/xhtml+xml",
    ".css": "text/css",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".bmp": "image/bmp",
    ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".m4a": "audio/mp4", ".m4v": "video/mp4",
    ".ogg": "audio/ogg", ".webm": "video/webm",
    ".js": "application/javascript", ".ncx": "application/x-dtbncx+xml",
    ".xml": "application/xml", ".smil": "application/smil+xml",
}


@dataclass
class ManifestEntry:
    id: str
    href: str              # relative to the OPF's own directory
    media_type: str
    properties: str = ""


@dataclass
class OpfPlan:
    manifest: list = field(default_factory=list)     # ManifestEntry, deterministic order
    spine_ids: list = field(default_factory=list)      # manifest ids, in reading order
    nav_id: str = ""
    ncx_id: str = ""
    orphan_resources: list = field(default_factory=list)  # real files with no reliable manifest role guessed
    missing_media_type: list = field(default_factory=list)  # real files whose extension has no known media type


def _safe_id(path: str, used: set) -> str:
    stem = posixpath.splitext(posixpath.basename(path))[0]
    candidate = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
    if not candidate or not (candidate[0].isalpha() or candidate[0] == "_"):
        candidate = f"id_{candidate}" if candidate else "id"
    base, n = candidate, 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def build_opf_plan(scan, registry, opf_dir: str = "") -> OpfPlan:
    """`opf_dir`: the directory the OPF file itself lives in (or will live
    in), relative to scan.root - every manifest href is written relative
    to THIS directory, per the OPF spec. Defaults to the directory the
    scanner's own detected OPF/NAV already live in, or the xhtml files'
    common directory when neither exists yet."""
    if not opf_dir:
        opf_dir = posixpath.dirname(scan.opf_path) if scan.opf_path else (
            posixpath.dirname(scan.nav_path) if scan.nav_path else
            (posixpath.dirname(scan.xhtml_files[0]) if scan.xhtml_files else ""))

    plan = OpfPlan()
    used_ids = set()

    def _rel(path: str) -> str:
        return posixpath.relpath(path, opf_dir) if opf_dir else path

    for doc in registry.documents:
        entry_id = _safe_id(doc.path, used_ids)
        plan.manifest.append(ManifestEntry(id=entry_id, href=_rel(doc.path), media_type="application/xhtml+xml"))
        plan.spine_ids.append(entry_id)

    if scan.nav_path:
        plan.nav_id = _safe_id(scan.nav_path, used_ids)
        plan.manifest.append(ManifestEntry(id=plan.nav_id, href=_rel(scan.nav_path),
                                            media_type="application/xhtml+xml", properties="nav"))
    if scan.ncx_path:
        plan.ncx_id = _safe_id(scan.ncx_path, used_ids)
        plan.manifest.append(ManifestEntry(id=plan.ncx_id, href=_rel(scan.ncx_path),
                                            media_type="application/x-dtbncx+xml"))

    other_files = (scan.css_files + scan.image_files + scan.svg_files + scan.font_files + scan.av_files)
    for path in sorted(other_files):
        ext = posixpath.splitext(path)[1].lower()
        media_type = _MEDIA_TYPE_BY_EXT.get(ext)
        if media_type is None:
            plan.missing_media_type.append(path)
            continue
        entry_id = _safe_id(path, used_ids)
        plan.manifest.append(ManifestEntry(id=entry_id, href=_rel(path), media_type=media_type))
        plan.orphan_resources.append(path)  # every non-xhtml/nav/ncx resource is reported as an orphan
                                              # CANDIDATE below - refined by mark_referenced() before use.
    return plan


def mark_referenced(plan: OpfPlan, registry) -> None:
    """Removes from plan.orphan_resources every resource that some content
    document actually links to (img/src, css @import/url(), etc. -
    core.epub_structure.link_resolver already collected every href/src
    value per document during registry building) - spec 11: "Report orphan
    resources separately" means resources NOTHING references, not merely
    "not the nav/ncx/xhtml/opf itself"."""
    referenced = set()
    for doc in registry.documents:
        doc_dir = posixpath.dirname(doc.path)
        for link in doc.links:
            path_part = link.value.split("#", 1)[0]
            if not path_part or "://" in path_part or path_part.startswith("data:"):
                continue
            resolved = posixpath.normpath(posixpath.join(doc_dir, path_part)) if doc_dir else posixpath.normpath(path_part)
            referenced.add(resolved)
    plan.orphan_resources = [p for p in plan.orphan_resources if p not in referenced]


_ACCESSIBILITY_META = [
    ("schema:accessibilitySummary",
     "This publication conforms to the EPUB Accessibility specification at WCAG Level 2.0 AA."),
    ("schema:accessMode", "textual"), ("schema:accessMode", "visual"),
    ("schema:accessModeSufficient", "textual,visual"),
    ("schema:accessibilityHazard", "none"),
    ("schema:accessibilityFeature", "tableOfContents"), ("schema:accessibilityFeature", "structuralNavigation"),
    ("schema:accessibilityFeature", "printPageNumbers"), ("schema:accessibilityFeature", "alternativeText"),
    ("schema:accessibilityFeature", "index"),
]


def render_opf(plan: OpfPlan, registry, metadata, normal_isbn=None, printed_isbn=None, profile=None,
               modified: str = "", front=None) -> object:
    """`metadata`: a core.epub_structure.metadata_detector.DetectedMetadata
    (title/subtitle/author/language/publisher/publication_date/edition,
    each already auto-detected from THIS project - never from a sample).
    `front`: core.epub.client_rules.front_matter.FrontMatter - what the
    book's own TITLE PAGE and COPYRIGHT PAGE say (title, authors, publisher,
    rights, year); it wins over generic detection for those fields.
    `normal_isbn`/`printed_isbn`: core.epub_structure.isbn.IsbnValidation,
    already validated - kept as two SEPARATE identifiers, never merged
    (spec: "Never replace Normal ISBN with Printed ISBN or vice versa").
    The PRIMARY identifier is the Normal (e-book) ISBN, id="isbn-id" (the id
    the client's validation tool reads); the Printed ISBN becomes a SEPARATE
    <dc:source id="src-id"> refined with identifier-type 15 and
    source-of pagination.

    Element order follows the client's sample OPF: title, creator(s),
    accessibility metadata, language, publisher, identifier, source, date,
    dcterms:modified, rights, cover meta."""
    from core.epub_structure.template_profile import TemplateProfile
    from core.epub_structure.isbn import normalized_urn
    profile = profile or TemplateProfile()
    epub_version = profile.opf_version or "3.0"
    refines = profile.opf_uses_refines_metadata

    package = etree.Element(f"{_OPF}package", nsmap={None: OPF_NS, "dc": DC_NS})
    package.set("version", epub_version)
    # must name the id of the dc:identifier written below ("isbn-id") - a
    # different literal here made every generated package invalid (OPF-030)
    package.set("unique-identifier", "isbn-id")
    md = etree.SubElement(package, f"{_OPF}metadata")

    title = (front.title if front and front.title else "") or metadata.title.value or ""
    subtitle = (front.subtitle if front and front.subtitle else "") or metadata.subtitle.value or ""
    title_el = etree.SubElement(md, f"{_DC}title")
    title_el.set("id", "t1")
    title_el.text = title
    if refines:
        if subtitle:
            _refine(md, "t1", "title-type", "main")
        _refine(md, "t1", "display-seq", "1")
    if subtitle:
        subtitle_el = etree.SubElement(md, f"{_DC}title")
        subtitle_el.set("id", "t2")
        subtitle_el.text = subtitle
        if refines:
            _refine(md, "t2", "title-type", "subtitle")
            _refine(md, "t2", "display-seq", "2")

    authors = list(front.authors) if front and front.authors else (
        [metadata.author.value] if metadata.author.value else [])
    for k, author in enumerate(authors, 1):
        cid = f"creator{k}"
        creator_el = etree.SubElement(md, f"{_DC}creator")
        creator_el.set("id", cid)
        creator_el.text = author
        if refines:
            role = _refine(md, cid, "role", "aut", scheme="marc:relators")
            role.set("id", f"role{k}")
            _refine(md, cid, "file-as", _file_as(author))
            _refine(md, cid, "display-seq", str(k))

    if profile.opf_includes_accessibility_metadata:
        for prop, text in _ACCESSIBILITY_META:
            meta_el = etree.SubElement(md, f"{_OPF}meta")
            meta_el.set("property", prop)
            meta_el.text = text

    if metadata.language.value:
        lang_el = etree.SubElement(md, f"{_DC}language")
        lang_el.text = metadata.language.value

    publisher = (front.publisher if front and front.publisher else "") or metadata.publisher.value
    if publisher:
        pub_el = etree.SubElement(md, f"{_DC}publisher")
        pub_el.text = publisher

    if normal_isbn is not None and normal_isbn.valid:
        identifier = etree.SubElement(md, f"{_DC}identifier")
        identifier.set("id", "isbn-id")
        identifier.text = normalized_urn(normal_isbn)
        if refines:
            _refine(md, "isbn-id", "identifier-type", "15", scheme="onix:codelist5")

    if printed_isbn is not None and printed_isbn.valid:
        source_el = etree.SubElement(md, f"{_DC}source")
        source_el.set("id", "src-id")
        source_el.text = normalized_urn(printed_isbn)
        if refines:
            _refine(md, "src-id", "identifier-type", "15", scheme="onix:codelist5")
            _refine(md, "src-id", "source-of", "pagination")

    if metadata.edition.value:
        edition_el = etree.SubElement(md, f"{_OPF}meta")
        edition_el.set("property", "schema:bookEdition")
        edition_el.text = metadata.edition.value

    year = (front.year if front and front.year else "") or metadata.publication_date.value
    if year:
        date_el = etree.SubElement(md, f"{_DC}date")
        date_el.text = f"{year}-01-01T00:00:00Z" if re.fullmatch(r"\d{4}", year) else year

    if epub_version.startswith("3"):
        meta_mod = etree.SubElement(md, f"{_OPF}meta")
        meta_mod.set("property", "dcterms:modified")
        meta_mod.text = modified or "2000-01-01T00:00:00Z"

    if front and front.rights:
        rights_el = etree.SubElement(md, f"{_DC}rights")
        rights_el.text = front.rights

    cover = _cover_entry(plan)
    if cover is not None:
        meta_cover = etree.SubElement(md, f"{_OPF}meta")
        meta_cover.set("name", "cover")
        meta_cover.set("content", "cover-image")

    manifest = etree.SubElement(package, f"{_OPF}manifest")
    for entry in plan.manifest:
        item = etree.SubElement(manifest, f"{_OPF}item")
        item.set("id", entry.id)
        item.set("href", entry.href)
        item.set("media-type", entry.media_type)
        if entry.properties:
            item.set("properties", entry.properties)

    spine = etree.SubElement(package, f"{_OPF}spine")
    if plan.ncx_id:
        spine.set("toc", plan.ncx_id)
    for spine_id in plan.spine_ids:
        itemref = etree.SubElement(spine, f"{_OPF}itemref")
        itemref.set("idref", spine_id)

    if profile.opf_includes_guide:
        guide = _build_guide(registry, plan)
        if guide is not None:
            package.append(guide)

    return package


def _file_as(name):
    from core.epub.client_rules.front_matter import file_as
    return file_as(name)


def _cover_entry(plan):
    """The cover image's manifest entry, re-identified as the client expects:
    id="cover-image" properties="cover-image" (EPUB-023)."""
    cands = [e for e in plan.manifest if e.media_type.startswith("image/") and
             re.match(r"^cover\.(jpe?g|png|gif|webp)$", posixpath.basename(e.href).lower())]
    if len(cands) != 1:
        return None
    entry = cands[0]
    if entry.id != "cover-image" and any(e.id == "cover-image" for e in plan.manifest):
        return None
    entry.id = "cover-image"
    entry.properties = " ".join(dict.fromkeys((entry.properties.split() if entry.properties else []) +
                                              ["cover-image"]))
    return entry


def _refine(metadata_el, refines_id: str, property_name: str, text: str, scheme: str = ""):
    meta_el = etree.SubElement(metadata_el, f"{_OPF}meta")
    meta_el.set("refines", f"#{refines_id}")
    meta_el.set("property", property_name)
    if scheme:
        meta_el.set("scheme", scheme)
    meta_el.text = text
    return meta_el


# document_type -> (guide reference type, title) - a legacy EPUB2-style
# <guide> some reading systems still consult; only emitted when the
# approved sample profile itself includes one (spec 6: templates control
# structure, never content).
_GUIDE_MAP = {"cover": ("cover", "Cover"), "toc": ("toc", "Table of Contents"), "index": ("index", "Index"),
              "name-index": ("index", "Index"), "subject-index": ("index", "Index"),
              "bibliography": ("bibliography", "Bibliography")}
_GUIDE_BEGIN_TYPES = {"chapter", "part", "preface", "introduction", "foreword"}


def _build_guide(registry, plan=None):
    """The client's guide: Cover / Table of Contents / Begin reading /
    (Bibliography) / Index - titles exactly as the client's validation tool
    expects them (EPUB-035), hrefs relative to the OPF."""
    hrefs = {}
    if plan is not None:
        for e in plan.manifest:
            hrefs[posixpath.basename(e.href)] = e.href
    order = {"cover": 0, "toc": 1, "text": 2, "bibliography": 3, "index": 4}
    entries = []
    seen = set()
    for doc in registry.documents:
        if doc.tree is None:
            continue
        mapped = _GUIDE_MAP.get(doc.document_type)
        if mapped and mapped[0] not in seen:
            seen.add(mapped[0])
            ref_type, title = mapped
            entries.append((ref_type, title, doc.path))
    begin = next((d for d in registry.documents if d.tree is not None and d.document_type in _GUIDE_BEGIN_TYPES), None)
    if begin is not None:
        entries.append(("text", "Begin reading", begin.path))
    if not entries:
        return None

    guide = etree.Element(f"{_OPF}guide")
    for ref_type, title, path in sorted(entries, key=lambda e: order.get(e[0], 5)):
        ref = etree.SubElement(guide, f"{_OPF}reference")
        ref.set("type", ref_type)
        ref.set("title", title)
        ref.set("href", hrefs.get(posixpath.basename(path), posixpath.basename(path)))
    return guide
