"""PDF <-> XML compare window (BITS Tool) - originally the Advanced Fidelity Compare (spec: "ADVANCED
FIDELITY + CONTENT + UNICODE + LAYOUT COMPARISON") - the real
implementation of the Launcher's COMPARISON card (previously a
placeholder - see app/comparison/comparison_window.py's own docstring on
why upgrading it rather than adding a redundant fourth launcher card was
the right call: its own description already promised exactly this
feature).

A completely independent, READ-ONLY subsystem (core/fidelity_compare/) -
this window only ever calls engine.run_full_comparison(), which only
ever reads the three input files. It never zones, never re-runs OCR,
never touches Reading Order/Split/Merge/XHTML/XML generation/Package
Builder/Validation in any way (spec's own repeated "DO NOT MODIFY" list).

Mirrors app/validation/validation_window.py's own "<- Home" Toplevel
pattern (same as the other two Launcher modules)."""
import os
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from tkinter import font as tkfont

from app.comparison.difference_panel import DifferencePanel
from app.comparison.fidelity_dashboard import FidelityDashboard
from app.comparison.layout_difference_panel import LayoutDifferencePanel
from app.comparison.report_viewer import ReportViewer
from app.comparison.synchronized_pdf_viewer import SynchronizedPdfViewer
from core.fidelity_compare import engine
from core.fidelity_compare.worker import ComparisonWorker
from core.fidelity_compare.pdf_xhtml_compare import compare_pdf_to_xhtml
from gui import theme

POLL_INTERVAL_MS = 150


