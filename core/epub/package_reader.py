"""Reads an existing .epub (a real ZIP) into a lightweight, READ-ONLY
EpubPackage - just enough structure (manifest/spine/metadata/zip contents)
for core.epub.quick_validator and the Validation UI. Never writes to the
original file, never modifies it (spec Golden Rule 1: "NEVER modify
original EPUB automatically").

Deliberately NOT the full PackageModel the master spec describes for a
future repair engine (ids/links/fragments/repair_actions/package_profile
etc.) - "make only validation part" - this is a minimal reader sized for
validation display and the quick structural checks, not for rebuilding
a package."""
import os
import posixpath
import zipfile
from dataclasses import dataclass, field

from lxml import etree

OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
DC_NS = "http://purl.org/dc/elements/1.1/"


@dataclass
class ManifestItem:
    id: str
    href: str            # exactly as written in the OPF (relative to the OPF's own directory)
    media_type: str = ""
    properties: str = ""


@dataclass
class SpineItemRef:
    idref: str
    linear: bool = True


@dataclass
class EpubPackage:
    path: str
    opf_path: str = ""            # zip-internal path, e.g. "OEBPS/content.opf"
    epub_version: str = ""
    title: str = ""
    identifier: str = ""
    language: str = ""
    manifest: list = field(default_factory=list)     # [ManifestItem, ...]
    spine: list = field(default_factory=list)          # [SpineItemRef, ...]
    zip_names: set = field(default_factory=set)         # every entry name actually present in the zip
    first_entry_name: str = ""
    mimetype_stored: bool = False    # True if "mimetype" is STORED (uncompressed), as the spec requires
    mimetype_content: str = ""
    nav_path: str = ""                # resolved zip-internal path of the manifest item with properties="nav" (EPUB 3), or ""
    ncx_path: str = ""                # resolved zip-internal path of the NCX document (EPUB 2, or an EPUB 3 fallback), or ""
    metadata_count: int = 0           # total child element count under <metadata> - a generic count, not just title/id/lang
    error: str = ""                  # non-empty if the package could not be read/parsed at all

    @property
    def opf_dir(self) -> str:
        return posixpath.dirname(self.opf_path)

    def resolve_href(self, href: str) -> str:
        """href, resolved relative to the OPF's own directory, normalized
        to a zip-internal path - the SAME resolution rule every manifest
        item/spine href uses, per the OPF spec."""
        href = (href or "").split("#", 1)[0]  # a fragment never affects the FILE the href points at
        return posixpath.normpath(posixpath.join(self.opf_dir, href)) if href else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def read_package(epub_path: str) -> EpubPackage:
    """THE single entry point - opens epub_path (never modifies it) and
    returns an EpubPackage. Never raises: any failure (not a real zip, no
    container.xml, no readable OPF, malformed XML) is captured in
    package.error instead, so a caller can always display SOMETHING useful
    rather than an unhandled exception (spec: "Unknown ... must not crash
    the program" applies equally here, not just to EPUBCheck codes)."""
    pkg = EpubPackage(path=epub_path)
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            infos = zf.infolist()
            pkg.zip_names = {i.filename for i in infos}
            if infos:
                pkg.first_entry_name = infos[0].filename
                if infos[0].filename == "mimetype":
                    pkg.mimetype_stored = infos[0].compress_type == zipfile.ZIP_STORED
            if "mimetype" in pkg.zip_names:
                try:
                    pkg.mimetype_content = zf.read("mimetype").decode("ascii", errors="replace").strip()
                except (KeyError, OSError):
                    pass

            if "META-INF/container.xml" not in pkg.zip_names:
                pkg.error = "META-INF/container.xml is missing - not a valid EPUB package."
                return pkg
            container_xml = zf.read("META-INF/container.xml")
            container_root = etree.fromstring(container_xml)
            rootfile = container_root.find(f".//{{{CONTAINER_NS}}}rootfile")
            if rootfile is None:
                pkg.error = "META-INF/container.xml has no <rootfile> entry."
                return pkg
            pkg.opf_path = rootfile.get("full-path") or ""
            if not pkg.opf_path or pkg.opf_path not in pkg.zip_names:
                pkg.error = f"The OPF referenced by container.xml ('{pkg.opf_path}') was not found in the package."
                return pkg

            opf_bytes = zf.read(pkg.opf_path)
            opf_root = etree.fromstring(opf_bytes)
    except zipfile.BadZipFile as e:
        pkg.error = f"Not a valid ZIP/EPUB file: {e}"
        return pkg
    except etree.XMLSyntaxError as e:
        pkg.error = f"Malformed XML: {e}"
        return pkg
    except OSError as e:
        pkg.error = f"Could not read the file: {e}"
        return pkg

    pkg.epub_version = opf_root.get("version") or ""

    metadata_el = opf_root.find(f"{{{OPF_NS}}}metadata")
    if metadata_el is not None:
        title_el = metadata_el.find(f"{{{DC_NS}}}title")
        id_el = metadata_el.find(f"{{{DC_NS}}}identifier")
        lang_el = metadata_el.find(f"{{{DC_NS}}}language")
        pkg.title = (title_el.text or "").strip() if title_el is not None else ""
        pkg.identifier = (id_el.text or "").strip() if id_el is not None else ""
        pkg.language = (lang_el.text or "").strip() if lang_el is not None else ""
        pkg.metadata_count = len(list(metadata_el))

    manifest_el = opf_root.find(f"{{{OPF_NS}}}manifest")
    if manifest_el is not None:
        for item_el in manifest_el.findall(f"{{{OPF_NS}}}item"):
            pkg.manifest.append(ManifestItem(
                id=item_el.get("id") or "", href=item_el.get("href") or "",
                media_type=item_el.get("media-type") or "", properties=item_el.get("properties") or ""))

    spine_el = opf_root.find(f"{{{OPF_NS}}}spine")
    if spine_el is not None:
        for ref_el in spine_el.findall(f"{{{OPF_NS}}}itemref"):
            pkg.spine.append(SpineItemRef(
                idref=ref_el.get("idref") or "", linear=(ref_el.get("linear") or "yes") != "no"))

    # NAV (EPUB 3): the manifest item declaring properties="nav" - resolved
    # to a real zip-internal path the same way any other manifest href is.
    for item in pkg.manifest:
        if "nav" in (item.properties or "").split():
            pkg.nav_path = pkg.resolve_href(item.href)
            break

    # NCX (EPUB 2, or an EPUB 3 fallback some packages still include):
    # located via <spine toc="idref"> first (the authoritative pointer),
    # falling back to the conventional application/x-dtbncx+xml media type
    # if the spine doesn't declare one - never guessed from a filename.
    ncx_id = spine_el.get("toc") if spine_el is not None else None
    if ncx_id:
        for item in pkg.manifest:
            if item.id == ncx_id:
                pkg.ncx_path = pkg.resolve_href(item.href)
                break
    if not pkg.ncx_path:
        for item in pkg.manifest:
            if item.media_type == "application/x-dtbncx+xml":
                pkg.ncx_path = pkg.resolve_href(item.href)
                break

    return pkg
