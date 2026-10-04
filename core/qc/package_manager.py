"""Central EPUB package manager for the PDF <-> XHTML QC engine.

Every structural operation (delete / merge / split / add / reorder / rename /
move content / page-marker and image corrections) goes through this class,
which keeps ALL package references consistent afterwards:

    OPF manifest + spine, navigation document (toc / page-list / landmarks),
    NCX, every internal link and anchor (href="file.xhtml#id"), IDs.

Works on an unpacked EPUB project folder (in place, the same folders the
Zoning / EPUB Structure modules produce) or on a .epub file (extracted into a
working folder; export_epub() writes a NEW .epub - the original is never
modified).

Every operation is one undo step: the text files of the package (XHTML,
OPF, NCX, sidecar) are snapshotted before the change and restored by undo().
IDs are never regenerated: a collision created by a merge gets a
deterministic replacement (<id>-<n>) and every reference to it is updated.
"""
import contextlib
import copy
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import etree

OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
SIDECAR = ".epubforge_qc.json"
XHTML_EXT = (".xhtml", ".html", ".htm")
TEXT_EXT = XHTML_EXT + (".opf", ".ncx", ".xml", ".css")
MAX_UNDO = 40


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


class PackageError(Exception):
    pass


@dataclass
class SplitInfo:
    path: str                      # root-relative POSIX path
    idref: str = ""
    linear: bool = True
    title: str = ""
    is_nav: bool = False
    locked: bool = False

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


@dataclass
class Impact:
    split: str
    pages: list = field(default_factory=list)
    words: int = 0
    images: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    inbound_links: list = field(default_factory=list)    # (from_file, href)
    outbound_links: int = 0
    nav_entries: int = 0

    def summary(self) -> str:
        return (f"{self.split}: {self.words} words, {len(self.pages)} page marker(s) "
                f"{self.pages[:8]}{'...' if len(self.pages) > 8 else ''}, {len(self.images)} image(s), "
                f"{len(self.ids)} id(s), {len(self.inbound_links)} link(s) from other files, "
                f"{self.nav_entries} navigation entr{'y' if self.nav_entries == 1 else 'ies'}")


@dataclass
class OperationResult:
    name: str
    changed_files: list = field(default_factory=list)
    created_files: list = field(default_factory=list)
    removed_files: list = field(default_factory=list)
    renamed_ids: dict = field(default_factory=dict)        # (file, old) -> new
    links_rewritten: int = 0
    needs_review: list = field(default_factory=list)        # human-readable items
    impact: Impact = None


