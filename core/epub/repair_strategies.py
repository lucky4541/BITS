"""Concrete deterministic repair strategies + StrategyRegistry (spec:
"EPUBForge - PHASE 3 - Universal Auto-Fix Engine" section 1: "RepairEngine,
RepairStrategy... StrategyRegistry", section 2's SAFE-repair examples).

Design choice, deliberate: every strategy here is a STRUCTURAL SWEEP over
the package's CURRENT real state (core.epub.package_builder.MutablePackage
+ a freshly-read core.epub.package_reader.EpubPackage), not a per-message
text-parser keyed to one specific finding. A strategy independently
detects whatever instances of its own condition currently exist and fixes
ALL of them, or does nothing at all if none exist. This is far more
robust than regexing EPUBCheck's own free-text messages (whose exact
wording differs across versions and is never something EPUBForge
controls), and it composes naturally with core.epub.repair_engine's own
"batch compatible repairs, build once, validate once" workflow (spec 5).
core.epub.error_analyzer's classification (see its own MIMETYPE_STRUCTURE/
UNDECLARED_RESOURCE/BROKEN_HREF_FIXABLE/NCX_PLAYORDER/DUPLICATE_ID
categories) is what decides WHETHER it's safe to run a strategy at all
this pass (via core.epub.safety_evaluator) - these functions never decide
that themselves, only HOW to fix a condition once greenlit.

Never invents content (spec 2): every value written here (mimetype's
fixed literal, a manifest media-type from a fixed extension table, a
renamed duplicate id, a href repointed at an ALREADY-EXISTING file, a
mechanically renumbered playOrder) is either a spec-mandated constant or
mechanically derived from data already present in the package - never a
guess at book content, wording, or authorship."""
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from core.epub import href_matching
from core.epub.package_reader import OPF_NS
from core.epub.xhtml_repair_strategy import (
    CATEGORY as XHTML_TAG_STRUCTURE,
    fix_xhtml_document,
)

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

# A fixed, IANA/EPUB-spec-standard extension -> media-type table - never
# guessed, never inferred from content. An extension not listed here is
# deliberately left undeclared rather than assigning a made-up type.
_MEDIA_TYPE_BY_EXT = {
    ".xhtml": "application/xhtml+xml", ".html": "application/xhtml+xml", ".htm": "application/xhtml+xml",
    ".css": "text/css",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".svg": "image/svg+xml", ".webp": "image/webp",
    ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".m4a": "audio/mp4", ".m4v": "video/mp4",
    ".js": "application/javascript", ".ncx": "application/x-dtbncx+xml",
    ".xml": "application/xml", ".smil": "application/smil+xml",
}


@dataclass
class RepairActionResult:
    applied: bool
    description: str = ""
    files: list = field(default_factory=list)   # zip-internal names touched, for the "Files Modified" report


def _relative_href(package, name: str) -> str:
    """name, expressed relative to the OPF's own directory - the SAME
    convention every existing manifest href already uses."""
    opf_dir = package.opf_dir
    if opf_dir and name.startswith(opf_dir + "/"):
        return name[len(opf_dir) + 1:]
    return name


