"""Step 25/42's required pipeline, assembled (spec: "COMPLETE PIPELINE").
THE single entry point (run_full_analysis) every caller (tests, GUI) uses -
never a second, parallel way to invoke this engine.

Backup scope is deliberately narrow: only the files this module could
EVER write (every XHTML content document, the nav document, the OPF, the
NCX) are backed up before anything is touched - never the project's
images/fonts/CSS, which this module never writes to at all (spec 16:
these must survive completely untouched; backing them up too would only
slow down a large book's run for no safety benefit)."""
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure import (
    anchor_normalizer, chapter_section_links, citation_links, citation_placement, content_integrity_guard,
    figure_table_links, figure_table_registry, footnote_links, id_assigner, index_locator_links,
    isbn as isbn_mod, link_resolver, metadata_detector, nav_generator, ncx_generator, opf_generator,
    page_reference_links, pagebreak_gap_filler, reference_links, registry as registry_mod, scanner, template_profile,
    toc_resolver, validator,
)

from core.epub_structure.progress import ProgressTracker

# Running-text page references ("p. 60", "pp. 78-89") are NOT linked -
# see the "Linking page references" step below. Index locators still are.
LINK_PAGE_REFERENCES = False

BACKUP_DIR_NAME = ".epub_structure_backup"


class Cancelled(Exception):
    pass


@dataclass
class GenerationResult:
    scan: object = None
    registry: object = None
    link_resolution: object = None
    toc_resolution: object = None
    placement_decisions: list = field(default_factory=list)
    figure_table_link_result: object = None
    chapter_section_link_result: object = None
    reference_link_result: object = None
    reference_registry: object = None
    citation_link_result: object = None
    citation_registry: object = None
    footnote_link_result: object = None
    page_reference_link_result: object = None
    index_locator_link_result: object = None
    anchor_normalize_result: object = None
    metadata: object = None
    normal_isbn: object = None
    printed_isbn: object = None
    front_matter: object = None          # core.epub.client_rules.front_matter.FrontMatter
    isbn_sources: dict = field(default_factory=dict)   # "normal"/"printed" -> where an ISBN not typed in came from
    image_preparation: list = field(default_factory=list)   # [(image, [notes])] - DPI / cover size for the client
    profile: object = None
    report: object = None
    rolled_back: bool = False
    cancelled: bool = False
    backup_dir: str = ""
    error: str = ""
    heading_ids_assigned: int = 0
    pagebreak_ids_assigned: int = 0
    page_gap_result: object = None
    files_written: list = field(default_factory=list)
    files_created: list = field(default_factory=list)   # mimetype/META-INF, when this run had to create them
    stage_timings: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    output_epub_path: str = ""


def _writable_paths(scan) -> list:
    paths = list(scan.xhtml_files)
    if scan.nav_path:
        paths.append(scan.nav_path)
    if scan.opf_path:
        paths.append(scan.opf_path)
    if scan.ncx_path:
        paths.append(scan.ncx_path)
    return paths


