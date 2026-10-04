"""PDF <-> XHTML QC WORKSPACE - PDF-aware EPUB comparison, auto-mapping,
visual comparison, validation, auto-correction and split management.

One window of the existing application (launcher card, Fidelity Compare
button). Everything shown comes from ONE core.qc.engine.QCSession: the PDF
model, the XHTML model, the unified mapping and the package manager.

    [PDF viewer]  <->  [Rendered XHTML | XHTML source]      (side by side)
    [Overlay: PDF + XHTML of the same page]                  (overlay mode)
    left:  split tree (drag & drop) / pages
    right: differences (filters, accept/reject/edit/ignore/apply-similar) / scores
"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app.qc.panels import DifferencePanel, PagesPanel, ScoresPanel, SplitTree, legend
from app.qc.viewers import DEFAULT_COLORS, OverlayView, PdfView, RenderedView, SourceView
from core.qc.mapping import STATE_LABELS

try:
    from gui import theme
except Exception:  # pragma: no cover - theme is optional for headless use
    theme = None

PREF_KEY = "qc_colors"


def _load_colors():
    colors = dict(DEFAULT_COLORS)
    try:
        from core import ui_prefs
        saved = ui_prefs.load_ui_prefs().get(PREF_KEY) or {}
        colors.update({k: v for k, v in saved.items() if k in colors})
    except Exception:
        pass
    return colors


def _save_colors(colors):
    try:
        from core import ui_prefs
        ui_prefs.save_ui_prefs({PREF_KEY: dict(colors)})
    except Exception:
        pass


class QCWorkspace:
    def __init__(self, launcher_root, on_home, pdf_path=None, epub_path=None, master=None):
        self.launcher_root = launcher_root
        self.on_home = on_home
        self.win = tk.Toplevel(master or launcher_root)
        self.win.title("EPUBForge - PDF ↔ XHTML QC")
        self.win.geometry("1500x920")
        self.win.minsize(1100, 700)
        self.win.protocol("WM_DELETE_WINDOW", self._go_home)
        self.session = None
        self.colors = _load_colors()
        self.show_highlights = tk.BooleanVar(value=True)
        self.show_matched = tk.BooleanVar(value=False)
        self.sync_scroll = tk.BooleanVar(value=True)
        self.mode = tk.StringVar(value="side")
        self.current_split = None
        self._current_word = None
        self._worker = None
        self._search_hits = []
        self._search_i = -1
        self.pdf_var = tk.StringVar(value=pdf_path or "")
        self.epub_var = tk.StringVar(value=epub_path or "")
        self._build()
        if pdf_path and epub_path:
            self.win.after(200, self.start)

    # ================================================================ UI
    def _build(self):
        p = theme.current.palette if theme is not None and hasattr(theme, "current") else {}
        hdr = tk.Frame(self.win, bg=p.get("header_bg", "#1F2430"))
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="PDF ↔ XHTML QC WORKSPACE", bg=p.get("header_bg", "#1F2430"),
                 fg=p.get("header_fg", "#F4F6FA"), font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT, padx=12, pady=8)
        tk.Label(hdr, text="mapping · visual comparison · validation · auto-correction · split management",
                 bg=p.get("header_bg", "#1F2430"), fg=p.get("header_fg_muted", "#AEB6C4"),
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        ttk.Button(hdr, text="⌂ Home", command=self._go_home).pack(side=tk.RIGHT, padx=10)

        inp = tk.Frame(self.win)
        inp.pack(fill=tk.X, padx=6, pady=4)
        for label, var, cmd in (("PDF:", self.pdf_var, self._browse_pdf),
                                ("EPUB / package folder:", self.epub_var, self._browse_epub)):
            tk.Label(inp, text=label).pack(side=tk.LEFT, padx=(6, 2))
            ttk.Entry(inp, textvariable=var, width=46).pack(side=tk.LEFT)
            ttk.Button(inp, text="…", width=3, command=cmd).pack(side=tk.LEFT, padx=(1, 0))
        ttk.Button(inp, text="Folder", width=6, command=self._browse_folder).pack(side=tk.LEFT, padx=1)
        self.start_btn = ttk.Button(inp, text="Open & Compare", command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=8)
        self.cancel_btn = ttk.Button(inp, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(inp, length=160, mode="determinate")
        self.progress.pack(side=tk.LEFT, padx=6)

        tb = tk.Frame(self.win)
        tb.pack(fill=tk.X, padx=6)
        ttk.Radiobutton(tb, text="Side by side", value="side", variable=self.mode,
                        command=self._apply_mode).pack(side=tk.LEFT)
        ttk.Radiobutton(tb, text="Overlay", value="overlay", variable=self.mode,
                        command=self._apply_mode).pack(side=tk.LEFT)
        ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6)
        for text, cmd in (("−", lambda: self.zoom(1 / 1.2)), ("+", lambda: self.zoom(1.2)),
                          ("Fit width", lambda: self.fit("width")), ("Fit page", lambda: self.fit("page")),
                          ("◀ Page", lambda: self.goto_pdf_page(self.pdf_view.page - 1)),
                          ("Page ▶", lambda: self.goto_pdf_page(self.pdf_view.page + 1))):
            ttk.Button(tb, text=text, command=cmd, width=len(text) + 2).pack(side=tk.LEFT, padx=1)
        ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self.search_var = tk.StringVar()
        e = ttk.Entry(tb, textvariable=self.search_var, width=18)
        e.pack(side=tk.LEFT)
        e.bind("<Return>", lambda _e: self.search())
        ttk.Button(tb, text="Search", command=self.search).pack(side=tk.LEFT, padx=1)
        ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(tb, text="◀ Prev diff", command=lambda: self.step_difference(-1)).pack(side=tk.LEFT)
        self.counter = tk.Label(tb, text="Difference 0/0", width=16, font=("Segoe UI", 9, "bold"))
        self.counter.pack(side=tk.LEFT)
        ttk.Button(tb, text="Next diff ▶", command=lambda: self.step_difference(1)).pack(side=tk.LEFT)
        ttk.Separator(tb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6)
        for text, var in (("Highlights", self.show_highlights), ("Show matches", self.show_matched),
                          ("Sync scroll", self.sync_scroll)):
            ttk.Checkbutton(tb, text=text, variable=var, command=self._redraw).pack(side=tk.LEFT)
        tb2 = tk.Frame(self.win)
        tb2.pack(fill=tk.X, padx=6, pady=(2, 4))
        for text, cmd in (("Colours…", self.edit_colors), ("Auto-correct (high confidence)", self.auto_correct),
                          ("Undo", self.undo), ("Redo", self.redo), ("Re-compare", self.recompare),
                          ("Final validation", self.validate), ("Export report…", self.export_report),
                          ("Build EPUB…", self.build_epub)):
            ttk.Button(tb2, text=text, command=cmd).pack(side=tk.LEFT, padx=1)
        self.legend_holder = tk.Frame(tb2)
        self.legend_holder.pack(side=tk.RIGHT)
        self._legend()

        body = ttk.PanedWindow(self.win, orient="horizontal")
        body.pack(fill=tk.BOTH, expand=True, padx=4)
        left = ttk.Notebook(body)
        self.split_tree = SplitTree(left, self)
        self.pages_panel = PagesPanel(left, self)
        left.add(self.split_tree, text="Splits")
        left.add(self.pages_panel, text="Pages")
        body.add(left, weight=1)
        self.center = tk.Frame(body)
        body.add(self.center, weight=4)
        self.side = ttk.PanedWindow(self.center, orient="horizontal")
        self.pdf_view = PdfView(self.side, self)
        self.side.add(self.pdf_view, weight=1)
        xnb = ttk.Notebook(self.side)
        self.rendered = RenderedView(xnb, self)
        self.source = SourceView(xnb, self)
        xnb.add(self.rendered, text="Rendered XHTML")
        xnb.add(self.source, text="XHTML source")
        self.side.add(xnb, weight=1)
        self.overlay = OverlayView(self.center, self)
        self.side.pack(fill=tk.BOTH, expand=True)
        right = ttk.Notebook(body)
        self.diff_panel = DifferencePanel(right, self)
        self.scores_panel = ScoresPanel(right, self)
        right.add(self.diff_panel, text="Differences")
        right.add(self.scores_panel, text="Scores")
        body.add(right, weight=2)

        self.status = tk.Label(self.win, text="Choose the source PDF and the EPUB (or unpacked package folder).",
                               anchor="w", relief="sunken", font=("Segoe UI", 9))
        self.status.pack(fill=tk.X, side=tk.BOTTOM)
        self.win.bind("<Control-z>", lambda e: self.undo())
        self.win.bind("<Control-y>", lambda e: self.redo())
        self.win.bind("<F3>", lambda e: self.step_difference(1))
        self.win.bind("<Shift-F3>", lambda e: self.step_difference(-1))

    def _legend(self):
        for w in self.legend_holder.winfo_children():
            w.destroy()
        legend(self.legend_holder, self.colors).pack()

    def set_status(self, text):
        self.status.configure(text=text)

    # ============================================================ inputs
    def _browse_pdf(self):
        p = filedialog.askopenfilename(parent=self.win, filetypes=[("PDF", "*.pdf")])
        if p:
            self.pdf_var.set(p)

    def _browse_epub(self):
        p = filedialog.askopenfilename(parent=self.win, filetypes=[("EPUB", "*.epub"), ("All", "*.*")])
        if p:
            self.epub_var.set(p)

    def _browse_folder(self):
        p = filedialog.askdirectory(parent=self.win, title="Unpacked EPUB package folder")
        if p:
            self.epub_var.set(p)

    def start(self):
        pdf, epub = self.pdf_var.get().strip(), self.epub_var.get().strip()
        if not os.path.isfile(pdf):
            messagebox.showerror("QC", "Choose an existing PDF.", parent=self.win)
            return
        if not os.path.exists(epub):
            messagebox.showerror("QC", "Choose an existing EPUB file or package folder.", parent=self.win)
            return
        from core.fidelity_compare.worker import ComparisonWorker
        from core.qc.engine import QCSession

        def run(progress, should_cancel):
            session = QCSession.open(pdf, epub)
            session.analyse(progress=progress, cancel=should_cancel)
            return session
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.set_status("Analysing…")
        self._worker = ComparisonWorker(run)
        self._worker.start()
        self.win.after(100, self._poll)

    def cancel(self):
        if self._worker:
            self._worker.cancel()
            self.set_status("Cancelling…")

    def _poll(self):
        if self._worker is None:
            return
        for msg in self._worker.poll():
            if msg[0] == "progress":
                _k, stage, cur, total = msg
                self.progress.configure(maximum=max(total, 1), value=cur)
                self.set_status(f"{stage} {cur}/{total}" if total > 1 else stage)
            elif msg[0] == "done":
                self._worker = None
                self._finish(msg[1])
                return
            elif msg[0] == "error":
                self._worker = None
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.set_status(f"Analysis failed: {msg[1]}")
                messagebox.showerror("QC", f"Analysis failed:\n{msg[1]}", parent=self.win)
                return
        self.win.after(100, self._poll)

    def _finish(self, session):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.progress.configure(value=0)
        if session.mapping is None:
            self.set_status("Cancelled.")
            return
        self.session = session
        self.current_split = None
        self.refresh_all()
        splits = session.mgr.split_paths()
        first = next((d for d in self.diff_panel.items), None)
        if first is not None:
            self.diff_panel.select(0)
        else:
            self.goto_pdf_page(1)
            if splits:
                self.show_split(splits[0])
        m = session.mapping
        self.set_status(f"Compared {len(session.pdf.pages) if session.pdf else 0} PDF pages with "
                        f"{len(splits)} split(s): Overall {m.scores.get('Overall')}%, "
                        f"{sum(1 for d in m.differences if d.status == 'open')} open difference(s).")

    # ========================================================== refresh
    def refresh_all(self, keep_id=None):
        self.split_tree.refresh()
        self.pages_panel.refresh()
        self.scores_panel.refresh()
        self.diff_panel.refresh(keep_id=keep_id)
        if self.current_split and self.current_split not in self.session.mgr.split_paths():
            self.current_split = None
        self.rendered.show_split(self.current_split, force=True)
        self.source.show_split(self.current_split, force=True)
        self.pdf_view.render()
        if self.mode.get() == "overlay":
            self.overlay.render()

    def _redraw(self):
        if self.session is None:
            return
        self.rendered.show_split(self.current_split, force=True)
        self.pdf_view.render()

    def after_change(self, message, reanalyse=False, keep_id=None):
        """Called after every user action. Session operations already
        re-compare incrementally (only changed splits are re-parsed); a
        direct source edit or RE-COMPARE passes reanalyse=True. Decisions
        (reject / ignore / reopen) only need the scores recomputed."""
        if self.session is None:
            return
        self.win.configure(cursor="watch")
        self.win.update_idletasks()
        try:
            if reanalyse:
                self.session.refresh()
            elif self.session.mapping is not None:
                self.session.mapping._scores()
        finally:
            self.win.configure(cursor="")
        self.refresh_all(keep_id=keep_id)
        self.set_status(message + f"   ·   Overall {self.session.mapping.scores.get('Overall')}%")

    def update_counter(self):
        n = len(self.diff_panel.items)
        k = self.diff_panel.index()
        self.counter.configure(text=f"Difference {k + 1 if k >= 0 else 0}/{n}")

    # ========================================================= navigation
    def show_split(self, rel):
        if rel == self.current_split:
            return
        self.current_split = rel
        self.rendered.show_split(rel)
        self.source.show_split(rel)
        self.split_tree.select(rel)
        self.scores_panel.update_current()
        if self.diff_panel.split_only.get():
            self.diff_panel.refresh()

    def goto_pdf_page(self, page, bboxes=None):
        s = self.session
        if s is None or s.pdf is None:
            return
        page = max(1, min(page, len(s.pdf.pages)))
        self.pdf_view.show(page, bboxes)
        self.pages_panel.select_page(page)
        self.scores_panel.update_current()
        if self.mode.get() == "overlay":
            self.overlay.render()

    def goto_xhtml_word(self, j, from_pdf=False):
        s = self.session
        if s is None or not s.xhtml.words:
            return
        j = max(0, min(j, len(s.xhtml.words) - 1))
        self._current_word = j
        split = s.xhtml.split_of_word(j)
        self.show_split(split)
        self.rendered.focus_words(j)
        blk = s.xhtml.block_of_word(j)
        if blk is not None:
            self.source.show_line(blk.sourceline)
        if not from_pdf:
            page, bbox = s.mapping.location_for_xhtml(j)
            self.goto_pdf_page(page, [bbox] if bbox else None)

    def sync_pdf_to_word(self, j):
        """Rendered view scrolled: bring the PDF to the page of word j."""
        s = self.session
        if s is None or s.mapping is None:
            return
        page, _bbox = s.mapping.location_for_xhtml(j)
        if page != self.pdf_view.page:
            self.goto_pdf_page(page)

    def goto_pdf_from_word(self, j):
        s = self.session
        if s is None or s.mapping is None:
            return
        self._current_word = j
        self.rendered.focus_words(j)
        blk = s.xhtml.block_of_word(j)
        if blk is not None:
            self.source.show_line(blk.sourceline)
        page, bbox = s.mapping.location_for_xhtml(j)
        self.goto_pdf_page(page, [bbox] if bbox else None)
        for d in s.differences:
            if d.x_start is not None and d.x_end is not None and d.x_start <= j < max(d.x_end, d.x_start + 1):
                if self.diff_panel.select_diff(d):
                    break

    def goto_from_source_line(self, split, line):
        s = self.session
        if s is None or not split:
            return
        best = None
        for blk in s.xhtml.blocks:
            if blk.split == split and blk.word_end > blk.word_start and blk.sourceline and blk.sourceline <= line:
                if best is None or blk.sourceline >= best.sourceline:
                    best = blk
        if best is not None:
            self._current_word = best.word_start
            self.rendered.focus_words(best.word_start, best.word_end)
            page, bbox = s.mapping.location_for_xhtml(best.word_start)
            self.goto_pdf_page(page, [bbox] if bbox else None)

    def show_marker(self, k):
        s = self.session
        mk = s.xhtml.markers[k]
        st = s.mapping.marker_state.get(k)
        for d in s.differences:
            if d.category == "page" and d.split == mk.split and d.x_start == mk.word_pos:
                self.diff_panel.select_diff(d)
                return
        for pm in s.mapping.pages:
            if pm.marker is mk:
                self.goto_pdf_page(pm.page)
                break
        self.set_status(f"Page marker '{mk.label}' (id {mk.id}) - {STATE_LABELS.get(st, 'not compared')}")

    def show_pdf_image(self, img):
        s = self.session
        for d in s.differences:
            if d.image and d.image.get("pdf_uid") == img.uid:
                if not self.diff_panel.select_diff(d):
                    self.diff_panel.status_var.set("all")
                    self.diff_panel.select_diff(d)
                return
        pair = next((pr for pr in s.mapping.image_pairs if pr.get("pdf_uid") == img.uid), None)
        if pair and pair.get("xhtml_index") is not None:
            xi = s.xhtml.images[pair["xhtml_index"]]
            self.show_split(xi.split)
            self.goto_xhtml_word(min(xi.word_pos, len(s.xhtml.words) - 1), from_pdf=True)
        self.set_status(f"PDF image {img.uid}: {STATE_LABELS[pair['state']] if pair else 'unmapped'}")

    def show_xhtml_image(self, k):
        s = self.session
        xi = s.xhtml.images[k]
        for d in s.differences:
            if d.image and d.image.get("xhtml_src") == xi.src and d.split == xi.split:
                self.diff_panel.select_diff(d)
                return
        pair = next((pr for pr in s.mapping.image_pairs if pr.get("xhtml_index") == k), None)
        if pair and pair.get("pdf_page"):
            self.goto_pdf_page(pair["pdf_page"], [pair.get("pdf_bbox")])
        self.set_status(f"XHTML image {xi.src}: {STATE_LABELS[pair['state']] if pair else 'unmapped'}")

    def navigate_to_difference(self, d):
        s = self.session
        if d.split:
            self.show_split(d.split)
        if d.x_start is not None and s.xhtml.words:
            j = min(d.x_start, len(s.xhtml.words) - 1)
            self._current_word = j
            if d.x_end and d.x_end > d.x_start:
                self.rendered.focus_words(j, d.x_end)
            else:
                self.rendered.focus_words(j)
        if d.sourceline:
            self.source.show_line(d.sourceline)
        if d.pdf_page:
            self.goto_pdf_page(d.pdf_page, d.pdf_bboxes[:20])
        self.update_counter()

    def step_difference(self, step):
        if not self.diff_panel.items:
            return
        k = self.diff_panel.index()
        self.diff_panel.select((k + step) % len(self.diff_panel.items) if k >= 0 else 0)

    def current_word(self):
        return self._current_word

    # ============================================================== view
    def _apply_mode(self):
        if self.mode.get() == "overlay":
            self.side.pack_forget()
            self.overlay.pack(fill=tk.BOTH, expand=True)
            self.overlay.render()
        else:
            self.overlay.stop()
            self.overlay.pack_forget()
            self.side.pack(fill=tk.BOTH, expand=True)

    def zoom(self, factor):
        self.pdf_view.fit = None
        self.pdf_view.zoom = max(0.3, min(5.0, self.pdf_view.zoom * factor))
        self.pdf_view.render()

    def fit(self, how):
        self.pdf_view.fit = how
        self.pdf_view.render()

    def search(self):
        s = self.session
        q = self.search_var.get().strip()
        if s is None or not q:
            return
        if getattr(self, "_last_query", None) != q:
            from core.qc import textnorm
            keys = [textnorm.key(w) for w in textnorm.words(q)]
            hits = []
            X = s.xhtml.words
            for j in range(len(X) - len(keys) + 1):
                if all(X[j + k].key == keys[k] for k in range(len(keys))):
                    hits.append(("x", j))
            if s.pdf is not None:
                xs = set(j for _t, j in hits)
                for page, i in s.pdf.search(q):
                    j = s.mapping.p2x[i] if s.mapping.p2x[i] >= 0 else None
                    if j is None or j not in xs:
                        hits.append(("p", i))
            self._search_hits, self._search_i, self._last_query = hits, -1, q
        if not self._search_hits:
            self.set_status(f"'{q}' not found in the PDF or the XHTML")
            return
        self._search_i = (self._search_i + 1) % len(self._search_hits)
        kind, idx = self._search_hits[self._search_i]
        if kind == "x":
            self.goto_xhtml_word(idx)
            where = "XHTML (+ mapped PDF)"
        else:
            w = s.pdf.words[idx]
            self.goto_pdf_page(w.page, [w.bbox])
            where = f"PDF page {w.page} only (missing from XHTML)"
        self.set_status(f"Match {self._search_i + 1}/{len(self._search_hits)} for '{q}' - {where}")

    def edit_colors(self):
        from tkinter import colorchooser
        dlg = tk.Toplevel(self.win)
        dlg.title("Comparison colours")
        for k, state in enumerate(self.colors):
            tk.Label(dlg, text=STATE_LABELS[state], width=12, anchor="w").grid(row=k, column=0, padx=6, pady=2)
            sw = tk.Label(dlg, text="      ", bg=self.colors[state])
            sw.grid(row=k, column=1)

            def pick(st=state, swatch=sw):
                _rgb, col = colorchooser.askcolor(self.colors[st], parent=dlg)
                if col:
                    self.colors[st] = col.upper()
                    swatch.configure(bg=col)
            ttk.Button(dlg, text="Change…", command=pick).grid(row=k, column=2, padx=6)

        def done(reset=False):
            if reset:
                self.colors.update(DEFAULT_COLORS)
            _save_colors(self.colors)
            self._legend()
            if self.session:
                self.refresh_all()
            dlg.destroy()
        row = len(self.colors)
        ttk.Button(dlg, text="Reset defaults", command=lambda: done(True)).grid(row=row, column=0, pady=6)
        ttk.Button(dlg, text="Save", command=done).grid(row=row, column=2, pady=6)

    # ============================================================ actions
    def _need_session(self):
        if self.session is None or self.session.mapping is None:
            messagebox.showinfo("QC", "Open and compare a PDF and an EPUB first.", parent=self.win)
            return False
        return True

    def auto_correct(self):
        if not self._need_session():
            return
        n = self.session.auto_correct()
        self.after_change(f"Auto-corrected {n} high-confidence structural difference(s) (one undo step)"
                          if n else "Nothing to auto-correct (text is never changed automatically)")

    def undo(self):
        if self.session is None:
            return
        name = self.session.undo()
        self.after_change(f"Undid: {name}" if name else "Nothing to undo")

    def redo(self):
        if self.session is None:
            return
        name = self.session.redo()
        self.after_change(f"Redid: {name}" if name else "Nothing to redo")

    def recompare(self):
        if self._need_session():
            self.after_change("Re-compared", reanalyse=True)

    def validate(self):
        if not self._need_session():
            return
        self.win.configure(cursor="watch")
        self.win.update_idletasks()
        try:
            rep = self.session.final_validation()
        finally:
            self.win.configure(cursor="")
        self._text_dialog("Final validation", rep.to_text())

    def export_report(self):
        if not self._need_session():
            return
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".html", filetypes=[
            ("HTML report", "*.html"), ("JSON", "*.json"), ("CSV", "*.csv"), ("PDF", "*.pdf")])
        if path:
            self.session.export_report(path)
            self.set_status(f"Report written: {path}")

    def build_epub(self):
        if not self._need_session():
            return
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".epub",
                                            filetypes=[("EPUB", "*.epub")])
        if not path:
            return
        out, rep = self.session.build_epub(path, require_valid=False)
        msg = rep.to_text()
        if rep.errors:
            msg = "EPUB written, but final validation reported errors:\n\n" + msg
        self._text_dialog(f"Build EPUB - {os.path.basename(path)}", msg)
        self.set_status(f"EPUB written: {out} - validating ...")
        # closed loop on the PACKAGED file: EPUBCheck -> safe repairs -> EPUBCheck ... (PDF-aware)
        from app.validation import autofix_dashboard
        autofix_dashboard.run_in_background(self.win, out, pdf_path=self.pdf_var.get().strip() or None,
                                            on_status=self.set_status, launcher_root=self.launcher_root,
                                            on_home=self.on_home)

    def _text_dialog(self, title, text):
        dlg = tk.Toplevel(self.win)
        dlg.title(title)
        t = tk.Text(dlg, width=100, height=34, font=("Consolas", 9))
        t.insert("1.0", text)
        t.configure(state="disabled")
        t.pack(fill=tk.BOTH, expand=True)
        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=4)

    def _go_home(self):
        if self._worker:
            self._worker.cancel()
        self.overlay.stop()
        self.win.destroy()
        if self.on_home:
            self.on_home()


def open_window(launcher_root, on_home, pdf_path=None, epub_path=None, master=None):
    return QCWorkspace(launcher_root, on_home, pdf_path=pdf_path, epub_path=epub_path, master=master)