def fix_mimetype(mp, package) -> RepairActionResult:
    """spec 2 example: a required structural file, restored/corrected
    exactly - 'application/epub+zip' is a fixed literal mandated by the
    EPUB OCF spec, never invented."""
    changed = []
    if not mp.exists("mimetype"):
        mp.add_entry("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        if "mimetype" in mp.order:
            mp.order.remove("mimetype")
        mp.order.insert(0, "mimetype")
        changed.append("Added missing 'mimetype' file ('application/epub+zip', stored uncompressed).")
    else:
        if mp.order and mp.order[0] != "mimetype":
            mp.order.remove("mimetype")
            mp.order.insert(0, "mimetype")
            changed.append("Moved 'mimetype' to be the first entry in the archive.")
        if mp.compress.get("mimetype") != zipfile.ZIP_STORED:
            mp.compress["mimetype"] = zipfile.ZIP_STORED
            changed.append("Changed 'mimetype' to be stored uncompressed.")
        if mp.get_bytes("mimetype") != b"application/epub+zip":
            mp.set_bytes("mimetype", b"application/epub+zip")
            mp.compress["mimetype"] = zipfile.ZIP_STORED
            changed.append("Corrected 'mimetype' content to exactly 'application/epub+zip'.")
    return RepairActionResult(applied=bool(changed), description="; ".join(changed),
                               files=["mimetype"] if changed else [])


def fix_duplicate_ids(mp, package) -> RepairActionResult:
    """spec 2 example: 'duplicate ID with deterministic references' - the
    FIRST occurrence of a duplicated id keeps it (so any href="#id"
    already pointing at it keeps resolving correctly, no reference
    rewritten, no data lost); every LATER occurrence is renamed to a
    fresh, guaranteed-unique value. Sweeps every real document in the
    package (OPF, NAV, NCX, every XHTML manifest item), since id
    uniqueness is a per-document XML constraint."""
    targets = set()
    if package.opf_path:
        targets.add(package.opf_path)
    if package.nav_path:
        targets.add(package.nav_path)
    if package.ncx_path:
        targets.add(package.ncx_path)
    for item in package.manifest:
        if item.media_type == "application/xhtml+xml":
            resolved = package.resolve_href(item.href)
            if resolved:
                targets.add(resolved)

    changed_files, descriptions = [], []
    for name in sorted(targets):
        if not mp.exists(name):
            continue
        try:
            tree = mp.get_tree(name)
        except etree.XMLSyntaxError:
            continue  # a document that doesn't even parse can't be safely edited here
        elements_by_id = {}
        for el in tree.iter():
            id_val = el.get("id")
            if id_val:
                elements_by_id.setdefault(id_val, []).append(el)
        all_ids = set(elements_by_id.keys())
        renamed_here = []
        for id_val, elements in elements_by_id.items():
            if len(elements) <= 1:
                continue
            n = 1
            for el in elements[1:]:
                n += 1
                new_id = f"{id_val}-{n}"
                while new_id in all_ids:
                    n += 1
                    new_id = f"{id_val}-{n}"
                all_ids.add(new_id)
                el.set("id", new_id)
                renamed_here.append((id_val, new_id))
        if renamed_here:
            mp.mark_tree_dirty(name)
            changed_files.append(name)
            for old, new in renamed_here:
                descriptions.append(f"{name}: renamed duplicate id '{old}' occurrence to '{new}'.")
    return RepairActionResult(applied=bool(changed_files), description="; ".join(descriptions), files=changed_files)


def fix_undeclared_resources(mp, package) -> RepairActionResult:
    """spec 2 example: 'missing manifest entry when resource exists' -
    every real file physically in the archive that no manifest item
    declares gets a new <item>, with a media type from the FIXED extension
    table above - a file type this module doesn't recognize is left alone
    rather than assigned a guessed type."""
    if not package.opf_path or not mp.exists(package.opf_path):
        return RepairActionResult(applied=False)
    declared = {package.resolve_href(item.href) for item in package.manifest if item.href}
    existing_ids = {item.id for item in package.manifest if item.id}

    to_add = []
    for name in sorted(mp.order):
        if name in mp.removed or name in declared or name in ("mimetype", package.opf_path):
            continue
        if name.startswith("META-INF/"):
            continue
        media_type = _MEDIA_TYPE_BY_EXT.get(posixpath.splitext(name)[1].lower())
        if media_type:
            to_add.append((name, media_type))
    if not to_add:
        return RepairActionResult(applied=False)

    opf_tree = mp.get_tree(package.opf_path)
    manifest_el = opf_tree.find(f"{{{OPF_NS}}}manifest")
    if manifest_el is None:
        return RepairActionResult(applied=False)

    descriptions = []
    for name, media_type in to_add:
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", posixpath.splitext(posixpath.basename(name))[0]) or "item"
        candidate, n = stem, 1
        while candidate in existing_ids:
            n += 1
            candidate = f"{stem}_{n}"
        existing_ids.add(candidate)
        item_el = etree.SubElement(manifest_el, f"{{{OPF_NS}}}item")
        item_el.set("id", candidate)
        item_el.set("href", _relative_href(package, name))
        item_el.set("media-type", media_type)
        descriptions.append(f"Added manifest entry for existing file '{name}' "
                             f"(id='{candidate}', media-type='{media_type}').")

    mp.mark_tree_dirty(package.opf_path)
    return RepairActionResult(applied=True, description="; ".join(descriptions), files=[package.opf_path])


def fix_broken_manifest_hrefs(mp, package) -> RepairActionResult:
    """spec 2 example: 'broken href with deterministic target' - only
    repoints a manifest item's href when EXACTLY ONE real file in the
    package matches its basename (see core.epub.href_matching); anything
    ambiguous is left for manual/review, never guessed."""
    if not package.opf_path or not mp.exists(package.opf_path):
        return RepairActionResult(applied=False)
    real_names = [n for n in mp.order if n not in mp.removed]

    opf_tree = mp.get_tree(package.opf_path)
    manifest_el = opf_tree.find(f"{{{OPF_NS}}}manifest")
    if manifest_el is None:
        return RepairActionResult(applied=False)

    descriptions, changed = [], False
    for item_el in manifest_el.findall(f"{{{OPF_NS}}}item"):
        href = item_el.get("href") or ""
        if not href:
            continue
        resolved = package.resolve_href(href)
        if not resolved or resolved in package.zip_names:
            continue
        match = href_matching.find_unique_basename_match(real_names, resolved, exclude={package.opf_path})
        if not match:
            continue
        new_href = _relative_href(package, match)
        item_el.set("href", new_href)
        descriptions.append(f"Fixed manifest item '{item_el.get('id')}' href: '{href}' -> '{new_href}' "
                             f"(unique match found in the package).")
        changed = True
    if changed:
        mp.mark_tree_dirty(package.opf_path)
    return RepairActionResult(applied=changed, description="; ".join(descriptions),
                               files=[package.opf_path] if changed else [])


def fix_ncx_playorder(mp, package) -> RepairActionResult:
    """spec 2 example: 'NCX playOrder inconsistencies when deterministically
    repairable' - renumbers every navPoint's playOrder sequentially, in the
    NCX's own existing document order. Never reorders content, only
    corrects the numbering to match what's already there."""
    if not package.ncx_path or not mp.exists(package.ncx_path):
        return RepairActionResult(applied=False)
    try:
        tree = mp.get_tree(package.ncx_path)
    except etree.XMLSyntaxError:
        return RepairActionResult(applied=False)
    nav_points = list(tree.iter(f"{{{NCX_NS}}}navPoint"))
    if not nav_points:
        return RepairActionResult(applied=False)
    changed = False
    for i, el in enumerate(nav_points, start=1):
        if el.get("playOrder") != str(i):
            el.set("playOrder", str(i))
            changed = True
    if changed:
        mp.mark_tree_dirty(package.ncx_path)
    return RepairActionResult(
        applied=changed,
        description=f"Renumbered {len(nav_points)} NCX navPoint playOrder value(s) sequentially." if changed else "",
        files=[package.ncx_path] if changed else [])


_CONTENT_REFERENCE_TAGS = {"img", "image", "source", "object"}
_CONTENT_REFERENCE_ATTRS = ("src", "data", "{http://www.w3.org/1999/xlink}href")


def fix_broken_content_references(mp, package) -> RepairActionResult:
    """spec 2 example: 'broken href with deterministic target', applied to
    resource references INSIDE CONTENT DOCUMENTS (img/src, SVG image/
    xlink:href, source/src, object/data) - fix_broken_manifest_hrefs above
    handles the same class of defect at the OPF manifest level; this is
    the content-document counterpart. A real, observed defect: a reference
    with an extra/duplicated directory segment (e.g. "images/images/
    fig.png") where the real file exists at the correct, shorter path.
    Only ever repoints an attribute when core.epub.href_matching.
    find_unique_basename_match finds EXACTLY ONE real candidate elsewhere
    in the archive - the referenced file's own bytes are never touched,
    and no reference is rewritten when the match is ambiguous or absent.
    Only the attribute VALUE changes, never a text node, so this can never
    be detected as a content change by core.epub.regression_checker's own
    visible-text comparison."""
    changed_files, descriptions = [], []
    for item in package.manifest:
        if item.media_type != "application/xhtml+xml" or not item.href:
            continue
        name = package.resolve_href(item.href)
        if not name or not mp.exists(name):
            continue
        try:
            tree = mp.get_tree(name)
        except etree.XMLSyntaxError:
            continue
        doc_dir = posixpath.dirname(name)
        changed_here = []
        for el in tree.iter():
            tag = el.tag
            if not isinstance(tag, str):
                continue  # skip comments/processing instructions
            local = tag.rsplit("}", 1)[-1]
            if local not in _CONTENT_REFERENCE_TAGS:
                continue
            for attr in _CONTENT_REFERENCE_ATTRS:
                value = el.get(attr)
                if not value:
                    continue
                path_part, _, fragment = value.partition("#")
                if not path_part or "://" in path_part or path_part.startswith("data:"):
                    continue  # external/data URIs are never this engine's concern
                resolved = posixpath.normpath(posixpath.join(doc_dir, path_part))
                if resolved in package.zip_names:
                    continue  # not broken
                match = href_matching.find_unique_basename_match(package.zip_names, resolved, exclude={name})
                if not match:
                    continue
                new_value = posixpath.relpath(match, doc_dir) + (f"#{fragment}" if fragment else "")
                el.set(attr, new_value)
                changed_here.append(f"{name}: repointed '{value}' -> '{new_value}'.")
        if changed_here:
            mp.mark_tree_dirty(name)
            changed_files.append(name)
            descriptions.extend(changed_here)
    return RepairActionResult(applied=bool(changed_files), description="; ".join(descriptions), files=changed_files)


class StrategyRegistry:
    """Maps a core.epub.error_analyzer RootCause.category to the strategy
    function that knows how to fix it. ONE registry, ONE lookup - never a
    second/parallel dispatch mechanism."""

    def __init__(self):
        self._by_category = {}

    def register(self, category: str, fn):
        self._by_category[category] = fn

    def strategies_for(self, safe_categories) -> list:
        return [(category, self._by_category[category])
                for category in safe_categories if category in self._by_category]


DEFAULT_REGISTRY = StrategyRegistry()
DEFAULT_REGISTRY.register("MIMETYPE_STRUCTURE", fix_mimetype)
DEFAULT_REGISTRY.register("DUPLICATE_ID", fix_duplicate_ids)
DEFAULT_REGISTRY.register("UNDECLARED_RESOURCE", fix_undeclared_resources)
DEFAULT_REGISTRY.register("BROKEN_HREF_FIXABLE", fix_broken_manifest_hrefs)
DEFAULT_REGISTRY.register("BROKEN_CONTENT_REFERENCE_FIXABLE", fix_broken_content_references)
DEFAULT_REGISTRY.register("NCX_PLAYORDER", fix_ncx_playorder)
DEFAULT_REGISTRY.register(
    XHTML_TAG_STRUCTURE,
    fix_xhtml_document,
)