def _backup(scan) -> str:
    backup_dir = os.path.join(scan.root, BACKUP_DIR_NAME, next(tempfile._get_candidate_names()))
    for rel_path in _writable_paths(scan):
        src = scan.abspath(rel_path)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(backup_dir, *rel_path.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    return backup_dir


def _restore_backup(scan, backup_dir: str, written: list):
    for rel_path in written:
        backup_src = os.path.join(backup_dir, *rel_path.split("/"))
        dest = scan.abspath(rel_path)
        if os.path.isfile(backup_src):
            shutil.copyfile(backup_src, dest)
        elif os.path.isfile(dest):
            os.remove(dest)  # this run CREATED a file (e.g. a brand-new nav.xhtml) that didn't exist before


def _write_tree(scan, rel_path: str, tree_root, doctype: str = "") -> None:
    # `doctype`: pass through whatever the SOURCE file actually had (see
    # registry.DocumentRecord.doctype / xhtml_parser.ParsedDocument.doctype)
    # so rewriting a dirty document never silently drops its own
    # "<!DOCTYPE html>" line - a real, confirmed bug otherwise (every
    # document this pipeline ever rewrote lost it, on every run). Omitted
    # (empty string -> None) for documents this module builds fresh
    # itself (nav/opf/ncx), which never had one to begin with.
    data = etree.tostring(tree_root, xml_declaration=True, encoding="UTF-8", doctype=doctype or None)
    abs_path = scan.abspath(rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        f.write(data)


def _default_nav_path(scan) -> str:
    """A NEW nav document goes next to the content documents (nav_generator
    writes every href as the target document's own file name, which only
    resolves from that folder - a nav next to the OPF, one level above an
    'xhtml/' folder, produced broken navigation links). Falls back to the
    OPF's folder when the content documents do not share one folder."""
    if scan.nav_path:
        return scan.nav_path
    content_dirs = {posixpath.dirname(p) for p in scan.xhtml_files}
    if len(content_dirs) == 1:
        base_dir = content_dirs.pop()
    else:
        base_dir = posixpath.dirname(scan.opf_path) if scan.opf_path else (
            posixpath.dirname(scan.xhtml_files[0]) if scan.xhtml_files else "")
    return posixpath.join(base_dir, "nav.xhtml") if base_dir else "nav.xhtml"


def _default_opf_path(scan) -> str:
    if scan.opf_path:
        return scan.opf_path
    base_dir = posixpath.dirname(scan.xhtml_files[0]) if scan.xhtml_files else ""
    return posixpath.join(base_dir, "content.opf") if base_dir else "content.opf"


def _default_ncx_path(scan) -> str:
    if scan.ncx_path:
        return scan.ncx_path
    base_dir = posixpath.dirname(scan.opf_path) if scan.opf_path else (
        posixpath.dirname(scan.xhtml_files[0]) if scan.xhtml_files else "")
    return posixpath.join(base_dir, "toc.ncx") if base_dir else "toc.ncx"


_CONTAINER_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0"
           xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _epub_opf_path(opf_path: str) -> str:
    """Return the OPF path as it will exist INSIDE the final EPUB.

    The working project may be flat (content.opf at project root), while
    the final OCF package must place publication resources below OEBPS.
    Already-structured projects that use OEBPS/ are left unchanged.
    """
    opf_path = (opf_path or "").replace("\\", "/").lstrip("/")
    if opf_path.lower().startswith("oebps/"):
        return opf_path
    return f"OEBPS/{opf_path}" if opf_path else "OEBPS/content.opf"


def _package_rel_path(rel_path: str) -> str:
    """Map a working-project path to its final EPUB-internal path.

    ROOT:
      mimetype
      META-INF/*

    OEBPS:
      every XHTML/HTML, CSS, image, font, media, OPF, NCX, nav, etc.

    If the project is already using OEBPS/, the path is preserved.
    """
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    if rel_path == "mimetype" or rel_path.lower().startswith("meta-inf/"):
        return rel_path
    if rel_path.lower() == "oebps" or rel_path.lower().startswith("oebps/"):
        return rel_path
    return f"OEBPS/{rel_path}"


def _ensure_container_files(scan, opf_path: str) -> list:
    """Step 40 (spec: "OUTPUT... verify META-INF/container.xml") - creates
    the two files every valid EPUB package needs at its ROOT (mimetype,
    META-INF/container.xml) ONLY when genuinely absent - never overwrites
    a real, already-present one (a fresh Zoning-sourced project has
    neither yet; an already-once-packaged project already has both and
    they are left completely alone). Returns the relative paths of
    whatever was newly created, for the report."""
    created = []
    mimetype_path = os.path.join(scan.root, "mimetype")
    if not os.path.isfile(mimetype_path):
        with open(mimetype_path, "w", encoding="ascii", newline="") as f:
            f.write("application/epub+zip")
        created.append("mimetype")

    container_path = os.path.join(scan.root, "META-INF", "container.xml")
    if not os.path.isfile(container_path):
        os.makedirs(os.path.dirname(container_path), exist_ok=True)
        with open(container_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_CONTAINER_XML_TEMPLATE.format(
                opf_path=_epub_opf_path(opf_path)
            ))
        created.append("META-INF/container.xml")
    else:
        # Keep an existing container.xml only if it already points to the
        # correct final OEBPS OPF location.  This prevents a stale
        # root-level "content.opf" reference from producing a broken EPUB.
        try:
            with open(container_path, "r", encoding="utf-8-sig") as f:
                container_text = f.read()
            wanted = _epub_opf_path(opf_path)
            if wanted not in container_text:
                with open(container_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(_CONTAINER_XML_TEMPLATE.format(opf_path=wanted))
                if "META-INF/container.xml" not in created:
                    created.append("META-INF/container.xml")
        except OSError:
            pass
    return created


def _write_final_epub(scan, output_path: str, prepare_images: bool = False) -> list:
    """Step 40: create a real OCF EPUB.

    The working directory can remain convenient and flat, but the FINAL
    .epub always has this structure:

        mimetype
        META-INF/container.xml
        OEBPS/<all publication resources>

    That means XHTML/HTML, CSS, images, fonts, media, nav.xhtml,
    content.opf and toc.ncx all live under OEBPS.  The mimetype entry is
    always first and uncompressed, as required by the EPUB/OCF package
    format.

    prepare_images: every PNG / JPEG is packaged as the client requires it
    (core.epub.client_rules.images): 150 DPI (inline / icon 135), the cover
    300 DPI and scaled into 1200 (W) x 1800 (H). Only the PACKAGED copy
    changes - the project's own image files are never modified. Returns
    [(path, [notes])] for every image that changed.
    """
    prepared = []
    with zipfile.ZipFile(output_path, "w") as z:
        mimetype_path = os.path.join(scan.root, "mimetype")
        if os.path.isfile(mimetype_path):
            z.write(
                mimetype_path,
                "mimetype",
                compress_type=zipfile.ZIP_STORED
            )
        else:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, b"application/epub+zip")

        # Write META-INF at the EPUB root, and EVERYTHING ELSE below OEBPS.
        # Skip the source .epub itself if a previous run somehow left one
        # inside the project directory.
        for rel_path in scan.all_files:
            if rel_path == "mimetype":
                continue
            if rel_path.lower().endswith(".epub"):
                continue

            epub_path = _package_rel_path(rel_path)

            # mimetype and META-INF must stay at the package root.
            # All other resources belong below OEBPS.
            if prepare_images and rel_path.lower().endswith((".png", ".jpg", ".jpeg")):
                from core.epub.client_rules import images as client_images
                with open(scan.abspath(rel_path), "rb") as f:
                    data, notes = client_images.prepare(rel_path, f.read())
                if notes:
                    prepared.append((rel_path, notes))
                z.writestr(epub_path, data, compress_type=zipfile.ZIP_DEFLATED)
                continue
            z.write(
                scan.abspath(rel_path),
                epub_path,
                compress_type=zipfile.ZIP_DEFLATED
            )
    return prepared


def _run_epubcheck_on_project(scan):
    """Best-effort (spec 33/40: "Run EPUBCheck after package generation") -
    zips the CURRENT project state into a throwaway temp .epub (never the
    project directory itself) and runs the SAME real, bundled EPUBCheck
    core.epub/core.validation already use - never a second/duplicate
    EPUBCheck integration. Returns None (never raises) when EPUBCheck
    itself is unavailable on this machine; the caller reports that
    honestly rather than pretending validation ran."""
    try:
        from core.epubcheck import runner as epubcheck_runner
    except ImportError:
        return None
    ok, _reason = epubcheck_runner.check_availability()
    if not ok:
        from core.epubcheck.parser import EpubCheckResult
        return EpubCheckResult(ran=False, error=_reason)

    with tempfile.TemporaryDirectory(prefix="epub_structure_check_") as tmp_dir:
        epub_path = os.path.join(tmp_dir, "package.epub")
        with zipfile.ZipFile(epub_path, "w") as z:
            mimetype_path = os.path.join(scan.root, "mimetype")
            if os.path.isfile(mimetype_path):
                z.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
            else:
                z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", zipfile.ZIP_STORED)
            for rel_path in scan.all_files:
                if rel_path == "mimetype":
                    continue
                z.write(scan.abspath(rel_path), rel_path)
        return epubcheck_runner.run_epubcheck(epub_path)


def _root_of(tree):
    if tree is None:
        return None
    return tree.getroot() if hasattr(tree, "getroot") else tree


def run_full_analysis(project_root: str, normal_isbn: str = "", printed_isbn: str = "",
                       nav_sample_path: str = "", opf_sample_path: str = "", ncx_sample_path: str = "",
                       generate_ncx: bool = False, run_epubcheck: bool = True,
                       progress_callback=None, cancel_check=None,
                       fill_missing_pages: bool = True, prepare_images: bool = True) -> GenerationResult:
    """THE single entry point (spec 42: "COMPLETE PIPELINE"). `generate_ncx`:
    spec 12/17 - "Do not generate NCX unless required" - the caller (GUI/
    test) decides this; an EXISTING ncx in the project is always
    regenerated in place regardless, since a stale/broken one left behind
    would itself be a real defect. `progress_callback(ProgressState)` and
    `cancel_check() -> bool` implement spec 38-45/52 (level-based progress
    + safe cancellation with rollback) - both optional; omitting them just
    means no live progress/cancellation, never a behavior change to the
    pipeline itself."""
    tracker = ProgressTracker(on_update=progress_callback, cancel_check=cancel_check)
    result = GenerationResult()
    written = []
    backup_dir = ""
    scan = None

    def _check_cancel():
        if cancel_check and cancel_check():
            raise Cancelled()

    try:
        # ---------------- LEVEL 1: PROJECT DISCOVERY ----------------
        tracker.start_level(1)
        scan = scanner.scan_project(project_root)
        result.scan = scan
        tracker.update("Scanning project directory", 1, 1)
        if not scan.xhtml_files:
            result.error = "No XHTML content documents were found in this project directory."
            return result
        _check_cancel()

        tracker.update("Backing up existing navigation/package files",
                       total=len(_writable_paths(scan)), completed=0)
        backup_dir = _backup(scan)
        result.backup_dir = backup_dir
        tracker.update("Taking a before-generation content snapshot")
        before_snapshot = content_integrity_guard.take_snapshot(scan)
        fingerprint_before = content_integrity_guard.compute_fingerprint(scan, before_snapshot)
        _check_cancel()

        # ---------------- LEVEL 2: CONTENT ANALYSIS ----------------
        tracker.start_level(2)
        registry = registry_mod.build_registry(
            scan, progress_cb=lambda done, total, path: tracker.update(
                "Reading XHTML and building the document registry", done, total, current_file=path))
        result.registry = registry
        _check_cancel()

        tracker.update("Auto-detecting metadata (title/author/publisher/...)")
        metadata = metadata_detector.detect_metadata(scan, registry)
        result.metadata = metadata
        # what the book's own title page and copyright page say (title,
        # authors, publisher, rights, year, ISBNs with their qualifiers)
        from core.epub.client_rules import front_matter
        front = front_matter.extract([(d.path, _root_of(d.tree)) for d in registry.documents])
        result.front_matter = front
        if not (normal_isbn or "").strip() and front.ebook_isbn():
            normal_isbn = front.ebook_isbn()
            result.isbn_sources["normal"] = f"copyright page ({front.copyright_page})"
        if not (printed_isbn or "").strip() and front.print_isbn(exclude=(re.sub(r"[^0-9Xx]", "", normal_isbn or ""),)):
            printed_isbn = front.print_isbn(exclude=(re.sub(r"[^0-9Xx]", "", normal_isbn or ""),))
            result.isbn_sources["printed"] = f"copyright page ({front.copyright_page})"
        result.normal_isbn = isbn_mod.validate_isbn(normal_isbn, "Normal ISBN")
        result.printed_isbn = isbn_mod.validate_isbn(printed_isbn, "Printed ISBN")

        profile = template_profile.load_template_profile(nav_sample_path, opf_sample_path, ncx_sample_path)
        result.profile = profile
        _check_cancel()

        # ---------------- LEVEL 3: NAVIGATION & LINKING ----------------
        tracker.start_level(3)
        tracker.update("Assigning ids to any heading/pagebreak that is missing one")
        existing_ids = set(registry.id_index.keys())
        for doc in registry.documents:
            if doc.tree is None:
                continue
            assigned_h = id_assigner.ensure_heading_ids(doc, existing_ids)
            assigned_p = id_assigner.ensure_pagebreak_ids(doc, existing_ids)
            if assigned_h or assigned_p:
                doc.dirty = True
            result.heading_ids_assigned += len(assigned_h)
            result.pagebreak_ids_assigned += len(assigned_p)
        _check_cancel()

        tracker.update("Scanning for missing page numbers" +
                       (" and filling safe gaps" if fill_missing_pages else ""))
        result.page_gap_result = pagebreak_gap_filler.fill_missing_pagebreaks(
            registry, existing_ids, apply=fill_missing_pages)
        _check_cancel()

        tracker.update("Analyzing figures/tables and their citations")
        fig_entries = figure_table_registry.build_figure_table_registry(registry)
        placement_decisions = citation_placement.plan_placements(fig_entries)
        result.placement_decisions = placement_decisions
        tracker.update("Moving figures/tables to their first citation (high confidence only)",
                       completed=len(placement_decisions), total=len(placement_decisions))
        moves = citation_placement.apply_placements(placement_decisions)
        for entry, _old, _new, _followers in moves:
            registry.by_path[entry.doc_path].dirty = True
        link_stats = figure_table_links.apply_figure_table_links(registry, fig_entries)
        result.figure_table_link_result = link_stats
        _check_cancel()

        tracker.update("Linking chapter/section cross-references")
        result.chapter_section_link_result = chapter_section_links.apply_chapter_section_links(registry)
        _check_cancel()

        tracker.update("Linking bibliography/reference citations")
        ref_registry = reference_links.build_reference_registry(registry)
        result.reference_link_result = reference_links.apply_reference_links(registry, ref_registry)
        result.reference_registry = ref_registry
        _check_cancel()

        tracker.update("Linking author-year bibliography citations")
        cite_registry = citation_links.build_citation_registry(registry)
        result.citation_link_result = citation_links.apply_citation_links(registry, cite_registry)
        result.citation_registry = cite_registry
        _check_cancel()

        tracker.update("Linking footnotes/endnotes")
        note_registry = footnote_links.build_note_registry(registry)
        result.footnote_link_result = footnote_links.apply_footnote_links(registry, note_registry)
        _check_cancel()

        # Page references in running text ("p. 60", "pp. 78-89") are left
        # as plain text - the client does not want them linked (abbreviated
        # ranges such as "pp. 184-8" also produced wrong targets: the "8"
        # was linked to printed page 8 in the front matter). Index page
        # locators are linked separately below and are unaffected.
        # Set LINK_PAGE_REFERENCES = True to turn the old behaviour back on.
        if LINK_PAGE_REFERENCES:
            tracker.update("Linking page references")
            result.page_reference_link_result = page_reference_links.apply_page_reference_links(registry)
        else:
            result.page_reference_link_result = page_reference_links.LinkResult()
            # a folder packaged by an older version still carries those links
            page_reference_links.remove_page_reference_links(registry)
        _check_cancel()

        tracker.update("Linking index page locators")
        result.index_locator_link_result = index_locator_links.apply_index_locator_links(registry)
        _check_cancel()

        tracker.update("Normalizing duplicate/nested anchor tags")
        result.anchor_normalize_result = anchor_normalizer.normalize_nested_anchors(registry)
        _check_cancel()

        # toc_resolver runs BEFORE link_resolver: both can rewrite the SAME
        # <a href> elements on the Contents page, and link_resolver always
        # reads the element's CURRENT live value - running the more
        # specific (number/text heading matching) TOC fix first means the
        # final, reported link_resolver pass reflects what's genuinely
        # still broken afterward.
        tracker.update("Resolving the Contents page")
        toc_resolution = toc_resolver.resolve_toc(registry)
        toc_changes = toc_resolver.apply_toc_resolution(registry, toc_resolution)
        result.toc_resolution = toc_resolution
        if toc_changes and toc_resolution.toc_path:
            registry.by_path[toc_resolution.toc_path].dirty = True
        _check_cancel()

        tracker.update("Resolving internal links",
                       completed=0, total=sum(len(d.links) for d in registry.documents))
        link_resolution = link_resolver.resolve_links(scan, registry)
        result.link_resolution = link_resolution
        for issue in link_resolution.fixed:
            registry.by_path[issue.doc_path].dirty = True

        # Only NOW (after link_resolver has already validated/repaired
        # every href in the book, including any pre-existing explicit
        # index-locator anchor) can this module's own already_valid/
        # auto_fixed/broken counts be filled in - see index_locator_links'
        # own module docstring for why this is a second, read-only pass
        # rather than a duplicate repair.
        index_locator_links.classify_existing_locators(
            registry, link_resolution, result.index_locator_link_result)
        _check_cancel()

        # ---------------- LEVEL 4: PACKAGE GENERATION ----------------
        tracker.start_level(4)
        dirty_docs = [d for d in registry.documents if getattr(d, "dirty", False) and d.tree is not None]
        for i, doc in enumerate(dirty_docs, start=1):
            tracker.update("Writing repaired content documents", i, len(dirty_docs), current_file=doc.path)
            _write_tree(scan, doc.path, doc.tree, doctype=doc.doctype)
            written.append(doc.path)
        _check_cancel()

        tracker.update("Generating the navigation document")
        nav_path = _default_nav_path(scan)

        css_href = (
            posixpath.relpath(
                scan.css_files[0],
                posixpath.dirname(nav_path) or "."
            )
            if scan.css_files
            else ""
        )
        book_title = metadata.title.value or registry.book_title or "Navigation"

        try:
            nav_doc = nav_generator.build_nav_document(
                registry,
                title=book_title,
                profile=profile,
                stylesheet_href=css_href
            )

            if nav_doc is None:
                raise RuntimeError(
                    "nav_generator.build_nav_document() returned None"
                )

            nav_abs_path = scan.abspath(nav_path)
            os.makedirs(os.path.dirname(nav_abs_path), exist_ok=True)

            _write_tree(scan, nav_path, nav_doc)

            if not os.path.isfile(nav_abs_path):
                raise RuntimeError(
                    f"Navigation file was not created: {nav_abs_path}"
                )

            written.append(nav_path)
            scan.nav_path = nav_path

            if nav_path not in scan.all_files:
                scan.all_files.append(nav_path)

        except Exception as exc:
            result.error = f"Navigation generation failed: {exc}"
            raise

        _check_cancel()

        tracker.update("Generating the OPF package document")
        opf_path = _default_opf_path(scan)
        opf_plan = opf_generator.build_opf_plan(scan, registry, opf_dir=posixpath.dirname(opf_path))
        opf_generator.mark_referenced(opf_plan, registry)
        opf_package = opf_generator.render_opf(opf_plan, registry, metadata, result.normal_isbn,
                                                result.printed_isbn, profile, front=front)
        _write_tree(scan, opf_path, opf_package)
        if not os.path.isfile(scan.abspath(opf_path)):
            raise RuntimeError(
                f"OPF file was not created: {scan.abspath(opf_path)}"
            )
        written.append(opf_path)
        scan.opf_path = opf_path
        if opf_path not in scan.all_files:
            scan.all_files.append(opf_path)
        _check_cancel()

        want_ncx = generate_ncx or bool(scan.ncx_path) or profile.ncx_present_in_sample
        if want_ncx:
            tracker.update("Generating the NCX document")
            ncx_path = _default_ncx_path(scan)
            identifier = isbn_mod.normalized_urn(result.normal_isbn) if result.normal_isbn.valid else ""
            ncx_doc = ncx_generator.build_ncx_document(registry, title=book_title, identifier=identifier)
            _write_tree(scan, ncx_path, ncx_doc)
            if not os.path.isfile(scan.abspath(ncx_path)):
                raise RuntimeError(
                    f"NCX file was not created: {scan.abspath(ncx_path)}"
                )
            written.append(ncx_path)
            scan.ncx_path = ncx_path
            if ncx_path not in scan.all_files:
                scan.all_files.append(ncx_path)

        tracker.update("Verifying META-INF/container.xml and mimetype")
        created = _ensure_container_files(scan, opf_path)
        for rel_path in created:
            written.append(rel_path)
            scan.all_files.append(rel_path)
        result.files_created = created
        result.files_written = written
        _check_cancel()

        # ---------------- LEVEL 5: VALIDATION & AUTO-REPAIR ----------------
        tracker.start_level(5)
        epubcheck_result = None
        if run_epubcheck:
            tracker.update("Running EPUBCheck")
            epubcheck_result = _run_epubcheck_on_project(scan)
        _check_cancel()

        # ---------------- LEVEL 6: FINAL VERIFICATION ----------------
        tracker.start_level(6)
        tracker.update("Verifying content integrity")
        after_snapshot = content_integrity_guard.take_snapshot(scan)
        fingerprint_after = content_integrity_guard.compute_fingerprint(scan, after_snapshot)
        integrity_result = content_integrity_guard.compare_snapshots(before_snapshot, after_snapshot)

        if not integrity_result.ok:
            tracker.update("Content integrity check failed - rolling back")
            _restore_backup(scan, backup_dir, written)
            result.rolled_back = True
            result.error = "Rolled back: " + "; ".join(integrity_result.mismatches)
            return result

        landmarks = nav_generator.build_landmarks_nav(registry, profile)
        page_list = nav_generator.build_page_list_nav(registry, profile)
        landmarks_count = len(landmarks.find(".//{http://www.w3.org/1999/xhtml}ol")) if landmarks is not None else 0
        page_count = len(page_list.find(".//{http://www.w3.org/1999/xhtml}ol")) if page_list is not None else 0

        tracker.update("Writing the final EPUB package")
        project_name = os.path.basename(os.path.normpath(scan.root)) or "package"
        output_epub_path = os.path.join(os.path.dirname(os.path.normpath(scan.root)), f"{project_name}.epub")
        result.image_preparation = _write_final_epub(scan, output_epub_path, prepare_images=prepare_images)
        result.output_epub_path = output_epub_path

        tracker.update("Building the final report")
        result.report = validator.build_report(
            registry, toc_resolution, page_count, landmarks_count, opf_plan, link_resolution,
            integrity_result, fingerprint_before, fingerprint_after, placement_decisions=placement_decisions,
            metadata=metadata, normal_isbn=result.normal_isbn, printed_isbn=result.printed_isbn,
            epubcheck_result=epubcheck_result, toc_href_changes=len(toc_changes),
            figure_table_link_result=result.figure_table_link_result,
            chapter_section_link_result=result.chapter_section_link_result,
            reference_link_result=result.reference_link_result, reference_registry=result.reference_registry,
            citation_link_result=result.citation_link_result, citation_registry=result.citation_registry,
            footnote_link_result=result.footnote_link_result,
            page_reference_link_result=result.page_reference_link_result,
            index_locator_link_result=result.index_locator_link_result,
            anchor_normalize_result=result.anchor_normalize_result,
            page_gap_result=result.page_gap_result, image_preparation=result.image_preparation)
        tracker.finish()
        result.stage_timings = dict(tracker._state.stage_timings)
        result.elapsed_seconds = tracker._state.elapsed_seconds
        return result

    except Cancelled:
        if scan is not None and backup_dir:
            _restore_backup(scan, backup_dir, written)
        result.cancelled = True
        result.error = "Package creation cancelled safely - all changes rolled back."
        return result
