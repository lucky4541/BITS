"""GUI layer of the CUPEPUB Auto Zone / Auto Tag engine.

Kept out of gui/main_window.py so the main window only needs a handful of
wiring lines: menu entries and the zone context menu call methods of
SmartAutoZoneUI; the PDF viewer calls draw_debug_overlays() after drawing
zones. Every action:
  * runs only for the CUPEPUB profile (the engine is CUPEPUB-scoped);
  * is one undo step per page;
  * preserves locked / manually overridden / hand-drawn zones.
"""
import json
import os
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from core import auto_validation, profile_manager
from core.zone_manager import ZoneManager, Zone
from auto_zoning.smart_auto_zone import SmartAutoZoner, DocumentRunState, record_manual_retag, ENGINE_VERSION

PROFILE = "CUPEPUB"
REVIEW_COLOR = "#C2185B"
LOCK_MARK = "\U0001F512"

# Debug overlay keys (settings["debug_overlay"][key] -> bool)
OVERLAYS = [
    ("chars", "Character / glyph boxes"),
    ("spans", "Text spans (font runs)"),
    ("blocks", "Detected layout blocks"),
    ("reading_order", "Reading order path"),
    ("decorations", "Underline / strike vectors + assigned characters"),
    ("floats", "Table / figure boundaries"),
    ("decisions", "Semantic role, tag decision and confidence"),
    ("hierarchy", "Parent / child relationships"),
]


