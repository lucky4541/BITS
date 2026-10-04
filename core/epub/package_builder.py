"""Mutable, in-memory EPUB package model + builder (spec: "EPUBForge -
PHASE 3 - Universal Auto-Fix Engine" workflow steps "PACKAGE MODEL",
"PACKAGE BUILDER", "TEMP EPUB"). Distinct from core.epub.package_reader.
EpubPackage, which is deliberately READ-ONLY and display-oriented (that
module's own docstring: "NOT the full PackageModel... for validation
display, not for rebuilding a package") - MutablePackage is exactly that
missing rebuilding half, used ONLY by the repair engine, never by the
Validation window's own read-only inspection path (spec 1: "ONE
implementation only" is preserved because MutablePackage never re-derives
manifest/spine/metadata itself - callers pair it with a normal
core.epub.package_reader.EpubPackage, read fresh via read_package(), for
that structural information).

Never mutates the ORIGINAL .epub file on disk (Golden Rule: "NEVER modify
original EPUB automatically"): load() opens the original read-only and
copies every entry into memory; write_epub() always writes to a NEW path
the caller supplies (a temp file during repair passes, the final
`<name>.repaired.epub` at the end) - there is no in-place save method."""
import zipfile

from lxml import etree


class MutablePackage:
    def __init__(self, path: str):
        self.path = path
        self.order = []          # zip-internal entry names, in ORIGINAL order (mimetype first)
        self.entries = {}         # name -> bytes (current content; kept in sync with any dirty parsed tree)
        self.compress = {}        # name -> zipfile compress type to preserve on rewrite
        self._trees = {}           # name -> cached lxml root Element
        self._dirty_trees = set()   # names whose cached tree must be re-serialized before their bytes are read
        self.modified = set()       # names whose CONTENT changed from the original (set_bytes/mark_tree_dirty)
        self.added = set()          # names that did not exist in the original archive at all
        self.removed = set()        # original names removed from the package

    @classmethod
    def load(cls, path: str) -> "MutablePackage":
        mp = cls(path)
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                mp.order.append(info.filename)
                mp.entries[info.filename] = zf.read(info.filename)
                mp.compress[info.filename] = info.compress_type
        return mp

    def exists(self, name: str) -> bool:
        return name in self.entries and name not in self.removed

    def get_bytes(self, name: str) -> bytes:
        if name in self._dirty_trees:
            self._flush_tree(name)
        return self.entries[name]

    def set_bytes(self, name: str, data: bytes):
        self.entries[name] = data
        self._trees.pop(name, None)
        self._dirty_trees.discard(name)
        self.modified.add(name)

    def get_tree(self, name: str):
        """Parses (and caches) name's content as an lxml tree for
        structural edits. A caller that mutates the returned Element MUST
        call mark_tree_dirty(name) so the change is actually serialized
        back into entries[name] before write_epub()."""
        if name not in self._trees:
            self._trees[name] = etree.fromstring(self.get_bytes(name))
        return self._trees[name]

    def mark_tree_dirty(self, name: str):
        self._dirty_trees.add(name)
        self.modified.add(name)

    def _flush_tree(self, name: str):
        tree = self._trees[name]
        # keep the document's own DOCTYPE (etree.tostring of a bare root
        # element would silently drop it - a needless whole-file change)
        doctype = tree.getroottree().docinfo.doctype or None
        self.entries[name] = etree.tostring(tree.getroottree(), xml_declaration=True, encoding="UTF-8",
                                            doctype=doctype)
        self._dirty_trees.discard(name)

    def add_entry(self, name: str, data: bytes, compress_type: int = zipfile.ZIP_DEFLATED):
        if name in self.removed:
            self.removed.discard(name)
        if name not in self.order:
            self.order.append(name)
        self.entries[name] = data
        self.compress[name] = compress_type
        self._trees.pop(name, None)
        self._dirty_trees.discard(name)
        self.added.add(name)

    def remove_entry(self, name: str):
        if name in self.order:
            self.order.remove(name)
        self.entries.pop(name, None)
        self.compress.pop(name, None)
        self._trees.pop(name, None)
        self._dirty_trees.discard(name)
        self.added.discard(name)
        self.removed.add(name)

    def write_epub(self, out_path: str):
        """Writes a NEW .epub at out_path. 'mimetype' is always written
        first and stored uncompressed, exactly as the EPUB OCF spec
        requires; every other entry preserves its original compression
        type unless explicitly added with a different one."""
        for name in list(self._dirty_trees):
            self._flush_tree(name)
        names = [n for n in self.order if n in self.entries]
        if "mimetype" in names:
            names.remove("mimetype")
            names.insert(0, "mimetype")
        with zipfile.ZipFile(out_path, "w") as zf:
            for name in names:
                compress = zipfile.ZIP_STORED if name == "mimetype" else self.compress.get(name, zipfile.ZIP_DEFLATED)
                zf.writestr(name, self.entries[name], compress)
