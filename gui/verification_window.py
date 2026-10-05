"""Verification window - the operator-facing UI for core/verification/.

An entirely new, additive Toplevel (gui/main_window.py's App.
open_verification_window is the only integration point). Reuses the
existing PDFDocument/ZoneManager already open in the main app; every
mutation goes through core.verification.verification_orchestrator,
which itself reuses the existing zone_manager.set_zone_text mechanism.

Redesigned (see the plan this was built from) to fix 8 confirmed defects
in the first build: blank panels on open, plain-text-only display (no
real bold/italic/sup/sub rendering), style application triggering a full
recheck + full widget rebuild (multi-second freeze), no PDF<->text
synchronization, no zone panel, no revision protection, no autosave, and
no Fast/Deep mode or alignment/structure checks.

Key invariant this file depends on throughout: the Text widget's own
displayed characters are ALWAYS exactly core.verification.span_model.
plain_text_of(self.current_spans) - style changes only ever affect Tk
tag formatting (font/weight/slant/underline/overstrike/offset/size),
never the character sequence itself. This keeps every character offset
computed via text_widget.count(...) valid against the Span model with
no translation layer."""
import copy
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

from core.verification import verification_orchestrator as orch
from core.verification import verification_models as vm
from core.verification import span_model as sm
from core.verification import paragraph_model as pm
from core.verification.undo_stack import UndoStack, Operation

STATUS_READY = "READY"
STATUS_LOADING = "LOADING"
STATUS_VERIFYING = "VERIFYING"
STATUS_ISSUES_FOUND = "ISSUES FOUND"
STATUS_PARTIALLY_VERIFIED = "PARTIALLY VERIFIED"
STATUS_VERIFIED = "VERIFIED"
STATUS_SAVE_REQUIRED = "SAVE REQUIRED"

_STATUS_COLORS = {
    STATUS_READY: "#3a7", STATUS_LOADING: "#888", STATUS_VERIFYING: "#e8a33d",
    STATUS_ISSUES_FOUND: "#d9534f", STATUS_PARTIALLY_VERIFIED: "#e8a33d",
    STATUS_VERIFIED: "#3a7", STATUS_SAVE_REQUIRED: "#d9534f",
}

# (button label, Span field name, keyboard shortcut display text)
_STYLE_BUTTONS = [
    ("B", "bold", "Ctrl+B"), ("I", "italic", "Ctrl+I"), ("U", "underline", "Ctrl+U"),
    ("S", "strike", None), ("x²", "superscript", "Ctrl+Shift+X"),
    ("x₂", "subscript", "Ctrl+Shift+D"), ("Small Caps", "small_caps", "Ctrl+Shift+M"),
]

AUTOSAVE_DEBOUNCE_MS = 2000
_SEVERITY_COLORS = {"HIGH": "#d9534f", "MEDIUM": "#e8a33d", "LOW": "#e0c341"}


class VerificationWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.pdf = app.pdf_document
        self.zone_manager = app.zone_manager
        self.title("ZoneTool — Verification")
        self.geometry("1440x900")

        self.session = app.verification_session or vm.VerificationSession()
        self.document_revision = 0
        self._dirty = False
        self._save_failed = False
        self._autosave_after_id = None
        self._verifying = False
        self._has_run_once = bool(self.session.issues) or app.verification_session is not None
        self._partial_error = False
        self.undo_stack = UndoStack()

        self.current_zone_id = None
        self.current_issue = None
        self.current_spans = []          # list[span_model.Span] for the CURRENTLY LOADED paragraph/zone
        self.current_zone_line_ranges = []  # [(char_start, char_end, pdf_line_bbox), ...] for the FOCUSED member zone
        self._page_photo = None
        self._pdf_render_scale = 1.0
        self._configured_tags = set()
        self._base_font = tkfont.Font(family="Georgia", size=12)

        # Paragraph model (Pass 3): a logical paragraph can span multiple
        # zones (Merge Previous) - built once from existing zone/merge
        # state (core.verification.paragraph_model, itself reusing
        # core.hierarchy's own reading_order.flatten_document_order +
        # paragraph_merge.merge_top_level_stream, the SAME functions real
        # XHTML generation uses), rebuilt whenever zone structure changes
        # (merge/split/undo-redo of either). current_paragraph_id/
        # current_paragraph_segments describe the CURRENTLY LOADED
        # paragraph view; segments map character ranges of self.
        # current_spans back to the underlying member zone(s) for
        # edit-routing (see _resolve_edit_segment).
        self.current_paragraph_id = None
        self.current_paragraph_segments = []   # list[paragraph_model.ParagraphSegment]
        self._paragraph_header_normal_text = ""
        self._issue_filter_group = None        # None = show all groups; else one group name from ISSUE_GROUPS

        # Independent PDF pane state - decoupled from "which zone is
        # loaded": an operator can pan/zoom/page-navigate the PDF pane on
        # its own, while the currently selected zone/issue still auto-
        # follows (jumps the displayed page) whenever a NEW zone/issue is
        # explicitly selected (see _refresh_pdf_pane's follow_zone param).
        self._pdf_page_num = 1
        self._pdf_zoom = 1.0
        self._pdf_zoom_mode = "fit_width"  # "fit_width" | "fit_page" | "custom"

        # LEAF zones only (no children) - a zone WITH children is a
        # structural container whose own bbox typically spans its entire
        # subtree (confirmed real bug: a "p" zone wrapping 27 nested
        # child fragments has a bbox covering the whole block, so
        # extracting/displaying/navigating to the CONTAINER itself as if
        # it were one leaf zone silently swallowed every child's own
        # individual identity - see core/verification/paragraph_model.py
        # for how those children instead become individually-addressable
        # members of a proper multi-zone paragraph). Never touches zone
        # creation/parenting itself - purely which zones this window's
        # own navigation considers independently selectable.
        self._zones_order = sorted(
            (z for z in self.zone_manager.zones.values() if not z.children),
            key=lambda z: (z.page, z.serial or 0))
        self.paragraph_index = pm.ParagraphIndex()
        self._rebuild_paragraph_index()

        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Spec section 3: the UI must load immediately - the current zone's
        # PDF render, real formatted text, and cached issues all appear
        # BEFORE any background verification scan starts or finishes.
        self._set_status(STATUS_LOADING)
        if self._zones_order:
            self._load_zone_into_panes(self._zones_order[0].zone_id)
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._update_status_badge()

        if not self._has_run_once:
            self.run_verification()

    # ================================================================
    # UI construction
    # ================================================================

    def _build_ui(self):
        # ---- toolbar row 1 ----
        row1 = tk.Frame(self)
        row1.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 0))
        tk.Button(row1, text="Run", command=self.run_verification).pack(side=tk.LEFT, padx=2)
        tk.Button(row1, text="Save", command=self.save_verification).pack(side=tk.LEFT, padx=2)
        # Main toolbar navigation drives ZONE-BY-ZONE reading (spec
        # section 1: "the user needs to read and verify the book zone-
        # by-zone... Do NOT navigate from issue to issue"), reusing the
        # exact same previous_zone/next_zone methods the Zone Navigator
        # panel's own buttons already call - one navigation mechanism,
        # not two. Issue-to-issue navigation stays available (Alt+N/
        # Alt+P, and the issue list's own selection) but no longer
        # drives these primary toolbar buttons.
        tk.Button(row1, text="← Previous Zone", command=self.previous_zone).pack(side=tk.LEFT, padx=2)
        tk.Button(row1, text="Next Zone →", command=self.next_zone).pack(side=tk.LEFT, padx=2)
        self.undo_btn = tk.Button(row1, text="↶ Undo", command=self.undo, state="disabled")
        self.undo_btn.pack(side=tk.LEFT, padx=(10, 1))
        self.redo_btn = tk.Button(row1, text="↷ Redo", command=self.redo, state="disabled")
        self.redo_btn.pack(side=tk.LEFT, padx=1)
        tk.Label(row1, text="Mode:").pack(side=tk.LEFT, padx=(10, 2))
        self.mode_var = tk.StringVar(value="FAST")
        ttk.Combobox(row1, textvariable=self.mode_var, values=["FAST", "DEEP"], state="readonly",
                     width=6).pack(side=tk.LEFT)
        self.status_badge = tk.Label(row1, text=STATUS_LOADING, fg="white", bg="#888",
                                      padx=8, pady=2, font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.status_badge.pack(side=tk.RIGHT, padx=6)
        self.status_badge.bind("<Button-1>", lambda e: self._on_status_badge_click())
        self.dirty_label = tk.Label(row1, text="")
        self.dirty_label.pack(side=tk.RIGHT, padx=6)

        # ---- toolbar row 2 (recheck scope) ----
        row2 = tk.Frame(self)
        row2.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(2, 4))
        tk.Button(row2, text="Recheck Selection (Ctrl+Shift+R)",
                  command=self.recheck_selection).pack(side=tk.LEFT, padx=2)
        tk.Button(row2, text="Recheck Zone", command=self.recheck_current_zone).pack(side=tk.LEFT, padx=2)
        tk.Button(row2, text="Recheck Page", command=self.recheck_current_page).pack(side=tk.LEFT, padx=2)
        tk.Button(row2, text="Recheck Book", command=self.recheck_book).pack(side=tk.LEFT, padx=2)

        # ---- status bar ----
        status_bar = tk.Frame(self, bg="#eee")
        status_bar.pack(side=tk.TOP, fill=tk.X)
        self.info_label = tk.Label(status_bar, text="", bg="#eee", anchor="w", padx=6, pady=2)
        self.info_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ---- outer draggable vertical splitter (spec section 9): PDF|text
        # area on top, the whole issues/tabs/tree/detail/zone-navigator
        # cluster below - replacing the old fixed pack-stacking so the
        # operator can drag to see more of either side. minsize=180 on
        # the bottom pane is a HARD Tk-enforced floor (spec: "do not
        # allow it to collapse accidentally to zero") - never bypassable
        # by dragging. The initial ~30% default position and the ~50%
        # max-height clamp are applied after the window first realizes
        # (see _set_initial_splitter_position/_clamp_splitter_position).
        self.outer_paned = tk.PanedWindow(self, orient=tk.VERTICAL, sashwidth=6, sashrelief=tk.RAISED)
        self.outer_paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.outer_paned.bind("<B1-Motion>", self._clamp_splitter_position)

        # ---- main split: PDF | rich text ----
        main = tk.PanedWindow(self.outer_paned, orient=tk.HORIZONTAL, sashwidth=4)

        left = tk.Frame(main, bg="#222")
        tk.Label(left, text="PDF SOURCE", bg="#222", fg="#aaa", anchor="w").pack(side=tk.TOP, fill=tk.X)

        pdf_nav = tk.Frame(left, bg="#222")
        pdf_nav.pack(side=tk.TOP, fill=tk.X)
        tk.Button(pdf_nav, text="◀", width=3, command=self.pdf_prev_page).pack(side=tk.LEFT, padx=1, pady=1)
        self.pdf_page_var = tk.StringVar(value="1")
        pdf_page_entry = tk.Entry(pdf_nav, textvariable=self.pdf_page_var, width=4, justify="center")
        pdf_page_entry.pack(side=tk.LEFT, padx=1)
        pdf_page_entry.bind("<Return>", lambda e: self.pdf_goto_page(self.pdf_page_var.get()))
        self.pdf_page_total_label = tk.Label(pdf_nav, text="/ 0", bg="#222", fg="#aaa")
        self.pdf_page_total_label.pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(pdf_nav, text="▶", width=3, command=self.pdf_next_page).pack(side=tk.LEFT, padx=1, pady=1)
        tk.Button(pdf_nav, text="−", width=3, command=self.pdf_zoom_out).pack(side=tk.LEFT, padx=(10, 1))
        self.pdf_zoom_label = tk.Label(pdf_nav, text="100%", bg="#222", fg="#aaa", width=5)
        self.pdf_zoom_label.pack(side=tk.LEFT)
        tk.Button(pdf_nav, text="+", width=3, command=self.pdf_zoom_in).pack(side=tk.LEFT, padx=1)
        tk.Button(pdf_nav, text="Fit Width", command=self.pdf_fit_width).pack(side=tk.LEFT, padx=(10, 1))
        tk.Button(pdf_nav, text="Fit Page", command=self.pdf_fit_page).pack(side=tk.LEFT, padx=1)

        pdf_canvas_frame = tk.Frame(left, bg="#222")
        pdf_canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        # yscrollincrement matches gui/pdf_viewer.py's PDFViewerPanel own
        # SCROLL_UNIT_PX convention, and mouse-wheel handling below reuses
        # that same module's normalize_wheel_units (already documented
        # there as shared across every Canvas-based scrollable panel in
        # the app) - no second wheel-normalization implementation.
        self.pdf_canvas = tk.Canvas(pdf_canvas_frame, bg="#222", highlightthickness=0, yscrollincrement=16)
        self.pdf_vbar = tk.Scrollbar(pdf_canvas_frame, orient=tk.VERTICAL, command=self.pdf_canvas.yview)
        self.pdf_hbar = tk.Scrollbar(pdf_canvas_frame, orient=tk.HORIZONTAL, command=self.pdf_canvas.xview)
        self.pdf_canvas.configure(yscrollcommand=self.pdf_vbar.set, xscrollcommand=self.pdf_hbar.set)
        self.pdf_canvas.grid(row=0, column=0, sticky="nsew")
        self.pdf_vbar.grid(row=0, column=1, sticky="ns")
        self.pdf_hbar.grid(row=1, column=0, sticky="ew")
        pdf_canvas_frame.grid_rowconfigure(0, weight=1)
        pdf_canvas_frame.grid_columnconfigure(0, weight=1)
        # Deliberately no <Configure>-triggered re-render: confirmed
        # directly as a real Tk feedback risk (this canvas's own
        # scrollregion config can re-fire <Configure> even with unchanged
        # width/height, and a debounce alone does not reliably break that
        # within a single script-driven update() call). The pane re-
        # renders on every explicit navigation (zone/issue/page/zoom
        # change) already, which covers every functional requirement.
        self.pdf_canvas.bind("<Button-1>", self._on_pdf_click)
        for widget in (self.pdf_canvas, self.pdf_vbar, self.pdf_hbar):
            widget.bind("<MouseWheel>", self._on_pdf_wheel)
            widget.bind("<Shift-MouseWheel>", self._on_pdf_wheel_h)
            widget.bind("<Control-MouseWheel>", self._on_pdf_ctrl_wheel)
            widget.bind("<Button-4>", lambda e: self.pdf_canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self.pdf_canvas.yview_scroll(3, "units"))
        main.add(left, width=480)

        center = tk.Frame(main)
        tk.Label(center, text="EXTRACTED / VERIFIED TEXT", anchor="w").pack(side=tk.TOP, fill=tk.X)
        style_bar = tk.Frame(center)
        style_bar.pack(side=tk.TOP, fill=tk.X)
        self._style_widgets = {}
        for label, field, accel in _STYLE_BUTTONS:
            b = tk.Button(style_bar, text=label, width=4 if label not in ("Small Caps",) else 10,
                          command=lambda f=field: self.apply_style_to_selection(f))
            b.pack(side=tk.LEFT, padx=1, pady=2)
            self._style_widgets[field] = b
        tk.Button(style_bar, text="Undo", command=self.undo).pack(side=tk.LEFT, padx=(10, 1))
        tk.Button(style_bar, text="Redo", command=self.redo).pack(side=tk.LEFT, padx=1)
        # Merge w/ Previous / Split live in the ZONE NAVIGATOR panel below
        # (see zone_nav_buttons) - a single, non-duplicated location now
        # that panel exists (Pass 3), rather than two buttons doing the
        # same thing in two places.

        # undo=False: Tk's own built-in text undo only ever covers raw
        # character content, not the style/attribute changes this window
        # also needs to undo - core.verification.undo_stack.UndoStack
        # (self.undo_stack) is the ONE real undo/redo mechanism for this
        # window, covering every mutation type uniformly (see self.undo/
        # self.redo and _snapshot_correction_state/_restore_correction_
        # state below). Leaving Tk's own undo=True enabled alongside it
        # would let Ctrl+Z(unbound here, but still reachable via a native
        # Text widget accelerator on some platforms) silently desync the
        # displayed text from self.current_spans.
        # Paragraph header (spec section 6: "Paragraph P001" above the
        # text) - a separate Label, never inserted INTO the Text widget's
        # own editable character stream, so every character-offset
        # computation against self.current_spans stays untranslated.
        # Blank/hidden for an ordinary single-zone paragraph (the common
        # case) - identical visual output to before Pass 3.
        self.paragraph_header_label = tk.Label(center, text="", anchor="w", font=("Segoe UI", 9, "bold"),
                                                 fg="#555")
        self.paragraph_header_label.pack(side=tk.TOP, fill=tk.X)
        self.text_widget = tk.Text(center, wrap="word", undo=False, font=self._base_font)
        self.text_widget.pack(fill=tk.BOTH, expand=True)
        self.text_widget.tag_configure("active_issue", background="#ffd6d6")
        self.text_widget.tag_configure("paragraph_boundary_marker", background="#eef", foreground="#77a")
        # Visual-only paragraph spacing (spec section 12: "approximately
        # one normal text line of vertical spacing after each paragraph
        # ... do NOT insert fake newline characters into the actual
        # document text"). Tk's own spacing3 (extra pixels rendered AFTER
        # a tagged line) is exactly this - a display/layout property,
        # never a character in self.current_spans/zone.text, so it can
        # never leak into XHTML generation. Applied only at a genuine
        # zone-segment break within a joined multi-zone view where the
        # separator is a real newline (see _apply_zone_gap_spacing) -
        # never in the middle of one flowing merged-chain sentence.
        self.text_widget.tag_configure("zone_paragraph_gap", spacing3=self._base_font.metrics("linespace"))
        self.text_widget.bind("<<Modified>>", self._on_text_modified)
        self.text_widget.bind("<ButtonRelease-1>", self._on_text_cursor_moved)
        self.text_widget.bind("<KeyRelease>", self._on_text_cursor_moved)
        self._context_menu = self._build_context_menu()
        self.text_widget.bind("<Button-3>", self._show_context_menu)
        main.add(center, width=520)
        self.outer_paned.add(main, minsize=300)

        # ---- issue area (spec section 9: everything below the PDF|text
        # split lives in ONE draggable pane) ----
        issue_area = tk.Frame(self.outer_paned)

        # ---- category tab bar (spec section 10) ----
        tabs_row = tk.Frame(issue_area)
        tabs_row.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 0))
        self._category_tab_buttons = {}
        self._build_category_tabs(tabs_row)

        # ---- issues panel (with category counters) ----
        issues_frame = tk.Frame(issue_area)
        issues_frame.pack(side=tk.TOP, fill=tk.X, padx=6)
        self.category_counter_label = tk.Label(issues_frame, text="", anchor="w", justify="left",
                                                 font=("Segoe UI", 9))
        self.category_counter_label.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))

        issue_tree_row = tk.Frame(issue_area)
        issue_tree_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(2, 4))
        self.issue_tree = ttk.Treeview(issue_tree_row, columns=("page", "zone", "paragraph", "status", "confidence"),
                                        show="tree headings", height=8)
        self.issue_tree.heading("#0", text="Issue")
        self.issue_tree.heading("page", text="Page")
        self.issue_tree.heading("zone", text="Zone")
        self.issue_tree.heading("paragraph", text="Paragraph")
        self.issue_tree.heading("status", text="Status")
        self.issue_tree.heading("confidence", text="Conf.")
        self.issue_tree.column("page", width=50)
        self.issue_tree.column("zone", width=60)
        self.issue_tree.column("paragraph", width=70)
        self.issue_tree.column("status", width=110)
        self.issue_tree.column("confidence", width=70)
        self.issue_tree.pack(fill=tk.BOTH, expand=True)
        self.issue_tree.bind("<<TreeviewSelect>>", self._on_issue_selected)

        # ---- bottom split: evidence | zone panel ----
        bottom = tk.PanedWindow(issue_area, orient=tk.HORIZONTAL, sashwidth=4, height=190)
        bottom.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4))

        evidence = tk.Frame(bottom)
        tk.Label(evidence, text="SELECTED ISSUE", anchor="w").pack(side=tk.TOP, fill=tk.X)
        self.detail_text = tk.Text(evidence, height=7, wrap="word", state="disabled")
        self.detail_text.pack(side=tk.TOP, fill=tk.X)
        actions = tk.Frame(evidence)
        actions.pack(side=tk.TOP, fill=tk.X, pady=4)
        tk.Button(actions, text="Apply Fix", command=self.apply_fix_to_selected).pack(side=tk.LEFT, padx=2)
        tk.Button(actions, text="Ignore", command=self.ignore_selected).pack(side=tk.LEFT, padx=2)
        tk.Button(actions, text="Edit", command=lambda: self.text_widget.focus_set()).pack(side=tk.LEFT, padx=2)
        tk.Button(actions, text="Recheck", command=self.recheck_current_zone).pack(side=tk.LEFT, padx=2)
        tk.Button(actions, text="Add to Dictionary", command=self.add_selected_to_dictionary).pack(
            side=tk.LEFT, padx=2)
        bottom.add(evidence, width=760)

        zone_panel = tk.Frame(bottom)
        zone_nav_header = tk.Frame(zone_panel)
        zone_nav_header.pack(side=tk.TOP, fill=tk.X)
        tk.Label(zone_nav_header, text="ZONE NAVIGATOR", anchor="w").pack(side=tk.LEFT)
        self.zone_nav_counter_label = tk.Label(zone_nav_header, text="ZONE 0 / 0", anchor="e")
        self.zone_nav_counter_label.pack(side=tk.RIGHT)
        zone_nav_buttons = tk.Frame(zone_panel)
        zone_nav_buttons.pack(side=tk.TOP, fill=tk.X)
        tk.Button(zone_nav_buttons, text="← Previous Zone", command=self.previous_zone).pack(
            side=tk.LEFT, padx=1, pady=1)
        tk.Button(zone_nav_buttons, text="Next Zone →", command=self.next_zone).pack(side=tk.LEFT, padx=1, pady=1)
        tk.Button(zone_nav_buttons, text="Merge w/ Previous", command=self.merge_current_zone_with_previous).pack(
            side=tk.LEFT, padx=(10, 1))
        tk.Button(zone_nav_buttons, text="Split", command=self.split_current_zone).pack(side=tk.LEFT, padx=1)
        self.zone_tree = ttk.Treeview(zone_panel, columns=("type", "page", "paragraph", "status"),
                                       show="tree headings", height=7)
        self.zone_tree.heading("#0", text="Zone")
        self.zone_tree.heading("type", text="Type")
        self.zone_tree.heading("page", text="Page")
        self.zone_tree.heading("paragraph", text="Paragraph")
        self.zone_tree.heading("status", text="Issues")
        self.zone_tree.column("type", width=90)
        self.zone_tree.column("page", width=45)
        self.zone_tree.column("paragraph", width=70)
        self.zone_tree.column("status", width=60)
        self.zone_tree.pack(fill=tk.BOTH, expand=True)
        self.zone_tree.bind("<<TreeviewSelect>>", self._on_zone_selected)
        bottom.add(zone_panel, width=320)

        self.outer_paned.add(issue_area, minsize=180)
        self.after(80, self._set_initial_splitter_position)

        # ---- footer: category pass/warn badges + Save/Generate (stays
        # pinned outside the draggable splitter - always visible) ----
        footer = tk.Frame(self)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=4)
        self.footer_badges_label = tk.Label(footer, text="", anchor="w")
        self.footer_badges_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(footer, text="Save Verification", command=self.save_verification).pack(side=tk.RIGHT, padx=2)
        tk.Button(footer, text="Generate XML", command=self._generate_xhtml).pack(side=tk.RIGHT, padx=2)

    def _build_context_menu(self):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Verify Selection", command=self.recheck_selection)
        menu.add_command(label="Compare PDF", command=self._refresh_pdf_pane)
        menu.add_command(label="Re-OCR Selection/Zone (Deep)", command=self.recheck_selection_deep)
        menu.add_separator()
        for label, field, _accel in _STYLE_BUTTONS:
            menu.add_command(label=f"Apply Style: {label}", command=lambda f=field: self.apply_style_to_selection(f))
        return menu

    def _show_context_menu(self, event):
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()

    def _bind_shortcuts(self):
        self.bind("<Control-b>", lambda e: self.apply_style_to_selection("bold"))
        self.bind("<Control-i>", lambda e: self.apply_style_to_selection("italic"))
        self.bind("<Control-u>", lambda e: self.apply_style_to_selection("underline"))
        self.bind("<Control-Shift-X>", lambda e: self.apply_style_to_selection("superscript"))
        self.bind("<Control-Shift-D>", lambda e: self.apply_style_to_selection("subscript"))
        self.bind("<Control-Shift-M>", lambda e: self.apply_style_to_selection("small_caps"))
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Control-Shift-Z>", lambda e: self.redo())
        self.bind("<Alt-n>", lambda e: self.select_next_issue())
        self.bind("<Alt-p>", lambda e: self.select_previous_issue())
        self.bind("<Control-Shift-R>", lambda e: self.recheck_selection())
        self.bind("<Control-s>", lambda e: self.save_verification())
        # Zone navigation (spec section 2) - confirmed free of any
        # conflict anywhere in the app (a full grep of every .bind( in
        # gui/ found neither sequence bound at all).
        self.bind("<Control-Alt-Left>", lambda e: self.previous_zone())
        self.bind("<Control-Alt-Right>", lambda e: self.next_zone())
        # Page navigation (spec section 3) - also confirmed free; guarded
        # by _focus_in_text_entry so typing Home/End/PageUp/PageDown in
        # the text pane or an Entry still moves the text cursor natively
        # instead of hijacking the keystroke for PDF page navigation
        # (mirrors gui/main_window.py's own _focus_in_entry guard on its
        # Left/Right page-turn shortcuts).
        self.bind("<Home>", self._on_home_key)
        self.bind("<End>", self._on_end_key)
        self.bind("<Prior>", self._on_pageup_key)
        self.bind("<Next>", self._on_pagedown_key)

    def _focus_in_text_entry(self) -> bool:
        widget = self.focus_get()
        return isinstance(widget, (tk.Entry, tk.Text))

    def _on_home_key(self, _event=None):
        if self._focus_in_text_entry():
            return
        self.pdf_goto_page(1)

    def _on_end_key(self, _event=None):
        if self._focus_in_text_entry():
            return
        self.pdf_goto_page(self.pdf.page_count)

    def _on_pageup_key(self, _event=None):
        if self._focus_in_text_entry():
            return
        self.pdf_prev_page()

    def _on_pagedown_key(self, _event=None):
        if self._focus_in_text_entry():
            return
        self.pdf_next_page()

    # ================================================================
    # status / dirty / revision
    # ================================================================

    def _set_status(self, status: str, text: str = None):
        self.status_badge.config(text=text or status, bg=_STATUS_COLORS.get(status, "#888"))

    def _severity_breakdown_text(self) -> str:
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for issue in self.session.issues:
            if issue.is_unresolved():
                counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return f"{counts['HIGH']}H / {counts['MEDIUM']}M / {counts['LOW']}L"

    def _update_status_badge(self):
        if self._save_failed:
            self._set_status(STATUS_SAVE_REQUIRED, "SAVE FAILED")
        elif self._dirty:
            self._set_status(STATUS_SAVE_REQUIRED)
        elif self._verifying:
            self._set_status(STATUS_VERIFYING)
        elif self._partial_error:
            self._set_status(STATUS_PARTIALLY_VERIFIED)
        elif self.session.unresolved_count() > 0:
            count = self.session.unresolved_count()
            self._set_status(STATUS_ISSUES_FOUND,
                              f"{STATUS_ISSUES_FOUND}: {count} ({self._severity_breakdown_text()})")
        elif self._has_run_once:
            self._set_status(STATUS_VERIFIED)
        else:
            self._set_status(STATUS_READY)

    def _on_status_badge_click(self):
        """Spec's own clickable-badge requirement: clicking ISSUES FOUND
        jumps straight to the first open issue, exactly like Alt+N would
        from no current selection."""
        if self.session.unresolved_count() == 0:
            return
        leaves = [iid for parent in self.issue_tree.get_children("") for iid in self.issue_tree.get_children(parent)]
        unresolved_leaves = [iid for iid in leaves if self._issue_by_id(iid) and self._issue_by_id(iid).is_unresolved()]
        if not unresolved_leaves:
            return
        self.issue_tree.selection_set(unresolved_leaves[0])
        self.issue_tree.see(unresolved_leaves[0])

    def _bump_revision(self) -> int:
        self.document_revision += 1
        return self.document_revision

    def _mark_dirty(self, reason: str):
        self._dirty = True
        self.dirty_label.config(text="● Unsaved changes", fg="#d9534f")
        self._update_status_badge()
        if self._autosave_after_id:
            self.after_cancel(self._autosave_after_id)
        self._autosave_after_id = self.after(AUTOSAVE_DEBOUNCE_MS, lambda: self._debounced_autosave(reason))

    def _debounced_autosave(self, reason: str):
        self._autosave_after_id = None
        self.dirty_label.config(text="Saving...", fg="#888")
        self.update_idletasks()
        try:
            self.app.verification_session = self.session
            self.app.mark_dirty(reason)
        except Exception as exc:  # noqa: BLE001 - a failed autosave must never silently drop the change
            self._save_failed = True
            self.dirty_label.config(text="Save Failed ✕", fg="#d9534f")
            self._update_status_badge()
            messagebox.showerror("Autosave", f"Autosave failed: {exc}\n\n"
                                              f"Your changes are kept in memory - please try Save again.")
            return
        self._save_failed = False
        self._dirty = False
        self.dirty_label.config(text="Saved ✓", fg="#3a7")
        self._update_status_badge()

    # ================================================================
    # verification run (background, revision-protected)
    # ================================================================

    def run_verification(self):
        self._verifying = True
        self._update_status_badge()
        revision = self.document_revision
        mode = self.mode_var.get().lower()
        result_queue = queue.Queue()

        def worker():
            try:
                session = orch.run_full_verification(
                    self.pdf, self.zone_manager, mode=mode,
                    ocr_cache=self.app.ocr_cache, ocr_settings=self.app.ocr_settings)
                self._append_merge_candidate_issues(session)
                result_queue.put(("ok", session))
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never crashes the app
                result_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, lambda: self._poll_verification(result_queue, revision))

    def _poll_verification(self, result_queue, started_at_revision: int):
        try:
            kind, payload = result_queue.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._poll_verification(result_queue, started_at_revision))
            return
        self._verifying = False
        if started_at_revision != self.document_revision:
            # Revision protection (spec section 30): a newer edit happened
            # while this scan was running - discard the stale result
            # rather than clobbering it.
            self._partial_error = False
            self._update_status_badge()
            return
        if kind == "error":
            self._partial_error = True
            messagebox.showerror("Verification", f"Verification failed: {payload}")
            self._update_status_badge()
            return
        self.session = payload
        self._has_run_once = True
        self.app.verification_session = self.session
        self.app.mark_dirty("verification run")
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._update_status_badge()

    # ================================================================
    # issue tree
    # ================================================================

    def _zone_display_index(self, zone_id) -> str:
        idx = next((i for i, z in enumerate(self._zones_order, 1) if z.zone_id == zone_id), None)
        return str(idx) if idx else "—"

    def _refresh_issue_tree(self):
        self.issue_tree.delete(*self.issue_tree.get_children())
        groups = {}
        for issue in self.session.issues:
            group = vm.ISSUE_GROUPS.get(issue.issue_type, "Structure")
            if self._issue_filter_group is not None and group != self._issue_filter_group:
                continue
            groups.setdefault(group, []).append(issue)
        for group_name in sorted(groups):
            issues = groups[group_name]
            group_id = self.issue_tree.insert("", "end", text=f"{group_name} ({len(issues)})", open=True)
            for issue in issues:
                label = f"{issue.issue_type}: {issue.extracted_text or issue.source_text}"[:70]
                paragraph_id = self.paragraph_index.paragraph_of(issue.zone_id) or "—"
                self.issue_tree.insert(
                    group_id, "end", iid=issue.id, text=label,
                    values=(issue.page or "—", self._zone_display_index(issue.zone_id), paragraph_id,
                            issue.status, issue.confidence),
                    tags=(issue.severity,))
        for sev, color in _SEVERITY_COLORS.items():
            self.issue_tree.tag_configure(sev, foreground=color)
        counts = self.session.counts_by_group()
        self.category_counter_label.config(
            text="   ".join(f"{g} ({c})" for g, c in sorted(counts.items())) or "No issues found")
        self._refresh_footer_badges()
        self._refresh_category_tabs()

    def _build_category_tabs(self, parent):
        """Spec section 10: category tabs above the issue list, filtering
        it when clicked. A group's own tab only appears once it actually
        has at least one issue (session.counts_by_group()'s own existing
        scope) - satisfies "If Grammar exists: Grammar (N)" etc. without
        hardcoding which groups a given book happens to produce."""
        self._tabs_row = parent
        self._all_tab_button = tk.Button(parent, text="All", command=lambda: self._set_category_filter(None))
        self._all_tab_button.pack(side=tk.LEFT, padx=1)
        self._group_tab_buttons = {}

    def _refresh_category_tabs(self):
        for btn in self._group_tab_buttons.values():
            btn.destroy()
        self._group_tab_buttons = {}
        counts = self.session.counts_by_group()
        for group in sorted(counts):
            btn = tk.Button(self._tabs_row, text=f"{group} ({counts[group]})",
                             relief="sunken" if self._issue_filter_group == group else "raised",
                             command=lambda g=group: self._set_category_filter(g))
            btn.pack(side=tk.LEFT, padx=1)
            self._group_tab_buttons[group] = btn
        self._all_tab_button.config(relief="sunken" if self._issue_filter_group is None else "raised")

    def _set_category_filter(self, group):
        self._issue_filter_group = group
        self._refresh_issue_tree()

    # ================================================================
    # draggable outer splitter (spec section 9)
    # ================================================================

    def _set_initial_splitter_position(self):
        """Default ~30% of window height for the issue area (spec: "min
        180px / max 50% / default ~30%") - applied once, shortly after
        first realization, since sash_place needs real widget geometry."""
        self.update_idletasks()
        total_h = self.outer_paned.winfo_height()
        if total_h <= 1:
            return
        sash_y = max(1, total_h - int(total_h * 0.3))
        try:
            self.outer_paned.sash_place(0, 0, sash_y)
        except tk.TclError:
            pass

    def _clamp_splitter_position(self, _event=None):
        """Live-clamps a drag so the bottom (issue) pane never exceeds
        ~50% of window height - the 180px FLOOR is separately, natively
        enforced by the minsize=180 passed to outer_paned.add() and can
        never be bypassed by dragging; this only handles the other half
        of the spec's own min/max requirement."""
        try:
            total_h = self.outer_paned.winfo_height()
            if total_h <= 1:
                return
            coord = self.outer_paned.sash_coord(0)
            min_sash_y = int(total_h * 0.5)
            if coord[1] < min_sash_y:
                self.outer_paned.sash_place(0, coord[0], min_sash_y)
        except tk.TclError:
            pass

    def _refresh_footer_badges(self):
        badge_groups = ["Content", "Word Joining", "Unicode", "Spelling", "Grammar",
                        "Formatting", "Sup/Sub", "Structure"]
        counts = self.session.counts_by_group()
        parts = []
        for g in badge_groups:
            unresolved = sum(1 for i in self.session.issues
                              if vm.ISSUE_GROUPS.get(i.issue_type, "Structure") == g and i.is_unresolved())
            parts.append(f"{g} {'✓' if unresolved == 0 else '⚠'}")
        self.footer_badges_label.config(text="   |   ".join(parts))

    def _issue_by_id(self, issue_id):
        return next((i for i in self.session.issues if i.id == issue_id), None)

    def _on_issue_selected(self, _event=None):
        selection = self.issue_tree.selection()
        if not selection:
            return
        issue = self._issue_by_id(selection[0])
        if issue is None:
            return
        self.current_issue = issue
        if issue.zone_id != self.current_zone_id:
            self._load_zone_into_panes(issue.zone_id)
        self._highlight_issue(issue)
        self._show_issue_detail(issue)

    def select_next_issue(self):
        self._step_issue(1)

    def select_previous_issue(self):
        self._step_issue(-1)

    def _step_issue(self, direction: int):
        leaves = [iid for parent in self.issue_tree.get_children("") for iid in self.issue_tree.get_children(parent)]
        unresolved_leaves = [iid for iid in leaves if self._issue_by_id(iid) and self._issue_by_id(iid).is_unresolved()]
        pool = unresolved_leaves or leaves
        if not pool:
            return
        current = self.issue_tree.selection()
        idx = pool.index(current[0]) if current and current[0] in pool else -1
        new_idx = (idx + direction) % len(pool)
        self.issue_tree.selection_set(pool[new_idx])
        self.issue_tree.see(pool[new_idx])

    # ================================================================
    # paragraph model (Pass 3 - "zone" != "paragraph", see
    # core/verification/paragraph_model.py)
    # ================================================================

    def _rebuild_paragraph_index(self):
        """Recomputed whenever zone structure changes (initial load,
        merge/split and their own undo/redo) - cheap (pure Python over
        already-in-memory zones, no PDF/OCR access) and always reflects
        the CURRENT merge state, so a paragraph id is never stale."""
        page_marker_tags, footnote_flow_tags, non_flow_tags = self._merge_skip_tags()
        self.paragraph_index = pm.build_paragraph_index(
            self.zone_manager, page_marker_tags, footnote_flow_tags, non_flow_tags)

    _TAG_LABELS = None  # lazily built class-level cache, see _tag_label

    @classmethod
    def _tag_label(cls, tag: str) -> str:
        """Friendly zone-type label for the Zone Navigator (spec's own
        "[01] Heading" style) - reuses core.constants.TAG_BUTTONS's
        existing label<->tag pairs (the SAME labels the main zoning UI's
        own tag buttons show) rather than inventing a second vocabulary;
        only the two CUPEPUB-only note tags (not present in that XML-
        profile list) get a small supplement."""
        if cls._TAG_LABELS is None:
            from core import constants
            labels = {t: label for label, t, _extra in constants.TAG_BUTTONS}
            labels.setdefault("fn", "Footnote")
            labels.setdefault("en", "Endnote")
            labels.setdefault("pagenum", "Page Number")
            cls._TAG_LABELS = labels
        return cls._TAG_LABELS.get(tag, tag.replace("_", " ").title())

    # ================================================================
    # zone navigator (page-scoped - spec section 2)
    # ================================================================

    def _current_page_number(self) -> int:
        zone = self.zone_manager.zones.get(self.current_zone_id) if self.current_zone_id else None
        return zone.page if zone is not None else self._pdf_page_num

    def _zones_on_current_page(self) -> list:
        page = self._current_page_number()
        return [z for z in self._zones_order if z.page == page]

    def _refresh_zone_panel(self):
        """Zone Navigator: lists only the CURRENT PAGE's zones (spec:
        "PAGE 1/3 ... Zones: [01] Heading [02] Paragraph ..."), not the
        whole book - jumping to a different page's zones is a separate,
        explicit page-navigation step, matching the spec's own described
        workflow."""
        self.zone_tree.delete(*self.zone_tree.get_children())
        page_zones = self._zones_on_current_page()
        for idx, zone in enumerate(page_zones, start=1):
            count = sum(1 for i in self.session.issues_for_zone(zone.zone_id) if i.is_unresolved())
            status = "✓" if count == 0 else f"⚠ {count}"
            paragraph_id = self.paragraph_index.paragraph_of(zone.zone_id) or "—"
            self.zone_tree.insert("", "end", iid=zone.zone_id, text=f"[{idx:02d}] {self._tag_label(zone.tag)}",
                                   values=(zone.tag, zone.page, paragraph_id, status))
        if hasattr(self, "zone_nav_counter_label"):
            cur_idx = next((i for i, z in enumerate(page_zones, 1) if z.zone_id == self.current_zone_id), 0)
            self.zone_nav_counter_label.config(text=f"ZONE {cur_idx} / {len(page_zones)}")

    def _on_zone_selected(self, _event=None):
        selection = self.zone_tree.selection()
        # Confirmed real bug during development: _load_zone_into_panes's
        # own selection_set(zone_id) call below (keeping the zone panel's
        # highlight in sync when a zone is loaded via the issue tree
        # instead) re-fires <<TreeviewSelect>> even when setting the
        # SAME selection that's already active - without this guard, that
        # becomes an infinite _on_zone_selected -> _load_zone_into_panes
        # -> selection_set -> _on_zone_selected loop.
        if not selection or selection[0] == self.current_zone_id:
            return
        # spec section 31: clicking a zone loads ONLY that zone - never
        # reloads the book.
        self._load_zone_into_panes(selection[0])

    def previous_zone(self):
        self._step_zone(-1)

    def next_zone(self):
        self._step_zone(1)

    def _step_zone(self, direction: int):
        """Confirmed real bug fixed here: this used to walk only
        _zones_on_current_page(), so Next Zone from the LAST zone of a
        page wrapped back to that SAME page's own first zone instead of
        advancing into the next page - it never crossed a page boundary
        at all. self._zones_order is the WHOLE BOOK's own leaf zones,
        already sorted (page, serial) - true global reading order (spec:
        "Build a navigation sequence... (page_index, zone_id/index), NOT
        zone_id alone" - exactly what this sort key already is). Walking
        that instead makes Page 1's last zone -> Page 2's first zone (and
        the reverse) fall out automatically, with no special-casing for
        a page boundary: it's just the next/previous entry in one global
        sequence. _load_zone_into_panes's own existing follow_zone
        behavior (Pass 2) already jumps the PDF pane to whatever page the
        target zone lives on, and _refresh_zone_panel already re-scopes
        the Zone Navigator's own page-filtered listing to that new
        current page - both automatic, no changes needed there."""
        ids = [z.zone_id for z in self._zones_order]
        if not ids:
            return
        idx = ids.index(self.current_zone_id) if self.current_zone_id in ids else -1
        new_idx = (idx + direction) % len(ids)
        self._load_zone_into_panes(ids[new_idx])

    # ================================================================
    # rich-text rendering (PAGE -> ZONE -> SPAN)
    # ================================================================

    def _load_zone_into_panes(self, zone_id: str):
        """Paragraph-aware (Pass 3): if zone_id belongs to a multi-zone
        paragraph (an explicit Merge Previous chain), the WHOLE joined
        paragraph is loaded - spec sections 6/16/60, "zone boundaries are
        NOT paragraph boundaries". For an ordinary single-zone paragraph
        (the overwhelming common case) this is byte-for-byte the same
        single-zone extraction/rendering as before Pass 3 existed.
        current_zone_id tracks the FOCUSED member zone within whichever
        paragraph is loaded (see _on_text_cursor_moved for how focus
        moves between member zones as the cursor crosses a seam)."""
        zone = self.zone_manager.zones.get(zone_id)
        if zone is None:
            return
        if zone.children:
            # Defensive redirect - a container zone's own bbox typically
            # spans its entire subtree (the exact real bug this guards
            # against: a "p" zone wrapping many nested child fragments
            # has a bbox covering the whole block, so extracting it
            # directly would silently swallow every child's own
            # identity into one giant blob). Redirect to the same first
            # leaf paragraph_model.py would ALSO resolve this subtree
            # to, so the two stay consistent by construction rather than
            # via a second, separately-maintained rule. Any caller that
            # somehow still reaches this with a container id (e.g. an
            # issue detected against a container zone by the existing,
            # unmodified verification engine) lands on real content
            # instead of a meaningless full-bbox dump.
            leaves = pm.leaf_descendant_ids(self.zone_manager, zone_id)
            if leaves and leaves[0] != zone_id:
                self._load_zone_into_panes(leaves[0])
            return
        self.current_zone_id = zone_id
        paragraph_id = self.paragraph_index.paragraph_of(zone_id)

        if paragraph_id is not None and self.paragraph_index.is_multi_zone(paragraph_id):
            self.current_paragraph_id = paragraph_id
            self.current_spans, self.current_paragraph_segments = pm.paragraph_plain_spans(
                self.zone_manager, self.pdf, paragraph_id, self.paragraph_index)
            member_ids = self.paragraph_index.paragraph_zone_ids[paragraph_id]
            self._paragraph_header_normal_text = (
                f"Paragraph {paragraph_id}  —  spans zones: {', '.join(member_ids)}  "
                f"(select one zone's own text to edit or apply formatting)")
        else:
            from core.text_extractor import extract_zone_formatted_text

            self.current_paragraph_id = paragraph_id
            page = self.pdf.get_page(zone.page)
            tagged_text = extract_zone_formatted_text(page, zone)
            self.current_spans = sm.parse_tagged_text(tagged_text)
            plain_len = len(sm.plain_text_of(self.current_spans))
            self.current_paragraph_segments = [pm.ParagraphSegment(zone_id, 0, plain_len, editable=True)]
            self._paragraph_header_normal_text = ""
        self.paragraph_header_label.config(text=self._paragraph_header_normal_text, fg="#555")

        self._render_spans_in_text_widget()
        self._recompute_line_ranges_for_zone(zone_id)
        self._refresh_pdf_pane()
        self._update_info_label()
        self._refresh_zone_panel()

        if self.zone_tree.exists(zone_id) and self.zone_tree.selection() != (zone_id,):
            self.zone_tree.selection_set(zone_id)

    def _flash_paragraph_header_warning(self, text: str, duration_ms: int = 4000):
        """Non-blocking inline notice shown in the paragraph header label
        (never messagebox.showinfo - see _on_text_modified's own comment
        for why a modal popup on every refused keystroke is both bad UX
        and a real deadlock risk for anything driving the widget non-
        interactively). Reverts to whatever the header's own normal text
        was after `duration_ms`."""
        self.paragraph_header_label.config(text=text, fg="#c00")
        self.after(duration_ms, lambda: self.paragraph_header_label.config(
            text=self._paragraph_header_normal_text, fg="#555"))

    def _recompute_line_ranges_for_zone(self, zone_id: str):
        """PDF-line <-> character-offset mapping for the FOCUSED member
        zone only (offsets already shifted into self.current_spans's own
        joined coordinate space via that zone's ParagraphSegment) - for
        an ordinary single-zone paragraph this is identical to Pass 2's
        own behavior (segment.start is always 0)."""
        from core.text_extractor import extract_lines

        zone = self.zone_manager.zones.get(zone_id)
        segment = next((s for s in self.current_paragraph_segments if s.zone_id == zone_id), None)
        if zone is None or segment is None or not segment.editable:
            self.current_zone_line_ranges = []
            return
        page = self.pdf.get_page(zone.page)
        raw_lines = extract_lines(page, zone.bbox)
        zone_plain = sm.plain_text_of(self.current_spans)[segment.start:segment.end]
        self.current_zone_line_ranges = self._map_lines_to_char_ranges(raw_lines, zone_plain, segment.start)

    def _map_lines_to_char_ranges(self, raw_lines: list, text: str, offset: int = 0) -> list:
        """Approximate char-range-per-PDF-line mapping (spec section 15's
        own "which exact area caused this" requirement) - matches each
        line's own plain text against `text` (one zone's own plain text
        slice) sequentially, since core.text_extractor.extract_lines' own
        line boundaries don't necessarily coincide with the Text widget's
        own word-wrapped display lines. `offset` shifts the result into
        self.current_spans's own (possibly multi-zone-joined) coordinate
        space."""
        from core.text_extractor import strip_tags_to_plain

        ranges = []
        search_from = 0
        for line_bbox, line_tagged_text in raw_lines:
            line_plain = strip_tags_to_plain(line_tagged_text).strip()
            if not line_plain:
                continue
            pos = text.find(line_plain, search_from)
            if pos == -1:
                pos = text.find(line_plain)
            if pos == -1:
                continue
            end = pos + len(line_plain)
            ranges.append((offset + pos, offset + end, line_bbox))
            search_from = end
        return ranges

    def _tag_name_for_span(self, span: sm.Span) -> str:
        flags = [f for f in ("bold", "italic", "underline", "strike", "superscript",
                              "subscript", "small_caps") if getattr(span, f)]
        return "style_" + "_".join(flags) if flags else "style_normal"

    def _ensure_tag_configured(self, tag_name: str, span: sm.Span):
        if tag_name in self._configured_tags:
            return
        self._configured_tags.add(tag_name)
        weight = "bold" if span.bold else "normal"
        slant = "italic" if span.italic else "roman"
        size = self._base_font.cget("size")
        offset = 0
        if span.superscript or span.subscript:
            size = max(6, int(size * 0.7))
            offset = int(self._base_font.cget("size") * 0.35) if span.superscript else \
                -int(self._base_font.cget("size") * 0.2)
        elif span.small_caps:
            # Visual approximation only (Tk has no real small-caps font
            # variant) - a smaller size, same case, same characters, so
            # the displayed text stays byte-identical to plain_text_of()
            # and every character-offset computation stays valid.
            size = max(7, int(size * 0.82))
        font = tkfont.Font(family=self._base_font.cget("family"), size=size, weight=weight, slant=slant)
        kwargs = {"font": font}
        if offset:
            kwargs["offset"] = offset
        if span.underline:
            kwargs["underline"] = True
        if span.strike:
            kwargs["overstrike"] = True
        self.text_widget.tag_configure(tag_name, **kwargs)

    def _render_spans_in_text_widget(self):
        """Full rebuild of the Text widget's content - acceptable here
        because this only ever happens when SWITCHING to a different
        zone (a handful of spans, typically well under a page of text),
        never on a style-application keystroke (see apply_style_to_
        selection, which re-tags in place without touching widget
        content at all)."""
        self.text_widget.edit_modified(False)  # suppress the <<Modified>> this triggers
        self.text_widget.delete("1.0", "end")
        pos = 0
        for span in self.current_spans:
            if not span.text:
                continue
            tag = self._tag_name_for_span(span)
            self._ensure_tag_configured(tag, span)
            segment = pm.segment_for_offset(self.current_paragraph_segments, pos)
            tags = (tag, "paragraph_boundary_marker") if segment is not None and not segment.editable else (tag,)
            self.text_widget.insert("end", span.text, tags)
            pos += len(span.text)
        self._apply_zone_gap_spacing()
        self.text_widget.edit_reset()
        self.text_widget.edit_modified(False)
        self._update_style_button_states()

    def _apply_zone_gap_spacing(self):
        """Tags the line each editable zone-segment ends on with
        zone_paragraph_gap wherever the ACTUAL separator to the next
        segment is a real newline (the nested-children "each zone gets
        its own line" case - see paragraph_model.py's own join-default
        docstring) - never for a merge-chain's own flowing, space-joined
        continuation, where adding a visual paragraph gap mid-sentence
        would be wrong."""
        segments = self.current_paragraph_segments
        if len(segments) < 2:
            return
        full_plain = sm.plain_text_of(self.current_spans)
        for i in range(len(segments) - 1):
            seg, nxt = segments[i], segments[i + 1]
            if not seg.editable:
                continue
            gap_text = full_plain[seg.end:nxt.start]
            if "\n" not in gap_text:
                continue
            self.text_widget.tag_add("zone_paragraph_gap", f"1.0+{max(0, seg.end - 1)}c", f"1.0+{seg.end}c")

    def _rerender_tags_only(self):
        """Re-applies span tags to the ALREADY-DISPLAYED text without any
        delete/insert - used after an instant style change so the exact
        same characters stay in place (spec section 13: "ONLY those
        characters/spans are modified... DO NOT recreate the entire
        document widget")."""
        for tag in list(self._configured_tags):
            self.text_widget.tag_remove(tag, "1.0", "end")
        pos = 0
        for span in self.current_spans:
            if not span.text:
                continue
            tag = self._tag_name_for_span(span)
            self._ensure_tag_configured(tag, span)
            start = f"1.0+{pos}c"
            end = f"1.0+{pos + len(span.text)}c"
            self.text_widget.tag_add(tag, start, end)
            pos += len(span.text)

    def _update_info_label(self):
        zone = self.zone_manager.zones.get(self.current_zone_id)
        if not zone:
            return
        idx = next((i for i, z in enumerate(self._zones_order, 1) if z.zone_id == self.current_zone_id), 0)
        pdf_name = (getattr(self.pdf, "path", "") or "").split("\\")[-1].split("/")[-1]
        self.info_label.config(
            text=f"PDF: {pdf_name}    PAGE: {zone.page} / {self.pdf.page_count}    "
                 f"ZONE: {idx} / {len(self._zones_order)}    REV: {self.document_revision}")

    # ================================================================
    # PDF pane rendering + click sync
    # ================================================================

    def _refresh_pdf_pane(self, force: bool = False, follow_zone: bool = True):
        """Renders self._pdf_page_num - independent of the currently
        loaded zone (spec: PDF pane must scroll/zoom/page-navigate on its
        own). `follow_zone=True` (the default, used by every zone/issue
        navigation call site) jumps the displayed page to match the
        just-selected zone, exactly like the previous single-page-only
        pane always did; explicit Prev/Next/page-jump/zoom actions pass
        follow_zone=False so a manual pan is never yanked back to the
        selected zone's page under the operator's own hands."""
        zone = self.zone_manager.zones.get(self.current_zone_id) if self.current_zone_id else None
        if follow_zone and zone is not None:
            self._pdf_page_num = zone.page
        page_num = self._pdf_page_num
        if page_num < 1 or page_num > self.pdf.page_count:
            return

        show_zone_bbox = zone is not None and zone.page == page_num
        issue_bbox = (self.current_issue.pdf_coordinates if self.current_issue
                      and self.current_issue.zone_id == self.current_zone_id and show_zone_bbox else None)

        # Definitive break for a real, confirmed Tk feedback loop: this
        # canvas's own scrollregion config below can re-fire <Configure>
        # even when width/height are UNCHANGED (confirmed directly - the
        # canvas stayed at a stable 480x466 across 50+ consecutive
        # <Configure> events). Without this guard, a same-size Configure
        # would keep re-arming a re-render indefinitely within one
        # script-driven update() call. Skipping the render entirely when
        # nothing relevant actually changed stops the cascade at its
        # source rather than trying to out-wait it.
        canvas_w = max(1, self.pdf_canvas.winfo_width() or 480)
        canvas_h = max(1, self.pdf_canvas.winfo_height() or 600)
        render_key = (page_num, canvas_w, canvas_h, self._pdf_zoom_mode, round(self._pdf_zoom, 3),
                      id(issue_bbox) if issue_bbox else None,
                      self.current_zone_id if show_zone_bbox else None)
        if not force and getattr(self, "_last_pdf_render_key", None) == render_key:
            return
        self._last_pdf_render_key = render_key

        from PIL import ImageDraw, ImageTk
        dpi = 110
        img = self.pdf.render_page_image(page_num, dpi=dpi)
        base_zoom = dpi / 72.0
        draw = ImageDraw.Draw(img)
        if show_zone_bbox:
            draw.rectangle([c * base_zoom for c in zone.bbox], outline="#e8a33d", width=2)
        if issue_bbox:
            draw.rectangle([issue_bbox["x0"] * base_zoom, issue_bbox["y0"] * base_zoom,
                             issue_bbox["x1"] * base_zoom, issue_bbox["y1"] * base_zoom],
                           outline="#d9534f", width=3)

        if self._pdf_zoom_mode == "fit_width":
            scale = canvas_w / img.width
        elif self._pdf_zoom_mode == "fit_page":
            scale = min(canvas_w / img.width, canvas_h / img.height)
        else:
            scale = self._pdf_zoom
        scale = max(0.1, scale)
        self._pdf_render_scale = base_zoom * scale
        if abs(scale - 1.0) > 0.01:
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        self._page_photo = ImageTk.PhotoImage(img)
        self.pdf_canvas.delete("all")
        self.pdf_canvas.create_image(0, 0, anchor="nw", image=self._page_photo)
        self.pdf_canvas.config(scrollregion=(0, 0, img.width, img.height))

        self.pdf_page_var.set(str(page_num))
        self.pdf_page_total_label.config(text=f"/ {self.pdf.page_count}")
        self.pdf_zoom_label.config(text=f"{int(round(scale * 100))}%")

    # ---- independent PDF pane navigation / zoom / wheel ----

    def pdf_prev_page(self):
        self.pdf_goto_page(self._pdf_page_num - 1)

    def pdf_next_page(self):
        self.pdf_goto_page(self._pdf_page_num + 1)

    def pdf_goto_page(self, page):
        try:
            page = int(page)
        except (TypeError, ValueError):
            return
        page = max(1, min(page, self.pdf.page_count))
        self._pdf_page_num = page
        self._refresh_pdf_pane(force=True, follow_zone=False)

    def pdf_zoom_in(self):
        self._pdf_zoom_mode = "custom"
        self._pdf_zoom = min(4.0, self._pdf_zoom * 1.25)
        self._refresh_pdf_pane(force=True, follow_zone=False)

    def pdf_zoom_out(self):
        self._pdf_zoom_mode = "custom"
        self._pdf_zoom = max(0.25, self._pdf_zoom / 1.25)
        self._refresh_pdf_pane(force=True, follow_zone=False)

    def pdf_fit_width(self):
        self._pdf_zoom_mode = "fit_width"
        self._refresh_pdf_pane(force=True, follow_zone=False)

    def pdf_fit_page(self):
        self._pdf_zoom_mode = "fit_page"
        self._refresh_pdf_pane(force=True, follow_zone=False)

    def _on_pdf_wheel(self, event):
        from gui.pdf_viewer import normalize_wheel_units
        units = normalize_wheel_units(event.delta)
        if units:
            self.pdf_canvas.yview_scroll(units, "units")

    def _on_pdf_wheel_h(self, event):
        from gui.pdf_viewer import normalize_wheel_units
        units = normalize_wheel_units(event.delta)
        if units:
            self.pdf_canvas.xview_scroll(units, "units")

    def _on_pdf_ctrl_wheel(self, event):
        if event.delta > 0:
            self.pdf_zoom_in()
        else:
            self.pdf_zoom_out()

    def _on_pdf_click(self, event):
        """Spec section 15: clicking the PDF selects the corresponding
        extracted text - hit-tests the click against the current zone's
        own mapped line ranges. Only meaningful while the pane is showing
        the current zone's own page (see follow_zone) - a click while
        manually browsing a different page has nothing to hit-test
        against and is deliberately a no-op rather than a wrong sync."""
        zone = self.zone_manager.zones.get(self.current_zone_id) if self.current_zone_id else None
        if not self.current_zone_line_ranges or not self._pdf_render_scale:
            return
        if zone is None or zone.page != self._pdf_page_num:
            return
        px = event.x / self._pdf_render_scale
        py = event.y / self._pdf_render_scale
        for start, end, line_bbox in self.current_zone_line_ranges:
            if line_bbox[1] - 2 <= py <= line_bbox[3] + 2:
                self.text_widget.tag_remove("sel", "1.0", "end")
                self.text_widget.tag_add("sel", f"1.0+{start}c", f"1.0+{end}c")
                self.text_widget.see(f"1.0+{start}c")
                self._update_style_button_states()
                return

    def _highlight_issue(self, issue):
        self.text_widget.tag_remove("active_issue", "1.0", "end")
        char_range = self._issue_char_range(issue)
        if char_range:
            start, end = char_range
            self.text_widget.tag_add("active_issue", f"1.0+{start}c", f"1.0+{end}c")
            self.text_widget.see(f"1.0+{start}c")
        self._refresh_pdf_pane()

    def _issue_char_range(self, issue):
        """An issue's own char_range/word_index is always computed LOCAL
        to its own zone's plain text (verification_engine.verify_zone
        never sees a joined multi-zone paragraph, only one zone at a
        time) - within a joined paragraph view this must be shifted by
        that zone's own ParagraphSegment.start before it's a valid offset
        into self.current_spans's own (possibly multi-zone) coordinate
        space. For an ordinary single-zone paragraph segment.start is
        always 0, so this is exactly the pre-Pass-3 behavior."""
        segment = next((s for s in self.current_paragraph_segments if s.zone_id == issue.zone_id), None)
        offset = segment.start if segment is not None else 0
        full_plain = sm.plain_text_of(self.current_spans)
        zone_plain = full_plain[segment.start:segment.end] if segment is not None else full_plain
        if issue.char_range:
            start, end = issue.char_range
            return offset + start, offset + end
        if issue.word_index is not None:
            spans = [m.span() for m in re.finditer(r"\S+", zone_plain)]
            if issue.word_index >= len(spans):
                return None
            last = min(issue.word_index + max(1, issue.word_span_count), len(spans)) - 1
            return offset + spans[issue.word_index][0], offset + spans[last][1]
        return None

    def _on_text_cursor_moved(self, _event=None):
        """Spec section 15 (reverse direction): selecting/moving in the
        text pane highlights the corresponding PDF region."""
        self._update_style_button_states()
        try:
            offset = self._char_offset_of("insert")
        except tk.TclError:
            return
        for start, end, line_bbox in self.current_zone_line_ranges:
            if start <= offset <= end:
                self._highlight_pdf_line(line_bbox)
                return
        # The cursor moved into a DIFFERENT member zone's own segment
        # within a multi-zone paragraph view (current_zone_line_ranges is
        # only ever scoped to the currently FOCUSED zone) - switch focus
        # to that zone's own page/bbox without reloading the whole
        # paragraph view or touching self.current_spans.
        segment = pm.segment_for_offset(self.current_paragraph_segments, offset)
        if segment is None or not segment.editable or segment.zone_id == self.current_zone_id:
            return
        self.current_zone_id = segment.zone_id
        self._recompute_line_ranges_for_zone(segment.zone_id)
        self._refresh_pdf_pane(force=True, follow_zone=True)
        self._update_info_label()
        for start, end, line_bbox in self.current_zone_line_ranges:
            if start <= offset <= end:
                self._highlight_pdf_line(line_bbox)
                return

    def _highlight_pdf_line(self, line_bbox):
        zone = self.zone_manager.zones.get(self.current_zone_id)
        if zone is None or not self._page_photo:
            return
        from PIL import ImageDraw
        # force=True: this transient cursor-position overlay must redraw
        # even when the zone/width/issue-derived render_key hasn't
        # changed (see _refresh_pdf_pane's own guard comment).
        self._refresh_pdf_pane(force=True)
        # _refresh_pdf_pane already redraws from a fresh render; overlay
        # the line highlight on top using the same canvas.
        zoom = self._pdf_render_scale
        self.pdf_canvas.create_rectangle(
            line_bbox[0] * zoom, line_bbox[1] * zoom, line_bbox[2] * zoom, line_bbox[3] * zoom,
            outline="#4da6ff", width=2)

    def _show_issue_detail(self, issue):
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        lines = [
            f"Type: {issue.issue_type}    Severity: {issue.severity}    Confidence: {issue.confidence}    "
            f"Status: {issue.status}",
            f"PDF Evidence: {issue.expected_text or issue.ocr_text or '(none)'}",
            f"Current: {issue.extracted_text or issue.source_text!r}",
            f"Suggested Fix: {issue.suggested_text!r}" if issue.suggested_text else "Suggested Fix: (none)",
            f"Evidence source: {issue.source_evidence}",
            f"Explanation: {issue.explanation}",
        ]
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.config(state="disabled")

    # ================================================================
    # instant local style application (spec sections 10/13/41 - the core fix)
    # ================================================================

    def _char_offset_of(self, index) -> int:
        """Safe wrapper around Text.count("1.0", index, "chars") - Tk's
        own count() returns None (not 0), rather than a subscriptable
        result, for a zero-length range - which is exactly what happens
        whenever `index` IS "1.0" itself (the very start of the widget).
        Confirmed real crash site: selecting a zone's own very first
        character (a selection starting at absolute offset 0 - e.g. the
        first word of a title) raised TypeError here instead of
        correctly computing offset 0, refusing to apply formatting to
        any selection that happens to start at the beginning of the
        currently-displayed text."""
        if self.text_widget.compare(index, "==", "1.0"):
            return 0
        result = self.text_widget.count("1.0", index, "chars")
        return int(result[0]) if result else 0

    def _char_offsets_from_selection(self):
        try:
            start_index = self.text_widget.index("sel.first")
            end_index = self.text_widget.index("sel.last")
        except tk.TclError:
            return None
        return self._char_offset_of(start_index), self._char_offset_of(end_index)

    # ================================================================
    # real, operation-level undo/redo (core.verification.undo_stack) -
    # covers EVERY local mutation this window performs (style apply,
    # manual text edit, applied fix, page-number correction, zone merge/
    # split) uniformly, via one generic zone-state snapshot/restore pair.
    # Never touches the PDF, never re-runs any verifier - restoring a
    # snapshot goes through exactly the same cheap, digital-only zone
    # reload (_load_zone_into_panes) any other zone navigation already
    # uses, not a fresh OCR/extraction pass.
    # ================================================================

    def undo(self):
        op = self.undo_stack.undo()
        if op:
            self._update_undo_redo_buttons()

    def redo(self):
        op = self.undo_stack.redo()
        if op:
            self._update_undo_redo_buttons()

    def _update_undo_redo_buttons(self):
        self.undo_btn.config(state="normal" if self.undo_stack.can_undo() else "disabled")
        self.redo_btn.config(state="normal" if self.undo_stack.can_redo() else "disabled")

    def _snapshot_correction_state(self, zone_id: str):
        """Captures exactly the pieces of zone state any of this window's
        own mutation paths (apply_style/apply_manual_text/apply_fix) can
        change - text, the manual_text flag, and style_overrides - so a
        single generic Operation can reverse ANY of them."""
        zone = self.zone_manager.zones.get(zone_id)
        if zone is None:
            return None
        return {
            "text": zone.text,
            "manual_text": zone.attributes.get("manual_text"),
            "style_overrides": copy.deepcopy(zone.attributes.get("style_overrides")),
        }

    def _apply_correction_snapshot(self, zone_id: str, snapshot):
        """Pure attribute mutation, no UI/refresh side effects - the
        shared core of both the single-zone and bulk (multi-zone)
        restore paths below."""
        zone = self.zone_manager.zones.get(zone_id)
        if zone is None or snapshot is None:
            return
        zone.text = snapshot["text"]
        if snapshot["manual_text"]:
            zone.attributes["manual_text"] = True
        else:
            zone.attributes.pop("manual_text", None)
        if snapshot["style_overrides"]:
            zone.attributes["style_overrides"] = snapshot["style_overrides"]
        else:
            zone.attributes.pop("style_overrides", None)

    def _restore_correction_state(self, zone_id: str, snapshot):
        self._apply_correction_snapshot(zone_id, snapshot)
        self._after_undo_redo_zone_change(zone_id, "correction")

    def _restore_correction_states_bulk(self, snapshots: dict, reason: str):
        """Multi-zone version, for a style operation whose selection
        spanned more than one underlying zone (spec: "Do NOT reject the
        selection merely because it crosses span/zone boundaries" - a
        selection like "Framing American Foreign Policy" can legitimately
        touch several zones at once). Restores EVERY zone's own
        attributes first, THEN refreshes/reloads exactly ONCE at the
        end - restoring one zone at a time and reloading after each
        (reusing the single-zone path in a loop) would display a
        transiently-mixed state partway through, since the joined
        paragraph view is rebuilt fresh from ALL member zones' CURRENT
        attributes on every reload."""
        for zone_id, snapshot in snapshots.items():
            self._apply_correction_snapshot(zone_id, snapshot)
        self._bump_revision()
        self._rebuild_paragraph_index()
        if self.current_zone_id:
            self._load_zone_into_panes(self.current_zone_id)
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._mark_dirty(f"verification undo/redo: {reason}")
        self._update_status_badge()

    def _after_undo_redo_zone_change(self, zone_id: str, reason: str):
        """Shared tail for every undo/redo restoration: rebuild the
        paragraph index (cheap - pure zone/attribute traversal, no PDF/
        OCR access - so it's always safe to refresh unconditionally
        rather than trying to guess which specific operations could have
        changed merge structure), reload the zone's panes ONLY if it's
        the one currently displayed (cheap digital re-extraction, never
        OCR/verification - same call _load_zone_into_panes already makes
        on ordinary navigation), refresh the issue/zone lists to reflect
        the reverted state, and mark dirty so the reversal itself is
        autosaved like any other edit."""
        self._bump_revision()
        self._rebuild_paragraph_index()
        if zone_id == self.current_zone_id:
            self._load_zone_into_panes(zone_id)
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._mark_dirty(f"verification undo/redo: {reason}")
        self._update_status_badge()

    def apply_style_to_selection(self, field: str):
        """INSTANT: split spans + re-tag the exact range only. Never
        touches the PDF, never calls a verifier, never rebuilds the
        widget. Forbidden operations during this call (spec section 41):
        OCR, PDF extraction, full verification, PDF rerender, spelling,
        grammar, deep comparison, JSON reload, editor rebuild, XHTML
        generation - none of those appear anywhere in this method."""
        if not self.current_zone_id:
            return
        offsets = self._char_offsets_from_selection()
        if offsets is None:
            return
        start, end = offsets
        # Paragraph-aware, multi-zone-safe edit routing: a selection like
        # "Framing American Foreign Policy" may legitimately span SEVERAL
        # underlying zones once a multi-zone paragraph is joined on
        # screen - a style operation (unlike a raw text edit) is safe to
        # split per zone, since applying the SAME field/value to each
        # zone's own overlapping sub-range independently produces exactly
        # the same combined visual result as one atomic operation would.
        # Never refused merely for crossing a zone-segment seam (a real
        # text edit still is - see segment_spanning_range/_on_text_
        # modified - because there's no equally safe per-zone split for
        # an ambiguous keystroke position).
        intersections = pm.segments_intersecting_range(self.current_paragraph_segments, start, end)
        if not intersections:
            self._flash_paragraph_header_warning(
                "This selection has no styleable text (it falls entirely on a page-number/footnote marker).")
            return

        current_value = sm.style_at_range(self.current_spans, start, end).get(field)
        new_value = not (current_value is True)
        self.current_spans = sm.apply_style_to_range(self.current_spans, start, end, field, new_value)
        self._rerender_tags_only()
        self._update_style_button_states()

        # THE fix for the confirmed "removed formatting doesn't survive"
        # bug: pass the explicit computed new_value (True OR False), not
        # a bare [field] list - a bare list always meant "add/set True"
        # (see inline_style.py's own docstring), so toggling an already-
        # italic selection OFF used to persist a no-op "add italic"
        # override on top of text native detection already calls
        # italic, silently surviving any fresh re-extraction (reopening
        # Verification, Generate XHTML, ...) even though the live
        # display correctly showed it as removed. Applied once per
        # intersecting zone, using THAT zone's own local offsets, and
        # bundled into ONE undo/redo Operation so a multi-zone selection
        # reverses/reapplies atomically rather than one zone at a time.
        befores, afters = {}, {}
        for seg, local_start, local_end in intersections:
            befores[seg.zone_id] = self._snapshot_correction_state(seg.zone_id)
            orch.apply_style(self.session, self.pdf, self.zone_manager, seg.zone_id,
                              local_start, local_end, {field: new_value}, mark_dirty=self._mark_dirty)
            afters[seg.zone_id] = self._snapshot_correction_state(seg.zone_id)

        self.undo_stack.push(Operation(
            f"apply style: {field}",
            undo=lambda snaps=befores: self._restore_correction_states_bulk(snaps, "style undo"),
            redo=lambda snaps=afters: self._restore_correction_states_bulk(snaps, "style redo")))
        self._update_undo_redo_buttons()

        self._bump_revision()
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._update_status_badge()

    def _update_style_button_states(self):
        offsets = self._char_offsets_from_selection()
        if offsets is None:
            try:
                pos = self._char_offset_of("insert")
                offsets = (pos, pos + 1)
            except tk.TclError:
                offsets = (0, 0)
        styles = sm.style_at_range(self.current_spans, *offsets)
        for field, button in self._style_widgets.items():
            value = styles.get(field, False)
            if value is True:
                button.config(relief="sunken", bg="#cde")
            elif value == "mixed":
                button.config(relief="raised", bg="#eed")
            else:
                button.config(relief="raised", bg=self.cget("bg"))

    def _on_text_modified(self, _event=None):
        if not self.text_widget.edit_modified():
            return
        self.text_widget.edit_modified(False)
        if not self.current_zone_id:
            return
        new_plain = self.text_widget.get("1.0", "end-1c")
        old_plain = sm.plain_text_of(self.current_spans)
        if new_plain == old_plain:
            return
        if self.current_paragraph_id is not None and self.paragraph_index.is_multi_zone(self.current_paragraph_id):
            # Direct free-text typing across a JOINED multi-zone paragraph
            # view has no single safe "which underlying zone(s) changed"
            # answer (a keystroke's own position is ambiguous once more
            # than one zone's text is concatenated on screen) - per this
            # pass's own explicit scope boundary (no full multi-zone
            # contiguous editor), revert the keystroke and point the
            # operator at the Zone Navigator instead, where selecting the
            # specific member zone loads its own single-zone view with
            # full editing exactly as before. Style application (bold/
            # italic/superscript/...) on an exact selection IS still
            # supported here (see apply_style_to_selection) since a
            # selection's start/end are unambiguous.
            # A non-blocking inline notice, not messagebox.showinfo - this
            # guard fires on every refused KEYSTROKE, and a modal dialog
            # popping up mid-typing would be both bad UX and (confirmed
            # directly while building this pass's own test) a genuine
            # deadlock risk for anything driving the Text widget
            # programmatically without an interactive user to dismiss it.
            self._flash_paragraph_header_warning(
                "Direct text edits aren't available in this joined multi-zone paragraph view — "
                "select the specific zone in the ZONE NAVIGATOR panel below to edit its own text directly.")
            self._render_spans_in_text_widget()
            return
        zone_id = self.current_zone_id
        before = self._snapshot_correction_state(zone_id)
        # A direct typed edit changes the character sequence itself (not
        # just formatting) - re-derive spans as a single normal-style run
        # over the new text (formatting on a freshly-typed region can't
        # be inferred) and persist as a manual text correction.
        self.current_spans = [sm.Span(new_plain)]
        orch.apply_manual_text(self.session, self.pdf, self.zone_manager, zone_id,
                                new_plain, mark_dirty=self._mark_dirty)
        after = self._snapshot_correction_state(zone_id)
        self.undo_stack.push(Operation(
            "manual text edit",
            undo=lambda zid=zone_id, snap=before: self._restore_correction_state(zid, snap),
            redo=lambda zid=zone_id, snap=after: self._restore_correction_state(zid, snap)))
        self._update_undo_redo_buttons()

        self._bump_revision()
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._update_status_badge()

    # ================================================================
    # recheck actions (always explicit, always background)
    # ================================================================

    def _run_recheck_job(self, fn, *args, **kwargs):
        started_at_revision = self.document_revision
        self._verifying = True
        self._update_status_badge()
        result_queue = queue.Queue()

        def worker():
            try:
                fn(*args, **kwargs)
                result_queue.put(("ok", None))
            except Exception as exc:  # noqa: BLE001
                result_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                kind, payload = result_queue.get_nowait()
            except queue.Empty:
                self.after(120, poll)
                return
            self._verifying = False
            if started_at_revision != self.document_revision:
                self._update_status_badge()
                return
            if kind == "error":
                self._partial_error = True
                messagebox.showerror("Recheck", f"Recheck failed: {payload}")
            self._refresh_issue_tree()
            self._refresh_zone_panel()
            self._update_status_badge()

        self.after(120, poll)

    def recheck_selection(self):
        self.recheck_current_zone()

    def recheck_selection_deep(self):
        if not self.current_zone_id:
            return
        self._run_recheck_job(orch.recheck_zone, self.session, self.pdf, self.zone_manager,
                               self.current_zone_id, force_formatting_check=True, mode="deep",
                               ocr_cache=self.app.ocr_cache, ocr_settings=self.app.ocr_settings)

    def recheck_current_zone(self):
        if not self.current_zone_id:
            return
        mode = self.mode_var.get().lower()
        self._run_recheck_job(orch.recheck_zone, self.session, self.pdf, self.zone_manager,
                               self.current_zone_id, mode=mode,
                               ocr_cache=self.app.ocr_cache, ocr_settings=self.app.ocr_settings)

    def recheck_current_page(self):
        zone = self.zone_manager.zones.get(self.current_zone_id) if self.current_zone_id else None
        if zone is None:
            return
        mode = self.mode_var.get().lower()
        self._run_recheck_job(orch.recheck_page, self.session, self.pdf, self.zone_manager, zone.page, mode=mode)

    def recheck_book(self):
        # Spec section 27: "Never automatically recheck the whole book" -
        # this is the one, explicit, confirm-gated exception.
        if not messagebox.askyesno("Recheck Book", "Recheck the ENTIRE book? This may take a while."):
            return
        mode = self.mode_var.get().lower()

        def _run():
            self.session = orch.run_full_verification(
                self.pdf, self.zone_manager, mode=mode,
                ocr_cache=self.app.ocr_cache, ocr_settings=self.app.ocr_settings)
            self._append_merge_candidate_issues(self.session)

        self._run_recheck_job(_run)

    # ================================================================
    # issue/fix actions
    # ================================================================

    def _is_merge_candidate_issue(self, issue) -> bool:
        return bool(issue and issue.id and issue.id.startswith("merge-candidate-"))

    def apply_fix_to_selected(self):
        if self.current_issue is None:
            return
        if self._is_merge_candidate_issue(self.current_issue):
            # [Merge Zones] quick action (spec sections 17/60) - reuses
            # the exact same Merge Previous mechanism/undo-redo path the
            # Zone Navigator's own Merge button uses, never a second
            # merge implementation. [Keep Separate] is the existing
            # Ignore action below (see ignore_selected) - functionally
            # identical for this issue type (no merge happens either
            # way), just reached via the same generic button.
            if self.current_zone_id != self.current_issue.zone_id:
                self._load_zone_into_panes(self.current_issue.zone_id)
            if self.merge_current_zone_with_previous():
                self.current_issue.status = vm.FIXED
                self._refresh_issue_tree()
                self._update_status_badge()
            return
        if self.current_issue.status != vm.AUTO_FIX_AVAILABLE:
            if not messagebox.askyesno(
                    "Apply Fix", "This issue is not marked as auto-fixable/evidence-backed. Apply the "
                                 "suggested text anyway?"):
                return
        zone_id = self.current_issue.zone_id
        if orch.is_issue_stale(self.pdf, self.zone_manager, self.current_issue):
            messagebox.showwarning(
                "Apply Fix", "Issue changed since verification - its stored location no longer matches "
                             "the zone's current text. Please Recheck before applying.")
            return
        before = self._snapshot_correction_state(zone_id)
        if not orch.apply_fix(self.session, self.pdf, self.zone_manager, self.current_issue,
                               mark_dirty=self._mark_dirty):
            messagebox.showinfo("Apply Fix", "This issue has no suggested text to apply.")
            return
        after = self._snapshot_correction_state(zone_id)
        self.undo_stack.push(Operation(
            f"apply fix: {self.current_issue.issue_type}",
            undo=lambda zid=zone_id, snap=before: self._restore_correction_state(zid, snap),
            redo=lambda zid=zone_id, snap=after: self._restore_correction_state(zid, snap)))
        self._update_undo_redo_buttons()

        self._bump_revision()
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._update_status_badge()
        if self.current_zone_id == zone_id:
            self._load_zone_into_panes(self.current_zone_id)

    def ignore_selected(self):
        if self.current_issue is None:
            return
        orch.ignore_issue(self.session, self.current_issue, mark_dirty=self._mark_dirty)
        self._refresh_issue_tree()
        self._refresh_zone_panel()
        self._update_status_badge()

    def add_selected_to_dictionary(self):
        if self.current_issue is None or self.current_issue.issue_type != vm.SPELLING:
            messagebox.showinfo("Add to Dictionary", "Select a spelling issue first.")
            return
        word = self.current_issue.extracted_text
        orch.add_to_dictionary(self.session, word, mark_dirty=self._mark_dirty)
        orch.ignore_issue(self.session, self.current_issue, mark_dirty=self._mark_dirty)
        self._refresh_issue_tree()

    # ================================================================
    # zone merge/split quick actions - reuse the existing, already-tested
    # ZoneManager.merge_with_previous/unmerge mechanism directly (see
    # core/zone_manager.py:806/1072); this window invents no new "one
    # logical unit spans multiple zones" data model. Both directions are
    # undo/redo-aware via this window's own UndoStack, independent of
    # ZoneManager's own separate main-canvas undo history.
    # ================================================================

    def _merge_skip_tags(self):
        profile = getattr(self.app, "active_profile", None) or {}
        return (profile.get("page_marker_tags"), profile.get("footnote_flow_tags"), profile.get("non_flow_tags"))

    def _append_merge_candidate_issues(self, session):
        """Spec sections 16-18/60 (completes Pass 2's own deferred item):
        suggests, never applies, a likely-missed paragraph continuation
        between two adjacent unlinked zones - see core.verification.
        paragraph_model.find_merge_candidate_issues for the actual
        evidence-based detection. Runs only as part of a WHOLE-BOOK
        verification pass (Run/Recheck Book) - per-zone/per-page rechecks
        stay scoped to their own zone/page, since a merge candidate is
        inherently a relationship BETWEEN two zones, not a property of
        one. Safe to call from a background thread - pure Python zone/
        PDF reads, no Tk widget access."""
        page_marker_tags, footnote_flow_tags, non_flow_tags = self._merge_skip_tags()
        candidates = pm.find_merge_candidate_issues(
            self.pdf, self.zone_manager, page_marker_tags, footnote_flow_tags, non_flow_tags)
        session.issues.extend(candidates)

    def merge_current_zone_with_previous(self) -> bool:
        zone_id = self.current_zone_id
        zone = self.zone_manager.zones.get(zone_id) if zone_id else None
        if zone is None:
            return False
        if zone.attributes.get("merged_with_previous"):
            messagebox.showinfo("Merge Zones", "This zone is already merged with its previous zone.")
            return False
        page_marker_tags, footnote_flow_tags, non_flow_tags = self._merge_skip_tags()
        ok, message, _ = self.zone_manager.merge_with_previous(
            zone_id, " ", page_marker_tags, footnote_flow_tags, non_flow_tags)
        if not ok:
            messagebox.showinfo("Merge Zones", message or "Could not merge this zone with the previous one.")
            return False
        self.undo_stack.push(Operation(
            "merge zone with previous",
            undo=lambda zid=zone_id: self._undo_zone_merge(zid),
            redo=lambda zid=zone_id, j=" ", pm=page_marker_tags, ff=footnote_flow_tags, nf=non_flow_tags:
                self._redo_zone_merge(zid, j, pm, ff, nf)))
        self._update_undo_redo_buttons()
        self._after_undo_redo_zone_change(zone_id, "zone merge")
        return True

    def split_current_zone(self):
        zone_id = self.current_zone_id
        zone = self.zone_manager.zones.get(zone_id) if zone_id else None
        if zone is None or not zone.attributes.get("merged_with_previous"):
            messagebox.showinfo("Split Zone", "This zone is not currently merged with a previous zone.")
            return
        join = zone.attributes.get("merge_join", " ")
        page_marker_tags, footnote_flow_tags, non_flow_tags = self._merge_skip_tags()
        if not self.zone_manager.unmerge(zone_id):
            messagebox.showinfo("Split Zone", "Could not split this zone.")
            return
        self.undo_stack.push(Operation(
            "split (unmerge) zone",
            undo=lambda zid=zone_id, j=join, pm=page_marker_tags, ff=footnote_flow_tags, nf=non_flow_tags:
                self._redo_zone_merge(zid, j, pm, ff, nf),
            redo=lambda zid=zone_id: self._undo_zone_merge(zid)))
        self._update_undo_redo_buttons()
        self._after_undo_redo_zone_change(zone_id, "zone split")

    def _undo_zone_merge(self, zone_id: str):
        self.zone_manager.unmerge(zone_id)
        self._after_undo_redo_zone_change(zone_id, "zone merge undo")

    def _redo_zone_merge(self, zone_id: str, join: str, page_marker_tags, footnote_flow_tags, non_flow_tags):
        self.zone_manager.merge_with_previous(zone_id, join, page_marker_tags, footnote_flow_tags, non_flow_tags)
        self._after_undo_redo_zone_change(zone_id, "zone merge redo")

    # ================================================================
    # save / generate / close
    # ================================================================

    def save_verification(self):
        if self._autosave_after_id:
            self.after_cancel(self._autosave_after_id)
            self._autosave_after_id = None
        try:
            self.app.verification_session = self.session
            self.app.mark_dirty("verification saved", critical=True)
        except Exception as exc:  # noqa: BLE001 - a failed save must never silently drop the change
            self._save_failed = True
            self.dirty_label.config(text="Save Failed ✕", fg="#d9534f")
            self._update_status_badge()
            messagebox.showerror("Save", f"Save failed: {exc}\n\nYour changes are kept in memory - "
                                          f"please try again.")
            return
        self._save_failed = False
        self._dirty = False
        self.dirty_label.config(text="Saved ✓", fg="#3a7")
        self._update_status_badge()

    def _generate_xhtml(self):
        """Spec section 56 - Generate XHTML must gate on unresolved HIGH-
        severity issues rather than silently generating from stale pre-
        verification data. Uses the existing, native 3-way confirm
        dialog (Yes/No/Cancel) rather than inventing a custom Toplevel
        for this one gate - Yes = Generate Anyway, No = Review Issues
        (jumps to the first open issue, same as clicking the status
        badge), Cancel = do nothing. Never blocks generation outright -
        the operator always has the final call, matching "Allow the
        existing workflow/profile rules to determine whether generation
        is permitted"."""
        unresolved_high = [i for i in self.session.issues if i.severity == "HIGH" and i.is_unresolved()]
        if unresolved_high:
            choice = messagebox.askyesnocancel(
                "Generate XML",
                f"Verification has {len(unresolved_high)} unresolved HIGH-severity issue(s) remaining.\n\n"
                "Generate anyway, or review the issues first?")
            if choice is None:
                return
            if choice is False:
                self._on_status_badge_click()
                return
        self.save_verification()
        self.app.generate_xml()

    def _on_close(self):
        self.app.verification_session = self.session
        self.app.mark_dirty("verification session updated")
        self.app.verification_window = None
        self.destroy()
