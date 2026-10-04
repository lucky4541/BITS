"""Deep CONTENT-INTEGRITY inventory of an EPUB and a before/after diff - the
"ZERO DATA LOSS" guard of the auto-repair engine (core.epub.repair_engine).

An inventory records, from the packaged .epub itself:

  per file      sha256, size
  per XHTML     normalized text (word sequence), paragraphs, headings (text),
                lists / list items / tables / figures / asides, images (src +
                whether it resolves), links (href + whether it resolves),
                IDs, page markers (labels), CSS links
  package       metadata entries, manifest (resolved paths, media types),
                spine documents, navigation entries (label + target),
                NCX navPoints, CSS rules / selectors per stylesheet,
                every binary asset (hash, size, image dimensions)

diff(before, after, allowed) returns findings:
  LOSS    - content / structure that disappeared or changed unexpectedly
            (text words removed or altered, a heading / paragraph / image /
            link / page marker / nav entry / metadata entry / spine document
            / manifest declaration / CSS selector / binary asset gone, a
            reference that resolved before and no longer resolves, an ID
            gone that was not a recorded rename)
  CHANGE  - an expected, recorded change (an ID renamed, a href repointed,
            a junk file removed) - reported, never a failure
A hash change alone is never a loss: valid repairs change files. Content is
compared SEMANTICALLY (normalized text, structure counts, references).

Malformed documents are read with a recovering parser after the same named-
entity normalisation the repair applies, so a document that only becomes
well-formed through the repair is compared fairly.
"""
import hashlib
import html.entities
import io
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import unquote

from lxml import etree

XHTML_TYPES = ("application/xhtml+xml",)
EPUB_NS = "http://www.idpf.org/2007/ops"
_XML_PREDEFINED = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_INVISIBLE_RE = re.compile("[­​‌‍⁠﻿]")
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def named_entities_to_numeric(text: str) -> str:
    """&nbsp; -> &#160; for every HTML named entity XML does not predefine
    (the exact same character, so no text changes). Unknown names are left
    untouched."""
    def repl(m):
        name = m.group(1)
        if name in _XML_PREDEFINED:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        if cp is None:
            val = html.entities.html5.get(name + ";")
            if not val:
                return m.group(0)
            return "".join(f"&#{ord(c)};" for c in val)
        return f"&#{cp};"
    return _ENTITY_RE.sub(repl, text)


def parse_lenient(data: bytes):
    """(root, well_formed) - strict parse first, recovering parse otherwise."""
    try:
        return etree.fromstring(data), True
    except etree.XMLSyntaxError:
        pass
    text = data.decode("utf-8", errors="replace")
    text = named_entities_to_numeric(text)
    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    try:
        root = etree.fromstring(text.encode("utf-8"), parser)
    except (etree.XMLSyntaxError, ValueError):
        root = None
    return root, False


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def normalize_words(text: str) -> list:
    text = unicodedata.normalize("NFKC", _INVISIBLE_RE.sub("", text or ""))
    return text.split()


# inline elements: wrapping text in one of these (a link, a span) must not
# change the words - every other element boundary separates words
INLINE = {"a", "span", "i", "b", "em", "strong", "sup", "sub", "small", "abbr", "cite", "code", "q", "u", "s",
          "mark", "dfn", "kbd", "samp", "var", "bdi", "bdo", "time", "data", "ruby", "rt", "rp", "del", "ins"}


def flow_text(el) -> str:
    """The element's text with a separator at block boundaries only."""
    out = []

    def walk(e):
        inline = _local(e.tag) in INLINE
        if not inline:
            out.append(" ")
        if e.text:
            out.append(e.text)
        for c in e:
            if isinstance(c.tag, str):
                walk(c)
            if c.tail:
                out.append(c.tail)
        if not inline:
            out.append(" ")
    walk(el)
    return "".join(out)


def _text_of(el) -> str:
    return " ".join("".join(el.itertext()).split())


def _epub_type(el):
    return el.get(f"{{{EPUB_NS}}}type") or el.get("epub:type") or ""


@dataclass
class DocInventory:
    path: str
    well_formed: bool = True
    words: list = field(default_factory=list)
    paragraphs: int = 0
    headings: list = field(default_factory=list)          # heading texts in order
    lists: int = 0
    list_items: int = 0
    tables: int = 0
    figures: int = 0
    asides: int = 0
    images: list = field(default_factory=list)            # (src, resolves)
    links: list = field(default_factory=list)             # (href, resolves)
    ids: list = field(default_factory=list)
    page_markers: list = field(default_factory=list)      # labels
    css_links: list = field(default_factory=list)


