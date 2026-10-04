"""QC session: the single object the GUI and tests drive.

    session = QCSession.open(pdf_path, epub_path_or_folder)
    session.analyse()                    # builds the unified mapping
    session.differences / .mapping.scores / .mapping.pages / .mapping.split_maps
    session.accept(diff) / reject / ignore / edit / apply_all_similar / auto_correct
    session.delete_split / merge_with_previous / merge_with_next / split_at_pdf_page / ...
    session.undo() / redo()
    session.export_report(path) ; session.final_validation() ; session.build_epub(path)

Decision priority (highest first): USER DECISION (accept / reject / edit /
ignore, remembered per difference signature in the project sidecar) >
LOCKED SPLIT > AUTO CORRECTION (only auto-safe, high-confidence) >
comparison results. Re-analysis never overrides a user decision.

Incremental: after any change only the modified splits are re-parsed (by
content hash); PDF pages, PDF images and package images come from caches,
so a correction never re-reads the PDF.
"""
import os
import tempfile

from core.qc import corrections
from core.qc.mapping import DocumentMapping
from core.qc.package_manager import EpubPackageManager, PackageError
from core.qc.pdf_model import PdfModel
from core.qc.xhtml_model import XhtmlModel

AUTO_CONFIDENCE = 0.9


def default_cache_dir():
    try:
        from core.resource_path import writable_root
        d = os.path.join(writable_root(), "qc_cache")
    except Exception:
        d = os.path.join(tempfile.gettempdir(), "epubforge_qc_cache")
    os.makedirs(d, exist_ok=True)
    return d