class SmartAutoZoneUI:
    def __init__(self, app):
        self.app = app
        self._zoner = None
        self._zoner_key = None
        self._doc_job = None
        self.preview = None
        self.overlay_vars = {}

    # ------------------------------------------------------------ basics
    def _check(self, need_pdf=True) -> bool:
        if self.app.settings.get("profile", "").upper() != PROFILE:
            messagebox.showinfo("Auto Zone / Auto Tag",
                                "The Auto Zone / Auto Tag engine works with the CUPEPUB profile.\n"
                                "Switch the Profile to CUPEPUB first.")
            return False
        if need_pdf and not self.app.pdf_document:
            messagebox.showwarning("Auto Zone / Auto Tag", "Open a PDF first.")
            return False
        return True

    def cache_dir(self):
        try:
            from gui.main_window import APP_ROOT
            d = os.path.join(str(APP_ROOT), "layout_cache")
        except Exception:
            d = os.path.join(tempfile.gettempdir(), "zonetool_layout_cache")
        os.makedirs(d, exist_ok=True)
        return d

    def zoner(self) -> SmartAutoZoner:
        key = (id(self.app.pdf_document), id(self.app.zone_manager), self.app.settings.get("profile"),
               json.dumps(self.app.settings.get("auto_tag_learning", {}), sort_keys=True),
               tuple(self.app.settings.get("auto_tag_dtd_paths", []) or []),
               tuple(self.app.settings.get("auto_tag_reference_xml_dirs", []) or []),
               id(self.app.reference_template))
        if self._zoner is None or self._zoner_key != key:
            self._zoner = SmartAutoZoner(self.app.pdf_document, self.app.zone_manager, self.app.active_profile,
                                         self.app.settings, self.app.reference_template, self.cache_dir())
            self._zoner_key = key
        return self._zoner

    def on_profile_changed(self):
        self._zoner = None
        from core import text_extractor
        text_extractor.set_decoration_detection_enabled(self.app.settings.get("profile", "").upper() == PROFILE)
        self.app.zone_manager.on_manual_retag = self._on_manual_retag

    def on_document_changed(self):
        self._zoner = None
        self.app.zone_manager.on_manual_retag = self._on_manual_retag

    def _on_manual_retag(self, zone, old_tag, old_attrs):
        record_manual_retag(self.app.settings, zone, old_tag, old_attrs)
        self._zoner = None  # learned preferences change the knowledge model

    def _finish(self, pages, message):
        self.app.on_zones_changed(recompute_reading_order=False)
        self.app.viewer.redraw()
        self.app.set_status(message)
        if self.preview is not None:
            self.preview.schedule_refresh()

    # ---------------------------------------------------------- actions
    def auto_zone_page(self, reanalyse=False):
        if not self._check():
            return
        page = self.app.current_page
        z = self.zoner()
        if reanalyse:
            z.invalidate(page)
        try:
            self.app.root.config(cursor="watch")
            self.app.root.update_idletasks()
            res = z.auto_zone_page(page, reanalyse=reanalyse)
        finally:
            self.app.root.config(cursor="")
        if res.error:
            messagebox.showerror("Auto Zone", f"Page {page}: {res.error}")
            return
        if not res.created and not reanalyse and any(
                zz.attributes.get("auto_engine") == ENGINE_VERSION for zz in self.app.zone_manager.zones_on_page(page)):
            messagebox.showinfo("Auto Zone", "This page was already auto-zoned.\n"
                                             "Use 'Re-analyse Page' to replace the automatic zones "
                                             "(locked and manually corrected zones are kept).")
            return
        linked = sum(1 for d in res.continuations if d.merge)
        self._finish([page], f"Auto Zone page {page}: {len(res.created)} zone(s), "
                             f"{res.needs_review} need review"
                     + (f", {linked} cross-page continuation(s) merged" if linked else "")
                     + (f", {res.dropped_for_protected} skipped (manual/locked zone there)"
                        if res.dropped_for_protected else "")
                     + (f", not zoned (no project tag): {sorted(set(res.unmapped))}" if res.unmapped else ""))

    def link_continuations_document(self):
        """Finds paragraphs / notes / reference entries that continue over
        a page break anywhere in the (already zoned) document and records
        them as Merge Previous - works on manual zones too. One undo step;
        a continuation the user unmerged is never linked again."""
        if not self._check():
            return
        from auto_zoning import continuity
        z = self.zoner()
        zm = self.app.zone_manager
        ctx = z.context(whole_document=True)
        cache, merged, considered = {}, [], 0
        self.app.root.config(cursor="watch")
        self.app.root.update_idletasks()
        zm.begin_batch()
        try:
            for page in range(2, self.app.pdf_document.page_count + 1):
                if not zm.zones_on_page(page) or not zm.zones_on_page(page - 1):
                    continue
                for d in continuity.link_page_boundary(zm, self.app.pdf_document, page, self.app.active_profile,
                                                       paragraph_indent=ctx.paragraph_indent,
                                                       body_size=ctx.body_size, line_cache=cache):
                    considered += 1
                    if d.merge:
                        merged.append((page, d))
        finally:
            zm.end_batch()
            self.app.root.config(cursor="")
        self._finish(None, f"Linked {len(merged)} cross-page continuation(s)")
        lines = [f"Page boundaries checked: {considered}", f"Continuations merged: {len(merged)}", ""]
        lines += [f"page {p}: {d.confidence:.0%} - {'; '.join(d.reasons)}" for p, d in merged[:40]]
        messagebox.showinfo("Link Page Continuations", "\n".join(lines) +
                            "\n\nMerged zones are shown yellow; right-click > Unmerge to undo one.")

    def reanalyse_page(self):
        if not self._check():
            return
        if messagebox.askyesno("Re-analyse Page",
                               "Replace this page's automatic zones with a fresh analysis?\n\n"
                               "Locked zones, manually corrected zones and hand-drawn zones are kept."):
            self.auto_zone_page(reanalyse=True)

    def auto_tag_page(self, force=False):
        if not self._check():
            return
        page = self.app.current_page
        res = self.zoner().auto_tag_page(page, force=force)
        if res.error:
            messagebox.showerror("Auto Tag", f"Page {page}: {res.error}")
            return
        self._finish([page], f"Auto Tag page {page}: {res.retagged} zone(s) retagged, {res.needs_review} need review")

    def auto_tag_document(self):
        if not self._check():
            return
        total = 0
        for page in sorted({zz.page for zz in self.app.zone_manager.zones.values()}):
            res = self.zoner().auto_tag_page(page)
            total += res.retagged
        self._finish(None, f"Auto Tag document: {total} zone(s) retagged")

    # ------------------------------------------------------- document run
    def auto_zone_document(self):
        if not self._check():
            return
        if self._doc_job is not None:
            messagebox.showinfo("Auto Zone Document", "A document run is already in progress.")
            return
        z = self.zoner()
        state = DocumentRunState.from_dict(self.app.settings.get("auto_zone_document_state"))
        pages = list(range(1, self.app.pdf_document.page_count + 1))
        if state.fingerprint != z.fingerprint:
            state = DocumentRunState(fingerprint=z.fingerprint, total=len(pages))
        elif state.completed and len(state.completed) < len(pages):
            choice = messagebox.askyesnocancel(
                "Auto Zone Document",
                f"A previous run completed {len(state.completed)} of {len(pages)} page(s).\n\n"
                "Yes = resume (skip completed pages)\nNo = start over (completed pages are kept, "
                "pages already auto-zoned are not duplicated)\nCancel = abort")
            if choice is None:
                return
            if not choice:
                state = DocumentRunState(fingerprint=z.fingerprint, total=len(pages))
        state.total = len(pages)
        # A separate PDF handle for the worker thread (PyMuPDF documents must
        # not be shared across threads).
        from core.pdf_loader import PDFDocument
        worker_pdf = PDFDocument(self.app.pdf_document.path)
        worker = SmartAutoZoner(worker_pdf, ZoneManager(worker_pdf), self.app.active_profile, self.app.settings,
                                self.app.reference_template, self.cache_dir(), knowledge=z.knowledge)
        cancel = threading.Event()
        q = queue.Queue()
        dlg = DocumentProgressDialog(self.app.root, len(pages), len(state.completed), cancel)

        def run():
            try:
                for page, layout, decisions, err in worker.compute_document(pages, cancel, state=state):
                    q.put(("page", page, layout, decisions, err))
            except Exception as e:  # noqa: BLE001
                q.put(("error", str(e)))
            q.put(("done",))

        self._doc_job = threading.Thread(target=run, daemon=True)
        self._doc_job.start()
        stats = {"zones": 0, "review": 0, "errors": []}

        def poll():
            try:
                while True:
                    item = q.get_nowait()
                    if item[0] == "page":
                        _, page, layout, decisions, err = item
                        if err:
                            stats["errors"].append(f"p{page}: {err}")
                        else:
                            res = z.auto_zone_page(page, decisions=decisions, layout=layout)
                            stats["zones"] += len(res.created)
                            stats["review"] += res.needs_review
                            stats["links"] = stats.get("links", 0) + sum(1 for d in res.continuations if d.merge)
                        state.completed.append(page)
                        self.app.settings["auto_zone_document_state"] = state.to_dict()
                        dlg.set_progress(len(set(state.completed)), page, stats)
                    elif item[0] == "error":
                        stats["errors"].append(item[1])
                    elif item[0] == "done":
                        self._doc_job = None
                        dlg.destroy()
                        self._finish(None, f"Auto Zone Document: {stats['zones']} zone(s), "
                                           f"{stats['review']} need review")
                        msg = (f"Pages completed: {len(set(state.completed))}/{len(pages)}\n"
                               f"Zones created: {stats['zones']}\nNeeds review: {stats['review']}\n"
                               f"Cross-page continuations merged: {stats.get('links', 0)}")
                        if cancel.is_set():
                            msg += "\n\nStopped - run Auto Zone Document again to resume."
                        if stats["errors"]:
                            msg += "\n\nErrors:\n" + "\n".join(stats["errors"][:10])
                        messagebox.showinfo("Auto Zone Document", msg)
                        return
            except queue.Empty:
                pass
            self.app.root.after(120, poll)

        poll()

    # --------------------------------------------------------- review
    def next_review_zone(self):
        zones = sorted((zz for zz in self.app.zone_manager.zones.values()
                        if zz.needs_review or (zz.confidence is not None and not zz.manual_override and
                                               zz.confidence < (self.app.settings.get("auto_zone_thresholds") or
                                                                {}).get("medium", 75))),
                       key=lambda zz: (zz.page, zz.serial or 0))
        if not zones:
            messagebox.showinfo("Review", "No zones need review.")
            return
        cur = self.app.selected_zone_id
        ids = [zz.zone_id for zz in zones]
        nxt = zones[(ids.index(cur) + 1) % len(zones)] if cur in ids else zones[0]
        if nxt.page != self.app.current_page:
            self.app.goto_page(nxt.page)
        self.app.select_zone_from_tree(nxt.zone_id)
        self.app.set_status(f"Review {ids.index(nxt.zone_id) + 1}/{len(zones)}: "
                            + "; ".join(nxt.attributes.get("review_reasons", []) or
                                        [f"confidence {nxt.confidence:.0f}%"]))

    def mark_reviewed(self, zone_id):
        zone = self.app.zone_manager.zones.get(zone_id)
        if not zone:
            return
        self.app.zone_manager._push_undo()
        zone.attributes.pop("needs_review", None)
        zone.attributes.pop("review_reasons", None)
        zone.attributes["manual_override"] = True   # accepted by the user = manual decision
        self._finish([zone.page], f"{zone_id} marked as reviewed")

    def toggle_lock(self, zone_id):
        zone = self.app.zone_manager.zones.get(zone_id)
        if not zone:
            return
        self.app.zone_manager.set_locked(zone_id, not zone.locked)
        self._finish([zone.page], f"{zone_id} {'locked' if zone.locked else 'unlocked'}")

    def explain_zone(self, zone_id):
        zone = self.app.zone_manager.zones.get(zone_id)
        if not zone:
            return
        a = zone.attributes
        lines = [f"Zone {zone.zone_id}  page {zone.page}  RO {zone.serial}",
                 f"Tag: {a.get('cup_name') or zone.tag}  (<{zone.tag}>)",
                 f"Locked: {zone.locked}   Manual override: {zone.manual_override}",
                 f"Role: {a.get('auto_role', '-')}   Confidence: {a.get('confidence', '-')}"]
        bd = a.get("confidence_breakdown") or {}
        if bd:
            lines.append("Confidence breakdown: " + ", ".join(f"{k} {v:.2f}" for k, v in bd.items()))
        if a.get("needs_review"):
            lines.append("NEEDS REVIEW: " + "; ".join(a.get("review_reasons", [])))
        if a.get("auto_evidence"):
            lines.append("")
            lines.append("Evidence:")
            lines.extend(f"  - {e}" for e in a["auto_evidence"])
        if a.get("auto_alternatives"):
            lines.append("")
            lines.append("Alternatives:")
            lines.extend(f"  - {lbl} (role {role}, score {sc:.2f})" for lbl, role, sc in a["auto_alternatives"])
        ranges = zone.formatting_ranges if zone.text else []
        if ranges:
            from core.formatting_ranges import parse_ranges
            plain = parse_ranges(zone.text)[0]
            lines.append("")
            lines.append("Formatting ranges:")
            lines.extend(f"  - {r['style']:10s} [{r['start']}:{r['end']}] {plain[r['start']:r['end']]!r}"
                         for r in ranges)
        TextDialog(self.app.root, "Auto Tag decision", "\n".join(lines))

    # ---------------------------------------------------- knowledge / io
    def show_knowledge(self):
        if not self._check(need_pdf=False):
            return
        from core.tag_knowledge import TagKnowledgeModel
        km = self._zoner.knowledge if self._zoner else TagKnowledgeModel.build(
            self.app.active_profile, self.app.settings, self.app.reference_template)
        TextDialog(self.app.root, "Tag Knowledge Model", km.report())

    def load_dtd(self):
        if not self._check(need_pdf=False):
            return
        path = filedialog.askopenfilename(title="Load project DTD", filetypes=[("DTD", "*.dtd"), ("All", "*.*")])
        if not path:
            return
        try:
            from core.tag_knowledge import DTDModel
            model = DTDModel.from_file(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Load DTD", f"Could not parse {path}:\n{e}")
            return
        paths = list(self.app.settings.get("auto_tag_dtd_paths", []) or [])
        if path not in paths:
            paths.append(path)
        self.app.settings["auto_tag_dtd_paths"] = paths
        self._zoner = None
        self.app.mark_dirty("settings_changed")
        messagebox.showinfo("Load DTD", f"Loaded {len(model.elements)} element declaration(s) from\n{path}")

    def load_reference_corpus(self):
        if not self._check(need_pdf=False):
            return
        d = filedialog.askdirectory(title="Folder of reference XML / XHTML files")
        if not d:
            return
        dirs = list(self.app.settings.get("auto_tag_reference_xml_dirs", []) or [])
        if d not in dirs:
            dirs.append(d)
        self.app.settings["auto_tag_reference_xml_dirs"] = dirs
        self._zoner = None
        if self.app.pdf_document:
            self.zoner().invalidate()
        self.app.mark_dirty("settings_changed")
        from core.tag_knowledge import TagKnowledgeModel
        km = TagKnowledgeModel.build(self.app.active_profile, self.app.settings, self.app.reference_template)
        n = len(km.corpus.files) if km.corpus else 0
        messagebox.showinfo("Reference XML corpus", f"{n} reference file(s) analysed.\n\n"
                            + json.dumps(km.corpus.summary()["top_succession"][:10], indent=1) if n else
                            "No XML/XHTML files found in that folder.")

    # --------------------------------------------------------- validation
    def validation_report(self):
        if not self._check():
            return
        km = self.zoner().knowledge
        report = auto_validation.run(self.app.zone_manager, self.app.pdf_document, self.app.active_profile, km,
                                     thresholds=self.app.settings.get("auto_zone_thresholds"))
        ValidationReportDialog(self.app.root, self.app, report)

    # ------------------------------------------------------------ preview
    def open_preview(self):
        if not self._check():
            return
        if self.preview is not None and self.preview.winfo_exists():
            self.preview.lift()
            return
        self.preview = XmlPreviewWindow(self.app.root, self.app, self)

    def notify_changed(self):
        if self.preview is not None:
            try:
                if self.preview.winfo_exists():
                    self.preview.schedule_refresh()
            except tk.TclError:
                self.preview = None

    # ------------------------------------------------------- overlays
    def overlay_enabled(self, key) -> bool:
        return bool((self.app.settings.get("debug_overlay") or {}).get(key))

    def set_overlay(self, key, value):
        ov = dict(self.app.settings.get("debug_overlay") or {})
        ov[key] = bool(value)
        self.app.settings["debug_overlay"] = ov
        self.app.viewer.redraw()

    def build_overlay_menu(self, menu):
        for key, label in OVERLAYS:
            var = tk.BooleanVar(value=self.overlay_enabled(key))
            self.overlay_vars[key] = var
            menu.add_checkbutton(label=label, variable=var,
                                 command=lambda k=key, v=var: self.set_overlay(k, v.get()))

    def draw_debug_overlays(self, viewer):
        if not self.app.pdf_document or not any(self.overlay_enabled(k) for k, _ in OVERLAYS):
            return
        from gui.debug_overlay import draw
        try:
            draw(viewer, self.app, self)
        except Exception as e:  # noqa: BLE001 - a debug view must never break the viewer
            self.app.set_status(f"Debug overlay error: {e}")


# ====================================================================== dialogs
class TextDialog(tk.Toplevel):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.title(title)
        self.geometry("820x600")
        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, wrap="none", font=("Consolas", 9))
        ys = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        tk.Button(self, text="Close", command=self.destroy).pack(pady=4)
        self.transient(parent)