@dataclass
class Inventory:
    path: str
    files: dict = field(default_factory=dict)             # name -> (sha256, size)
    docs: dict = field(default_factory=dict)              # name -> DocInventory
    metadata: list = field(default_factory=list)          # "tag=text|attrs"
    manifest: dict = field(default_factory=dict)          # resolved path -> media type
    spine: list = field(default_factory=list)             # resolved paths
    nav_entries: list = field(default_factory=list)       # (label, resolved target)
    ncx_points: list = field(default_factory=list)
    css: dict = field(default_factory=dict)               # name -> sorted selector list
    binaries: dict = field(default_factory=dict)          # name -> {"sha256", "size", "dims"}
    opf_path: str = ""
    error: str = ""

    # ---------------------------------------------------------- totals
    def totals(self) -> dict:
        d = self.docs.values()
        return {
            "xhtml_files": len(self.docs),
            "characters": sum(len("".join(x.words)) for x in d),
            "words": sum(len(x.words) for x in d),
            "paragraphs": sum(x.paragraphs for x in d),
            "headings": sum(len(x.headings) for x in d),
            "lists": sum(x.lists for x in d),
            "images": sum(len(x.images) for x in d),
            "image_files": sum(1 for n in self.binaries if self.binaries[n].get("dims") is not None),
            "links": sum(len(x.links) for x in d),
            "ids": sum(len(x.ids) for x in d),
            "page_markers": sum(len(x.page_markers) for x in d),
            "nav_entries": len(self.nav_entries),
            "metadata": len(self.metadata),
            "manifest_items": len(self.manifest),
            "spine_items": len(self.spine),
        }

    def to_dict(self):
        return {"path": self.path, "totals": self.totals(),
                "files": {k: {"sha256": v[0], "size": v[1]} for k, v in sorted(self.files.items())},
                "binaries": self.binaries, "metadata": self.metadata, "spine": self.spine,
                "nav_entries": self.nav_entries,
                "docs": {k: {"well_formed": v.well_formed, "words": len(v.words), "paragraphs": v.paragraphs,
                             "headings": v.headings, "images": v.images, "links": len(v.links),
                             "ids": len(v.ids), "page_markers": v.page_markers}
                         for k, v in sorted(self.docs.items())}}


def _image_dims(data: bytes):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return list(im.size)
    except Exception:
        return None