class FidelityCompareWindow:
    def __init__(self, launcher_root: tk.Tk, on_home):
        self.launcher_root = launcher_root
        self.on_home = on_home
        palette = theme.current.palette
        self.win = tk.Toplevel(launcher_root)
        self.win.title("BITS Tool - PDF \u2194 XML Compare")
        self.win.geometry("1300x860")
        self.win.configure(bg=palette["app_bg"])
        self.win.protocol("WM_DELETE_WINDOW", self._go_home)

        self._worker = None
        self._result = None
        self._original_path = tk.StringVar()
        self._epub_path = tk.StringVar()
        self._converted_path = tk.StringVar()
        self._xhtml_result = None
        self._xhtml_issues = []
        # Comparison mode (spec: "ZONETOOL - ADVANCED PDF COMPARISON &
        # PRODUCTION QA ENGINE" - "[OPEN ORIGINAL PDF] [OPEN GENERATED PDF]"
        # - a direct PDF<->PDF mode with NO EPUB required, additive to the
        # existing 3-input Full Fidelity mode, never a second window/engine.
        self._mode_var = tk.StringVar(value="xhtml")      # PDF -> XML proof (BITS / JATS)

        self._build_header(palette)
        self._build_inputs(palette)
        self._build_body(palette)
        self._apply_mode()

    # ---------------- layout ----------------

    def _build_header(self, palette):
        header = tk.Frame(self.win, bg=palette["header_bg"], height=40)
        header.pack(side=tk.TOP, fill=tk.X)
        header.pack_propagate(False)
        left = tk.Frame(header, bg=palette["header_bg"])
        left.pack(side=tk.LEFT, padx=14)
        home_btn = tk.Label(left, text="< Home", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                             font=theme.FONT_BODY_BOLD, cursor="hand2", padx=6)
        home_btn.pack(side=tk.LEFT, padx=(0, 10))
        home_btn.bind("<Button-1>", lambda e: self._go_home())
        tk.Label(left, text="◈ BITS Tool - PDF \u2194 XML Compare", bg=palette["header_bg"],
                 fg=palette["header_fg"], font=theme.FONT_APP_TITLE).pack(side=tk.LEFT)

    def _build_inputs(self, palette):
        frame = tk.Frame(self.win, bg=palette["panel_bg"])
        frame.pack(fill=tk.X, padx=10, pady=8)

        mode_row = tk.Frame(frame, bg=palette["panel_bg"])
        mode_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(mode_row, text="Comparison Mode:", bg=palette["panel_bg"], fg=palette["text"],
                 font=theme.FONT_BODY_BOLD).pack(side=tk.LEFT, padx=(0, 10))
        tk.Radiobutton(mode_row, text="PDF <-> PDF (Direct)", variable=self._mode_var, value="direct",
                        bg=palette["panel_bg"], fg=palette["text"], selectcolor=palette["surface"],
                        command=self._apply_mode).pack(side=tk.LEFT, padx=(0, 12))
        tk.Radiobutton(mode_row, text="PDF -> XML (BITS / JATS proof)", variable=self._mode_var,
                        value="xhtml", bg=palette["panel_bg"], fg=palette["text"], selectcolor=palette["surface"],
                        command=self._apply_mode).pack(side=tk.LEFT, padx=(12, 0))

        self._original_row = self._path_row(frame, "Original PDF:", self._original_path, [("PDF files", "*.pdf")])
        self._epub_row = self._path_row(frame, "Generated XML:", self._epub_path,
                                        [("XML files", "*.xml"), ("All files", "*.*")])
        self._converted_row = self._path_row(
            frame, "Generated PDF:", self._converted_path, [("PDF files", "*.pdf")])

        action_row = tk.Frame(frame, bg=palette["panel_bg"])
        action_row.pack(fill=tk.X, pady=(6, 0))
        self.start_btn = tk.Button(action_row, text="Start Comparison", command=self._start,
                                    bg=palette["accent"], fg=palette["accent_fg"], relief=tk.FLAT,
                                    font=theme.FONT_BODY_BOLD, padx=14, pady=6)
        self.start_btn.pack(side=tk.LEFT)
        self.cancel_btn = tk.Button(action_row, text="Cancel Comparison", command=self._cancel,
                                     state=tk.DISABLED, padx=14, pady=6)
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.progress_label = tk.Label(action_row, text="", bg=palette["panel_bg"], fg=palette["text_muted"],
                                        font=theme.FONT_BODY)
        self.progress_label.pack(side=tk.LEFT, padx=12)
        self.save_xhtml_btn = tk.Button(
            action_row, text="Save XML Report", command=self._save_xhtml_report,
            state=tk.DISABLED, padx=12, pady=6
        )
        self.save_xhtml_btn.pack(side=tk.RIGHT)

    def _path_row(self, parent, label, var, filetypes):
        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=label, width=16, anchor="w", bg=parent["bg"]).pack(side=tk.LEFT)
        entry = tk.Entry(row, textvariable=var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        tk.Button(row, text="Browse...", command=lambda: self._browse(var, filetypes)).pack(side=tk.LEFT)
        return row

    def _apply_mode(self):
        """Switch between existing PDF modes and direct PDF -> XHTML QA."""
        mode = self._mode_var.get()

        if mode == "direct":
            self._epub_row.pack_forget()
            self._converted_row.pack(fill=tk.X, pady=2)
            self.start_btn.configure(text="Start PDF Comparison")
            self.save_xhtml_btn.configure(state=tk.DISABLED)
            self.progress_label.configure(text="PDF ↔ PDF")
            try:
                self.xhtml_frame.pack_forget()
                self._notebook.pack(fill=tk.BOTH, expand=True)
                self.detail_text.pack(fill=tk.X, pady=(6, 0))
                self.viewer.pack(fill=tk.BOTH, expand=True)
            except Exception:
                pass

        elif mode == "full":
            self._epub_row.pack(fill=tk.X, pady=2, after=self._original_row)
            self._converted_row.pack(fill=tk.X, pady=2)
            self.start_btn.configure(text="Start Full Fidelity")
            self.save_xhtml_btn.configure(state=tk.DISABLED)
            self.progress_label.configure(text="PDF ↔ EPUB ↔ PDF")
            try:
                self.xhtml_frame.pack_forget()
                self._notebook.pack(fill=tk.BOTH, expand=True)
                self.detail_text.pack(fill=tk.X, pady=(6, 0))
                self.viewer.pack(fill=tk.BOTH, expand=True)
            except Exception:
                pass

        else:
            self._epub_row.pack(fill=tk.X, pady=2, after=self._original_row)
            # the generated BITS / JATS XML file
            self._converted_row.pack_forget()
            self.start_btn.configure(text="Compare PDF → XML")
            self.save_xhtml_btn.configure(state=tk.DISABLED)
            self.progress_label.configure(
                text="Word-by-word proof of the XML against the PDF"
            )

            try:
                self._notebook.pack_forget()
                self.detail_text.pack_forget()
                self.xhtml_frame.pack(fill=tk.BOTH, expand=True)
                self.viewer.pack(fill=tk.BOTH, expand=True)
            except Exception:
                pass

    def _browse(self, var, filetypes):
        path = filedialog.askopenfilename(
            parent=self.win,
            filetypes=filetypes
        )
        if path:
            var.set(path)

    def _build_body(self, palette):
        self.dashboard = FidelityDashboard(self.win, on_card_click=self._on_summary_card_click)
        self.dashboard.pack(fill=tk.X, padx=10, pady=(0, 6))

        # View-mode toolbar (spec: "ADVANCED PDF COMPARISON / PRODUCTION
        # PROOFING" section 2 - "Do NOT permanently consume half the
        # window with an error list. The error panel must be collapsible.
        # Provide: [Show Errors] [Hide Errors]"). Toggling only shows/hides
        # self._left_frame in the PanedWindow below - the difference
        # panel/layout panel/detail text widgets themselves are never
        # destroyed, so all their state (filter, selection, scroll
        # position) survives being hidden and shown again.
        view_bar = tk.Frame(self.win, bg=palette["app_bg"])
        view_bar.pack(fill=tk.X, padx=10, pady=(0, 4))
        self._errors_visible = True
        self.toggle_errors_btn = tk.Button(view_bar, text="Hide Errors", command=self._toggle_error_panel)
        self.toggle_errors_btn.pack(side=tk.LEFT)
        tk.Label(view_bar, text="Search:", bg=palette["app_bg"], fg=palette["text"]).pack(side=tk.LEFT, padx=(16, 4))
        self._search_var = tk.StringVar()
        search_entry = tk.Entry(view_bar, textvariable=self._search_var, width=30)
        search_entry.pack(side=tk.LEFT)
        search_entry.bind("<Return>", lambda e: self._run_search())
        tk.Button(view_bar, text="Search Differences", command=self._run_search).pack(side=tk.LEFT, padx=(6, 0))
        self.search_status_lbl = tk.Label(view_bar, text="", bg=palette["app_bg"], fg=palette["text_muted"])
        self.search_status_lbl.pack(side=tk.LEFT, padx=(10, 0))

        # Direct PDF -> XHTML proof controls.
        self._proof_highlight_mode = "word"
        self.word_highlight_btn = tk.Button(
            view_bar, text="Word Highlight",
            command=lambda: self._set_proof_highlight_mode("word")
        )
        self.word_highlight_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self.paragraph_highlight_btn = tk.Button(
            view_bar, text="Paragraph Highlight",
            command=lambda: self._set_proof_highlight_mode("paragraph")
        )
        self.paragraph_highlight_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self.fullscreen_proof_btn = tk.Button(
            view_bar, text="Full Screen Proof",
            command=self._open_fullscreen_proof
        )
        self.fullscreen_proof_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self._paned = ttk.Panedwindow(self.win, orient=tk.HORIZONTAL)
        self._paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        self._left_frame = ttk.Frame(self._paned)
        self._paned.add(self._left_frame, weight=1)
        notebook = ttk.Notebook(self._left_frame)
        self._notebook = notebook
        notebook.pack(fill=tk.BOTH, expand=True)

        diff_tab = ttk.Frame(notebook)
        self.difference_panel = DifferencePanel(diff_tab, on_select=self._on_difference_selected)
        self.difference_panel.pack(fill=tk.BOTH, expand=True)
        notebook.add(diff_tab, text="All Differences")

        layout_tab = ttk.Frame(notebook)
        self.layout_panel = LayoutDifferencePanel(layout_tab, on_select=self._on_difference_selected)
        self.layout_panel.pack(fill=tk.BOTH, expand=True)
        notebook.add(layout_tab, text="Layout Differences")

        self.detail_text = tk.Text(self._left_frame, height=10, wrap=tk.WORD, bg=palette["surface"],
                                    fg=palette["text"], relief=tk.FLAT, font=theme.FONT_BODY)
        self.detail_text.pack(fill=tk.X, pady=(6, 0))
        self.detail_text.insert("1.0", "Select a difference to see full detail here.")
        self.detail_text.configure(state=tk.DISABLED)

        right_frame = ttk.Frame(self._paned)
        # weight=2 (vs the error panel's weight=1 above) approximates the
        # spec's own "each viewer ~50% of the width" once BOTH sides are
        # shown - Hide Errors then gives the viewer effectively the whole
        # paned area, satisfying "PDF View Only" without a separate,
        # redundant third layout mode.
        self._paned.add(right_frame, weight=2)
        # Existing PDF viewer remains available. In PDF -> XHTML mode it
        # displays the original PDF on both sides only as a navigation/
        # bounding-box proof surface; the actual XHTML proof is shown below.
        self.viewer = SynchronizedPdfViewer(right_frame)
        self.viewer.pack(fill=tk.BOTH, expand=True)

        self.xhtml_frame = tk.Frame(self._left_frame, bg=palette["surface"])
        self.xhtml_header = tk.Label(
            self.xhtml_frame,
            text="PDF → XHTML PROOF — RED = MISSING/WRONG/SYMBOL   YELLOW = MODIFIED   GREEN = MATCH",
            bg=palette["surface"],
            fg=palette["text"],
            font=theme.FONT_BODY_BOLD,
            anchor="w",
            padx=8,
            pady=6,
        )
        self.xhtml_header.pack(fill=tk.X)

        xbody = tk.PanedWindow(
            self.xhtml_frame,
            orient=tk.HORIZONTAL,
            sashrelief=tk.RAISED,
            bg=palette["border"],
        )
        xbody.pack(fill=tk.BOTH, expand=True)

        issue_box = tk.Frame(xbody, bg=palette["surface"])
        proof_box = tk.Frame(xbody, bg=palette["surface"])
        xbody.add(issue_box, minsize=420)
        xbody.add(proof_box, minsize=420)

        tk.Label(
            issue_box, text="DIRECT PDF → XHTML ISSUES",
            bg=palette["surface"], fg=palette["text"],
            font=theme.FONT_BODY_BOLD, anchor="w"
        ).pack(fill=tk.X, padx=6, pady=(6, 2))

        self.xhtml_tree = ttk.Treeview(
            issue_box,
            columns=("type", "pdf", "bbox", "file", "line", "pdftext", "xtext"),
            show="headings",
            selectmode="browse",
        )
        for col, title, width in (
            ("type", "TYPE", 130),
            ("pdf", "PDF PAGE", 70),
            ("bbox", "PDF BBOX", 150),
            ("file", "XHTML FILE", 170),
            ("line", "LINE", 60),
            ("pdftext", "PDF TEXT", 180),
            ("xtext", "XHTML TEXT", 180),
        ):
            self.xhtml_tree.heading(col, text=title)
            self.xhtml_tree.column(col, width=width, anchor="w")
        self.xhtml_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.xhtml_tree.bind("<<TreeviewSelect>>", self._on_xhtml_tree_select)
        self.xhtml_tree.tag_configure("match", background="#bbf7d0", foreground="#166534")
        self.xhtml_tree.tag_configure("error", background="#fecaca", foreground="#991b1b")
        self.xhtml_tree.tag_configure("modified", background="#fde68a", foreground="#92400e")

        # Proof area: two synchronized textual proof panes. The PDF image
        # viewer remains on the right side of the main window; these panes
        # provide exact token-by-token GREEN/YELLOW/RED proof for both sides.
        proof_paned = tk.PanedWindow(
            proof_box, orient=tk.HORIZONTAL,
            sashrelief=tk.RAISED, bg=palette["border"]
        )
        proof_paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        pdf_proof_box = tk.Frame(proof_paned, bg=palette["surface"])
        xhtml_proof_box = tk.Frame(proof_paned, bg=palette["surface"])
        proof_paned.add(pdf_proof_box, minsize=300)
        proof_paned.add(xhtml_proof_box, minsize=300)

        tk.Label(
            pdf_proof_box, text="ORIGINAL PDF TEXT PROOF",
            bg=palette["surface"], fg=palette["text"],
            font=theme.FONT_BODY_BOLD, anchor="w"
        ).pack(fill=tk.X, padx=4, pady=(2, 2))

        tk.Label(
            xhtml_proof_box, text="GENERATED XHTML TEXT PROOF",
            bg=palette["surface"], fg=palette["text"],
            font=theme.FONT_BODY_BOLD, anchor="w"
        ).pack(fill=tk.X, padx=4, pady=(2, 2))

        self.pdf_proof = ScrolledText(
            pdf_proof_box, wrap=tk.WORD, font=("Times New Roman", 11),
            bg="#fbfbfb", fg=palette["text"], relief=tk.FLAT
        )
        self.pdf_proof.pack(fill=tk.BOTH, expand=True)

        self.xhtml_proof = ScrolledText(
            xhtml_proof_box, wrap=tk.WORD, font=("Times New Roman", 11),
            bg="#fbfbfb", fg=palette["text"], relief=tk.FLAT
        )
        self.xhtml_proof.pack(fill=tk.BOTH, expand=True)

        for widget in (self.pdf_proof, self.xhtml_proof):
            widget.tag_configure("match", background="#bbf7d0", foreground="#166534")
            widget.tag_configure("error", background="#fecaca", foreground="#991b1b")
            widget.tag_configure("modified", background="#fde68a", foreground="#92400e")
            widget.tag_configure("selected", background="#93c5fd", foreground="#111827",
                                 font=("Consolas", 10, "bold"))
            widget.tag_configure("paragraph_selected", background="#fca5a5", foreground="#111827")
            widget.configure(state=tk.DISABLED)

        # Exact Tk ranges for every proof token.  This prevents repeated words
        # such as "of", "the", "Communities", etc. from highlighting the first
        # occurrence instead of the actual matched occurrence.
        self._proof_token_ranges = {"pdf": {}, "xhtml": {}}
        self._proof_paragraph_ranges = {"pdf": {}, "xhtml": {}}
        self._last_xhtml_issue = None
        self._fullscreen_proof = None

        self.xhtml_frame.pack_forget()

        self.report_viewer = ReportViewer(self.win)
        self.report_viewer.pack(fill=tk.X, padx=10, pady=(0, 8))

    def _toggle_error_panel(self):
        if self._errors_visible:
            self._paned.forget(self._left_frame)
            self.toggle_errors_btn.configure(text="Show Errors")
        else:
            self._paned.insert(0, self._left_frame, weight=1)
            self.toggle_errors_btn.configure(text="Hide Errors")
        self._errors_visible = not self._errors_visible

    def _run_search(self):
        """Spec section 69: "Search Text / Search Difference / Search
        Error Type / Search Page / Search Unicode" - one text box matches
        against everything a difference already exposes (original/
        converted text, type, category, page numbers, Unicode names/code
        points) rather than four separate search boxes, since a single
        query like "U+03B1" or "84" or "missing" is already unambiguous
        against these fields. Delegates to difference_panel.search(),
        which combines the query with the ACTIVE category filter without
        ever mutating the panel's own full difference list - a card click
        or filter change right after a search still sees everything. If
        the error panel is currently hidden, showing it is implied by the
        user asking to search it."""
        query = self._search_var.get().strip()
        if not query or self._result is None:
            return
        if not self._errors_visible:
            self._toggle_error_panel()
        n = self.difference_panel.search(query)
        self.search_status_lbl.configure(
            text=f"{n} match(es) for '{query}'" if n else f"No matches for '{query}'")

    # ---------------- comparison lifecycle ----------------

    def _start(self):
        mode = self._mode_var.get()
        original_path = self._original_path.get().strip()
        epub_path = self._epub_path.get().strip()
        converted_path = self._converted_path.get().strip()

        if not original_path or not os.path.isfile(original_path):
            messagebox.showwarning(
                "Advanced Fidelity Compare",
                "Please choose a valid Original PDF file first.",
                parent=self.win,
            )
            return

        if mode == "xhtml":
            if not epub_path or not os.path.exists(epub_path):
                messagebox.showwarning(
                    "PDF → XML",
                    "Choose the generated BITS / JATS XML file.",
                    parent=self.win,
                )
                return
        else:
            required = [("Generated PDF", converted_path)]
            if mode == "full":
                required.insert(0, ("Generated EPUB", epub_path))
            for label, path in required:
                if not path or not os.path.isfile(path):
                    messagebox.showwarning(
                        "Advanced Fidelity Compare",
                        f"Please choose a valid {label} file first.",
                        parent=self.win,
                    )
                    return

        self.start_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.progress_label.configure(text="Starting...")
        self._xhtml_result = None

        if mode == "xhtml":
            self._clear_xhtml_ui()

            def _run(progress_cb, should_cancel):
                # Direct comparator is read-only. It intentionally ignores
                # cancellation during individual extraction, then returns a
                # complete deterministic result.
                return compare_pdf_to_xhtml(
                    original_path,
                    epub_path,
                )

            try:
                # Load the source PDF immediately. Both panes are the same source
                # only in this special mode; XHTML proof is shown separately.
                self.viewer.load_documents(original_path, original_path)
            except Exception:
                pass
            self._worker = ComparisonWorker(_run)
            self._worker.start()
            self.win.after(POLL_INTERVAL_MS, self._poll)
            return

        self.dashboard.show_empty()
        self.viewer.load_documents(original_path, converted_path)

        if mode == "direct":
            def _run(progress_cb, should_cancel):
                return engine.run_pdf_to_pdf_comparison(
                    original_path, converted_path,
                    progress_cb=progress_cb, should_cancel=should_cancel
                )
        else:
            def _run(progress_cb, should_cancel):
                return engine.run_full_comparison(
                    original_path, epub_path, converted_path,
                    progress_cb=progress_cb, should_cancel=should_cancel
                )

        self._worker = ComparisonWorker(_run)
        self._worker.start()
        self.win.after(POLL_INTERVAL_MS, self._poll)

    def _cancel(self):
        if self._worker:
            self._worker.cancel()
            self.progress_label.configure(text="Cancelling...")

    def _poll(self):
        if not self._worker:
            return
        for msg in self._worker.poll():
            kind = msg[0]
            if kind == "progress":
                _, stage, current, total = msg
                self.progress_label.configure(text=f"{stage}" + (f" ({current}/{total})" if total > 1 else ""))
            elif kind == "done":
                self._on_done(msg[1])
                return
            elif kind == "error":
                self._on_error(msg[1])
                return
        self.win.after(POLL_INTERVAL_MS, self._poll)

    def _on_done(self, result):
        mode = self._mode_var.get()
        self.start_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)

        if mode == "xhtml":
            self._xhtml_result = result
            self.save_xhtml_btn.configure(state=tk.NORMAL)
            self.progress_label.configure(
                text=(
                    f"Complete — {result.total_changes} issue(s) • "
                    f"{result.fidelity_percent:.2f}% fidelity"
                )
            )
            self._populate_xhtml_results(result)
            return

        if result.get("cancelled"):
            self.progress_label.configure(
                text="Cancelled - no files were modified."
            )
            return

        self._result = result
        self.progress_label.configure(
            text=f"Done - {len(result['differences'])} difference(s) found."
        )
        self.dashboard.show_scores(
            result["scores"].to_dict(),
            result.get("production_status"),
            result.get("summary")
        )
        self.difference_panel.set_differences(result["differences"])
        self.layout_panel.set_differences(result["differences"])
        self.viewer.update_page_groups(result["page_groups"])
        self.report_viewer.set_result(
            result,
            self._original_path.get(),
            self._converted_path.get()
        )

    def _clear_xhtml_ui(self):
        for item in self.xhtml_tree.get_children():
            self.xhtml_tree.delete(item)

        self._xhtml_issues = []

        for widget, title in (
            (self.pdf_proof, "Waiting for direct PDF → XHTML comparison..."),
            (self.xhtml_proof, "Waiting for direct PDF → XHTML comparison..."),
        ):
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert(
                "1.0",
                f"{title}\n\n"
                "GREEN  = exact content/style match\n"
                "RED    = missing / extra / wrong Unicode / wrong content\n"
                "YELLOW = formatting mismatch (italic/bold/underline/super/sub)\n"
            )
            widget.configure(state=tk.DISABLED)

    @staticmethod
    def _xhtml_issue_tag(issue):
        kind = str(getattr(issue, "kind", "") or "").upper()
        if (
            "MISSING" in kind
            or "EXTRA" in kind
            or "SYMBOL" in kind
            or kind in {"CHANGED", "WRONG", "UNICODE_MISMATCH"}
        ):
            return "error"
        if (
            "MISMATCH" in kind
            or kind in {"ORDER", "MODIFIED"}
        ):
            return "modified"
        return "match"

    def _xhtml_style_tag(self, widget, token):
        """Apply the XHTML token's real typography to the proof pane.

        The old proof pane displayed every token as plain Consolas text, which
        made valid <i>/<b>/<u>/<sup>/<sub> markup look wrong even when the
        comparator correctly detected it.  This keeps the proof text readable
        while visually reproducing the important inline XHTML styles.
        """
        style = getattr(token, "style", {}) or {}
        key = (
            bool(style.get("bold")), bool(style.get("italic")),
            bool(style.get("underline")), bool(style.get("superscript")),
            bool(style.get("subscript")),
        )
        tag = "xhtml_style_" + "_".join("1" if x else "0" for x in key)
        if tag not in self._proof_fonts:
            base_size = 11
            size = 9 if (key[3] or key[4]) else base_size
            weight = "bold" if key[0] else "normal"
            slant = "italic" if key[1] else "roman"
            underline = "underline" if key[2] else False
            self._proof_fonts[tag] = tkfont.Font(
                family="Times New Roman", size=size,
                weight=weight, slant=slant, underline=underline
            )
        try:
            widget.tag_configure(tag, font=self._proof_fonts[tag],
                                 offset=("3p" if key[3] else "-2p" if key[4] else "0p"))
        except Exception:
            widget.tag_configure(tag, font=self._proof_fonts[tag])
        return tag

    def _render_token_stream(self, widget, tokens, side):
        """Render proof tokens and retain exact Tk ranges for navigation.

        Each token gets an exact start/end range keyed by id(token), and each
        page/paragraph gets a range.  This is deliberately range-based rather
        than using Text.search(), because repeated words are common in books.
        """
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)

        self._proof_token_ranges.setdefault(side, {}).clear()
        self._proof_paragraph_ranges.setdefault(side, {}).clear()

        if not tokens:
            widget.insert("1.0", "No proof tokens available.")
            widget.configure(state=tk.DISABLED)
            return

        previous_page = None
        previous_para = None
        previous_file = None
        paragraph_start = None
        paragraph_key = None

        def finish_paragraph(end_index):
            nonlocal paragraph_start, paragraph_key
            if paragraph_key is not None and paragraph_start is not None:
                self._proof_paragraph_ranges[side][paragraph_key] = (
                    paragraph_start, end_index
                )
            paragraph_start = None
            paragraph_key = None

        for token in tokens:
            page = getattr(token, "page", None)
            para = getattr(token, "paragraph", None)
            file_name = getattr(token, "file", None)
            word_index = getattr(token, "word_index", None)

            current_group = (
                page if side == "pdf" else file_name,
                para,
            )

            if side == "pdf":
                if page != previous_page:
                    if paragraph_key is not None:
                        finish_paragraph(widget.index(tk.END))
                    if previous_page is not None:
                        widget.insert(tk.END, "\n")
                    widget.insert(tk.END, f"\n--- PDF PAGE {page} ---\n")
                    previous_page = page
                    previous_para = None
            else:
                if file_name != previous_file:
                    if paragraph_key is not None:
                        finish_paragraph(widget.index(tk.END))
                    if previous_file is not None:
                        widget.insert(tk.END, "\n")
                    widget.insert(tk.END, f"\n--- XHTML {file_name} ---\n")
                    previous_file = file_name
                    previous_para = None

            if current_group != paragraph_key:
                if paragraph_key is not None:
                    finish_paragraph(widget.index(tk.END))
                widget.insert(tk.END, "\n")
                paragraph_start = widget.index(tk.END)
                paragraph_key = current_group
                previous_para = para

            status = str(getattr(token, "proof_status", "MATCH") or "MATCH").upper()
            tag = {
                "MATCH": "match",
                "ERROR": "error",
                "STYLE": "modified",
            }.get(status, "match")

            start_index = widget.index(tk.END)
            token_text = str(getattr(token, "text", "") or "")
            widget.insert(tk.END, token_text)
            end_index = widget.index(tk.END)
            widget.tag_add(tag, start_index, end_index)
            if side == "xhtml":
                style_tag = self._xhtml_style_tag(widget, token)
                widget.tag_add(style_tag, start_index, end_index)

            self._proof_token_ranges[side][id(token)] = {
                "start": start_index,
                "end": end_index,
                "page": page,
                "paragraph": para,
                "file": file_name,
                "word_index": word_index,
                "token": token,
                "tag": tag,
            }

            widget.insert(tk.END, " ")

        if paragraph_key is not None:
            finish_paragraph(widget.index(tk.END))

        widget.configure(state=tk.DISABLED)

    def _set_proof_highlight_mode(self, mode):
        self._proof_highlight_mode = "paragraph" if mode == "paragraph" else "word"
        if self._last_xhtml_issue is not None:
            self._highlight_selected_xhtml_issue(self._last_xhtml_issue)

    def _clear_proof_selection(self):
        for widget in (self.pdf_proof, self.xhtml_proof):
            widget.configure(state=tk.NORMAL)
            widget.tag_remove("selected", "1.0", tk.END)
            widget.tag_remove("paragraph_selected", "1.0", tk.END)
            widget.configure(state=tk.DISABLED)

    def _highlight_exact_token(self, widget, side, token):
        if token is None:
            return False

        meta = self._proof_token_ranges.get(side, {}).get(id(token))
        if not meta:
            return False

        widget.configure(state=tk.NORMAL)
        widget.tag_add("selected", meta["start"], meta["end"])
        widget.see(meta["start"])
        widget.configure(state=tk.DISABLED)
        return True

    def _highlight_exact_paragraph(self, widget, side, token):
        if token is None:
            return False

        page = getattr(token, "page", None)
        para = getattr(token, "paragraph", None)
        file_name = getattr(token, "file", None)
        key = (page if side == "pdf" else file_name, para)

        meta = self._proof_paragraph_ranges.get(side, {}).get(key)
        if not meta:
            return False

        widget.configure(state=tk.NORMAL)
        widget.tag_add("paragraph_selected", meta[0], meta[1])
        widget.see(meta[0])
        widget.configure(state=tk.DISABLED)
        return True

    def _find_pdf_proof_token(self, issue):
        tokens = getattr(self._xhtml_result, "pdf_tokens", []) or []
        exact = getattr(issue, "pdf_token_index", None)
        if exact is not None and 0 <= exact < len(tokens):
            return tokens[exact]

        text = getattr(issue, "pdf_text", "") or ""
        page = getattr(issue, "pdf_page", None)
        bbox = getattr(issue, "pdf_bbox", None)
        best = None
        for token in tokens:
            if page is not None and getattr(token, "page", None) != page:
                continue
            if text and getattr(token, "text", "") != text:
                continue
            tb = getattr(token, "bbox", None)
            score = 0
            if bbox and tb:
                try:
                    score = sum(abs(float(a)-float(b)) for a,b in zip(tb, bbox))
                except Exception:
                    score = 999999
            if best is None or score < best[0]:
                best = (score, token)
        return best[1] if best else None

    def _find_xhtml_proof_token(self, issue):
        tokens = getattr(self._xhtml_result, "xhtml_tokens", []) or []
        exact = getattr(issue, "xhtml_token_index", None)
        if exact is not None and 0 <= exact < len(tokens):
            return tokens[exact]

        text = getattr(issue, "xhtml_text", "") or ""
        file_name = getattr(issue, "xhtml_file", None)
        para = getattr(issue, "xhtml_paragraph", None)
        word = getattr(issue, "xhtml_word", None)
        for token in tokens:
            if file_name and getattr(token, "file", None) != file_name:
                continue
            if para is not None and getattr(token, "paragraph", None) != para:
                continue
            if word is not None and getattr(token, "word_index", None) != word:
                continue
            if text and getattr(token, "text", "") != text:
                continue
            return token
        return None

    def _highlight_token_in_widget(self, widget, token):
        """Backward-compatible wrapper using exact token metadata."""
        if token is None:
            return

        side = "pdf" if widget is self.pdf_proof else "xhtml"
        self._highlight_exact_token(widget, side, token)

    def _populate_xhtml_results(self, result):
        for item in self.xhtml_tree.get_children():
            self.xhtml_tree.delete(item)

        self._xhtml_issues = list(result.differences)

        for idx, issue in enumerate(self._xhtml_issues):
            kind = issue.kind
            tag = self._xhtml_issue_tag(issue)

            self.xhtml_tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    kind,
                    issue.pdf_page or "",
                    str(issue.pdf_bbox) if issue.pdf_bbox else "",
                    os.path.basename(issue.xhtml_file) if issue.xhtml_file else "",
                    issue.xhtml_line or "",
                    issue.pdf_text,
                    issue.xhtml_text,
                ),
                tags=(tag,),
            )

        self._render_xhtml_proof(result)

        # Immediately select the first real problem so the user can see its
        # PDF page number and bounding-box highlight without another click.
        if self._xhtml_issues:
            self.xhtml_tree.selection_set("0")
            self.xhtml_tree.focus("0")
            self._on_xhtml_tree_select()

    def _render_xhtml_proof(self, result):
        # Full token proof: every aligned token is green; actual errors are
        # red; formatting-only mismatches are yellow.
        pdf_tokens = getattr(result, "pdf_tokens", []) or []
        xhtml_tokens = getattr(result, "xhtml_tokens", []) or []

        if pdf_tokens or xhtml_tokens:
            self._render_token_stream(self.pdf_proof, pdf_tokens, "pdf")
            self._render_token_stream(self.xhtml_proof, xhtml_tokens, "xhtml")
            return

        # Backward-compatible fallback for an older comparator result.
        for widget in (self.pdf_proof, self.xhtml_proof):
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert(
                tk.END,
                "PDF → XHTML DIRECT CONTENT PROOF\n"
                "================================\n\n"
                f"Fidelity: {result.fidelity_percent:.2f}%\n"
                f"Matched: {result.matched_words}\n"
                f"Missing: {result.missing_words}\n"
                f"Extra: {result.extra_words}\n"
                f"Changed: {result.changed_words}\n"
                f"Order: {result.order_changes}\n\n"
            )
            widget.configure(state=tk.DISABLED)

    def _on_xhtml_tree_select(self, _event=None):
        selection = self.xhtml_tree.selection()
        if not selection or not self._xhtml_issues:
            return

        try:
            issue = self._xhtml_issues[int(selection[0])]
        except (ValueError, IndexError):
            return

        self._highlight_selected_xhtml_issue(issue)
        if self._fullscreen_proof is not None:
            try:
                if self._fullscreen_proof.winfo_exists():
                    self._update_fullscreen_selection(issue)
            except Exception:
                pass

        # Feed a small compatible difference object to the existing PDF
        # viewer. This gives missing text a real page number and a real
        # bounding-box highlight on the source PDF. XHTML-only extras have no
        # PDF bbox, so only the source side is highlighted when available.
        if issue.pdf_page:
            diff = SimpleNamespace(
                original_page=issue.pdf_page,
                converted_page=None,
                original_bbox=(
                    SimpleNamespace(
                        x0=issue.pdf_bbox[0], y0=issue.pdf_bbox[1],
                        x1=issue.pdf_bbox[2], y1=issue.pdf_bbox[3]
                    ) if issue.pdf_bbox else None
                ),
                converted_bbox=None,
                severity=(
                    "HIGH" if issue.kind in {"MISSING", "SYMBOL_MISMATCH"}
                    else "MEDIUM"
                ),
            )
            try:
                self.viewer.show_difference(diff)
            except Exception:
                pass

    def _highlight_selected_xhtml_issue(self, issue):
        self._last_xhtml_issue = issue
        self._clear_proof_selection()

        pdf_token = self._find_pdf_proof_token(issue)
        xhtml_token = self._find_xhtml_proof_token(issue)

        if self._proof_highlight_mode == "paragraph":
            self._highlight_exact_paragraph(self.pdf_proof, "pdf", pdf_token)
            self._highlight_exact_paragraph(self.xhtml_proof, "xhtml", xhtml_token)
        else:
            self._highlight_exact_token(self.pdf_proof, "pdf", pdf_token)
            self._highlight_exact_token(self.xhtml_proof, "xhtml", xhtml_token)

        # Always retain the exact selected word on top of paragraph highlighting.
        if self._proof_highlight_mode == "paragraph":
            self._highlight_exact_token(self.pdf_proof, "pdf", pdf_token)
            self._highlight_exact_token(self.xhtml_proof, "xhtml", xhtml_token)

    def _open_fullscreen_proof(self):
        """Open a true full-screen side-by-side PDF/XHTML text proof.

        The main comparison window remains untouched.  The proof window is
        read-only and shows the complete extracted streams side by side,
        exact page/file separators, colors, selected word/paragraph and a
        bottom detail area containing Unicode/codepoint information.
        """
        if self._xhtml_result is None:
            messagebox.showinfo(
                "PDF → XHTML",
                "Run a PDF → XHTML comparison first.",
                parent=self.win,
            )
            return

        if self._fullscreen_proof is not None:
            try:
                if self._fullscreen_proof.winfo_exists():
                    self._fullscreen_proof.deiconify()
                    self._fullscreen_proof.lift()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.win)
        self._fullscreen_proof = win
        win.title("BITS Tool - PDF → XML Full Screen Proof")
        win.configure(bg=theme.current.palette["app_bg"])
        win.attributes("-fullscreen", True)
        win.protocol("WM_DELETE_WINDOW", self._close_fullscreen_proof)
        win.bind("<Escape>", lambda e: self._close_fullscreen_proof())
        win.bind("<F11>", lambda e: self._close_fullscreen_proof())

        palette = theme.current.palette

        top = tk.Frame(win, bg=palette["header_bg"])
        top.pack(fill=tk.X)

        tk.Label(
            top,
            text="PDF → XHTML FULL SCREEN PROOF",
            bg=palette["header_bg"],
            fg=palette["header_fg"],
            font=theme.FONT_APP_TITLE,
        ).pack(side=tk.LEFT, padx=14, pady=8)

        tk.Button(
            top, text="Exit Full Screen", command=self._close_fullscreen_proof
        ).pack(side=tk.RIGHT, padx=10, pady=6)

        mode_frame = tk.Frame(win, bg=palette["app_bg"])
        mode_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(
            mode_frame, text="Word Highlight",
            command=lambda: self._fullscreen_set_mode("word")
        ).pack(side=tk.LEFT)

        tk.Button(
            mode_frame, text="Paragraph Highlight",
            command=lambda: self._fullscreen_set_mode("paragraph")
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(mode_frame, text="< Previous", command=self._fullscreen_previous).pack(side=tk.LEFT, padx=5)
        tk.Button(mode_frame, text="Next >", command=self._fullscreen_next).pack(side=tk.LEFT, padx=2)
        self._fullscreen_sync_var = tk.BooleanVar(value=True)
        tk.Checkbutton(mode_frame, text="Sync Scroll", variable=self._fullscreen_sync_var,
                       bg=palette["app_bg"], fg=palette["text"],
                       selectcolor=palette["surface"]).pack(side=tk.LEFT, padx=8)

        self._fullscreen_status = tk.Label(
            mode_frame, text="Select a difference in the main window.",
            bg=palette["app_bg"], fg=palette["text_muted"],
            font=theme.FONT_BODY
        )
        self._fullscreen_status.pack(side=tk.LEFT, padx=12)

        panes = tk.PanedWindow(
            win, orient=tk.HORIZONTAL, sashrelief=tk.RAISED,
            bg=palette["border"]
        )
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        left = tk.Frame(panes, bg=palette["surface"])
        right = tk.Frame(panes, bg=palette["surface"])
        panes.add(left, minsize=400)
        panes.add(right, minsize=400)

        tk.Label(
            left, text="ORIGINAL PDF — COMPLETE TEXT",
            bg=palette["surface"], fg=palette["text"],
            font=theme.FONT_BODY_BOLD, anchor="w"
        ).pack(fill=tk.X, padx=6, pady=5)

        tk.Label(
            right, text="GENERATED XHTML — COMPLETE TEXT",
            bg=palette["surface"], fg=palette["text"],
            font=theme.FONT_BODY_BOLD, anchor="w"
        ).pack(fill=tk.X, padx=6, pady=5)

        self._fullscreen_pdf = ScrolledText(
            left, wrap=tk.WORD, font=("Times New Roman", 11),
            bg="#fbfbfb", fg=palette["text"], relief=tk.FLAT
        )
        self._fullscreen_pdf.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._fullscreen_xhtml = ScrolledText(
            right, wrap=tk.WORD, font=("Times New Roman", 11),
            bg="#fbfbfb", fg=palette["text"], relief=tk.FLAT
        )
        self._fullscreen_xhtml.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        for widget in (self._fullscreen_pdf, self._fullscreen_xhtml):
            widget.tag_configure("match", background="#bbf7d0", foreground="#166534")
            widget.tag_configure("error", background="#fecaca", foreground="#991b1b")
            widget.tag_configure("modified", background="#fde68a", foreground="#92400e")
            widget.tag_configure("selected", background="#93c5fd", foreground="#111827",
                                 font=("Consolas", 11, "bold"))
            widget.tag_configure("paragraph_selected", background="#fca5a5",
                                 foreground="#111827")
            widget.configure(state=tk.DISABLED)

        # One wheel event moves both proof panes, keeping the comparison
        # visually aligned without forcing identical line wrapping.
        self._fullscreen_pdf.bind("<MouseWheel>", self._fullscreen_mousewheel, add="+")
        self._fullscreen_xhtml.bind("<MouseWheel>", self._fullscreen_mousewheel, add="+")
        self._fullscreen_pdf.bind("<Button-4>", self._fullscreen_mousewheel, add="+")
        self._fullscreen_pdf.bind("<Button-5>", self._fullscreen_mousewheel, add="+")
        self._fullscreen_xhtml.bind("<Button-4>", self._fullscreen_mousewheel, add="+")
        self._fullscreen_xhtml.bind("<Button-5>", self._fullscreen_mousewheel, add="+")

        detail = tk.Frame(win, bg=palette["surface"])
        detail.pack(fill=tk.X, padx=10, pady=(0, 10))

        self._fullscreen_detail = tk.Label(
            detail,
            text="No difference selected.",
            justify=tk.LEFT,
            anchor="w",
            bg=palette["surface"],
            fg=palette["text"],
            font=("Consolas", 10),
            padx=8,
            pady=6,
        )
        self._fullscreen_detail.pack(fill=tk.X)

        # Copy the already rendered complete proof streams.
        self._copy_proof_to_fullscreen()

        if self._last_xhtml_issue is not None:
            self._update_fullscreen_selection(self._last_xhtml_issue)

    def _copy_proof_to_fullscreen(self):
        if self._fullscreen_proof is None:
            return

        for src_widget, dst_widget in (
            (self.pdf_proof, self._fullscreen_pdf),
            (self.xhtml_proof, self._fullscreen_xhtml),
        ):
            content = src_widget.get("1.0", tk.END)
            dst_widget.configure(state=tk.NORMAL)
            dst_widget.delete("1.0", tk.END)
            dst_widget.insert("1.0", content)
            # Reapply the status colors and XHTML typography from the source.
            tags_to_copy = ["match", "error", "modified"]
            if src_widget is self.xhtml_proof:
                tags_to_copy.extend([t for t in src_widget.tag_names() if t.startswith("xhtml_style_")])
            for tag in tags_to_copy:
                ranges = src_widget.tag_ranges(tag)
                for i in range(0, len(ranges), 2):
                    s = str(ranges[i])
                    e = str(ranges[i + 1])
                    dst_widget.tag_add(tag, s, e)
            dst_widget.configure(state=tk.DISABLED)

    def _fullscreen_mousewheel(self, event):
        if not getattr(self, "_fullscreen_sync_var", None) or not self._fullscreen_sync_var.get():
            return
        if self._fullscreen_syncing:
            return "break"
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            units = -3 if event.delta > 0 else 3
        self._fullscreen_syncing = True
        try:
            self._fullscreen_pdf.yview_scroll(units, "units")
            self._fullscreen_xhtml.yview_scroll(units, "units")
        finally:
            self._fullscreen_syncing = False
        return "break"

    def _fullscreen_previous(self):
        if not self._xhtml_issues:
            return
        try:
            current = int(self.xhtml_tree.selection()[0]) if self.xhtml_tree.selection() else 0
        except Exception:
            current = 0
        idx = (current - 1) % len(self._xhtml_issues)
        iid = str(idx)
        self.xhtml_tree.selection_set(iid)
        self.xhtml_tree.focus(iid)
        self._on_xhtml_tree_select()

    def _fullscreen_next(self):
        if not self._xhtml_issues:
            return
        try:
            current = int(self.xhtml_tree.selection()[0]) if self.xhtml_tree.selection() else -1
        except Exception:
            current = -1
        idx = (current + 1) % len(self._xhtml_issues)
        iid = str(idx)
        self.xhtml_tree.selection_set(iid)
        self.xhtml_tree.focus(iid)
        self._on_xhtml_tree_select()

    def _fullscreen_set_mode(self, mode):
        self._proof_highlight_mode = "paragraph" if mode == "paragraph" else "word"
        if self._last_xhtml_issue is not None:
            self._update_fullscreen_selection(self._last_xhtml_issue)

    def _update_fullscreen_selection(self, issue):
        if self._fullscreen_proof is None:
            return

        for widget in (self._fullscreen_pdf, self._fullscreen_xhtml):
            widget.configure(state=tk.NORMAL)
            widget.tag_remove("selected", "1.0", tk.END)
            widget.tag_remove("paragraph_selected", "1.0", tk.END)
            widget.configure(state=tk.DISABLED)

        pdf_token = self._find_pdf_proof_token(issue)
        xhtml_token = self._find_xhtml_proof_token(issue)

        # The full-screen panes are exact copies of the main proof streams.
        # Therefore the character offsets recorded during rendering can be
        # transferred directly.  This is important for repeated words:
        # "the", "of", "1997", etc. must never jump to the first occurrence.
        for side, widget, token in (
            ("pdf", self._fullscreen_pdf, pdf_token),
            ("xhtml", self._fullscreen_xhtml, xhtml_token),
        ):
            if token is None:
                continue

            meta = self._proof_token_ranges.get(side, {}).get(id(token))
            if not meta:
                continue

            start = meta["start"]
            end = meta["end"]

            widget.configure(state=tk.NORMAL)

            if self._proof_highlight_mode == "paragraph":
                page = getattr(token, "page", None)
                para = getattr(token, "paragraph", None)
                file_name = getattr(token, "file", None)
                key = (page if side == "pdf" else file_name, para)
                para_range = self._proof_paragraph_ranges.get(side, {}).get(key)
                if para_range:
                    widget.tag_add(
                        "paragraph_selected",
                        para_range[0],
                        para_range[1],
                    )

            widget.tag_add("selected", start, end)
            try:
                widget.tag_raise("selected")
            except Exception:
                pass
            widget.see(start)
            widget.configure(state=tk.DISABLED)

        self._fullscreen_status.configure(
            text=(
                f"Page: {getattr(issue, 'pdf_page', '') or '-'}   "
                f"Type: {getattr(issue, 'kind', '')}   "
                f"PDF: {getattr(issue, 'pdf_text', '')!r}   "
                f"XHTML: {getattr(issue, 'xhtml_text', '')!r}"
            )
        )

        def uinfo(value):
            if not value:
                return "—"
            char = value.get("char", "")
            cp = value.get("codepoint", "")
            name = value.get("name", "")
            return f"{char!r}  {cp}  {name}"

        self._fullscreen_detail.configure(
            text=(
                f"PDF Unicode:   {uinfo(getattr(issue, 'pdf_unicode', None))}\n"
                f"XHTML Unicode: {uinfo(getattr(issue, 'xhtml_unicode', None))}\n"
                f"PDF BBox:      {getattr(issue, 'pdf_bbox', None) or '—'}\n"
                f"XHTML file:    {getattr(issue, 'xhtml_file', None) or '—'}"
                f"   line: {getattr(issue, 'xhtml_line', None) or '—'}"
            )
        )

    def _close_fullscreen_proof(self):
        try:
            if self._fullscreen_proof is not None:
                self._fullscreen_proof.destroy()
        except Exception:
            pass
        self._fullscreen_proof = None

    def _save_xhtml_report(self):
        if self._xhtml_result is None:
            messagebox.showinfo(
                "PDF → XHTML",
                "Run a PDF → XHTML comparison first.",
                parent=self.win,
            )
            return

        path = filedialog.asksaveasfilename(
            parent=self.win,
            title="Save PDF → XHTML Report",
            defaultextension=".html",
            filetypes=[
                ("HTML report", "*.html"),
                ("JSON report", "*.json"),
                ("PDF report", "*.pdf"),
            ],
        )
        if not path:
            return

        try:
            if path.lower().endswith(".json"):
                self._xhtml_result.write_json(path)
            elif path.lower().endswith(".pdf"):
                self._xhtml_result.write_pdf(path)
            else:
                self._xhtml_result.write_html(path)
            messagebox.showinfo(
                "Report Saved",
                f"Report saved to:\\n{path}",
                parent=self.win,
            )
        except Exception as exc:
            messagebox.showerror(
                "Report Error",
                str(exc),
                parent=self.win,
            )

    def _on_error(self, exc: Exception):
        self.start_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        self.progress_label.configure(text="Error - see message box.")
        messagebox.showerror("Advanced Fidelity Compare", f"Comparison failed:\n{exc}")

    def _on_difference_selected(self, diff):
        self.viewer.show_difference(diff)
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        lines = [
            f"TYPE: {diff.type}", f"CATEGORY: {diff.category}", f"SEVERITY: {diff.severity}",
            f"CONFIDENCE: {diff.confidence} ({diff.confidence_score:.1%})",
            f"RESULT KIND: {diff.result_kind}   STAGE: {diff.comparison_stage}",
            f"ORIGINAL PAGE: {diff.original_page}    CONVERTED PAGE: {diff.converted_page}",
            f"ORIGINAL TEXT: {diff.original_text}", f"CONVERTED TEXT: {diff.converted_text}",
        ]
        # Exact hierarchical location (spec: "EXACT CONTENT DIFFERENCE
        # ENGINE" section 20 / "INTERACTIVE ERROR CARDS" section 2 -
        # "Store: ...paragraph...word...character index...") - shown only
        # when actually populated (a layout/figure/table finding has no
        # paragraph/word/character position at all, and never fakes one).
        if diff.original_paragraph or diff.converted_paragraph:
            lines.append(f"PARAGRAPH: original {diff.original_paragraph}    "
                         f"converted {diff.converted_paragraph}")
        if diff.original_word_index is not None or diff.converted_word_index is not None:
            lines.append(f"WORD: original #{diff.original_word_index}    "
                         f"converted #{diff.converted_word_index}")
        if diff.original_character_index is not None:
            lines.append(f"CHARACTER POSITION IN WORD: {diff.original_character_index}")
        if diff.original_unicode:
            u = diff.original_unicode
            lines.append(f"ORIGINAL UNICODE: {u.get('char')!r}  {u.get('codepoint')}  "
                         f"{u.get('name')}  ({u.get('script')})")
        if diff.converted_unicode:
            u = diff.converted_unicode
            lines.append(f"CONVERTED UNICODE: {u.get('char')!r}  {u.get('codepoint')}  "
                         f"{u.get('name')}  ({u.get('script')})")
        if diff.original_layout:
            lines.append(f"ORIGINAL LAYOUT: {diff.original_layout}")
        if diff.converted_layout:
            lines.append(f"CONVERTED LAYOUT: {diff.converted_layout}")
        if diff.explanation:
            lines.append(f"EXPLANATION: {diff.explanation}")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state=tk.DISABLED)

        # If the direct XHTML proof window is open, show the same selected
        # difference there when the Difference object contains proof text.
        if self._fullscreen_proof is not None:
            try:
                if self._fullscreen_proof.winfo_exists():
                    compatible = SimpleNamespace(
                        pdf_page=getattr(diff, "original_page", None),
                        pdf_text=getattr(diff, "original_text", ""),
                        xhtml_text=getattr(diff, "converted_text", ""),
                        kind=getattr(diff, "type", ""),
                        pdf_bbox=(
                            (
                                getattr(diff.original_bbox, "x0", 0.0),
                                getattr(diff.original_bbox, "y0", 0.0),
                                getattr(diff.original_bbox, "x1", 0.0),
                                getattr(diff.original_bbox, "y1", 0.0),
                            )
                            if getattr(diff, "original_bbox", None) else None
                        ),
                        xhtml_file="",
                        xhtml_line="",
                        pdf_unicode=getattr(diff, "original_unicode", None),
                        xhtml_unicode=getattr(diff, "converted_unicode", None),
                    )
                    self._update_fullscreen_selection(compatible)
            except Exception:
                pass

    def _on_summary_card_click(self, filter_name: str):
        """Spec: "INTERACTIVE ERROR CARDS + EXACT DIFFERENCE VIEW" section
        1/32 - clicking a summary card applies the matching filter and
        jumps straight to the first difference in it; difference_panel's
        own <<TreeviewSelect>> firing from that selection already calls
        _on_difference_selected above (the SAME callback a manual row
        click uses), which navigates both viewers/highlights/shows
        details - no separate navigation path to keep in sync."""
        if self._result is None:
            return
        found = self.difference_panel.apply_filter_and_select_first(filter_name)
        if not found:
            messagebox.showinfo("Advanced Fidelity Compare", f"No differences found for: {filter_name}")

    def _go_home(self):
        self._close_fullscreen_proof()
        self.win.destroy()
        self.on_home()


def open_window(launcher_root: tk.Tk, on_home):
    return FidelityCompareWindow(launcher_root, on_home)