class DocumentProgressDialog(tk.Toplevel):
    def __init__(self, parent, total, done, cancel_event):
        super().__init__(parent)
        self.title("Auto Zone Document")
        self.resizable(False, False)
        self._cancel = cancel_event
        pad = tk.Frame(self, padx=18, pady=14)
        pad.pack(fill="both", expand=True)
        self._label = tk.Label(pad, text=f"Analysing... {done}/{total} page(s) done", anchor="w", width=56)
        self._label.pack(fill="x")
        self._bar = ttk.Progressbar(pad, mode="determinate", maximum=total, length=360)
        self._bar.pack(fill="x", pady=8)
        self._bar["value"] = done
        self._total = total
        tk.Button(pad, text="Stop (completed pages are kept - resume later)", command=self._stop).pack()
        self.protocol("WM_DELETE_WINDOW", self._stop)
        self.transient(parent)

    def _stop(self):
        self._cancel.set()
        self._label.config(text="Stopping after the current page...")

    def set_progress(self, done, page, stats):
        if not self.winfo_exists():
            return
        self._bar["value"] = done
        self._label.config(text=f"Page {page} done - {done}/{self._total} - {stats['zones']} zone(s), "
                                f"{stats['review']} to review")


class ValidationReportDialog(tk.Toplevel):
    def __init__(self, parent, app, report):
        super().__init__(parent)
        self.app = app
        self.report = report
        self.title(f"Validation Report - {report.count('error')} error(s), {report.count('warning')} warning(s)")
        self.geometry("980x560")
        cols = ("level", "page", "zone", "message")
        tree = ttk.Treeview(self, columns=cols, show="tree headings")
        tree.heading("#0", text="Stage")
        for c, w in zip(cols, (70, 50, 70, 640)):
            tree.heading(c, text=c.title())
            tree.column(c, width=w, stretch=(c == "message"))
        tree.column("#0", width=200)
        for stage in auto_validation.STAGES:
            items = [i for i in report.issues if i.stage == stage]
            status = "FAILED" if any(i.level == "error" for i in items) else "OK"
            node = tree.insert("", "end", text=f"{auto_validation.STAGE_TITLES[stage]} - {status}",
                               values=("", "", "", f"{len(items)} item(s)"), open=bool(items) and status == "FAILED")
            for i in items:
                tree.insert(node, "end", text="", values=(i.level, i.page or "", i.zone_id or "", i.message))
        ys = ttk.Scrollbar(self, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda e: self._goto(tree))
        bar = tk.Frame(self)
        bar.pack(fill="x")
        tk.Button(bar, text="Save as TXT...", command=lambda: self._save("txt")).pack(side="left", padx=4, pady=4)
        tk.Button(bar, text="Save as JSON...", command=lambda: self._save("json")).pack(side="left", padx=4)
        tk.Button(bar, text="Close", command=self.destroy).pack(side="right", padx=4)
        self.transient(parent)

    def _goto(self, tree):
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values")
        if len(vals) >= 3 and vals[2]:
            zone = self.app.zone_manager.zones.get(vals[2])
            if zone:
                if zone.page != self.app.current_page:
                    self.app.goto_page(zone.page)
                self.app.select_zone_from_tree(zone.zone_id)
        elif len(vals) >= 2 and vals[1]:
            self.app.goto_page(int(vals[1]))

    def _save(self, kind):
        path = filedialog.asksaveasfilename(defaultextension=f".{kind}", filetypes=[(kind.upper(), f"*.{kind}")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.report.to_text() if kind == "txt" else self.report.to_json())


class XmlPreviewWindow(tk.Toplevel):
    """Live XML preview generated by the SAME pipeline as Generate XML (the
    XML generator, then the BITS / JATS structure), never a separately
    maintained XML representation. Regenerated (debounced) whenever zones
    change."""

    def __init__(self, parent, app, ui):
        super().__init__(parent)
        self.app = app
        self.ui = ui
        self.title("Live XML Preview (BITS / JATS)")
        self.geometry("760x640")
        top = tk.Frame(self)
        top.pack(fill="x")
        self.scope = tk.StringVar(value="page")
        self.stage = tk.StringVar(value="intermediate")
        for val, lbl in (("page", "Current page"), ("document", "Whole document")):
            ttk.Radiobutton(top, text=lbl, value=val, variable=self.scope,
                            command=self.schedule_refresh).pack(side="left", padx=4)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=6)
        for val, lbl in (("intermediate", "Zone XML (generator)"), ("final", "Final BITS / JATS XML")):
            ttk.Radiobutton(top, text=lbl, value=val, variable=self.stage,
                            command=self.schedule_refresh).pack(side="left", padx=4)
        self.status = tk.Label(top, text="", anchor="e")
        self.status.pack(side="right", padx=6)
        self.text = tk.Text(self, wrap="none", font=("Consolas", 9))
        ys = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self._after = None
        self._tmp = tempfile.mkdtemp(prefix="zt_preview_")
        self.transient(parent)
        self.schedule_refresh()

    def schedule_refresh(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
        self._after = self.after(600, self.refresh)

    def _zone_view(self):
        zm = self.app.zone_manager
        if self.scope.get() == "document":
            return zm
        page = self.app.current_page
        view = ZoneManager(zm.pdf_document)
        for z in zm.zones.values():
            if z.page == page:
                view.zones[z.zone_id] = Zone.from_dict(z.to_dict())
        for z in view.zones.values():
            if z.parent_id not in view.zones:
                z.parent_id = None
            z.children = [c for c in z.children if c in view.zones]
        return view

    def refresh(self):
        self._after = None
        from lxml import etree
        from core import xml_generator
        from core.bits import structure, pipeline
        try:
            view = self._zone_view()
            profile = self.app.active_profile
            gen = xml_generator.XMLGenerator(view, self.app.pdf_document, self._tmp, "preview",
                                             split_back_matter=False)
            root = gen.generate_tree()
            stats = {"elements_normalized": 0}
            if self.stage.get() == "final":
                root = structure.build(pipeline.kind_of(profile), root, self.app.settings.get("bits_meta") or {},
                                       prefix="preview")
                stats["elements_normalized"] = sum(1 for _ in root.iter())
            xml = etree.tostring(root, pretty_print=True, encoding="unicode")
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", xml)
            self.text.configure(state="disabled")
            self.status.config(text=f"{len(view.zones)} zone(s), {stats['elements_normalized']} element(s)")
        except Exception as e:  # noqa: BLE001
            self.status.config(text=f"Preview failed: {type(e).__name__}: {e}")