def pixel_hash(data: bytes):
    """sha256 of the decoded pixels (mode + size + raw data) - identical for
    two files that differ only in header metadata such as the DPI."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return hashlib.sha256(f"{im.mode}{im.size}".encode() + im.tobytes()).hexdigest()
    except Exception:
        return None


def _resolve(base_dir, href):
    path = unquote(href.split("#", 1)[0])
    if not path:
        return ""
    return posixpath.normpath(posixpath.join(base_dir, path)) if base_dir else posixpath.normpath(path)


def _is_external(href):
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href or "")) and not href.lower().startswith("file:")


def build_inventory(epub_path: str) -> Inventory:
    inv = Inventory(path=epub_path)
    try:
        zf = zipfile.ZipFile(epub_path)
    except (OSError, zipfile.BadZipFile) as e:
        inv.error = f"cannot open package: {e}"
        return inv
    with zf:
        names = [i.filename for i in zf.infolist() if not i.filename.endswith("/")]
        data = {n: zf.read(n) for n in names}
    name_set = set(names)
    for n, b in data.items():
        inv.files[n] = (hashlib.sha256(b).hexdigest(), len(b))
    # container -> OPF
    opf = ""
    try:
        croot = etree.fromstring(data.get("META-INF/container.xml", b""))
        rf = next((e for e in croot.iter() if _local(e.tag) == "rootfile"), None)
        opf = rf.get("full-path", "") if rf is not None else ""
    except etree.XMLSyntaxError:
        pass
    if not opf:
        opf = next((n for n in names if n.lower().endswith(".opf")), "")
    inv.opf_path = opf
    opf_dir = posixpath.dirname(opf)
    xhtml, nav_path, ncx_path = set(), "", ""
    if opf in data:
        oroot, _ok = parse_lenient(data[opf])
        if oroot is not None:
            ids_to_path = {}
            for el in oroot.iter():
                ln = _local(el.tag)
                parent = el.getparent()
                if parent is not None and _local(parent.tag) == "metadata" and isinstance(el.tag, str):
                    attrs = ",".join(f"{_local(k)}={v}" for k, v in sorted(el.attrib.items()) if _local(k) != "id")
                    inv.metadata.append(f"{ln}={' '.join((el.text or '').split())}|{attrs}")
                if ln == "item":
                    p = _resolve(opf_dir, el.get("href", ""))
                    inv.manifest[p] = el.get("media-type", "")
                    ids_to_path[el.get("id")] = p
                    if el.get("media-type") in XHTML_TYPES:
                        xhtml.add(p)
                    if "nav" in (el.get("properties") or "").split():
                        nav_path = p
                    if el.get("media-type") == "application/x-dtbncx+xml":
                        ncx_path = p
            for el in oroot.iter():
                if _local(el.tag) == "itemref" and ids_to_path.get(el.get("idref")):
                    inv.spine.append(ids_to_path[el.get("idref")])
    # XHTML documents (manifest-declared, plus any .xhtml/.html file in the archive)
    xhtml |= {n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))}
    all_ids = {}
    for name in sorted(xhtml):
        if name not in data:
            continue
        root, ok = parse_lenient(data[name])
        doc = DocInventory(path=name, well_formed=ok)
        inv.docs[name] = doc
        if root is None:
            continue
        body = next((e for e in root.iter() if _local(e.tag) == "body"), root)
        doc.words = normalize_words(flow_text(body))
        base = posixpath.dirname(name)
        for el in root.iter():
            ln = _local(el.tag)
            if not ln:
                continue
            if el.get("id"):
                doc.ids.append(el.get("id"))
            if ln == "p":
                doc.paragraphs += 1
            elif ln in HEADINGS:
                doc.headings.append(_text_of(el))
            elif ln in ("ul", "ol", "dl"):
                doc.lists += 1
            elif ln in ("li", "dd", "dt"):
                doc.list_items += 1
            elif ln == "table":
                doc.tables += 1
            elif ln == "figure":
                doc.figures += 1
            elif ln == "aside":
                doc.asides += 1
            elif ln == "link" and "stylesheet" in (el.get("rel") or ""):
                doc.css_links.append(el.get("href", ""))
            if ln in ("img", "image"):
                src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
                ok_ref = _is_external(src) or src.startswith("data:") or _resolve(base, src.replace("\\", "/")) in name_set
                doc.images.append((src, ok_ref))
            if ln == "a" and el.get("href") is not None:
                doc.links.append((el.get("href"), None))
            if _epub_type(el) == "pagebreak" or el.get("role") == "doc-pagebreak":
                doc.page_markers.append(el.get("aria-label") or el.get("title") or _text_of(el))
        all_ids[name] = set(doc.ids)
    # link resolution (needs every document's ids)
    for name, doc in inv.docs.items():
        base = posixpath.dirname(name)
        resolved = []
        for href, _ in doc.links:
            resolved.append((href, _link_resolves(href, base, name, name_set, all_ids)))
        doc.links = resolved
    # navigation
    if nav_path in data:
        root, _ok = parse_lenient(data[nav_path])
        if root is not None:
            base = posixpath.dirname(nav_path)
            for a in root.iter():
                if _local(a.tag) == "a" and a.get("href") is not None:
                    inv.nav_entries.append([_text_of(a), _resolve(base, a.get("href")) +
                                            ("#" + a.get("href").split("#", 1)[1] if "#" in a.get("href") else "")])
    if ncx_path in data:
        root, _ok = parse_lenient(data[ncx_path])
        if root is not None:
            inv.ncx_points = [_text_of(e) for e in root.iter() if _local(e.tag) == "navpoint"]
    # CSS and binaries
    for n, b in data.items():
        low = n.lower()
        if low.endswith(".css"):
            text = re.sub(r"/\*.*?\*/", "", b.decode("utf-8", errors="replace"), flags=re.S)
            inv.css[n] = sorted(" ".join(s.split()) for s in re.findall(r"([^{}]+)\{", text) if s.strip())
        elif not low.endswith((".xhtml", ".html", ".htm", ".opf", ".ncx", ".xml", ".css", ".txt")) and n != "mimetype":
            dims = _image_dims(b) if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif",
                                                   ".tiff")) else None
            inv.binaries[n] = {"sha256": inv.files[n][0], "size": len(b), "dims": dims}
            if dims is not None:
                inv.binaries[n]["pixels"] = pixel_hash(b)
    return inv


def _link_resolves(href, base, here, name_set, all_ids):
    if href is None:
        return False
    if _is_external(href) or href.startswith(("mailto:", "data:")):
        return True
    path, _, frag = href.partition("#")
    target = here if not path else _resolve(base, path.replace("\\", "/"))
    if target not in name_set:
        return False
    if frag:
        ids = all_ids.get(target)
        return ids is None or unquote(frag) in ids
    return True


# ===================================================================== diff
@dataclass
class Finding:
    severity: str            # "LOSS" | "CHANGE"
    category: str
    file: str
    detail: str

    def __str__(self):
        return f"[{self.severity}] {self.category} {self.file}: {self.detail}"


@dataclass
class Allowed:
    """Changes a repair declared it makes on purpose."""
    renamed_ids: dict = field(default_factory=dict)       # (file, old_id) -> new_id
    removed_files: set = field(default_factory=set)       # junk entries deliberately not packaged
    text_changes: dict = field(default_factory=dict)      # file -> reason (never used by automatic repairs)
    # narrowly declared edits (client-rule repairs): only exactly these may differ
    text_edits: dict = field(default_factory=dict)        # file -> [(old text, new text)] in the visible text
    metadata_changes: dict = field(default_factory=dict)  # old metadata entry -> reason (value corrected)
    binary_changes: dict = field(default_factory=dict)    # asset -> reason (header only; pixels proven identical)
    nav_relabels: dict = field(default_factory=dict)      # old navigation label -> new label
    added_text: dict = field(default_factory=dict)        # file -> [text a repair added (e.g. a landmark entry)]
    resized_images: dict = field(default_factory=dict)    # image -> [w, h] it was deliberately scaled to (the cover)

    def merge(self, other):
        self.renamed_ids.update(other.renamed_ids)
        self.removed_files |= set(other.removed_files)
        self.text_changes.update(other.text_changes)
        for f, edits in other.text_edits.items():
            mine = self.text_edits.setdefault(f, [])
            mine.extend(e for e in edits if e not in mine)
        self.metadata_changes.update(other.metadata_changes)
        self.binary_changes.update(other.binary_changes)
        self.resized_images.update(other.resized_images)
        self.nav_relabels.update(other.nav_relabels)
        for f, texts in other.added_text.items():
            self.added_text.setdefault(f, []).extend(texts)


@dataclass
class IntegrityResult:
    findings: list = field(default_factory=list)

    @property
    def losses(self):
        return [f for f in self.findings if f.severity == "LOSS"]

    @property
    def ok(self):
        return not self.losses

    def summary(self):
        if self.ok:
            changes = sum(1 for f in self.findings if f.severity == "CHANGE")
            return "PASS - no content lost" + (f" ({changes} expected change(s))" if changes else "")
        return f"FAIL - {len(self.losses)} loss(es): " + "; ".join(str(f) for f in self.losses[:5])


def _word_diff(a, b, limit=3):
    out = []
    sm = SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append(f"{tag}: '{' '.join(a[i1:i2])[:80]}' -> '{' '.join(b[j1:j2])[:80]}'")
        if len(out) >= limit:
            break
    return out


def _apply_edits(text, edits):
    for old, new in edits:
        text = text.replace(old, new, 1)
    return text


def _remove_added(words, added):
    words = list(words)
    for text in added:
        seq = normalize_words(text)
        n = len(seq)
        for k in range(len(words) - n, -1, -1):
            if n and words[k:k + n] == seq:
                del words[k:k + n]
                break
    return words


def _lost(before, after):
    """Elements of multiset `before` missing from `after`."""
    c = Counter(before)
    c.subtract(Counter(after))
    return [k for k, v in c.items() for _ in range(max(0, v))]


def diff(before: Inventory, after: Inventory, allowed: Allowed = None) -> IntegrityResult:
    allowed = allowed or Allowed()
    res = IntegrityResult()

    def loss(cat, f, detail):
        res.findings.append(Finding("LOSS", cat, f, detail))

    def change(cat, f, detail):
        res.findings.append(Finding("CHANGE", cat, f, detail))

    if after.error:
        loss("package", after.path, after.error)
        return res
    # files that disappeared
    for name in before.files:
        if name not in after.files:
            if name in allowed.removed_files:
                change("file", name, "junk file not packaged")
            else:
                loss("file", name, "file disappeared from the package")
    # documents
    for name, b in before.docs.items():
        a = after.docs.get(name)
        if a is None:
            continue                                      # reported as a file loss above
        edits = allowed.text_edits.get(name)
        b_words, b_headings = b.words, b.headings
        a_words = _remove_added(a.words, allowed.added_text.get(name, []))
        if edits:
            b_words = normalize_words(_apply_edits(" ".join(b.words), edits))
            b_headings = [" ".join(_apply_edits(h, edits).split()) for h in b.headings]
        if b_words != a_words and name not in allowed.text_changes:
            removed = len(_lost(b_words, a_words))
            loss("text", name, f"text changed ({removed} word(s) removed/altered): " +
                 " | ".join(_word_diff(b_words, a_words)))
        elif (edits or allowed.added_text.get(name)) and b.words != a.words:
            change("text", name, "declared edits: " + "; ".join(
                [f"'{o}' -> '{n}'" for o, n in (edits or [])[:4]] +
                [f"added '{t}'" for t in allowed.added_text.get(name, [])[:4]]))
        for label, bv, av in (("paragraphs", b.paragraphs, a.paragraphs), ("lists", b.lists, a.lists),
                              ("list items", b.list_items, a.list_items), ("tables", b.tables, a.tables),
                              ("figures", b.figures, a.figures), ("asides/notes", b.asides, a.asides)):
            if av < bv:
                loss(label, name, f"{bv} -> {av}")
        for h in _lost(b_headings, a.headings):
            loss("heading", name, f"heading '{h[:60]}' disappeared or changed")
        if len(a.images) < len(b.images):
            loss("image reference", name, f"{len(b.images)} -> {len(a.images)} image(s)")
        if sum(1 for _s, ok in a.images if ok) < sum(1 for _s, ok in b.images if ok):
            loss("image reference", name, "an image reference that resolved no longer resolves")
        for s0, s1 in zip(b.images, a.images):
            if s0[0] != s1[0]:
                change("image reference", name, f"src '{s0[0]}' -> '{s1[0]}'")
        if len(a.links) < len(b.links):
            loss("link", name, f"{len(b.links)} -> {len(a.links)} link(s)")
        if sum(1 for _h, ok in a.links if ok) < sum(1 for _h, ok in b.links if ok):
            loss("link", name, "a link that resolved no longer resolves")
        for h0, h1 in zip(b.links, a.links):
            if h0[0] != h1[0]:
                change("link", name, f"href '{h0[0]}' -> '{h1[0]}'")
        for pm in _lost(b.page_markers, a.page_markers):
            loss("page marker", name, f"page marker '{pm}' disappeared")
        for old in _lost(b.ids, a.ids):
            new = allowed.renamed_ids.get((name, old))
            if new and new in a.ids:
                change("id", name, f"duplicate id '{old}' occurrence renamed to '{new}'")
            elif old in a.ids:
                change("id", name, f"duplicate id '{old}' reduced")
            else:
                loss("id", name, f"id '{old}' disappeared")
        for css in _lost(b.css_links, a.css_links):
            loss("css link", name, f"stylesheet link '{css}' disappeared")
    # package
    for m in _lost(before.metadata, after.metadata):
        if m in allowed.metadata_changes:
            change("metadata", before.opf_path, f"'{m[:80]}': {allowed.metadata_changes[m]}")
        else:
            loss("metadata", before.opf_path, f"metadata entry '{m[:80]}' disappeared")
    for p in before.manifest:
        if p not in after.manifest and p in after.files:
            loss("manifest", before.opf_path, f"'{p}' no longer declared in the manifest")
    for p in set(before.spine):
        if p not in set(after.spine) and p in after.files:
            loss("spine", before.opf_path, f"'{p}' removed from the reading order")
    if len(after.nav_entries) < len(before.nav_entries):
        loss("navigation", "nav", f"{len(before.nav_entries)} -> {len(after.nav_entries)} entries")
    after_labels = [e[0] for e in after.nav_entries]
    for label in _lost([e[0] for e in before.nav_entries], after_labels):
        if allowed.nav_relabels.get(label) in after_labels:
            change("navigation", "nav", f"entry '{label[:60]}' relabelled '{allowed.nav_relabels[label]}'")
        else:
            loss("navigation", "nav", f"navigation entry '{label[:60]}' disappeared")
    if len(after.ncx_points) < len(before.ncx_points):
        loss("ncx", "ncx", f"{len(before.ncx_points)} -> {len(after.ncx_points)} navPoints")
    for name, sels in before.css.items():
        if name in after.css:
            for s in _lost(sels, after.css[name]):
                loss("css", name, f"CSS rule '{s[:60]}' disappeared")
    for name, info in before.binaries.items():
        if name in allowed.removed_files:
            continue
        a = after.binaries.get(name)
        if a is None:
            continue                                      # reported as a file loss
        if a["sha256"] != info["sha256"]:
            if name in allowed.binary_changes and a.get("dims") == info.get("dims") and a.get("pixels") and \
                    a.get("pixels") == info.get("pixels"):
                change("binary", name, allowed.binary_changes[name])
            elif name in allowed.resized_images and a.get("dims") == list(allowed.resized_images[name]):
                change("binary", name, f"resized {info.get('dims')} -> {a.get('dims')} (declared)")
            else:
                loss("binary", name, "asset bytes changed (images / fonts / media are never rewritten)")
    return res
