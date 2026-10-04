"""EPUB PACKAGE CREATION / EPUB STRUCTURE module (spec: "ZONE TOOL -
COMPLETE AUTOMATIC EPUB PACKAGE CREATION MODULE"). A completely separate
module from Zoning/OCR/Reading Order/Split/Merge/Validation/Comparison -
it never imports gui.main_window, core.zone_manager, core.ocr, or
auto_zoning, never touches app/validation or app/comparison, and never
initializes the PDF renderer or OCR engine. It only ever reads the final
XHTML/OPF/CSS/images an EPUB project directory already contains, and
generates/repairs that project's own navigation, OPF, and NCX.

Deliberately ONE coordinated "Generate & Validate" action rather than
separate, independently-clickable "Generate NAV"/"Generate OPF"/...
buttons some spec sketches list: core.epub_structure.orchestrator.
run_full_analysis() already runs figure/table placement -> link
resolution -> Contents resolution -> NAV/OPF/NCX generation -> EPUBCheck
-> content-integrity-guarded verification as ONE atomic, backup-and-
rollback-guarded transaction - exposing the individual steps as separate
buttons would let a user run one step against data another step was
supposed to repair first, producing a WORSE result than the coordinated
pipeline. "Analyze Project" (read-only) and "Generate & Validate" (the
real transaction, run on a background thread so the GUI stays responsive
- spec 51) cover every capability the various menu sketches name.

Level-based progress (spec 38-52) is wired directly to core.epub_structure.
progress.ProgressTracker's own real level/percentage/ETA - this module
never fabricates a percentage or fake per-file progress, and never touches
Zoning/Validation/Comparison's own, completely separate progress systems."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui import theme

STATE_READY = "READY"
STATE_SCANNING = "SCANNING"
STATE_GENERATING = "GENERATING"
STATE_COMPLETE = "COMPLETE"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"

_LEVEL_NAMES = ["Project Discovery", "Content Analysis", "Navigation & Linking",
                "Package Generation", "Validation & Auto-Repair", "Final Verification"]


def open_window(launcher_root: tk.Tk, on_home):
    from core.epub_structure import orchestrator, registry as registry_mod, scanner
    from core.epub_structure.progress import format_duration

    palette = theme.current.palette
    win = tk.Toplevel(launcher_root)
    win.title("EPUBForge - EPUB Package Creation")
    win.geometry("1180x860")
    win.configure(bg=palette["app_bg"])

    state = {
        "project_root": None, "scan": None, "registry": None, "last_result": None,
        "nav_sample": "", "opf_sample": "", "ncx_sample": "",
        "cancel_event": None, "worker": None, "queue": None,
    }

    # ---------------- header ----------------
    header = tk.Frame(win, bg=palette["header_bg"], height=40)
    header.pack(side=tk.TOP, fill=tk.X)
    header.pack_propagate(False)
    left = tk.Frame(header, bg=palette["header_bg"])
    left.pack(side=tk.LEFT, padx=14)
    home_btn = tk.Label(left, text="← Home", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                         font=theme.FONT_BODY_BOLD, cursor="hand2", padx=6)
    home_btn.pack(side=tk.LEFT, padx=(0, 10))

    def _go_home():
        if state["worker"] is not None and state["worker"].is_alive():
            if not messagebox.askyesno("EPUB Package Creation", "A build is still running. Cancel it and leave?",
                                        parent=win):
                return
            state["cancel_event"].set()
        win.destroy()
        on_home()

    home_btn.bind("<Button-1>", lambda e: _go_home())
    win.protocol("WM_DELETE_WINDOW", _go_home)
    tk.Label(left, text="◈ EPUBForge Package Creation", bg=palette["header_bg"], fg=palette["header_fg"],
             font=theme.FONT_APP_TITLE).pack(side=tk.LEFT)

    # ---------------- project selection ----------------
    action_row = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=8)
    action_row.pack(side=tk.TOP, fill=tk.X)
    select_btn = tk.Button(action_row, text="Select Project Folder...")
    theme.style_button(select_btn, palette, kind="secondary")
    select_btn.pack(side=tk.LEFT, padx=(0, 12))
    path_lbl = tk.Label(action_row, text="(no project selected)", bg=palette["app_bg"], fg=palette["text"],
                         font=theme.FONT_BODY_BOLD, anchor="w")
    path_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ---------------- ISBN input (spec: "USER INPUT — ONLY TWO REQUIRED FIELDS") ----------------
    isbn_section = tk.Frame(win, bg=palette["app_bg"], padx=12)
    isbn_section.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
    tk.Label(isbn_section, text="ISBN INFORMATION", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT)
    ttk.Separator(win, orient="horizontal").pack(side=tk.TOP, fill=tk.X, padx=12)

    isbn_row = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=6)
    isbn_row.pack(side=tk.TOP, fill=tk.X)
    tk.Label(isbn_row, text="Normal ISBN:", bg=palette["app_bg"], fg=palette["text"],
              font=theme.FONT_BODY).pack(side=tk.LEFT)
    normal_isbn_var = tk.StringVar()
    normal_isbn_entry = tk.Entry(isbn_row, textvariable=normal_isbn_var, width=20)
    normal_isbn_entry.pack(side=tk.LEFT, padx=(6, 20))
    tk.Label(isbn_row, text="Printed ISBN:", bg=palette["app_bg"], fg=palette["text"],
              font=theme.FONT_BODY).pack(side=tk.LEFT)
    printed_isbn_var = tk.StringVar()
    printed_isbn_entry = tk.Entry(isbn_row, textvariable=printed_isbn_var, width=20)
    printed_isbn_entry.pack(side=tk.LEFT, padx=(6, 0))
    isbn_status_lbl = tk.Label(isbn_row, text="", bg=palette["app_bg"], fg=palette["text_muted"],
                                font=theme.FONT_SMALL)
    isbn_status_lbl.pack(side=tk.LEFT, padx=(16, 0))

    # ---------------- approved sample templates (spec: "APPROVED SAMPLE FILES") ----------------
    sample_section = tk.Frame(win, bg=palette["app_bg"], padx=12)
    sample_section.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
    tk.Label(sample_section, text="APPROVED SAMPLE TEMPLATES (OPTIONAL)", bg=palette["app_bg"],
              fg=palette["text_muted"], font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT)
    ttk.Separator(win, orient="horizontal").pack(side=tk.TOP, fill=tk.X, padx=12)

    sample_row = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=6)
    sample_row.pack(side=tk.TOP, fill=tk.X)
    sample_labels = {}

    def _make_sample_picker(key, label_text, filetypes):
        btn = tk.Button(sample_row, text=label_text)
        theme.style_button(btn, palette, kind="secondary")
        btn.pack(side=tk.LEFT, padx=(0, 6))
        lbl = tk.Label(sample_row, text="(none)", bg=palette["app_bg"], fg=palette["text_muted"],
                        font=theme.FONT_SMALL)
        lbl.pack(side=tk.LEFT, padx=(0, 16))
        sample_labels[key] = lbl

        def _pick():
            path = filedialog.askopenfilename(title=label_text, parent=win, filetypes=filetypes)
            if not path:
                return
            state[key] = path
            lbl.config(text=os.path.basename(path), fg=palette["text"])

        btn.config(command=_pick)
        return btn

    _make_sample_picker("nav_sample", "Nav Sample...", [("XHTML files", "*.xhtml;*.html"), ("All files", "*.*")])
    _make_sample_picker("opf_sample", "OPF Sample...", [("OPF files", "*.opf"), ("All files", "*.*")])
    _make_sample_picker("ncx_sample", "NCX Sample...", [("NCX files", "*.ncx"), ("All files", "*.*")])
    theme.Tooltip(sample_row, "These control FORMAT only (namespaces, attribute conventions, metadata style) - "
                               "never book-specific data. All book content always comes from the current project.")

    # ---------------- actions ----------------
    action_row2 = tk.Frame(win, bg=palette["app_bg"], padx=12)
    action_row2.pack(side=tk.TOP, fill=tk.X, pady=(8, 8))
    analyze_btn = tk.Button(action_row2, text="Analyze Project", state="disabled")
    theme.style_button(analyze_btn, palette, kind="secondary")
    analyze_btn.pack(side=tk.LEFT, padx=(0, 8))
    theme.Tooltip(analyze_btn, "Scans the project and shows its document structure - read-only, "
                                "changes nothing on disk.")
    generate_btn = tk.Button(action_row2, text="Generate & Validate", state="disabled")
    theme.style_button(generate_btn, palette, kind="primary")
    generate_btn.pack(side=tk.LEFT, padx=(0, 8))
    theme.Tooltip(generate_btn, "Repairs internal links, resolves the Contents page, places figures/tables at "
                                 "their first citation, and (re)generates NAV/OPF/NCX as one guarded transaction. "
                                 "A backup is made first; if content integrity cannot be verified afterward, "
                                 "every change is automatically rolled back.")
    cancel_btn = tk.Button(action_row2, text="Cancel", state="disabled")
    theme.style_button(cancel_btn, palette, kind="secondary")
    cancel_btn.pack(side=tk.LEFT, padx=(0, 8))
    ncx_var = tk.BooleanVar(value=False)
    ncx_check = tk.Checkbutton(action_row2, text="Generate NCX", variable=ncx_var, bg=palette["app_bg"],
                                fg=palette["text"], selectcolor=palette["surface"], font=theme.FONT_BODY)
    ncx_check.pack(side=tk.LEFT, padx=(8, 0))
    epubcheck_var = tk.BooleanVar(value=True)
    epubcheck_check = tk.Checkbutton(action_row2, text="Run EPUBCheck", variable=epubcheck_var, bg=palette["app_bg"],
                                      fg=palette["text"], selectcolor=palette["surface"], font=theme.FONT_BODY)
    epubcheck_check.pack(side=tk.LEFT, padx=(8, 0))
    fill_pages_var = tk.BooleanVar(value=True)
    fill_pages_check = tk.Checkbutton(action_row2, text="Fill missing page numbers", variable=fill_pages_var,
                                       bg=palette["app_bg"], fg=palette["text"], selectcolor=palette["surface"],
                                       font=theme.FONT_BODY)
    fill_pages_check.pack(side=tk.LEFT, padx=(8, 0))
    images_var = tk.BooleanVar(value=True)
    images_check = tk.Checkbutton(action_row2, text="Images: 150 dpi, cover 300 dpi + 1200x1800",
                                  variable=images_var, bg=palette["app_bg"], fg=palette["text"],
                                  selectcolor=palette["surface"], font=theme.FONT_BODY)
    images_check.pack(side=tk.LEFT, padx=(8, 0))
    theme.Tooltip(images_check, "In the packaged EPUB: every image at 150 dpi (inline / icon images 135 dpi, as "
                                "the client's tool requires), the cover at 300 dpi and scaled to 1200 px wide or "
                                "1800 px high (never larger, aspect ratio kept). DPI is written into the image "
                                "header - pixels unchanged. Your project's image files are never modified.")
    autofix_var = tk.BooleanVar(value=True)
    autofix_check = tk.Checkbutton(action_row2, text="Validate & Auto-Fix output", variable=autofix_var,
                                   bg=palette["app_bg"], fg=palette["text"], selectcolor=palette["surface"],
                                   font=theme.FONT_BODY)
    autofix_check.pack(side=tk.LEFT, padx=(8, 0))
    theme.Tooltip(autofix_check, "After the EPUB is packaged: EPUBCheck -> safe repairs (each checked for "
                                 "content loss) -> repackage -> EPUBCheck again, until clean or only manual-"
                                 "review issues remain. Writes '<name>.repaired.epub' + a report; the generated "
                                 "EPUB itself is kept as it is.")
    theme.Tooltip(fill_pages_check, "Scans the printed page sequence for gaps (e.g. 45 -> 47) and inserts the "
                                    "missing page markers - blank pages at a chapter end go at the end of that "
                                    "chapter. Front-matter pages before the first marker are only reported.")
    export_btn = tk.Button(action_row2, text="Export Report...", state="disabled")
    theme.style_button(export_btn, palette, kind="secondary")
    export_btn.pack(side=tk.RIGHT, padx=(4, 0))
    open_output_btn = tk.Button(action_row2, text="Open Output Folder", state="disabled")
    theme.style_button(open_output_btn, palette, kind="secondary")
    open_output_btn.pack(side=tk.RIGHT)

    # ---------------- level-based progress panel (spec 38-52) ----------------
    progress_section = tk.Frame(win, bg=palette["app_bg"], padx=12)
    progress_section.pack(side=tk.TOP, fill=tk.X, pady=(0, 0))
    tk.Label(progress_section, text="BUILD PROGRESS", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT)
    ttk.Separator(win, orient="horizontal").pack(side=tk.TOP, fill=tk.X, padx=12)

    progress_frame = tk.Frame(win, bg=palette["surface"], padx=12, pady=8, highlightthickness=1,
                               highlightbackground=palette["border"])
    progress_frame.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(4, 8))

    bar = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate", maximum=100)
    bar.pack(side=tk.TOP, fill=tk.X)
    overall_lbl = tk.Label(progress_frame, text="Overall: 0%", bg=palette["surface"], fg=palette["text"],
                            font=theme.FONT_BODY_BOLD)
    overall_lbl.pack(side=tk.TOP, anchor="w", pady=(4, 4))

    levels_row = tk.Frame(progress_frame, bg=palette["surface"])
    levels_row.pack(side=tk.TOP, fill=tk.X)
    level_labels = []
    for name in _LEVEL_NAMES:
        lbl = tk.Label(levels_row, text=f"○ {name}", bg=palette["surface"], fg=palette["text_muted"],
                        font=theme.FONT_SMALL, anchor="w")
        lbl.pack(side=tk.TOP, anchor="w")
        level_labels.append(lbl)

    detail_row = tk.Frame(progress_frame, bg=palette["surface"])
    detail_row.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
    operation_lbl = tk.Label(detail_row, text="Current: —", bg=palette["surface"], fg=palette["text"],
                              font=theme.FONT_BODY, anchor="w")
    operation_lbl.pack(side=tk.TOP, anchor="w")
    file_lbl = tk.Label(detail_row, text="File: —", bg=palette["surface"], fg=palette["text_muted"],
                         font=theme.FONT_SMALL, anchor="w")
    file_lbl.pack(side=tk.TOP, anchor="w")
    items_lbl = tk.Label(detail_row, text="Items: —", bg=palette["surface"], fg=palette["text_muted"],
                          font=theme.FONT_SMALL, anchor="w")
    items_lbl.pack(side=tk.TOP, anchor="w")
    time_lbl = tk.Label(detail_row, text="Elapsed: 00:00   Estimated remaining: Calculating...",
                         bg=palette["surface"], fg=palette["text_muted"], font=theme.FONT_SMALL, anchor="w")
    time_lbl.pack(side=tk.TOP, anchor="w")

    # ---------------- results ----------------
    summary_header = tk.Frame(win, bg=palette["app_bg"], padx=12)
    summary_header.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
    tk.Label(summary_header, text="PROJECT SUMMARY", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT)
    ttk.Separator(win, orient="horizontal").pack(side=tk.TOP, fill=tk.X, padx=12)

    summary_frame = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=8)
    summary_frame.pack(side=tk.TOP, fill=tk.X)
    columns = ("path", "type", "headings", "pagebreaks", "figures", "notes")
    tree = ttk.Treeview(summary_frame, columns=columns, show="headings", height=6, selectmode="none")
    headings = {"path": "Document", "type": "Type", "headings": "Headings", "pagebreaks": "Pagebreaks",
                "figures": "Figures", "notes": "Notes"}
    widths = {"path": 320, "type": 130, "headings": 80, "pagebreaks": 90, "figures": 70, "notes": 70}
    for col in columns:
        tree.heading(col, text=headings[col])
        tree.column(col, width=widths[col], anchor="w")
    vsb = ttk.Scrollbar(summary_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)

    report_section = tk.Frame(win, bg=palette["app_bg"], padx=12)
    report_section.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
    tk.Label(report_section, text="VALIDATION REPORT", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT)
    ttk.Separator(win, orient="horizontal").pack(side=tk.TOP, fill=tk.X, padx=12)

    report_frame = tk.Frame(win, bg=palette["app_bg"], padx=12)
    report_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 8))
    report_text = tk.Text(report_frame, bg=palette["surface"], fg=palette["text"], font=theme.FONT_MONO_SMALL,
                           wrap="none", padx=10, pady=8, borderwidth=0, highlightthickness=0, height=14)
    report_vsb = ttk.Scrollbar(report_frame, orient="vertical", command=report_text.yview)
    report_text.configure(yscrollcommand=report_vsb.set)
    report_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    report_vsb.pack(side=tk.RIGHT, fill=tk.Y)
    report_text.tag_configure("pass", foreground=palette["success"])
    report_text.tag_configure("review", foreground=palette["warning"])
    report_text.tag_configure("fail", foreground=palette["error"])

    # ---------------- status bar ----------------
    status_bar = tk.Label(win, text=f"Status: {STATE_READY}", bg=palette["header_bg"],
                           fg=palette["header_fg_muted"], font=theme.FONT_SMALL, anchor="w", padx=10)
    status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _set_state(new_state: str, detail: str = ""):
        status_bar.config(text=f"Status: {new_state}" + (f" — {detail}" if detail else ""))

    # ---------------- handlers ----------------
    def _populate_summary(registry):
        tree.delete(*tree.get_children())
        for doc in registry.documents:
            tree.insert("", "end", values=(doc.path, doc.document_type, len(doc.headings),
                                            len(doc.pagebreaks), len(doc.figures), len(doc.notes)))

    def _reset_progress_ui():
        bar["value"] = 0
        overall_lbl.config(text="Overall: 0%")
        for lbl, name in zip(level_labels, _LEVEL_NAMES):
            lbl.config(text=f"○ {name}", fg=palette["text_muted"])
        operation_lbl.config(text="Current: —")
        file_lbl.config(text="File: —")
        items_lbl.config(text="Items: —")
        time_lbl.config(text="Elapsed: 00:00   Estimated remaining: Calculating...")

    def _select_project():
        path = filedialog.askdirectory(title="Select EPUB Project/Output Folder", parent=win)
        if not path:
            return
        state["project_root"] = path
        path_lbl.config(text=path, fg=palette["text"])
        analyze_btn.config(state="normal")
        generate_btn.config(state="normal")
        tree.delete(*tree.get_children())
        report_text.config(state="normal")
        report_text.delete("1.0", "end")
        report_text.config(state="disabled")
        export_btn.config(state="disabled")
        open_output_btn.config(state="disabled")
        _reset_progress_ui()
        _set_state(STATE_READY, "project selected - click Analyze Project")

    def _run_analyze():
        root = state["project_root"]
        if not root:
            return
        select_btn.config(state="disabled")
        analyze_btn.config(state="disabled")
        generate_btn.config(state="disabled")
        _set_state(STATE_SCANNING, "scanning project directory")
        win.update_idletasks()
        scan = scanner.scan_project(root)
        if not scan.xhtml_files:
            messagebox.showwarning("Analyze Project", "No XHTML content documents were found in this folder.",
                                    parent=win)
            _set_state(STATE_FAILED, "no XHTML files found")
            select_btn.config(state="normal")
            analyze_btn.config(state="normal")
            generate_btn.config(state="normal")
            return
        state["scan"] = scan
        _set_state(STATE_SCANNING, "building the global document registry")
        win.update_idletasks()
        registry = registry_mod.build_registry(scan)
        state["registry"] = registry
        _populate_summary(registry)
        _set_state(STATE_COMPLETE, f"{len(registry.documents)} document(s) analyzed - "
                                    f"{len(scan.image_files)} image(s), {len(scan.css_files)} stylesheet(s)")
        select_btn.config(state="normal")
        analyze_btn.config(state="normal")
        generate_btn.config(state="normal")

    def _render_report(text_widget, report):
        from core.epub_structure.validator import render_report
        text_widget.config(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", render_report(report))
        start = text_widget.search("FINAL STATUS:", "1.0", tk.END)
        if start:
            end = f"{start}+{len('FINAL STATUS: ')+len(report.final_status)}c"
            tag = {"PASS": "pass", "REVIEW": "review", "FAIL": "fail"}.get(report.final_status, "")
            if tag:
                text_widget.tag_add(tag, start, end)
        text_widget.config(state="disabled")

    def _apply_progress_state(pstate):
        bar["value"] = round(pstate.overall_pct * 100, 1)
        overall_lbl.config(text=f"Overall: {pstate.overall_pct * 100:.0f}%")
        for lbl, (level_num, name) in zip(level_labels, enumerate(_LEVEL_NAMES, start=1)):
            status = pstate.level_status.get(level_num, "pending")
            symbol = {"done": "✓", "active": "→", "pending": "○"}[status]
            color = {"done": palette["success"], "active": palette["accent"],
                     "pending": palette["text_muted"]}[status]
            lbl.config(text=f"{symbol} {name}", fg=color)
        operation_lbl.config(text=f"Current: {pstate.operation}")
        file_lbl.config(text=f"File: {pstate.current_file or '—'}")
        items_lbl.config(text=f"Items: {pstate.completed} / {pstate.total}" if pstate.total else "Items: —")
        time_lbl.config(text=f"Elapsed: {format_duration(pstate.elapsed_seconds)}   "
                              f"Estimated remaining: {format_duration(pstate.estimated_remaining_seconds)}")

    def _poll_worker_queue():
        q = state["queue"]
        if q is None:
            return
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "progress":
                    _apply_progress_state(payload)
                elif kind == "done":
                    _on_generate_finished(payload)
        except queue.Empty:
            pass
        if state["worker"] is not None and state["worker"].is_alive():
            win.after(80, _poll_worker_queue)

    def _on_generate_finished(result):
        state["last_result"] = result
        state["worker"] = None
        cancel_btn.config(state="disabled")
        select_btn.config(state="normal")
        analyze_btn.config(state="normal")
        generate_btn.config(state="normal")

        if result.scan is not None:
            state["registry"] = registry_mod.build_registry(result.scan)
            _populate_summary(state["registry"])

        if result.cancelled:
            _set_state(STATE_CANCELLED, result.error)
            messagebox.showinfo("Generate & Validate", "Package creation cancelled safely.", parent=win)
            return
        if result.error and result.rolled_back:
            messagebox.showerror("Generate & Validate", f"Rolled back - nothing was kept:\n\n{result.error}",
                                  parent=win)
            _set_state(STATE_FAILED, "rolled back")
            return
        if result.error:
            messagebox.showerror("Generate & Validate", result.error, parent=win)
            _set_state(STATE_FAILED, result.error)
            return
        if result.report is not None:
            _render_report(report_text, result.report)
            export_btn.config(state="normal")
            if result.output_epub_path:
                open_output_btn.config(state="normal")
            _set_state(STATE_COMPLETE, f"FINAL STATUS: {result.report.final_status}  "
                                        f"({format_duration(result.elapsed_seconds)})")
            if autofix_var.get() and result.output_epub_path and os.path.isfile(result.output_epub_path):
                # spec: never "successful" before the PACKAGED EPUB is validated (and repaired)
                from app.validation import autofix_dashboard
                base_status = f"FINAL STATUS: {result.report.final_status}"
                generate_btn.config(state="disabled")
                autofix_dashboard.run_in_background(
                    win, result.output_epub_path,
                    on_status=lambda t: _set_state(STATE_COMPLETE, f"{base_status}  |  {t}"),
                    on_done=lambda _r: generate_btn.config(state="normal"),
                    launcher_root=launcher_root, on_home=on_home)

    def _run_generate():
        root = state["project_root"]
        if not root:
            return
        proceed = messagebox.askyesno(
            "Generate & Validate",
            "This repairs internal links, resolves the Contents page, places figures/tables at their first "
            "citation, and (re)generates this project's NAV/OPF" + ("/NCX" if ncx_var.get() else "") + ".\n\n"
            "A backup of every file that could be modified is made first. If content integrity cannot be "
            "verified afterward, every change is automatically rolled back.\n\nContinue?", parent=win)
        if not proceed:
            return

        # Every value the background worker needs is read from its Tk
        # Variable HERE, on the main thread, and passed as a plain Python
        # value - Tkinter variables/widgets are not thread-safe, and
        # reading one from the worker thread itself would risk the exact
        # "main thread is not in main loop" class of failure this app's
        # own core/autosave_service.py was written specifically to avoid.
        normal_isbn_value = normal_isbn_var.get()
        printed_isbn_value = printed_isbn_var.get()
        generate_ncx_value = ncx_var.get()
        run_epubcheck_value = epubcheck_var.get()
        fill_pages_value = fill_pages_var.get()
        images_value = images_var.get()
        nav_sample_value = state["nav_sample"]
        opf_sample_value = state["opf_sample"]
        ncx_sample_value = state["ncx_sample"]

        normal_v = isbn_mod_validate(normal_isbn_value)
        printed_v = isbn_mod_validate(printed_isbn_value)
        # an EMPTY field is taken from the book's own copyright page (the
        # e-book ISBN as identifier, the print ISBN as dc:source); only a
        # typed-in value that fails validation needs a decision
        problems = [v.error for v, raw in ((normal_v, normal_isbn_value), (printed_v, printed_isbn_value))
                    if v.error and raw.strip()]
        if problems and not messagebox.askyesno(
                "ISBN Validation",
                "\n".join(problems) + "\n\nContinue anyway? (ISBN(s) will be omitted from the OPF)", parent=win):
            return

        select_btn.config(state="disabled")
        analyze_btn.config(state="disabled")
        generate_btn.config(state="disabled")
        export_btn.config(state="disabled")
        open_output_btn.config(state="disabled")
        cancel_btn.config(state="normal")
        _reset_progress_ui()
        _set_state(STATE_GENERATING, "starting")

        cancel_event = threading.Event()
        state["cancel_event"] = cancel_event
        q = queue.Queue()
        state["queue"] = q

        def _worker():
            result = orchestrator.run_full_analysis(
                root, normal_isbn=normal_isbn_value, printed_isbn=printed_isbn_value,
                nav_sample_path=nav_sample_value, opf_sample_path=opf_sample_value,
                ncx_sample_path=ncx_sample_value, generate_ncx=generate_ncx_value,
                run_epubcheck=run_epubcheck_value, fill_missing_pages=fill_pages_value,
                prepare_images=images_value,
                progress_callback=lambda pstate: q.put(("progress", pstate)),
                cancel_check=cancel_event.is_set)
            q.put(("done", result))

        worker = threading.Thread(target=_worker, daemon=True)
        state["worker"] = worker
        worker.start()
        win.after(80, _poll_worker_queue)

    def _cancel_generate():
        if state["cancel_event"] is not None:
            state["cancel_event"].set()
            cancel_btn.config(state="disabled")
            _set_state(STATE_GENERATING, "cancelling - rolling back safely...")

    def _export_report():
        result = state.get("last_result")
        if result is None or result.report is None:
            return
        from core.epub_structure.validator import render_report
        out_path = filedialog.asksaveasfilename(
            title="Export Report", parent=win, defaultextension=".txt",
            initialfile="epub_structure_report.txt", filetypes=[("Text files", "*.txt")])
        if not out_path:
            return
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(render_report(result.report))
        except OSError as e:
            messagebox.showerror("Export Report", f"Could not save report: {e}", parent=win)
            return
        _set_state(STATE_COMPLETE, f"report exported to {os.path.basename(out_path)}")

    def _open_output_folder():
        result = state.get("last_result")
        if result is None or not result.output_epub_path:
            return
        folder = os.path.dirname(result.output_epub_path)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606 - a real, user-initiated "reveal in explorer" action
            else:
                subprocess.run(["xdg-open", folder], check=False)
        except OSError as e:
            messagebox.showerror("Open Output Folder", f"Could not open folder: {e}", parent=win)

    select_btn.config(command=_select_project)
    analyze_btn.config(command=_run_analyze)
    generate_btn.config(command=_run_generate)
    cancel_btn.config(command=_cancel_generate)
    export_btn.config(command=_export_report)
    open_output_btn.config(command=_open_output_folder)

    # Exposed for automated testing and any future external driver.
    win.epub_structure_state = state
    win.epub_structure_widgets = {
        "select_btn": select_btn, "analyze_btn": analyze_btn, "generate_btn": generate_btn,
        "cancel_btn": cancel_btn, "export_btn": export_btn, "open_output_btn": open_output_btn,
        "ncx_var": ncx_var, "epubcheck_var": epubcheck_var, "tree": tree, "report_text": report_text,
        "path_lbl": path_lbl, "normal_isbn_var": normal_isbn_var, "printed_isbn_var": printed_isbn_var,
        "isbn_status_lbl": isbn_status_lbl, "progress_bar": bar, "level_labels": level_labels,
        "operation_lbl": operation_lbl,
    }
    win.epub_structure_actions = {
        "select_project": _select_project, "run_analyze": _run_analyze, "run_generate": _run_generate,
        "cancel_generate": _cancel_generate, "export_report": _export_report,
        "open_output_folder": _open_output_folder, "poll_worker_queue": _poll_worker_queue,
    }

    return win


def isbn_mod_validate(raw: str):
    from core.epub_structure.isbn import validate_isbn
    return validate_isbn(raw, "ISBN")
