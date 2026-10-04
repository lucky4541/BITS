"""VALIDATION module. Built across three phases: Phase 1 ("Validation
Foundation + Launcher Integration") - Open EPUB, package inspection
display, Quick Validator + REAL W3C EPUBCheck, a sortable results table
with a per-row detail panel, and an explicit validation state machine.
Phase 2 ("Universal Error Analyzer + Root Cause Analysis") - extended the
same detail panel with Root Cause / Repairability. Phase 3 ("Universal
Auto-Fix Engine + Full Auto Repair") - [Full Auto Repair] now runs
core.epub.repair_engine's real, deterministic-only repair workflow and
shows its report in a separate window; [Validate Only]'s own tool-running
logic was refactored into core.epub.validation_runner (the ONE shared
implementation both this window and repair_engine call, never two copies).

Lazy imports throughout (core.epub/core.epubcheck are only imported once
this window actually opens) - matches this module's own established
"never initialize at launcher startup" rule; zero impact on Zoning/PDF/
OCR startup time. Nothing in this file touches gui/, core/zone_manager.py,
auto_zoning/, or any other zoning-related module."""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui import theme

# Explicit validation state machine (spec section 12) - a single source of
# truth for what the status line displays, never free-form text scattered
# across handlers.
STATE_READY = "READY"
STATE_OPENING = "OPENING"
STATE_INSPECTING = "INSPECTING"
STATE_VALIDATING = "VALIDATING"
STATE_EPUBCHECK = "EPUBCHECK"
STATE_COMPLETE = "COMPLETE"
STATE_FAILED = "FAILED"