class EpubPackageManager:
    # ------------------------------------------------------------ opening
    def __init__(self, root: str, source_epub: str = None):
        self.root = os.path.abspath(root)
        self.source_epub = source_epub
        self.opf_path = ""
        self.opf_tree = None
        self.nav_path = ""
        self.ncx_path = ""
        self._docs = {}            # rel -> (ElementTree, doctype, xml_decl)
        self._undo = []
        self._redo = []
        self._tx = None
        self._group_depth = 0
        self.sidecar = {"locks": [], "decisions": {}, "history": []}
        self.listeners = []         # callables(op_result) after every change
        self.reload()

    @classmethod
    def open(cls, path: str, work_dir: str = None) -> "EpubPackageManager":
        if os.path.isdir(path):
            return cls(path)
        if not (os.path.isfile(path) and zipfile.is_zipfile(path)):
            raise PackageError(f"Not an EPUB file or folder: {path}")
        base = work_dir or os.path.join(tempfile.gettempdir(), "epubforge_qc")
        stem = posixpath.splitext(os.path.basename(path))[0]
        digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
        root = os.path.join(base, f"{stem}_{digest}")
        if os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(root)
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                target = os.path.normpath(os.path.join(root, info.filename))
                if not target.startswith(root):
                    continue  # zip-slip guard
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(z.read(info.filename))
        return cls(root, source_epub=path)

    def reload(self):
        self._docs.clear()
        self.opf_path = self._find_opf()
        self.opf_tree = self._parse_xml(self.opf_path) if self.opf_path else None
        self.nav_path = ""
        self.ncx_path = ""
        if self.opf_tree is not None:
            for item in self._manifest_items():
                props = (item.get("properties") or "").split()
                href = self._opf_to_rel(item.get("href", ""))
                if "nav" in props:
                    self.nav_path = href
                if item.get("media-type") == "application/x-dtbncx+xml":
                    self.ncx_path = href
        else:
            from core.epub_structure import scanner
            scan = scanner.scan_project(self.root)
            self.nav_path = scan.nav_path
            self.ncx_path = scan.ncx_path
        sc = os.path.join(self.root, SIDECAR)
        if os.path.isfile(sc):
            try:
                with open(sc, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # replace (not merge): after an undo the restored sidecar is authoritative
                self.sidecar = {"locks": [], "decisions": {}, "history": [], **loaded}
            except (OSError, ValueError):
                pass
        else:
            self.sidecar = {"locks": [], "decisions": {}, "history": []}

    # ------------------------------------------------------------ paths
    def abspath(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def exists(self, rel: str) -> bool:
        return os.path.isfile(self.abspath(rel))

    def _find_opf(self) -> str:
        container = os.path.join(self.root, "META-INF", "container.xml")
        if os.path.isfile(container):
            try:
                tree = etree.parse(container)
                for rf in tree.iter(f"{{{CONTAINER_NS}}}rootfile"):
                    p = rf.get("full-path")
                    if p and self.exists(p):
                        return p
            except etree.XMLSyntaxError:
                pass
        for dirpath, _dirs, files in os.walk(self.root):
            for name in sorted(files):
                if name.lower().endswith(".opf"):
                    return posixpath.relpath(os.path.join(dirpath, name).replace(os.sep, "/"),
                                             self.root.replace(os.sep, "/"))
        return ""

    @property
    def opf_dir(self) -> str:
        return posixpath.dirname(self.opf_path)

    def _opf_to_rel(self, href: str) -> str:
        href = unquote((href or "").split("#", 1)[0])
        return posixpath.normpath(posixpath.join(self.opf_dir, href)) if href else ""

    def _rel_to_opf(self, rel: str) -> str:
        return posixpath.relpath(rel, self.opf_dir or ".")

    @staticmethod
    def resolve(from_rel: str, href: str):
        """(target_rel, fragment) for an href written inside from_rel, or
        (None, None) for an external / non-file reference."""
        if not href or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href) or href.startswith("//"):
            return None, None
        path, _, frag = href.partition("#")
        path = unquote(path)
        if not path:
            return from_rel, frag
        return posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), path)), frag

    @staticmethod
    def make_href(from_rel: str, target_rel: str, fragment: str = "") -> str:
        if target_rel == from_rel and fragment:
            return f"#{fragment}"
        rel = posixpath.relpath(target_rel, posixpath.dirname(from_rel) or ".")
        return f"{rel}#{fragment}" if fragment else rel

    # ------------------------------------------------------------ xml io
    def _parse_xml(self, rel: str):
        try:
            parser = etree.XMLParser(resolve_entities=False, remove_blank_text=False)
            return etree.parse(self.abspath(rel), parser)
        except (OSError, etree.XMLSyntaxError):
            return None

    def doc(self, rel: str):
        """Parsed ElementTree of an XHTML/XML file (cached until changed)."""
        if rel not in self._docs:
            tree = self._parse_xml(rel)
            if tree is None:
                raise PackageError(f"{rel} is missing or not well-formed XML")
            self._docs[rel] = tree
        return self._docs[rel]

    def write_doc(self, rel: str, tree=None):
        tree = tree if tree is not None else self._docs.get(rel)
        if tree is None:
            return
        self._touch(rel)
        doctype = tree.docinfo.doctype or None
        data = etree.tostring(tree, xml_declaration=True, encoding="utf-8", doctype=doctype)
        with open(self.abspath(rel), "wb") as f:
            f.write(data)
        self._docs[rel] = tree

    def write_opf(self):
        if self.opf_tree is not None:
            self.write_doc(self.opf_path, self.opf_tree)

    # ---------------------------------------------------------- manifest
    def _manifest_el(self):
        return None if self.opf_tree is None else self.opf_tree.getroot().find(f"{{{OPF_NS}}}manifest")

    def _spine_el(self):
        return None if self.opf_tree is None else self.opf_tree.getroot().find(f"{{{OPF_NS}}}spine")

    def _manifest_items(self):
        m = self._manifest_el()
        return [] if m is None else list(m.findall(f"{{{OPF_NS}}}item"))

    def manifest(self) -> dict:
        """rel path -> manifest item element."""
        return {self._opf_to_rel(it.get("href", "")): it for it in self._manifest_items()}

    def manifest_id_for(self, rel: str):
        it = self.manifest().get(rel)
        return it.get("id") if it is not None else None

    def _new_manifest_id(self, rel: str) -> str:
        used = {it.get("id") for it in self._manifest_items()}
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", posixpath.basename(rel))
        if not re.match(r"[A-Za-z_]", base):
            base = "x" + base
        cand, n = base, 2
        while cand in used:
            cand = f"{base}-{n}"
            n += 1
        return cand

    def add_manifest_item(self, rel: str, media_type: str, after_rel: str = None, properties: str = None):
        m = self._manifest_el()
        if m is None:
            return None
        if rel in self.manifest():
            return self.manifest()[rel]
        el = etree.Element(f"{{{OPF_NS}}}item", id=self._new_manifest_id(rel),
                           href=self._rel_to_opf(rel), **{"media-type": media_type})
        if properties:
            el.set("properties", properties)
        anchor = self.manifest().get(after_rel) if after_rel else None
        if anchor is not None:
            anchor.addnext(el)
            el.tail = anchor.tail
        else:
            m.append(el)
        return el

    def remove_manifest_item(self, rel: str):
        it = self.manifest().get(rel)
        if it is None:
            return
        idref = it.get("id")
        it.getparent().remove(it)
        sp = self._spine_el()
        if sp is not None:
            for ref in list(sp):
                if ref.get("idref") == idref:
                    sp.remove(ref)

    # ------------------------------------------------------------ splits
    def splits(self) -> list:
        """Content documents in reading (spine) order."""
        locks = set(self.sidecar.get("locks", []))
        out = []
        if self.opf_tree is not None:
            by_id = {it.get("id"): it for it in self._manifest_items()}
            sp = self._spine_el()
            for ref in (list(sp) if sp is not None else []):
                it = by_id.get(ref.get("idref"))
                if it is None:
                    continue
                rel = self._opf_to_rel(it.get("href", ""))
                if _ext(rel) not in XHTML_EXT:
                    continue
                out.append(SplitInfo(path=rel, idref=ref.get("idref"), linear=ref.get("linear", "yes") != "no",
                                     is_nav=(rel == self.nav_path), locked=rel in locks))
        else:
            from core.epub_structure import scanner
            for rel in scanner.scan_project(self.root).xhtml_files:
                out.append(SplitInfo(path=rel, is_nav=(rel == self.nav_path), locked=rel in locks))
        for s in out:
            s.title = self.split_title(s.path)
        return out

    def split_paths(self) -> list:
        return [s.path for s in self.splits()]

    def content_documents(self) -> list:
        """Every XHTML document of the package (spine + non-spine, e.g. nav)."""
        docs = list(self.split_paths())
        if self.opf_tree is not None:
            for rel, it in self.manifest().items():
                if it.get("media-type") == "application/xhtml+xml" and rel not in docs:
                    docs.append(rel)
        elif self.nav_path and self.nav_path not in docs:
            docs.append(self.nav_path)
        return [d for d in docs if self.exists(d)]

    def split_title(self, rel: str) -> str:
        try:
            root = self.doc(rel).getroot()
        except PackageError:
            return posixpath.basename(rel)
        for el in root.iter():
            if _local(el.tag) in ("h1", "h2", "h3"):
                text = " ".join("".join(el.itertext()).split())
                if text:
                    return text[:80]
        for el in root.iter():
            if _local(el.tag) == "title":
                text = " ".join("".join(el.itertext()).split())
                if text:
                    return text[:80]
        return posixpath.basename(rel)

    def body(self, rel: str):
        root = self.doc(rel).getroot()
        for el in root.iter():
            if _local(el.tag) == "body":
                return el
        raise PackageError(f"{rel} has no <body>")

    def is_locked(self, rel: str) -> bool:
        return rel in set(self.sidecar.get("locks", []))

    def _check_unlocked(self, *rels):
        for r in rels:
            if self.is_locked(r):
                raise PackageError(f"{posixpath.basename(r)} is locked - unlock it first")
            if r == self.nav_path:
                raise PackageError("The navigation document cannot be changed by split operations")

    # ------------------------------------------------------------ ids/links
    def ids_in(self, rel: str) -> dict:
        out = {}
        for el in self.doc(rel).getroot().iter():
            if isinstance(el.tag, str) and el.get("id"):
                out[el.get("id")] = el
        return out

    def all_ids(self) -> dict:
        return {rel: set(self.ids_in(rel)) for rel in self.content_documents()}

    def _href_elements(self, rel: str):
        """(element, attribute) pairs carrying a document reference."""
        for el in self.doc(rel).getroot().iter():
            if not isinstance(el.tag, str):
                continue
            name = _local(el.tag)
            if el.get("href") is not None and name in ("a", "area", "link"):
                if name == "link" and "stylesheet" in (el.get("rel") or ""):
                    continue
                yield el, "href"
            elif name == "content" and el.get("src") is not None:   # NCX
                yield el, "src"

    def link_index(self) -> list:
        """[(from_file, element, attr, target_rel, fragment)] for every
        internal document reference in the package (nav + NCX included)."""
        out = []
        docs = list(self.content_documents())
        if self.ncx_path and self.exists(self.ncx_path):
            docs.append(self.ncx_path)
        for rel in docs:
            try:
                for el, attr in self._href_elements(rel):
                    target, frag = self.resolve(rel, el.get(attr))
                    if target is None:
                        continue
                    out.append((rel, el, attr, target, frag))
            except PackageError:
                continue
        return out

    def broken_links(self) -> list:
        """[(from_file, href, reason, suggestion_href_or_None)]"""
        ids = self.all_ids()
        id_owner = {}
        for rel, s in ids.items():
            for i in s:
                id_owner.setdefault(i, []).append(rel)
        out = []
        for rel, el, attr, target, frag in self.link_index():
            href = el.get(attr)
            if _ext(target) not in XHTML_EXT:
                if not self.exists(target):
                    out.append((rel, href, "target file does not exist", None))
                continue
            if not self.exists(target):
                owners = id_owner.get(frag, []) if frag else []
                sugg = self.make_href(rel, owners[0], frag) if len(owners) == 1 else None
                out.append((rel, href, "target file does not exist", sugg))
                continue
            if frag and frag not in ids.get(target, set()):
                owners = id_owner.get(frag, [])
                sugg = self.make_href(rel, owners[0], frag) if len(owners) == 1 else None
                out.append((rel, href, f"#{frag} does not exist in {posixpath.basename(target)}", sugg))
        return out

    def _rewrite_links(self, fn, result: OperationResult, location_moves: dict = None):
        """fn(target_rel, fragment) -> (new_target_rel, new_fragment) | None.
        location_moves: {old_containing_rel: new_containing_rel} - links
        inside moved content are written relative to their new location."""
        location_moves = location_moves or {}
        for rel, el, attr, target, frag in self.link_index():
            new = fn(target, frag)
            container = location_moves.get(rel, rel)
            if new is None and container == rel:
                continue
            new_target, new_frag = new if new is not None else (target, frag)
            new_href = self.make_href(container, new_target, new_frag)
            if new_href != el.get(attr):
                el.set(attr, new_href)
                result.links_rewritten += 1
                self._mark(rel, result)

    def _mark(self, rel, result):
        if rel not in result.changed_files:
            result.changed_files.append(rel)

    def _flush(self, result: OperationResult):
        for rel in result.changed_files:
            if rel in result.removed_files:
                continue
            if rel == self.opf_path:
                self.write_opf()
            elif rel in self._docs:
                self.write_doc(rel)

    # ------------------------------------------------------------ undo
    def _text_files(self):
        out = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".epub_structure_backup")]
            for name in files:
                if name.lower().endswith(TEXT_EXT) or name == SIDECAR:
                    rel = posixpath.relpath(os.path.join(dirpath, name).replace(os.sep, "/"),
                                            self.root.replace(os.sep, "/"))
                    out.append(rel)
        return out

    def _snapshot(self) -> dict:
        snap = {}
        for rel in self._text_files():
            try:
                with open(self.abspath(rel), "rb") as f:
                    snap[rel] = f.read()
            except OSError:
                pass
        return snap

    def _restore(self, snap: dict):
        current = set(self._text_files())
        for rel in current - set(snap):
            try:
                os.remove(self.abspath(rel))
            except OSError:
                pass
        for rel, data in snap.items():
            path = self.abspath(rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        self.reload()

    def _touch(self, rel):
        pass  # snapshot is taken up-front per transaction

    def _begin(self, name: str):
        if self._tx is not None:
            raise PackageError("nested package operation")
        self._tx = (name, self._snapshot())

    def _commit(self, result: OperationResult):
        name, snap = self._tx
        self._tx = None
        self._flush(result)
        self._save_sidecar(history_entry=name)
        self._undo.append((name, snap))
        if not self._group_depth:
            del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self.reload()
        for cb in list(self.listeners):
            try:
                cb(result)
            except Exception:
                pass
        return result

    def _abort(self):
        if self._tx is None:
            return
        _name, snap = self._tx
        self._tx = None
        self._restore(snap)

    def run(self, name: str, fn, *args, **kwargs) -> OperationResult:
        """Runs one structural operation as a single undoable transaction."""
        self._begin(name)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._abort()
            raise
        return self._commit(result)

    @contextlib.contextmanager
    def group(self, name: str):
        """Every operation run inside the block becomes ONE undo step
        (e.g. a batch of corrections that mixes content edits and package
        fixes)."""
        start = len(self._undo)
        self._group_depth += 1
        try:
            yield
        finally:
            self._group_depth -= 1
            added = self._undo[start:]
            if len(added) > 1:
                del self._undo[start:]
                self._undo.append((name, added[0][1]))
            if not self._group_depth:
                del self._undo[:-MAX_UNDO]

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo(self) -> str:
        if not self._undo:
            return ""
        name, snap = self._undo.pop()
        self._redo.append((name, self._snapshot()))
        self._restore(snap)
        self._notify(OperationResult(name=f"Undo {name}"))
        return name

    def redo(self) -> str:
        if not self._redo:
            return ""
        name, snap = self._redo.pop()
        self._undo.append((name, self._snapshot()))
        self._restore(snap)
        self._notify(OperationResult(name=f"Redo {name}"))
        return name

    def _notify(self, result):
        for cb in list(self.listeners):
            try:
                cb(result)
            except Exception:
                pass

    def _save_sidecar(self, history_entry: str = None):
        if history_entry:
            self.sidecar.setdefault("history", []).append(history_entry)
            self.sidecar["history"] = self.sidecar["history"][-200:]
        with open(os.path.join(self.root, SIDECAR), "w", encoding="utf-8") as f:
            json.dump(self.sidecar, f, indent=1, ensure_ascii=False)

    # ----------------------------------------------------------- impact
    def impact(self, rel: str) -> Impact:
        from core.epub_structure.pagebreak_analyzer import extract_pagebreaks
        imp = Impact(split=rel)
        root = self.doc(rel).getroot()
        imp.pages = [pb.label for pb in extract_pagebreaks(root)]
        body = self.body(rel)
        imp.words = len(" ".join(body.itertext()).split())
        for el in root.iter():
            if _local(el.tag) in ("img", "image"):
                src = el.get("src") or el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
                if src:
                    imp.images.append(src)
        imp.ids = sorted(self.ids_in(rel))
        for frm, el, attr, target, frag in self.link_index():
            if target == rel and frm != rel:
                if frm == self.nav_path or frm == self.ncx_path:
                    imp.nav_entries += 1
                else:
                    imp.inbound_links.append((frm, el.get(attr)))
            if frm == rel and target != rel:
                imp.outbound_links += 1
        return imp

    # -------------------------------------------------------- operations
    def delete_split(self, rel: str) -> OperationResult:
        self._check_unlocked(rel)
        return self.run(f"Delete {posixpath.basename(rel)}", self._delete_split, rel)

    def _delete_split(self, rel):
        res = OperationResult(name=f"Delete {rel}", impact=self.impact(rel))
        # nav / NCX entries pointing at the deleted file are removed
        for nav_rel in [p for p in (self.nav_path, self.ncx_path) if p and self.exists(p)]:
            removed = self._remove_nav_entries(nav_rel, lambda t, f: t == rel)
            if removed:
                self._mark(nav_rel, res)
        # inbound links can no longer resolve: unwrap them (text kept) and flag
        for frm, el, attr, target, frag in self.link_index():
            if target == rel and frm != rel and frm not in (self.nav_path, self.ncx_path):
                text = "".join(el.itertext()).strip()
                self._unwrap(el)
                res.needs_review.append(f"{posixpath.basename(frm)}: link '{text[:40]}' pointed into the deleted "
                                        f"{posixpath.basename(rel)} and was removed (text kept)")
                self._mark(frm, res)
        if self.opf_tree is not None:
            self.remove_manifest_item(rel)
            self._mark(self.opf_path, res)
        self._flush(res)
        os.remove(self.abspath(rel))
        self._docs.pop(rel, None)
        res.removed_files.append(rel)
        self._unlock(rel)
        return res

    @staticmethod
    def _unwrap(el):
        """Replaces an element by its own content (text, children, tail)."""
        parent = el.getparent()
        if parent is None:
            return
        idx = parent.index(el)
        prev = el.getprevious()

        def add_text(text):
            if not text:
                return
            if prev is not None:
                prev.tail = (prev.tail or "") + text
            else:
                parent.text = (parent.text or "") + text
        add_text(el.text)
        kids = list(el)
        for i, k in enumerate(kids):
            parent.insert(idx + i, k)
        tail = el.tail or ""
        parent.remove(el)
        if kids:
            kids[-1].tail = (kids[-1].tail or "") + tail
        else:
            add_text(tail)

    def _remove_nav_entries(self, nav_rel, predicate) -> int:
        removed = 0
        tree = self.doc(nav_rel)
        for el, attr in list(self._href_elements(nav_rel)):
            target, frag = self.resolve(nav_rel, el.get(attr))
            if target is None or not predicate(target, frag):
                continue
            # the list item / navPoint / pageTarget that owns the link
            owner = el
            while owner is not None and _local(owner.tag) not in ("li", "navPoint", "pageTarget", "navTarget"):
                owner = owner.getparent()
            if owner is None:
                continue
            nested = [c for c in owner if _local(c.tag) in ("ol", "navPoint")]
            parent = owner.getparent()
            if nested and parent is not None:
                # keep children entries (e.g. sections of a chapter that moved)
                pos = parent.index(owner)
                for n in nested:
                    for sub in list(n) if _local(n.tag) == "ol" else [n]:
                        parent.insert(pos, sub)
                        pos += 1
            if parent is not None:
                parent.remove(owner)
                removed += 1
        if removed and nav_rel == self.ncx_path:
            self._renumber_ncx(tree)
        return removed

    def _renumber_ncx(self, tree):
        n = 0
        for el in tree.getroot().iter():
            if _local(el.tag) in ("navPoint", "pageTarget") and el.get("playOrder") is not None:
                n += 1
                el.set("playOrder", str(n))

    def merge_with_previous(self, rel: str) -> OperationResult:
        order = self.split_paths()
        i = order.index(rel)
        if i == 0:
            raise PackageError("The first split has no previous split")
        return self.merge(order[i - 1], rel)

    def merge_with_next(self, rel: str) -> OperationResult:
        order = self.split_paths()
        i = order.index(rel)
        if i + 1 >= len(order):
            raise PackageError("The last split has no next split")
        return self.merge(rel, order[i + 1])

    def merge(self, keep: str, absorb: str) -> OperationResult:
        self._check_unlocked(keep, absorb)
        return self.run(f"Merge {posixpath.basename(absorb)} into {posixpath.basename(keep)}",
                        self._merge, keep, absorb)

    def _unique_id(self, base: str, taken: set) -> str:
        if base not in taken:
            return base
        n = 2
        cand = f"{base}-{n}"
        while cand in taken:
            n += 1
            cand = f"{base}-{n}"
        return cand

    def _merge(self, keep, absorb):
        res = OperationResult(name=f"Merge {absorb} -> {keep}")
        keep_body, absorb_body = self.body(keep), self.body(absorb)
        all_ids = set()
        for s in self.all_ids().values():
            all_ids |= s
        keep_ids = set(self.ids_in(keep))
        renames = {}
        for old, el in self.ids_in(absorb).items():
            if old in keep_ids:
                new = self._unique_id(old, all_ids | keep_ids)
                el.set("id", new)
                all_ids.add(new)
                renames[old] = new
                res.renamed_ids[(absorb, old)] = new
        # anchor for links to the absorbed file without a fragment (only
        # created when such a link exists - ids are never added needlessly)
        first = next((c for c in absorb_body if isinstance(c.tag, str)), None)
        entry_id = None
        bare_links = any(t == absorb and not f and frm != absorb for frm, _el, _a, t, f in self.link_index())
        if first is not None and bare_links:
            entry_id = first.get("id")
            if not entry_id:
                entry_id = self._unique_id(re.sub(r"[^A-Za-z0-9_-]", "_",
                                                  posixpath.splitext(posixpath.basename(absorb))[0]) + "-start",
                                           all_ids | keep_ids)
                first.set("id", entry_id)
        # stylesheets the absorbed document used that keep lacks
        self._merge_head_links(keep, absorb, res)
        # links (everywhere) to the absorbed file now point into keep
        def fn(target, frag):
            if target != absorb:
                return None
            if not frag:
                return keep, entry_id or ""
            return keep, renames.get(frag, frag)
        self._rewrite_links(fn, res, location_moves={absorb: keep})
        # move the content (keep its text exactly, including the body text)
        if absorb_body.text and absorb_body.text.strip():
            last = keep_body[-1] if len(keep_body) else None
            if last is not None:
                last.tail = (last.tail or "") + absorb_body.text
            else:
                keep_body.text = (keep_body.text or "") + absorb_body.text
        for child in list(absorb_body):
            keep_body.append(child)
        self._mark(keep, res)
        if self.opf_tree is not None:
            self.remove_manifest_item(absorb)
            self._mark(self.opf_path, res)
        self._flush(res)
        os.remove(self.abspath(absorb))
        self._docs.pop(absorb, None)
        res.removed_files.append(absorb)
        self._unlock(absorb)
        return res

    def _merge_head_links(self, keep, absorb, res):
        def head(rel):
            for el in self.doc(rel).getroot().iter():
                if _local(el.tag) == "head":
                    return el
            return None
        kh, ah = head(keep), head(absorb)
        if kh is None or ah is None:
            return
        have = set()
        for el in kh:
            if _local(el.tag) == "link" and el.get("href"):
                have.add(self.resolve(keep, el.get("href"))[0])
        for el in ah:
            if _local(el.tag) == "link" and el.get("href"):
                target = self.resolve(absorb, el.get("href"))[0]
                if target and target not in have:
                    new = copy.deepcopy(el)
                    new.set("href", self.make_href(keep, target))
                    kh.append(new)
                    have.add(target)
                    self._mark(keep, res)

    def _new_split_name(self, near_rel: str, suffix: str = "split") -> str:
        d = posixpath.dirname(near_rel)
        stem, ext = posixpath.splitext(posixpath.basename(near_rel))
        stem = re.sub(rf"-{suffix}\d+$", "", stem)
        n = 1
        while True:
            cand = posixpath.join(d, f"{stem}-{suffix}{n}{ext}") if d else f"{stem}-{suffix}{n}{ext}"
            if not self.exists(cand):
                return cand
            n += 1

    def _new_document_like(self, template_rel: str, title: str = None):
        tree = copy.deepcopy(self.doc(template_rel))
        root = tree.getroot()
        for el in root.iter():
            if _local(el.tag) == "body":
                for c in list(el):
                    el.remove(c)
                el.text = "\n"
                for k in list(el.attrib):
                    if k == "id":
                        del el.attrib[k]
            if _local(el.tag) == "title" and title is not None:
                el.text = title
        return tree

    def split_document(self, rel: str, at_element) -> OperationResult:
        """Splits `rel` so that `at_element` (an element inside its body, or
        an id) and everything after it moves to a new document inserted
        directly after `rel` in the spine."""
        self._check_unlocked(rel)
        return self.run(f"Split {posixpath.basename(rel)}", self._split_document, rel, at_element)

    def _split_document(self, rel, at_element):
        res = OperationResult(name=f"Split {rel}")
        body = self.body(rel)
        if isinstance(at_element, str):
            at_element = self.ids_in(rel).get(at_element)
        if at_element is None:
            raise PackageError("Split point not found")
        # climb to the ancestor directly under body to know the wrapper chain
        chain = []
        node = at_element
        while node is not None and node is not body:
            chain.append(node)
            node = node.getparent()
        if node is not body:
            raise PackageError("Split point is not inside the document body")
        chain.reverse()      # [top-level ancestor, ..., at_element]
        if len(chain) == 1 and chain[0].getprevious() is None and not (body.text or "").strip():
            raise PackageError("Split point is at the very start of the document - nothing to split off")
        new_rel = self._new_split_name(rel)
        new_tree = self._new_document_like(rel)
        new_body = next(el for el in new_tree.getroot().iter() if _local(el.tag) == "body")

        def carve(level, dest):
            """at_element and everything after it - at every level of its
            wrapper chain - moves to dest; wrappers are re-created (without
            their id, which stays unique in the original document)."""
            node = chain[level]
            followers = list(node.itersiblings())
            if level == len(chain) - 1:
                for m in [node] + followers:
                    dest.append(m)
                return
            wrapper = etree.SubElement(dest, node.tag, {k: v for k, v in node.attrib.items() if k != "id"})
            carve(level + 1, wrapper)
            for m in followers:
                dest.append(m)
        carve(0, new_body)
        moved_ids = set()
        for el in new_body.iter():
            if isinstance(el.tag, str) and el.get("id"):
                moved_ids.add(el.get("id"))
        with open(self.abspath(new_rel), "wb") as f:
            f.write(b"")
        self._docs[new_rel] = new_tree
        res.created_files.append(new_rel)
        self._mark(rel, res)
        self._mark(new_rel, res)
        if self.opf_tree is not None:
            item = self.add_manifest_item(new_rel, "application/xhtml+xml", after_rel=rel)
            old_id = self.manifest_id_for(rel)
            sp = self._spine_el()
            for ref in sp:
                if ref.get("idref") == old_id:
                    new_ref = etree.Element(f"{{{OPF_NS}}}itemref", idref=item.get("id"))
                    ref.addnext(new_ref)
                    new_ref.tail = ref.tail
                    break
            self._mark(self.opf_path, res)

        def fn(target, frag):
            if target == rel and frag in moved_ids:
                return new_rel, frag
            return None
        # links inside the moved content were written relative to rel; same
        # directory, so only fragment-only links into the OLD file need care
        self._rewrite_links(fn, res)
        for el, attr in list(self._href_elements(new_rel)):
            href = el.get(attr)
            if href.startswith("#") and href[1:] not in moved_ids:
                el.set(attr, self.make_href(new_rel, rel, href[1:]))
                res.links_rewritten += 1
        for el, attr in list(self._href_elements(rel)):
            href = el.get(attr)
            if href.startswith("#") and href[1:] in moved_ids:
                el.set(attr, self.make_href(rel, new_rel, href[1:]))
                res.links_rewritten += 1
        self._add_nav_entry_for_new_split(new_rel, res)
        return res

    def _add_nav_entry_for_new_split(self, new_rel, res):
        """New split gets a TOC entry when it starts with a heading; page-list
        entries are already rewritten through the link pass."""
        heading = None
        for el in self.body(new_rel).iter():
            if _local(el.tag) in ("h1", "h2", "h3", "h4", "h5", "h6"):
                heading = el
                break
        if heading is None or not self.nav_path or not self.exists(self.nav_path):
            if heading is None:
                res.needs_review.append(f"{posixpath.basename(new_rel)} has no heading - no TOC entry added")
            return
        text = " ".join("".join(heading.itertext()).split())
        if not heading.get("id"):
            taken = set()
            for s in self.all_ids().values():
                taken |= s
            heading.set("id", self._unique_id("toc", taken))
        nav_root = self.doc(self.nav_path).getroot()
        toc_ol = None
        for nav in nav_root.iter():
            if _local(nav.tag) == "nav" and "toc" in (nav.get(f"{{{EPUB_NS}}}type") or ""):
                toc_ol = next((c for c in nav if _local(c.tag) == "ol"), None)
                break
        if toc_ol is None:
            return
        ns = toc_ol.tag.rsplit("}", 1)[0] + "}" if "}" in toc_ol.tag else ""
        li = etree.Element(f"{ns}li")
        a = etree.SubElement(li, f"{ns}a", href=self.make_href(self.nav_path, new_rel, heading.get("id")))
        a.text = text
        # insert after the last entry that points at a document before new_rel in the spine
        order = self.split_paths()
        pos = order.index(new_rel) if new_rel in order else len(order)
        insert_at = len(toc_ol)
        for i, item in enumerate(toc_ol):
            link = next((x for x in item.iter() if _local(x.tag) == "a"), None)
            if link is None:
                continue
            t, _ = self.resolve(self.nav_path, link.get("href"))
            if t in order and order.index(t) >= pos:
                insert_at = i
                break
        toc_ol.insert(insert_at, li)
        self._mark(self.nav_path, res)

    def split_before_text_position(self, rel: str, element) -> OperationResult:
        return self.split_document(rel, element)

    def add_split(self, after_rel: str, title: str = "New Section") -> OperationResult:
        return self.run(f"Add split after {posixpath.basename(after_rel)}", self._add_split, after_rel, title)

    def _add_split(self, after_rel, title):
        res = OperationResult(name=f"Add split after {after_rel}")
        new_rel = self._new_split_name(after_rel, suffix="new")
        tree = self._new_document_like(after_rel, title=title)
        body = next(el for el in tree.getroot().iter() if _local(el.tag) == "body")
        ns = body.tag.rsplit("}", 1)[0] + "}" if "}" in body.tag else ""
        h = etree.SubElement(body, f"{ns}h1")
        h.text = title
        with open(self.abspath(new_rel), "wb") as f:
            f.write(b"")
        self._docs[new_rel] = tree
        res.created_files.append(new_rel)
        self._mark(new_rel, res)
        if self.opf_tree is not None:
            item = self.add_manifest_item(new_rel, "application/xhtml+xml", after_rel=after_rel)
            old_id = self.manifest_id_for(after_rel)
            for ref in self._spine_el():
                if ref.get("idref") == old_id:
                    nr = etree.Element(f"{{{OPF_NS}}}itemref", idref=item.get("id"))
                    ref.addnext(nr)
                    nr.tail = ref.tail
                    break
            self._mark(self.opf_path, res)
        return res

    def reorder(self, new_order: list) -> OperationResult:
        return self.run("Reorder splits", self._reorder, list(new_order))

    def _reorder(self, new_order):
        res = OperationResult(name="Reorder splits")
        current = self.split_paths()
        if sorted(current) != sorted(new_order):
            raise PackageError("Reorder must contain exactly the current splits")
        if self.opf_tree is None:
            res.needs_review.append("No OPF in this project - spine order cannot be stored")
            return res
        sp = self._spine_el()
        man = self.manifest()
        refs = {ref.get("idref"): ref for ref in sp}
        xhtml_refs = [refs[man[r].get("id")] for r in new_order if man.get(r) is not None]
        slots = [i for i, ref in enumerate(list(sp)) if ref in xhtml_refs]
        children = list(sp)
        for slot, ref in zip(slots, xhtml_refs):
            children[slot] = ref
        for c in list(sp):
            sp.remove(c)
        for c in children:
            sp.append(c)
        self._mark(self.opf_path, res)
        # navigation follows reading order
        if self.nav_path and self.exists(self.nav_path):
            if self._reorder_nav(new_order):
                self._mark(self.nav_path, res)
        if self.ncx_path and self.exists(self.ncx_path):
            self._reorder_ncx(new_order)
            self._mark(self.ncx_path, res)
        return res

    def _entry_target_index(self, item, from_rel, order):
        for x in item.iter():
            if _local(x.tag) in ("a", "content"):
                t, _ = self.resolve(from_rel, x.get("href") or x.get("src"))
                if t in order:
                    return order.index(t)
        return 10 ** 6

    def _reorder_nav(self, order) -> bool:
        changed = False
        root = self.doc(self.nav_path).getroot()
        for ol in [e for e in root.iter() if _local(e.tag) == "ol"]:
            items = [c for c in ol if _local(c.tag) == "li"]
            keyed = sorted(items, key=lambda it: self._entry_target_index(it, self.nav_path, order))
            if keyed != items:
                for it in items:
                    ol.remove(it)
                for it in keyed:
                    ol.append(it)
                changed = True
        return changed

    def _reorder_ncx(self, order):
        tree = self.doc(self.ncx_path)
        for parent in [e for e in tree.getroot().iter() if _local(e.tag) in ("navMap", "navPoint", "pageList")]:
            items = [c for c in parent if _local(c.tag) in ("navPoint", "pageTarget")]
            keyed = sorted(items, key=lambda it: self._entry_target_index(it, self.ncx_path, order))
            if keyed != items:
                first = parent.index(items[0])
                for it in items:
                    parent.remove(it)
                for k, it in enumerate(keyed):
                    parent.insert(first + k, it)
        self._renumber_ncx(tree)

    def rename_split(self, rel: str, new_name: str) -> OperationResult:
        self._check_unlocked(rel)
        if not new_name.lower().endswith(XHTML_EXT):
            new_name += _ext(rel)
        if "/" in new_name or "\\" in new_name:
            raise PackageError("Give a file name, not a path")
        new_rel = posixpath.join(posixpath.dirname(rel), new_name) if posixpath.dirname(rel) else new_name
        if self.exists(new_rel):
            raise PackageError(f"{new_name} already exists")
        return self.run(f"Rename {posixpath.basename(rel)}", self._rename, rel, new_rel)

    def _rename(self, rel, new_rel):
        res = OperationResult(name=f"Rename {rel} -> {new_rel}")
        self.doc(rel)
        self._rewrite_links(lambda t, f: (new_rel, f) if t == rel else None, res, location_moves={rel: new_rel})
        self._flush(res)
        os.rename(self.abspath(rel), self.abspath(new_rel))
        self._docs[new_rel] = self._docs.pop(rel)
        if self.opf_tree is not None:
            it = self.manifest().get(rel)
            if it is not None:
                it.set("href", self._rel_to_opf(new_rel))
                self._mark(self.opf_path, res)
        res.changed_files = [new_rel if c == rel else c for c in res.changed_files]
        if new_rel not in res.changed_files:
            res.changed_files.append(new_rel)
        res.removed_files.append(rel)
        res.created_files.append(new_rel)
        locks = self.sidecar.get("locks", [])
        if rel in locks:
            locks[locks.index(rel)] = new_rel
        return res

    def move_content(self, src: str, elements: list, dst: str, position: str = "end") -> OperationResult:
        """Moves body-level elements of `src` to the start/end of `dst`."""
        self._check_unlocked(src, dst)
        return self.run(f"Move content {posixpath.basename(src)} -> {posixpath.basename(dst)}",
                        self._move_content, src, elements, dst, position)

    def _move_content(self, src, elements, dst, position):
        res = OperationResult(name=f"Move content {src} -> {dst}")
        dst_body = self.body(dst)
        dst_ids = set(self.ids_in(dst))
        all_ids = set()
        for s in self.all_ids().values():
            all_ids |= s
        moved, renames = set(), {}
        for el in elements:
            for d in el.iter():
                if isinstance(d.tag, str) and d.get("id"):
                    old = d.get("id")
                    if old in dst_ids:
                        new = self._unique_id(old, all_ids | dst_ids)
                        d.set("id", new)
                        renames[old] = new
                        res.renamed_ids[(src, old)] = new
                    moved.add(old)
        self._rewrite_links(lambda t, f: (dst, renames.get(f, f)) if t == src and f in moved else None, res)
        for i, el in enumerate(elements):
            if position == "start":
                dst_body.insert(i, el)
            else:
                dst_body.append(el)
        self._mark(src, res)
        self._mark(dst, res)
        return res

    def lock(self, rel: str, locked: bool = True):
        locks = self.sidecar.setdefault("locks", [])
        if locked and rel not in locks:
            locks.append(rel)
        elif not locked and rel in locks:
            locks.remove(rel)
        self._save_sidecar()

    def _unlock(self, rel):
        locks = self.sidecar.get("locks", [])
        if rel in locks:
            locks.remove(rel)

    # ------------------------------------------------------ generic edit
    def edit(self, name: str, fn, files: list) -> OperationResult:
        """Runs fn(manager) -> needs_review list as one undoable operation
        that modifies the given files (used by corrections)."""
        for f in files:
            if f != self.opf_path and f not in (self.nav_path, self.ncx_path):
                self._check_unlocked(f)

        def _do():
            res = OperationResult(name=name)
            out = fn(self) or []
            res.needs_review.extend(out)
            for f in files:
                self._mark(f, res)
            return res
        return self.run(name, _do)

    # ---------------------------------------------------------- package
    def package_problems(self) -> list:
        """[(kind, message, fix_callable_or_None)] - manifest/spine/file
        consistency (missing files, unlisted resources, duplicate entries)."""
        out = []
        if self.opf_tree is None:
            out.append(("no_opf", "Project has no OPF package document", None))
            return out
        seen = {}
        for it in self._manifest_items():
            rel = self._opf_to_rel(it.get("href", ""))
            if rel in seen:
                out.append(("duplicate_manifest", f"{rel} is listed twice in the manifest (ids {seen[rel]}, "
                            f"{it.get('id')})", ("remove_manifest_duplicate", it.get("id"))))
            else:
                seen[rel] = it.get("id")
            if not self.exists(rel):
                out.append(("missing_file", f"manifest item {it.get('id')} -> {rel} does not exist", None))
        ids = {it.get("id") for it in self._manifest_items()}
        for ref in (self._spine_el() if self._spine_el() is not None else []):
            if ref.get("idref") not in ids:
                out.append(("bad_spine", f"spine itemref {ref.get('idref')} has no manifest item",
                            ("remove_spine_ref", ref.get("idref"))))
        # referenced resources that are not in the manifest
        listed = set(seen)
        for rel in self.content_documents():
            try:
                root = self.doc(rel).getroot()
            except PackageError as e:
                out.append(("xml", str(e), None))
                continue
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                src = el.get("src") if _local(el.tag) in ("img", "audio", "video", "source", "script") else (
                    el.get("href") if _local(el.tag) == "link" else None)
                if not src:
                    continue
                target, _ = self.resolve(rel, src)
                if target and target not in listed:
                    if self.exists(target):
                        out.append(("unlisted_resource", f"{target} is used by {posixpath.basename(rel)} but not "
                                    "in the manifest", ("add_manifest", target)))
                    else:
                        out.append(("missing_resource", f"{posixpath.basename(rel)} references missing {target}",
                                    None))
        return out

    def fix_package_problem(self, fix) -> OperationResult:
        kind, arg = fix

        def _do(mgr):
            if kind == "remove_manifest_duplicate":
                for it in mgr._manifest_items():
                    if it.get("id") == arg:
                        it.getparent().remove(it)
                        break
            elif kind == "remove_spine_ref":
                sp = mgr._spine_el()
                for ref in list(sp):
                    if ref.get("idref") == arg:
                        sp.remove(ref)
            elif kind == "add_manifest":
                mgr.add_manifest_item(arg, guess_media_type(arg))
            return []
        return self.edit(f"Package fix: {kind}", _do, [self.opf_path])

    # ----------------------------------------------------------- export
    def export_epub(self, out_path: str):
        """Writes a valid OCF container (mimetype first, stored)."""
        with zipfile.ZipFile(out_path, "w") as z:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, b"application/epub+zip")
            for dirpath, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in sorted(files):
                    if name == "mimetype" or name.startswith(".") or name.lower().endswith(".epub"):
                        continue
                    full = os.path.join(dirpath, name)
                    arc = posixpath.relpath(full.replace(os.sep, "/"), self.root.replace(os.sep, "/"))
                    z.write(full, arc, compress_type=zipfile.ZIP_DEFLATED)
        return out_path


def guess_media_type(rel: str) -> str:
    return {
        ".xhtml": "application/xhtml+xml", ".html": "application/xhtml+xml", ".htm": "application/xhtml+xml",
        ".css": "text/css", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp", ".ncx": "application/x-dtbncx+xml",
        ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
        ".js": "application/javascript", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
    }.get(_ext(rel), "application/octet-stream")
