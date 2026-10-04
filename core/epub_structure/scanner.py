"""Step 1 (spec: "EPUB STRUCTURE / PACKAGE BUILDER" section 1/25) - scans
an EPUB project/output DIRECTORY (an already-unzipped or freshly-generated
tree of XHTML/OPF/CSS/images - never a .epub zip; core.epub/ already
covers the zip case) and categorizes every real file it finds. Never
requires the user to select files one at a time; never assumes a fixed
book-specific filename pattern.

Extension buckets deliberately mirror core.epub.package_tree's own
categories (kept as an independent, small, literal copy rather than an
import - a directory scan and a package_reader.EpubPackage's manifest-
driven categorization are different enough data shapes that sharing code
would only add coupling for no real benefit)."""
import os
import posixpath
import re
from dataclasses import dataclass, field

_XHTML_EXTENSIONS = {".xhtml", ".html", ".htm"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_SVG_EXTENSIONS = {".svg"}
_FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}
_AV_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".m4v", ".ogg", ".webm"}
_CSS_EXTENSIONS = {".css"}
_OPF_EXTENSIONS = {".opf"}
_NCX_EXTENSIONS = {".ncx"}

# Directories never worth walking into for package content - housekeeping/
# backup artifacts this same app itself creates alongside a project (spec
# 24 doesn't want an "EPUB Structure" run to trip over its own backups).
_SKIP_DIR_NAMES = {".git", "__pycache__", ".epub_structure_backup"}

_TOC_NAV_RE = re.compile(r'epub:type\s*=\s*"[^"]*\btoc\b[^"]*"', re.IGNORECASE)
_NAV_TAG_RE = re.compile(r"<nav\b", re.IGNORECASE)
_LEADING_DIGITS_RE = re.compile(r"^(\d+)")


@dataclass
class ProjectScan:
    root: str                                # absolute filesystem path of the scanned directory
    xhtml_files: list = field(default_factory=list)   # relative, POSIX-style paths, in NATURAL document order
    opf_path: str = ""
    nav_path: str = ""
    ncx_path: str = ""
    css_files: list = field(default_factory=list)
    image_files: list = field(default_factory=list)
    svg_files: list = field(default_factory=list)
    font_files: list = field(default_factory=list)
    av_files: list = field(default_factory=list)
    other_files: list = field(default_factory=list)
    all_files: list = field(default_factory=list)     # every file found, relative POSIX path

    def abspath(self, relative: str) -> str:
        return os.path.join(self.root, *relative.split("/"))


def _natural_sort_key(relative_path: str):
    """Sorts "2_ch2.xhtml" before "10_ch10.xhtml" - a plain string sort
    would put "10_" before "2_". Splits into (numeric-prefix, filename)
    pairs so any real numeric ordering already baked into the project's
    own filenames is respected without ever assuming a SPECIFIC pattern
    (spec: "must work for other books with completely different filenames
    and numbers")."""
    name = posixpath.basename(relative_path)
    m = _LEADING_DIGITS_RE.match(name)
    prefix_num = int(m.group(1)) if m else float("inf")
    return (prefix_num, name.lower(), relative_path.lower())


def _looks_like_nav_document(abs_path: str) -> bool:
    """A real EPUB 3 nav document is identified by CONTENT (a <nav
    epub:type="toc"> element), never by filename alone (spec 5: "Do not
    hard-code the filename" - applied here to NAV detection too, since a
    project may name it "Nav.xhtml", "nav.xhtml", "toc.xhtml", or anything
    else). Read as raw text, not parsed - this is a cheap pre-filter, real
    structural parsing happens later in core.epub_structure.xhtml_parser."""
    try:
        with open(abs_path, "r", encoding="utf-8-sig", errors="replace") as f:
            content = f.read()
    except OSError:
        return False
    return bool(_NAV_TAG_RE.search(content) and _TOC_NAV_RE.search(content))


def scan_project(root_dir: str) -> ProjectScan:
    """THE single entry point for step 1. `root_dir`: an absolute path to
    the project/output directory the user selected. Every path recorded on
    the returned ProjectScan is a POSIX-style path RELATIVE to root_dir
    (never an absolute filesystem path, never backslashes) - the same
    convention core.epub.package_reader.EpubPackage already uses for zip-
    internal paths, so downstream code never has to juggle two different
    path conventions."""
    root_dir = os.path.abspath(root_dir)
    scan = ProjectScan(root=root_dir)
    xhtml_candidates = []
    nav_candidates = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for filename in filenames:
            abs_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(abs_path, root_dir).replace(os.sep, "/")
            scan.all_files.append(rel_path)
            ext = posixpath.splitext(filename)[1].lower()

            if ext in _OPF_EXTENSIONS:
                scan.opf_path = rel_path
            elif ext in _NCX_EXTENSIONS:
                scan.ncx_path = rel_path
            elif ext in _CSS_EXTENSIONS:
                scan.css_files.append(rel_path)
            elif ext in _SVG_EXTENSIONS:
                scan.svg_files.append(rel_path)
            elif ext in _IMAGE_EXTENSIONS:
                scan.image_files.append(rel_path)
            elif ext in _FONT_EXTENSIONS:
                scan.font_files.append(rel_path)
            elif ext in _AV_EXTENSIONS:
                scan.av_files.append(rel_path)
            elif ext in _XHTML_EXTENSIONS:
                xhtml_candidates.append(rel_path)
                if _looks_like_nav_document(abs_path):
                    nav_candidates.append(rel_path)
            elif filename.lower() == "mimetype":
                scan.other_files.append(rel_path)
            else:
                scan.other_files.append(rel_path)

    # A real nav document is CONTENT, never spine reading matter - it is
    # never also listed among the book's own chapter/frontmatter documents
    # (spec 8's nav.xhtml is a navigation aid, not a "chapter").
    if nav_candidates:
        scan.nav_path = sorted(nav_candidates, key=_natural_sort_key)[0]
        xhtml_candidates = [p for p in xhtml_candidates if p not in nav_candidates]

    scan.xhtml_files = sorted(xhtml_candidates, key=_natural_sort_key)
    scan.css_files.sort()
    scan.image_files.sort()
    scan.svg_files.sort()
    scan.font_files.sort()
    scan.av_files.sort()
    scan.other_files.sort()
    scan.all_files.sort()
    return scan