def open_window(launcher_root: tk.Tk, on_home):
    """Opens the Validation window as a Toplevel - same <- Home navigation
    pattern gui.main_window.App/app.comparison.comparison_window use."""
    from core.epub import package_reader
    from core.epubcheck import runner as epubcheck_runner
    from core.epubcheck import test_suite_runner

    palette = theme.current.palette
    win = tk.Toplevel(launcher_root)
    win.title("EPUBForge - Validation")
    win.geometry("1150x760")
    win.configure(bg=palette["app_bg"])

    state = {"package": None, "epub_path": None, "row_details": {}}

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
        win.destroy()
        on_home()

    home_btn.bind("<Button-1>", lambda e: _go_home())
    win.protocol("WM_DELETE_WINDOW", _go_home)
    tk.Label(left, text="◈ EPUBForge Validation", bg=palette["header_bg"], fg=palette["header_fg"],
             font=theme.FONT_APP_TITLE).pack(side=tk.LEFT)

    # spec: "PART L - STARTUP CHECK" - a lightweight, LOCAL-ONLY check
    # (core.epubcheck.runner.check_availability + core.epubcheck.
    # test_suite_runner.check_suite_availability, both cheap existence
    # checks that never themselves run Java/EPUBCheck/the Test Suite) of
    # ALL THREE required components, run once when this window opens
    # (matching the established "never initialize at launcher startup"
    # rule - this fires when Validation itself opens, not at app launch).
    # "If missing: show 'Validation runtime incomplete'... explain exactly
    # which component is missing" - the header itself reflects this, not
    # just the tooltip, since EPUBCheck alone being available is not
    # sufficient once the Test Suite button is expected to work too.
    ok, avail_msg = epubcheck_runner.check_availability()
    version = epubcheck_runner.get_epubcheck_version() if ok else ""
    suite_ok, suite_msg = test_suite_runner.check_suite_availability()
    runtime_complete = ok and suite_ok
    avail_color = palette["success"] if runtime_complete else palette["error"]
    avail_text = f"EPUBCheck: {version or 'available'}" if runtime_complete else "Validation runtime incomplete"
    avail_label = tk.Label(header, text=avail_text, bg=palette["header_bg"], fg=avail_color,
                            font=theme.FONT_SMALL_BOLD)
    avail_label.pack(side=tk.RIGHT, padx=14)

    # spec: "PART K - VERSION INFORMATION" - EPUBForge/EPUBCheck/Java/Test
    # Suite versions, all resolved LOCALLY (no internet call anywhere in
    # this block) - surfaced via this same tooltip rather than a new
    # panel, since Part J's own required layout doesn't reserve separate
    # screen space for it.
    from app.launcher.launcher_window import APP_VERSION
    java_version = epubcheck_runner.get_java_version() if ok else ""
    missing = [] if ok else [f"EPUBCheck/Java: {avail_msg}"]
    if not suite_ok:
        missing.append(f"W3C Test Suite: {suite_msg}")
    version_info = (
        (("MISSING: " + "; ".join(missing) + "\n\n") if missing else "")
        + f"EPUBForge Version: {APP_VERSION}\n"
        f"EPUBCheck Version: {version or 'unavailable'}\n"
        f"Java Runtime Version: {java_version or 'unavailable'}\n"
        f"W3C Test Suite Version: {test_suite_runner.suite_version()}")
    theme.Tooltip(avail_label, version_info)

    def _section(title):
        frame = tk.Frame(win, bg=palette["app_bg"], padx=12)
        frame.pack(side=tk.TOP, fill=tk.X, pady=(10, 4))
        tk.Label(frame, text=title, bg=palette["app_bg"], fg=palette["text_muted"],
                 font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT)
        ttk.Separator(win, orient="horizontal").pack(side=tk.TOP, fill=tk.X, padx=12)
        return frame

    # ---------------- Open EPUB ----------------
    _section("OPEN EPUB")
    open_row = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=8)
    open_row.pack(side=tk.TOP, fill=tk.X)
    open_btn = tk.Button(open_row, text="Open EPUB...")
    theme.style_button(open_btn, palette, kind="secondary")
    open_btn.pack(side=tk.LEFT, padx=(0, 12))
    tk.Label(open_row, text="Selected EPUB:", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_BODY).pack(side=tk.LEFT)
    file_label = tk.Label(open_row, text="(none)", bg=palette["app_bg"], fg=palette["text"],
                           font=theme.FONT_BODY_BOLD, anchor="w")
    file_label.pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

    # ---------------- Package Information ----------------
    _section("PACKAGE INFORMATION")
    info_grid = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=8)
    info_grid.pack(side=tk.TOP, fill=tk.X)
    info_fields = {}
    info_layout = [
        ("EPUB Version:", "version"), ("OPF:", "opf"), ("NAV:", "nav"), ("NCX:", "ncx"),
        ("Manifest:", "manifest"), ("Spine:", "spine"), ("Metadata:", "metadata"),
    ]
    for col, (label_text, key) in enumerate(info_layout):
        cell = tk.Frame(info_grid, bg=palette["app_bg"])
        cell.grid(row=0, column=col, sticky="w", padx=(0, 20))
        tk.Label(cell, text=label_text, bg=palette["app_bg"], fg=palette["text_muted"],
                 font=theme.FONT_SMALL).pack(anchor="w")
        value_lbl = tk.Label(cell, text="—", bg=palette["app_bg"], fg=palette["text"], font=theme.FONT_BODY_BOLD)
        value_lbl.pack(anchor="w")
        info_fields[key] = value_lbl

    def _set_info(package):
        if package is None or package.error:
            for lbl in info_fields.values():
                lbl.config(text="—")
            return
        info_fields["version"].config(text=package.epub_version or "?")
        info_fields["opf"].config(text=os.path.basename(package.opf_path) or "?")
        info_fields["nav"].config(text=os.path.basename(package.nav_path) if package.nav_path else "(none)")
        info_fields["ncx"].config(text=os.path.basename(package.ncx_path) if package.ncx_path else "(none)")
        info_fields["manifest"].config(text=str(len(package.manifest)))
        info_fields["spine"].config(text=str(len(package.spine)))
        info_fields["metadata"].config(text=str(package.metadata_count))

    # ---------------- Validation actions ----------------
    _section("VALIDATION")
    action_row = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=8)
    action_row.pack(side=tk.TOP, fill=tk.X)
    validate_btn = tk.Button(action_row, text="Validate Only", state="disabled")
    theme.style_button(validate_btn, palette, kind="primary")
    validate_btn.pack(side=tk.LEFT, padx=(0, 8))

    # Full Auto Repair (spec: "EPUBForge - PHASE 3 ONLY - Universal Auto-Fix
    # Engine + Full Auto Repair") - runs core.epub.repair_engine's
    # iterative, deterministic-only repair workflow. Disabled until an EPUB
    # is open; command wired at the bottom of open_window() alongside the
    # other handlers.
    repair_btn = tk.Button(action_row, text="VALIDATE & AUTO-FIX EPUB", state="disabled")
    theme.style_button(repair_btn, palette, kind="primary")
    repair_btn.pack(side=tk.LEFT)
    theme.Tooltip(repair_btn, "Closed loop: backup -> EPUBCheck -> root-cause repair plan -> safe repairs, each "
                               "checked for content loss (rolled back if anything would be lost) -> EPUBCheck "
                               "again ... -> final EPUBCheck on the packaged file. The original EPUB is never "
                               "modified; a '<name>.repaired.epub' and a '<name>_autofix' session folder "
                               "(backup, working copies, history, reports) are written next to it.")
    pdf_btn = tk.Button(action_row, text="Source PDF (optional)...")
    theme.style_button(pdf_btn, palette, kind="secondary")
    pdf_btn.pack(side=tk.LEFT, padx=(8, 0))
    pdf_label = tk.Label(action_row, text="", bg=palette["app_bg"], fg=palette["text_muted"], font=theme.FONT_SMALL)
    pdf_label.pack(side=tk.LEFT, padx=(4, 0))
    theme.Tooltip(pdf_btn, "With the source PDF, the auto-fix also checks page markers and images against the "
                           "PDF (high-confidence fixes only) and adds a PDF <-> XHTML comparison report.")

    # Client validation: the publisher's own delivery rules (CUPEPUB =
    # the client's SPiXVali tool, core.epub.client_rules) - run on their
    # own, and repaired inside VALIDATE & AUTO-FIX when a profile is chosen.
    client_row = tk.Frame(win, bg=palette["app_bg"], padx=12, pady=0)
    tk.Label(client_row, text="Client rules:", bg=palette["app_bg"], fg=palette["text_muted"],
             font=theme.FONT_SMALL).pack(side=tk.LEFT)
    from core.epub import client_rules as _client_rules
    client_var = tk.StringVar(value=_client_rules.DEFAULT_PROFILE)
    client_combo = ttk.Combobox(client_row, textvariable=client_var, state="readonly", width=12,
                                values=["(none)"] + sorted(_client_rules.PROFILES))
    client_combo.pack(side=tk.LEFT, padx=(4, 8))
    theme.Tooltip(client_combo, "The client's own validation rules (CUPEPUB = the client's SPiXVali tool, rules "
                                "EPUB-001 ... EPUB-051). VALIDATE & AUTO-FIX also repairs them: junk characters, "
                                "missing links (linked when a valid target exists, otherwise wrapped in a <span>), "
                                "index page numbers / ranges / see also, OPF identifiers, guide and landmarks, "
                                "image DPI ...")
    client_btn = tk.Button(client_row, text="Client Validate", state="disabled")
    theme.style_button(client_btn, palette, kind="secondary")
    client_btn.pack(side=tk.LEFT)
    client_log_btn = tk.Button(client_row, text="Client tool log (optional)...")
    theme.style_button(client_log_btn, palette, kind="secondary")
    client_log_btn.pack(side=tk.LEFT, padx=(8, 0))
    client_log_label = tk.Label(client_row, text="", bg=palette["app_bg"], fg=palette["text_muted"],
                                font=theme.FONT_SMALL)
    client_log_label.pack(side=tk.LEFT, padx=(4, 0))
    theme.Tooltip(client_log_btn, "A log written by the client's tool (SPiXVali) for this EPUB - its findings are "
                                  "listed and compared with the native client rules.")

    # Edit EPUB (spec: "EPUBForge - PHASE 4 ONLY - Real EPUB Editor") -
    # opens app.editor.editor_window as a SIBLING window (same launcher_root/
    # on_home this window itself received), pointed at the same package.
    edit_btn = tk.Button(action_row, text="Edit EPUB", state="disabled")
    theme.style_button(edit_btn, palette, kind="secondary")
    edit_btn.pack(side=tk.LEFT, padx=(8, 0))
    theme.Tooltip(edit_btn, "Open the real package/text editor for this EPUB.")

    # spec: "FINAL VALIDATION PHASE - Part A2" - "clearly separate from
    # [Validate Only] and [Full Auto Repair]" (and, by the same logic,
    # Edit EPUB): a visible vertical separator plus extra spacing, and -
    # unlike the other three - NEVER gated on an open user EPUB, since it
    # runs entirely against the bundled Test Suite's OWN files (spec A5:
    # isolated from whatever the user has open).
    ttk.Separator(action_row, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=16, pady=2)
    test_suite_btn = tk.Button(action_row, text="Run W3C EPUBCheck Test Suite")
    theme.style_button(test_suite_btn, palette, kind="secondary")
    test_suite_btn.pack(side=tk.LEFT)
    theme.Tooltip(test_suite_btn,
                  "Runs the OFFICIAL W3C EPUBCheck Test Suite (bundled locally) against the bundled "
                  "EPUBCheck - a separate, isolated operation from normal validation. "
                  "Never touches any EPUB you have open.")
    if not suite_ok:
        test_suite_btn.config(state="disabled")
        theme.Tooltip(test_suite_btn, f"W3C Test Suite unavailable: {suite_msg}")
    client_row.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

    result_summary = tk.Frame(win, bg=palette["app_bg"], padx=12)
    result_summary.pack(side=tk.TOP, fill=tk.X)
    quick_status_lbl = tk.Label(result_summary, text="Quick Validator: —", bg=palette["app_bg"],
                                 fg=palette["text_muted"], font=theme.FONT_BODY_BOLD)
    quick_status_lbl.pack(side=tk.LEFT, padx=(0, 24))
    epubcheck_status_lbl = tk.Label(result_summary, text="EPUBCheck: —", bg=palette["app_bg"],
                                     fg=palette["text_muted"], font=theme.FONT_BODY_BOLD)
    epubcheck_status_lbl.pack(side=tk.LEFT, padx=(0, 24))
    overall_status_lbl = tk.Label(result_summary, text="Overall: —", bg=palette["app_bg"],
                                   fg=palette["text_muted"], font=(theme.FONT_FAMILY, 12, "bold"))
    overall_status_lbl.pack(side=tk.LEFT)

    # ---------------- Results ----------------
    _section("RESULTS")
    results_area = tk.Frame(win, bg=palette["app_bg"], padx=12)
    results_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 8))

    table_frame = tk.Frame(results_area, bg=palette["app_bg"])
    table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    columns = ("tool", "severity", "code", "file", "line", "col", "message")
    tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
    headings = {"tool": "Tool", "severity": "Severity", "code": "Code", "file": "File",
                "line": "Line", "col": "Column", "message": "Message"}
    widths = {"tool": 90, "severity": 80, "code": 90, "file": 220, "line": 55, "col": 65, "message": 380}

    def _sort_by(col, reverse):
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]

        def _key(pair):
            val = pair[0]
            try:
                return (0, float(val))
            except (TypeError, ValueError):
                return (1, val.lower())

        rows.sort(key=_key, reverse=reverse)
        for index, (_, iid) in enumerate(rows):
            tree.move(iid, "", index)
        tree.heading(col, command=lambda: _sort_by(col, not reverse))

    for col in columns:
        tree.heading(col, text=headings[col], command=lambda c=col: _sort_by(c, False))
        tree.column(col, width=widths[col], anchor="w", stretch=(col == "message"))
    vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    tree.tag_configure("ERROR", foreground=palette["error"])
    tree.tag_configure("FATAL", foreground=palette["error"])
    tree.tag_configure("WARNING", foreground=palette["warning"])
    tree.tag_configure("USAGE", foreground=palette["text_muted"])
    tree.tag_configure("INFO", foreground=palette["text_muted"])

    # Error detail panel (spec section 10: "Prepare the architecture so
    # Phase 3 can add exact editor navigation... Do not create a second
    # error-management system") - reads the SAME row data the table itself
    # holds (state["row_details"], keyed by the tree item id), never a
    # separate/duplicated results store.
    detail_frame = tk.Frame(results_area, bg=palette["surface"], highlightthickness=1,
                             highlightbackground=palette["border"], padx=10, pady=8)
    detail_frame.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
    detail_fields = {}
    # Root Cause / Repairability (spec: "EPUBForge - PHASE 2 ONLY -
    # Universal Error Analyzer" section 4: "Selecting an error must
    # display: Code, Severity, Message, File, Line, Column, Root Cause,
    # Repairability") - the SAME detail panel Phase 1 already built, only
    # extended with two more rows, never a second error-detail UI.
    for label_text, key in [("File:", "file"), ("Line:", "line"), ("Column:", "col"),
                             ("Code:", "code"), ("Severity:", "severity"), ("Message:", "message"),
                             ("Root Cause:", "root_cause"), ("Repairability:", "repairability")]:
        row = tk.Frame(detail_frame, bg=palette["surface"])
        row.pack(side=tk.TOP, fill=tk.X, anchor="w")
        tk.Label(row, text=label_text, bg=palette["surface"], fg=palette["text_muted"], font=theme.FONT_SMALL,
                 width=12, anchor="w").pack(side=tk.LEFT)
        value_lbl = tk.Label(row, text="—", bg=palette["surface"], fg=palette["text"], font=theme.FONT_BODY,
                              anchor="w", justify=tk.LEFT, wraplength=1000)
        value_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        detail_fields[key] = value_lbl

    def _on_select_row(_event=None):
        selection = tree.selection()
        if not selection:
            return
        detail = state["row_details"].get(selection[0])
        if not detail:
            return
        for key, lbl in detail_fields.items():
            lbl.config(text=str(detail.get(key, "—")) or "—")

    tree.bind("<<TreeviewSelect>>", _on_select_row)

    # ---------------- status bar ----------------
    status_bar = tk.Label(win, text=f"Status: {STATE_READY}", bg=palette["header_bg"],
                           fg=palette["header_fg_muted"], font=theme.FONT_SMALL, anchor="w", padx=10)
    status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _set_state(new_state: str, detail: str = ""):
        text = f"Status: {new_state}" + (f" — {detail}" if detail else "")
        status_bar.config(text=text)
        win.update_idletasks()

    # ---------------- handlers ----------------
    def _open_epub():
        path = filedialog.askopenfilename(
            title="Open EPUB", filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")], parent=win)
        if not path:
            return
        _set_state(STATE_OPENING, os.path.basename(path))
        state["epub_path"] = path
        file_label.config(text=os.path.basename(path), fg=palette["text"])
        tree.delete(*tree.get_children())
        state["row_details"] = {}
        for lbl in detail_fields.values():
            lbl.config(text="—")
        quick_status_lbl.config(text="Quick Validator: —", fg=palette["text_muted"])
        epubcheck_status_lbl.config(text="EPUBCheck: —", fg=palette["text_muted"])
        overall_status_lbl.config(text="Overall: —", fg=palette["text_muted"])

        _set_state(STATE_INSPECTING, "reading package structure")
        package = package_reader.read_package(path)
        state["package"] = package
        _set_info(package)

        if package.error:
            validate_btn.config(state="disabled")
            repair_btn.config(state="disabled")
            edit_btn.config(state="disabled")
            _set_state(STATE_FAILED, package.error)
            return
        validate_btn.config(state="normal")
        repair_btn.config(state="normal")
        edit_btn.config(state="normal")
        client_btn.config(state="normal")
        _set_state(STATE_READY, "package loaded - click Validate Only")

    def _add_row(tool, severity, code, file_, line, col, message):
        iid = tree.insert("", "end", values=(tool, severity, code, file_, line, col, message),
                           tags=(severity,))
        state["row_details"][iid] = {"file": file_, "line": line, "col": col, "code": code,
                                      "severity": severity, "message": message,
                                      "root_cause": "—", "repairability": "—"}
        return iid

    def _apply_analysis(iid, analysis):
        """Writes Phase 2's Root Cause / Repairability (core.epub.
        error_analyzer.ErrorAnalysis) into the SAME state["row_details"]
        dict the table/detail-panel already share - no second
        error-management system."""
        cascade_note = " [cascading]" if analysis.root_cause.is_cascading else ""
        state["row_details"][iid]["root_cause"] = (
            f"{analysis.root_cause.category}{cascade_note} — {analysis.root_cause.explanation}")
        state["row_details"][iid]["repairability"] = (
            f"{analysis.repair_plan.repairability.value} — {analysis.repair_plan.summary}")

    def _run_validation():
        """Phase 3 refactor: the actual 'run Quick Validator + real
        EPUBCheck + Error Analyzer' sequence now lives in ONE place,
        core.epub.validation_runner.run_validation (spec: "ONE
        implementation only") - reused identically by core.epub.
        repair_engine's own before/after passes. This function only
        renders the returned ValidationSnapshot into the UI."""
        package = state["package"]
        epub_path = state["epub_path"]
        if not package or package.error:
            return
        from core.epub import validation_runner
        validate_btn.config(state="disabled")
        repair_btn.config(state="disabled")
        open_btn.config(state="disabled")
        tree.delete(*tree.get_children())
        state["row_details"] = {}
        for lbl in detail_fields.values():
            lbl.config(text="—")

        _set_state(STATE_VALIDATING, "running Quick Validator")
        snapshot = validation_runner.run_validation(epub_path)
        state["package"] = snapshot.package
        qv_result = snapshot.quick_result
        for issue, analysis in zip(qv_result.issues, snapshot.qv_analyses):
            iid = _add_row("Quick", issue.severity, issue.code, issue.file, "", "", issue.message)
            _apply_analysis(iid, analysis)
        quick_status_lbl.config(text=f"Quick Validator: {'PASS' if qv_result.is_valid else 'FAIL'}",
                                 fg=palette["success"] if qv_result.is_valid else palette["error"])

        ec_result = snapshot.epubcheck_result
        if ec_result is not None:
            _set_state(STATE_EPUBCHECK, "running real EPUBCheck (this can take several seconds)")
            if ec_result.ran:
                paired = sorted(
                    zip(ec_result.messages, snapshot.ec_analyses),
                    key=lambda p: ({"FATAL": 0, "ERROR": 1, "WARNING": 2}.get(p[0].severity, 3), p[0].file, p[0].line))
                for m, analysis in paired:
                    iid = _add_row("EPUBCheck", m.severity, m.code, m.file,
                                    m.line if m.line >= 0 else "", m.column if m.column >= 0 else "", m.message)
                    _apply_analysis(iid, analysis)
                epubcheck_status_lbl.config(text=f"EPUBCheck: {'PASS' if ec_result.is_valid else 'FAIL'}",
                                             fg=palette["success"] if ec_result.is_valid else palette["error"])
            else:
                _add_row("EPUBCheck", "ERROR", "EC-UNAVAILABLE", "", "", "",
                         f"EPUBCheck could not run: {ec_result.error}")
                epubcheck_status_lbl.config(text="EPUBCheck: FAIL", fg=palette["error"])
        else:
            _add_row("EPUBCheck", "WARNING", "EC-UNAVAILABLE", "", "", "",
                     "EPUBCheck is not available on this machine - only Quick Validator results are shown.")
            epubcheck_status_lbl.config(text="EPUBCheck: UNAVAILABLE", fg=palette["warning"])

        # Spec: "Never show PASS unless the actual checks pass" - Overall is
        # VALID only when Quick Validator passed AND EPUBCheck genuinely
        # ran and passed; anything else (EPUBCheck unavailable/failed to
        # run/found errors) is reported honestly, never silently upgraded.
        qv_ok = qv_result.is_valid
        ec_ran_and_ok = bool(ec_result and ec_result.ran and ec_result.is_valid)
        if qv_ok and ec_ran_and_ok:
            overall_status_lbl.config(text="Overall: VALID", fg=palette["success"])
            _set_state(STATE_COMPLETE, "VALID")
        else:
            overall_status_lbl.config(text="Overall: INVALID", fg=palette["error"])
            reason = "EPUBCheck unavailable" if ec_result is None else (
                "EPUBCheck failed to run" if not ec_result.ran else "errors found")
            _set_state(STATE_COMPLETE, f"INVALID - {reason}")

        validate_btn.config(state="normal")
        repair_btn.config(state="normal")
        open_btn.config(state="normal")

    def _show_repair_report(report):
        """spec: "EPUBForge - PHASE 3 - Universal Auto-Fix Engine" section
        8 ("Show: Initial Errors, Fixed Automatically, Review Required,
        Remaining, Files Modified, Files Added, Files Removed, Quick
        Validator, EPUBCheck, Content Integrity, Final Status... List every
        modification.") - a separate Toplevel rather than shoehorned into
        Phase 1's own exact, already-specified layout, since the
        modification list is unbounded in length."""
        top = tk.Toplevel(win)
        top.title("EPUBForge - Full Auto Repair Report")
        top.geometry("760x620")
        top.configure(bg=palette["app_bg"])

        btn_row = tk.Frame(top, bg=palette["app_bg"])
        btn_row.pack(side=tk.BOTTOM, fill=tk.X)
        close_btn = tk.Button(btn_row, text="Close", command=top.destroy)
        theme.style_button(close_btn, palette, kind="secondary")
        close_btn.pack(side=tk.RIGHT, padx=10, pady=8)

        text_frame = tk.Frame(top, bg=palette["app_bg"])
        text_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        report_text = tk.Text(text_frame, bg=palette["surface"], fg=palette["text"], font=theme.FONT_MONO_SMALL,
                               wrap="word", padx=10, pady=10, borderwidth=0, highlightthickness=0)
        vsb2 = ttk.Scrollbar(text_frame, orient="vertical", command=report_text.yview)
        report_text.configure(yscrollcommand=vsb2.set)
        report_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb2.pack(side=tk.RIGHT, fill=tk.Y)

        def _list(title, items):
            out = [f"{title} ({len(items)}):"]
            out.extend(f"  - {item}" for item in items) if items else out.append("  (none)")
            return out

        # spec: "ZERO CONTENT LOSS / CONTENT PRESERVATION" - "Before
        # declaring success, display... Content integrity: VERIFIED" -
        # every count below comes straight from RepairReport's own
        # original_*/final_* fields, which core.epub.repair_engine computed
        # via regression_checker.compute_fingerprint() (the SAME visible-
        # text extraction check_content_integrity() itself used to decide
        # pass/fail/rollback) - this block can never show a number that
        # disagrees with the actual pass/fail verdict above it.
        def _count_lines(label, before, after):
            lost = max(0, before - after)
            return [f"Original {label}:".ljust(24) + f"{before:,}",
                    f"Final {label}:".ljust(24) + f"{after:,}",
                    f"Lost {label}:".ljust(24) + f"{lost:,}", ""]

        integrity_lines = [
            "CONTENT INTEGRITY", "",
            *_count_lines("characters", report.original_characters, report.final_characters),
            *_count_lines("words", report.original_words, report.final_words),
            *_count_lines("images", report.original_images, report.final_images),
        ]
        integrity_lines += [
            "Original XHTML files:".ljust(24) + str(report.original_xhtml_files),
            "Final XHTML files:".ljust(24) + str(report.final_xhtml_files),
            "",
            "Content integrity:".ljust(24) + ("VERIFIED" if report.content_integrity_verified else "MISMATCH"),
        ]

        lines = [
            "FULL AUTO REPAIR REPORT", "=" * 60, "",
            f"Initial Errors:      {report.initial_errors}",
            f"Fixed Automatically: {report.fixed_automatically}",
            f"Review Required:     {report.review_required}",
            f"Remaining:           {report.remaining}",
            "",
            f"Passes Run: {report.passes_run}" + ("  (ROLLED BACK)" if report.rolled_back else ""),
            "",
            "Quick Validator:",
            f"  Before: {report.quick_validator_before}",
            f"  After:  {report.quick_validator_after}",
            "EPUBCheck:",
            f"  Before: {report.epubcheck_before}",
            f"  After:  {report.epubcheck_after}",
            "",
            f"Content Integrity: {report.content_integrity}",
            "",
            "=" * 60,
            *integrity_lines,
            "=" * 60,
            "",
            f"Final Status: {report.final_status}",
            "",
            f"Backup: {report.backup_path}",
            f"Output: {report.output_path}",
            "",
        ]
        lines += _list("Files Modified", report.files_modified) + [""]
        lines += _list("Files Added", report.files_added) + [""]
        lines += _list("Files Removed", report.files_removed) + [""]
        lines += _list("Modifications", report.modifications)

        report_text.insert("1.0", "\n".join(lines))
        report_text.config(state="disabled")
        if getattr(report, "dashboard", None):
            # closed-loop engine: the dashboard is the main view; this text stays as its Summary tab
            top.destroy()
            from app.validation import autofix_dashboard

            def _edit(path):
                from app.editor import editor_window
                editor_window.open_window(launcher_root, on_home, initial_epub_path=path)

            def _compare(pdf, epub):
                from app.qc import qc_window
                qc_window.open_window(launcher_root, None, pdf_path=pdf, epub_path=epub, master=win)
            return autofix_dashboard.show(win, report, summary_text="\n".join(lines), on_edit=_edit,
                                          on_compare=_compare, pdf_path=state.get("pdf_path"))
        return top

    def _client_profile():
        v = client_var.get()
        return v if v in _client_rules.PROFILES else None

    def _choose_client_log():
        path = filedialog.askopenfilename(parent=win, title="Client tool log (SPiXVali)",
                                          filetypes=[("Log / text", "*.log *.txt"), ("All files", "*.*")])
        state["client_log"] = path or None
        client_log_label.config(text=os.path.basename(path) if path else "")

    def _run_client_validation():
        """The client's rules on their own - rows tagged with the profile name
        are added to the table (previous client rows are replaced)."""
        profile = _client_profile()
        epub_path = state["epub_path"]
        if not profile or not epub_path:
            messagebox.showinfo("Client Validate", "Choose a client profile and open an EPUB first.", parent=win)
            return
        import zipfile
        validate, _fixes = _client_rules.PROFILES[profile]
        with zipfile.ZipFile(epub_path) as zf:
            files = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.filename.endswith("/")}
        report = validate(files, os.path.basename(epub_path))
        for iid in list(tree.get_children()):
            if tree.item(iid, "values")[0] == profile:
                tree.delete(iid)
                state["row_details"].pop(iid, None)
        from core.epub.client_rules import cupepub
        for f in report.findings:
            iid = _add_row(profile, "ERROR" if f.severity == "Error" else "WARNING", f.code, f.file, f.line, f.col,
                           f.message)
            state["row_details"][iid]["root_cause"] = f"CLIENT RULES ({profile})"
            state["row_details"][iid]["repairability"] = (
                f"automatic: {f.fix}" if f.fix else "manual") + " - " + cupepub.manual_action(f.code)
        c = report.counts()
        extra = ""
        if state.get("client_log"):
            with open(state["client_log"], encoding="utf-8", errors="replace") as fh:
                tool = _client_rules.parse_log(fh.read())
            diff = [f"{code}: tool {a} / native {b}" for code, a, b in _client_rules.compare(tool, report) if a != b]
            extra = (f"\n\nClient tool log: {tool.totals}." + ("\nDifferences per rule:\n" + "\n".join(diff[:20])
                                                                if diff else "\nSame counts per rule."))
        _set_state(STATE_COMPLETE, f"{profile}: {c['Error']} error(s), {c['Warning']} warning(s)")
        messagebox.showinfo("Client Validate", f"{profile}: {c['Error']} error(s), {c['Warning']} warning(s).\n\n"
                            "VALIDATE & AUTO-FIX repairs what can be repaired safely." + extra, parent=win)

    def _choose_pdf():
        path = filedialog.askopenfilename(parent=win, title="Source PDF (optional)",
                                          filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        state["pdf_path"] = path or None
        pdf_label.config(text=os.path.basename(path) if path else "")

    def _run_repair():
        """VALIDATE & AUTO-FIX EPUB - core.epub.repair_engine's closed loop,
        on a worker thread (EPUBCheck runs many times); progress and the
        result come back through a queue polled on the Tk thread. The
        original EPUB is never modified."""
        package = state["package"]
        epub_path = state["epub_path"]
        if not package or not epub_path:
            return
        proceed = messagebox.askyesno(
            "Validate & Auto-Fix EPUB",
            "A protected backup, a '<name>_autofix' session folder and a new '<name>.repaired.epub' are "
            "created. The original EPUB is never modified.\n\n"
            "Only safe / high-confidence repairs are applied, each checked for content loss and re-validated "
            "with EPUBCheck; anything uncertain is listed for manual review - never guessed, never deleted."
            + (f"\n\nSource PDF: {os.path.basename(state['pdf_path'])}" if state.get("pdf_path") else "")
            + (f"\n\nClient rules: {_client_profile()} (validated and repaired as well)" if _client_profile() else "")
            + "\n\nContinue?", parent=win)
        if not proceed:
            return
        import queue
        import threading
        from core.epub import repair_engine
        for b in (validate_btn, repair_btn, open_btn, pdf_btn, client_btn):
            b.config(state="disabled")
        _set_state(STATE_VALIDATING, "starting Validate & Auto-Fix")
        q = queue.Queue()
        pdf_path = state.get("pdf_path")
        client_profile = _client_profile()
        client_log = state.get("client_log")

        def work():
            try:
                rep = repair_engine.run_full_auto_repair(epub_path, progress=lambda t: q.put(("progress", t)),
                                                         pdf_path=pdf_path, client_profile=client_profile,
                                                         client_log=client_log)
                q.put(("done", rep))
            except Exception as e:  # noqa: BLE001 - shown to the user, never crashes the window
                q.put(("error", e))
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "progress":
                        _set_state(STATE_VALIDATING, val)
                    elif kind == "error":
                        messagebox.showerror("Validate & Auto-Fix", f"Could not complete: {val}", parent=win)
                        _set_state(STATE_FAILED, str(val))
                        _finish()
                        return
                    else:
                        _set_state(STATE_COMPLETE, f"{val.overall} - {val.final_status}")
                        state["last_repair_report"] = val
                        _show_repair_report(val)
                        _finish()
                        return
            except queue.Empty:
                pass
            win.after(150, poll)

        def _finish():
            for b in (validate_btn, repair_btn, open_btn, pdf_btn, client_btn):
                b.config(state="normal")
        poll()

    def _open_editor():
        """spec: "EPUBForge - PHASE 4 ONLY - Real EPUB Editor". Opens
        app.editor.editor_window as a SIBLING window using the SAME
        launcher_root/on_home this window itself received (not `win`) -
        its own "← Home" then behaves identically to this window's."""
        epub_path = state["epub_path"]
        if not epub_path:
            return
        from app.editor import editor_window
        editor_window.open_window(launcher_root, on_home, initial_epub_path=epub_path)

    def _show_test_suite_report(report):
        """spec A3/A4: suite-level totals + version info, and a sortable
        per-test results table (Test ID/Description/Expected/Actual/
        Status/Message) - the SAME Toplevel-report-window pattern as
        _show_repair_report, never a second report architecture. Also
        writes JSON (required) + HTML (preferred) report files under the
        existing writable-data directory (spec A4)."""
        from core.resource_path import writable_path
        import datetime as _dt

        reports_dir = writable_path("test_suite_reports")
        os.makedirs(reports_dir, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(reports_dir, f"w3c_test_suite_{stamp}.json")
        html_path = os.path.join(reports_dir, f"w3c_test_suite_{stamp}.html")
        test_suite_runner.write_json_report(report, json_path)
        test_suite_runner.write_html_report(report, html_path)

        top = tk.Toplevel(win)
        top.title("EPUBForge - W3C EPUBCheck Test Suite Report")
        top.geometry("1150x700")
        top.configure(bg=palette["app_bg"])

        btn_row = tk.Frame(top, bg=palette["app_bg"])
        btn_row.pack(side=tk.BOTTOM, fill=tk.X)
        close_btn = tk.Button(btn_row, text="Close", command=top.destroy)
        theme.style_button(close_btn, palette, kind="secondary")
        close_btn.pack(side=tk.RIGHT, padx=10, pady=8)

        summary = tk.Frame(top, bg=palette["app_bg"], padx=12, pady=10)
        summary.pack(side=tk.TOP, fill=tk.X)
        tk.Label(summary, justify=tk.LEFT, anchor="w", bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_BODY_BOLD,
                 text=(f"Test Suite Version: {report.suite_version}    "
                       f"EPUBCheck Version: {report.epubcheck_version}    "
                       f"Duration: {report.duration_seconds:.1f}s\n"
                       f"Total: {report.total}    Passed: {report.passed}    Failed: {report.failed}    "
                       f"Skipped: {report.skipped}    Errors: {report.errors}")).pack(side=tk.TOP, anchor="w")
        tk.Label(summary, justify=tk.LEFT, anchor="w", bg=palette["app_bg"], fg=palette["text_muted"],
                 font=theme.FONT_SMALL,
                 text=f"JSON report: {json_path}\nHTML report: {html_path}").pack(
            side=tk.TOP, anchor="w", pady=(4, 0))

        table_frame = tk.Frame(top, bg=palette["app_bg"], padx=12)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(8, 8))
        columns = ("test_id", "feature", "description", "expected", "actual", "status", "message")
        headings = {"test_id": "Test ID", "feature": "Feature", "description": "Description",
                    "expected": "Expected", "actual": "Actual", "status": "Status", "message": "Message"}
        widths = {"test_id": 150, "feature": 130, "description": 220, "expected": 130,
                  "actual": 130, "status": 70, "message": 260}
        tree_ts = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            tree_ts.heading(col, text=headings[col])
            tree_ts.column(col, width=widths[col], anchor="w", stretch=(col in ("description", "message")))
        vsb_ts = ttk.Scrollbar(table_frame, orient="vertical", command=tree_ts.yview)
        tree_ts.configure(yscrollcommand=vsb_ts.set)
        tree_ts.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb_ts.pack(side=tk.RIGHT, fill=tk.Y)
        tree_ts.tag_configure("PASS", foreground=palette["success"])
        tree_ts.tag_configure("FAIL", foreground=palette["error"])
        tree_ts.tag_configure("ERROR", foreground=palette["error"])
        tree_ts.tag_configure("SKIPPED", foreground=palette["warning"])
        for r in report.results:
            tree_ts.insert("", "end", tags=(r.status,),
                            values=(r.test_id, r.feature, r.description, r.expected, r.actual, r.status, r.message))

    def _run_test_suite():
        """spec Part A: runs the OFFICIAL, locally-bundled W3C EPUBCheck
        Test Suite against the bundled EPUBCheck - entirely independent of
        whatever EPUB the user has open (never reads/writes state[
        "epub_path"]/state["package"], spec A5's isolation requirement).
        A real run is hundreds of separate EPUBCheck invocations and runs
        synchronously on this thread, matching every other EPUBCheck call
        in this window - the user is warned about the duration up front."""
        proceed = messagebox.askyesno(
            "Run W3C EPUBCheck Test Suite",
            "This runs the full official W3C EPUBCheck Test Suite (hundreds of real scenarios) "
            "against the bundled EPUBCheck.\n\nThis can take several minutes, during which "
            "EPUBForge will be unresponsive. It does not touch any EPUB you have open.\n\nContinue?",
            parent=win)
        if not proceed:
            return
        test_suite_btn.config(state="disabled")
        open_btn.config(state="disabled")
        validate_btn.config(state="disabled")
        repair_btn.config(state="disabled")
        edit_btn.config(state="disabled")
        _set_state(STATE_VALIDATING, "starting W3C EPUBCheck Test Suite")
        report = test_suite_runner.run_test_suite(progress=lambda text: _set_state(STATE_VALIDATING, text))
        _set_state(STATE_COMPLETE, "Test Suite Complete")
        _show_test_suite_report(report)
        test_suite_btn.config(state="normal")
        open_btn.config(state="normal")
        if state["package"] and not state["package"].error:
            validate_btn.config(state="normal")
            repair_btn.config(state="normal")
            edit_btn.config(state="normal")

    open_btn.config(command=_open_epub)
    validate_btn.config(command=_run_validation)
    repair_btn.config(command=_run_repair)
    pdf_btn.config(command=_choose_pdf)
    client_btn.config(command=_run_client_validation)
    client_log_btn.config(command=_choose_client_log)
    edit_btn.config(command=_open_editor)
    test_suite_btn.config(command=_run_test_suite)

    # Exposed for automated testing (tests/test_repair_report_content_
    # integrity_display.py and any future external driver) - matches the
    # same win.editor_state/editor_actions convention app.editor.
    # editor_window.py already uses; this module is otherwise pure
    # closures with no way to observe or drive it from outside.
    win.validation_state = state
    win.validation_widgets = {"open_btn": open_btn, "validate_btn": validate_btn, "repair_btn": repair_btn,
                               "pdf_btn": pdf_btn, "client_btn": client_btn, "client_combo": client_combo,
                               "client_log_btn": client_log_btn,
                               "edit_btn": edit_btn, "tree": tree}
    win.validation_actions = {"open_epub": _open_epub, "run_validation": _run_validation,
                               "run_client_validation": _run_client_validation,
                               "run_repair": _run_repair, "show_repair_report": _show_repair_report}

    return win
