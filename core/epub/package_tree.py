"""Categorizes a package's real entries into the Editor's own tree groups
(spec: "EPUBForge - PHASE 4 ONLY - Real EPUB Editor" layout: META-INF /
OEBPS / XHTML / CSS / Images / Fonts / OPF / NAV / NCX). Pure, GUI-free
logic so app.editor.editor_window's Treeview population is directly
testable without Tkinter.

Categorization walks the REAL package structure (core.epub.package_reader.
EpubPackage's manifest + its own zip_names) - never guesses from folder
naming conventions. Every real zip entry ends up in EXACTLY ONE category;
anything not covered by a more specific bucket goes to 'Other' rather than
being silently hidden from the tree."""
import posixpath

# spec: "Allow editing: .xhtml .xml .opf .css .ncx" - "NAV XHTML must also
# be editable" (nav.xhtml already has extension .xhtml, so it's covered
# here regardless of which tree category it displays under).
EDITABLE_EXTENSIONS = {".xhtml", ".xml", ".opf", ".css", ".ncx"}

_FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp"}
_XHTML_EXTENSIONS = {".xhtml", ".html", ".htm"}

CATEGORY_ORDER = ["OPF", "NAV", "NCX", "META-INF", "XHTML", "CSS", "Images", "Fonts", "Other"]


def is_editable(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in EDITABLE_EXTENSIONS


def categorize(package) -> dict:
    """Returns {category: [zip_name, ...] sorted}, only for categories
    that actually have at least one entry. `package`: a
    core.epub.package_reader.EpubPackage."""
    remaining = set(package.zip_names)
    groups = {name: [] for name in CATEGORY_ORDER}

    for name in sorted(package.zip_names):
        if name.startswith("META-INF/"):
            groups["META-INF"].append(name)
            remaining.discard(name)

    for category, path in (("OPF", package.opf_path), ("NAV", package.nav_path), ("NCX", package.ncx_path)):
        if path and path in remaining:
            groups[category].append(path)
            remaining.discard(path)

    media_by_resolved = {}
    for item in package.manifest:
        resolved = package.resolve_href(item.href) if item.href else ""
        if resolved:
            media_by_resolved[resolved] = item.media_type or ""

    for name in sorted(remaining):
        if name == "mimetype":
            groups["Other"].append(name)
            continue
        media_type = media_by_resolved.get(name, "")
        ext = posixpath.splitext(name)[1].lower()
        if media_type == "application/xhtml+xml" or ext in _XHTML_EXTENSIONS:
            groups["XHTML"].append(name)
        elif media_type == "text/css" or ext == ".css":
            groups["CSS"].append(name)
        elif media_type.startswith("image/") or ext in _IMAGE_EXTENSIONS:
            groups["Images"].append(name)
        elif media_type.startswith("font/") or ext in _FONT_EXTENSIONS:
            groups["Fonts"].append(name)
        else:
            groups["Other"].append(name)

    return {category: names for category, names in groups.items() if names}