class QCSession:
    def __init__(self, pdf_path, mgr, cache_dir=None):
        self.cache_dir = cache_dir or default_cache_dir()
        self.mgr = mgr
        self.pdf = PdfModel(pdf_path, os.path.join(self.cache_dir, "pdf")) if pdf_path else None
        self.xhtml = XhtmlModel(mgr)
        self.mapping = None
        self._pdf_built = False

    @classmethod
    def open(cls, pdf_path, epub_path, cache_dir=None):
        cache_dir = cache_dir or default_cache_dir()
        mgr = EpubPackageManager.open(epub_path, work_dir=os.path.join(cache_dir, "work"))
        return cls(pdf_path, mgr, cache_dir)

    # ----------------------------------------------------------- analysis
    def analyse(self, progress=None, cancel=None):
        if self.pdf is not None and not self._pdf_built:
            self.pdf.build(progress=progress, cancel=cancel)
            self._pdf_built = True
        self.xhtml.build(progress=progress)
        self.mapping = DocumentMapping(self.pdf, self.xhtml, self.mgr,
                                       decisions=self.mgr.sidecar.get("decisions", {})).compute(progress)
        return self.mapping

    def refresh(self, progress=None):
        """Incremental re-analysis after an edit (changed splits only)."""
        self.mgr.reload()
        return self.analyse(progress)

    @property
    def differences(self):
        return self.mapping.differences if self.mapping else []

    # ----------------------------------------------------------- decisions
    def _record(self, diff, status, note=""):
        decisions = self.mgr.sidecar.setdefault("decisions", {})
        decisions[diff.id] = {"status": status, "kind": diff.kind, "note": note,
                              "page": diff.pdf_page, "split": diff.split}
        diff.status = status
        self.mgr._save_sidecar()

    def reject(self, diff, note=""):
        self._record(diff, "rejected", note)

    def ignore(self, diff, note=""):
        self._record(diff, "ignored", note)

    def reopen(self, diff):
        self.mgr.sidecar.get("decisions", {}).pop(diff.id, None)
        diff.status = "open"
        self.mgr._save_sidecar()

    def accept(self, diff, refresh=True):
        """Applies the suggested correction (a user decision: always wins)."""
        if diff.correction is None:
            # nothing to change in the package - accepting means "this is correct as is"
            self._record(diff, "accepted", "accepted without change")
            return None
        if diff.split and self.mgr.is_locked(diff.split):
            raise PackageError(f"{diff.split} is locked")
        res = corrections.apply(self, diff)
        self._record(diff, "accepted")
        if refresh:
            self.refresh()
        return res

    def edit(self, diff, new_text, refresh=True):
        res = corrections.apply(self, diff, edit_text=new_text)
        self._record(diff, "edited", new_text)
        if refresh:
            self.refresh()
        return res

    def similar(self, diff):
        if diff.correction is None:
            return []
        return [d for d in self.differences if d.kind == diff.kind and d.status == "open" and d.correction
                and d.correction.action == diff.correction.action
                and not (d.split and self.mgr.is_locked(d.split))]

    def apply_batch(self, diffs, name="Apply corrections"):
        """Applies several corrections as ONE undoable step - in reverse
        document order, so earlier element paths stay valid."""
        diffs = [d for d in diffs if d.correction is not None]
        if not diffs:
            return None
        order = {s: i for i, s in enumerate(self.xhtml.splits)}
        diffs.sort(key=lambda d: (order.get(d.split, -1), d.x_start or 0), reverse=True)
        files = set()
        for d in diffs:
            files.update(corrections.files_for(self, d.correction))
        structural = [d for d in diffs if d.correction.action in ("package_fix", "reorder_splits")]
        content = [d for d in diffs if d not in structural]

        def run(mgr):
            notes = []
            for d in content:
                try:
                    notes.extend(corrections.ACTIONS[d.correction.action](self, mgr, d.correction.params) or [])
                except Exception as e:  # noqa: BLE001 - one bad target never aborts the batch
                    notes.append(f"{d.label}: {e}")
            return notes
        res = None
        with self.mgr.group(name):
            if content:
                res = self.mgr.edit(name, run, sorted(files))
            for d in structural:
                corrections.apply(self, d)
        for d in diffs:
            self._record(d, "accepted", "batch")
        self.refresh()
        return res

    def apply_all_similar(self, diff):
        return self.apply_batch(self.similar(diff), name=f"Apply to all similar: {diff.label}")

    def auto_correct(self, min_confidence=AUTO_CONFIDENCE):
        """High-confidence, structurally safe corrections only (page markers,
        image mapping, links, package entries). Text content is never
        changed automatically. Decided differences and locked splits are
        skipped."""
        decided = set(self.mgr.sidecar.get("decisions", {}))
        todo = [d for d in self.differences if d.status == "open" and d.id not in decided and d.correction
                and d.correction.auto_safe and d.correction.confidence >= min_confidence
                and d.category != "text" and not (d.split and self.mgr.is_locked(d.split))]
        if not todo:
            return 0
        self.apply_batch(todo, name=f"Auto-correct {len(todo)} difference(s)")
        return len(todo)

    # --------------------------------------------------------------- images
    def package_images(self):
        """Package-relative paths of every image in the manifest."""
        return sorted(rel for rel, (_mid, mt, _props) in self._manifest_media().items() if mt.startswith("image/"))

    def _manifest_media(self):
        out = {}
        for item in self.mgr._manifest_items():
            rel = self.mgr._opf_to_rel(item.get("href", ""))
            out[rel] = (item.get("id"), item.get("media-type", ""), item.get("properties"))
        return out

    def remap_image(self, diff, file_rel):
        """REMAP: points the XHTML image of `diff` at another package image
        (or, for an image missing from the XHTML, inserts that image at the
        PDF reading position). A user decision."""
        from core.qc.mapping import Correction
        info = diff.image or {}
        if info.get("xhtml_xpath") and info.get("xhtml_split"):
            corr = Correction("remap_image", {"split": info["xhtml_split"], "xpath": info["xhtml_xpath"],
                                              "file": file_rel}, 1.0, description=f"Remap to {file_rel}")
        else:
            corr = Correction("insert_image", {"x_pos": diff.x_start or 0, "file": file_rel}, 1.0,
                              description=f"Insert {file_rel}")
        split = info.get("xhtml_split") or diff.split
        if split and self.mgr.is_locked(split):
            raise PackageError(f"{split} is locked")
        old = diff.correction
        diff.correction = corr
        try:
            res = corrections.apply(self, diff)
        finally:
            diff.correction = old
        self._record(diff, "edited", f"image -> {file_rel}")
        self.refresh()
        return res

    def change_image(self, diff, disk_path):
        """CHANGE: copies a new image file into the package (next to the
        existing images, unique name, manifest entry) and remaps to it."""
        import shutil
        from core.qc.package_manager import guess_media_type
        images = self.package_images()
        folder = os.path.dirname(images[0]) if images else os.path.dirname(self.mgr.split_paths()[0])
        base = os.path.basename(disk_path)
        stem, ext = os.path.splitext(base)
        rel = "/".join(p for p in (folder, base) if p)
        n = 2
        while self.mgr.exists(rel):
            rel = "/".join(p for p in (folder, f"{stem}-{n}{ext}") if p)
            n += 1
        os.makedirs(os.path.dirname(self.mgr.abspath(rel)), exist_ok=True)
        shutil.copyfile(disk_path, self.mgr.abspath(rel))
        self.mgr.edit(f"Add image {os.path.basename(rel)}",
                      lambda m: (m.add_manifest_item(rel, guess_media_type(rel)), m.write_opf(), [])[-1],
                      [self.mgr.opf_path])
        return self.remap_image(diff, rel)

    # --------------------------------------------------------------- splits
    def _after(self, res):
        self.refresh()
        return res

    def impact(self, rel):
        return self.mgr.impact(rel)

    def delete_split(self, rel):
        return self._after(self.mgr.delete_split(rel))

    def merge_with_previous(self, rel):
        return self._after(self.mgr.merge_with_previous(rel))

    def merge_with_next(self, rel):
        return self._after(self.mgr.merge_with_next(rel))

    def add_split(self, after_rel, title="New Section"):
        return self._after(self.mgr.add_split(after_rel, title))

    def rename_split(self, rel, new_name):
        return self._after(self.mgr.rename_split(rel, new_name))

    def reorder_splits(self, order):
        return self._after(self.mgr.reorder(order))

    def lock(self, rel, locked=True):
        self.mgr.lock(rel, locked)

    def split_at_element(self, rel, element):
        return self._after(self.mgr.split_document(rel, element))

    def split_at_word(self, j):
        """Splits before the block containing XHTML word j (or the next
        block boundary when j is mid-paragraph)."""
        x = self.xhtml
        w = x.words[j]
        blk = x.blocks[w.block]
        if blk.word_start != j:
            nxt = next((b for b in x.blocks[w.block + 1:] if b.split == w.split and b.word_end > b.word_start), None)
            if nxt is None:
                raise PackageError("No block boundary after this position in the split")
            blk = nxt
        el = self.mgr.doc(blk.split).xpath(blk.xpath)[0]
        return self.split_at_element(blk.split, el)

    def split_at_pdf_page(self, page_no):
        """SPLIT AT PDF PAGE: located through the mapping (never a plain text
        search): the page's marker when it sits at a block start, otherwise
        the block where the page's first mapped word lives."""
        if self.mapping is None:
            raise PackageError("Run the comparison first")
        pm = self.mapping.pages[page_no - 1]
        j = self.mapping.page_start_x(page_no)
        if pm.marker is not None:
            j = pm.marker.word_pos
        if j >= len(self.xhtml.words):
            raise PackageError(f"PDF page {page_no} maps to the end of the XHTML")
        return self.split_at_word(j)

    def move_block_to_neighbour(self, rel, block_index, direction):
        """Moves a top-level block of `rel` to the end of the previous split
        (direction -1) or the start of the next split (+1)."""
        order = self.mgr.split_paths()
        k = order.index(rel)
        dst = order[k + direction] if 0 <= k + direction < len(order) else None
        if dst is None:
            raise PackageError("No neighbouring split in that direction")
        body = self.mgr.body(rel)
        kids = [c for c in body if isinstance(c.tag, str)]
        if direction < 0:
            elements = kids[:block_index + 1]
            return self._after(self.mgr.move_content(rel, elements, dst, position="end"))
        elements = kids[block_index:]
        return self._after(self.mgr.move_content(rel, elements, dst, position="start"))

    def undo(self):
        name = self.mgr.undo()
        if name:
            self.refresh()
        return name

    def redo(self):
        name = self.mgr.redo()
        if name:
            self.refresh()
        return name

    # ------------------------------------------------------------ output
    def export_report(self, path):
        from core.qc import reports
        return reports.export(self, path)

    def final_validation(self):
        from core.qc import final_validation
        return final_validation.run(self)

    def build_epub(self, out_path, require_valid=False):
        from core.qc import final_validation
        report = final_validation.run(self)
        if require_valid and report.errors:
            return None, report
        self.mgr.export_epub(out_path)
        report.epub_path = out_path
        return out_path, report
