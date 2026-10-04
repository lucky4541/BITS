"""Top toolbar - grouped into labeled sections (Document / Navigation /
Edit / View / Zones / Profile / Output - spec 66.3) with tooltips (spec
66.28) and primary-vs-secondary button emphasis (Generate XHTML/XML get
accent styling; everything else stays neutral). Most one-off actions still
live inside a single "File" dropdown menu (Open/Save/Load Reference/Auto
Zone/Save Corrections/Rotate/Settings/Help) - unchanged from before, just
now sitting first in the DOCUMENT group instead of alone. Every button
still calls the identical existing App method it always called; this file
only changes layout/styling, never behavior (spec 66.32)."""
import tkinter as tk
from tkinter import ttk

from core import profile_manager, xhtml_profile_manager, generation_types
from gui import theme

GENERATION_TYPES = ["CUPEPUB", "XHTML-EPUB", "Client XHTML"]


class Toolbar(tk.Frame):
    def __init__(self, parent, app):
        palette = theme.current.palette
        super().__init__(parent, bg=palette["toolbar_bg"], padx=theme.SPACE_MD, pady=6,
                          highlightthickness=1, highlightbackground=palette["toolbar_border"])
        self.app = app
        self.palette = palette
        self._tooltips = []

        # Two rows rather than one long horizontal strip - a single row with
        # every group (Document/Navigation/Edit/View/Zones/Profile/Output)
        # doesn't fit at the spec's own stated minimum resolution (1366x768,
        # spec 66.24) without clipping; splitting into "document editing"
        # (row1) and "tagging/output" (row2) fits comfortably even at
        # 1280x720, still fully grouped/labeled, just wrapped by group
        # rather than by mid-group truncation.
        row1 = tk.Frame(self, bg=palette["toolbar_bg"])
        row1.pack(side=tk.TOP, fill=tk.X)
        row2 = tk.Frame(self, bg=palette["toolbar_bg"])
        row2.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))

        def group_label(text, parent=row1):
            tk.Label(parent, text=text.upper(), bg=palette["toolbar_bg"], fg=palette["text_faint"],
                      font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT, padx=(0, 4))

        def sep(parent=row1):
            ttk.Separator(parent, orient="vertical").pack(side=tk.LEFT, fill="y", padx=6, pady=2)

        def btn(text, command, parent=row1, kind="secondary", tooltip=None, width=None):
            b = tk.Button(parent, text=text, command=command, width=width)
            theme.style_button(b, palette, kind=kind)
            b.pack(side=tk.LEFT, padx=2)
            if tooltip:
                self._tooltips.append(theme.Tooltip(b, tooltip))
            return b

        # ---------------- DOCUMENT ----------------
        group_label("Document")
        file_mb = tk.Menubutton(row1, text="File ▾", relief="flat", bg=palette["button_bg"],
                                  fg=palette["text"], font=theme.FONT_BODY, cursor="hand2",
                                  bd=1, highlightthickness=1, highlightbackground=palette["border"], padx=10, pady=3)
        file_mb.pack(side=tk.LEFT, padx=2)

        self.file_menu = tk.Menu(file_mb, tearoff=0)
        file_mb.config(menu=self.file_menu)
        self.file_menu.add_command(label="Open PDF", command=app.open_pdf)
        self.file_menu.add_command(label="Save Project", command=app.save_project)
        self.file_menu.add_command(label="Load Reference Project", command=app.load_reference_project)
        self._auto_zone_index = 3
        self.file_menu.add_command(label="Auto Zone", command=app.auto_zone, state="disabled")
        # "Auto Zone > Index" (spec 25) - the SAME App.auto_zone_index the
        # Tag Toolbox's contextual button already calls (gui/zone_panel.py),
        # just a second, always-reachable entry point into it that doesn't
        # depend on Index tags currently being visible in the toolbox.
        # Inserted AFTER "Auto Zone" (not before) so self._auto_zone_index
        # above still points at the right menu position.
        self.file_menu.add_command(label="Auto Zone Index", command=app.auto_zone_index)
        # "Recalculate Reading Order" (spec: "ZoneTool - Fix Multi-Column
        # Reading Order") - an explicit, on-demand entry point into the
        # SAME column-aware core.reading_order.recompute_all_pages() every
        # ordinary zoning edit already triggers automatically (App.
        # on_zones_changed) - needed because loading an EXISTING saved
        # project deliberately does NOT auto-recompute (a project's
        # zone.serial values are trusted as-is on load, so a prior
        # explicit manual reorder is never silently overwritten just by
        # reopening the file - see load_project()). This gives the user a
        # way to apply the corrected column-aware algorithm to a project
        # that was saved before the fix, without having to nudge every
        # zone individually just to trigger a recompute as a side effect.
        self.file_menu.add_command(label="Recalculate Reading Order", command=app.recalculate_reading_order)
        self.file_menu.add_command(label="Auto Analyse", command=app.auto_analyse)
        # "Auto Zone -> Paragraph -> Current Page/Entire File" and "Auto
        # Zone -> Bibliography / Auto Zone by Pattern" (spec: "ZoneTool -
        # Paragraph Auto Zone + Bibliography Auto Zone ONLY") - a SECOND,
        # separate "Auto Zone" entry from the plain one above (that one is
        # the older, reference-template-driven feature; this cascade is the
        # new geometry-only paragraph/bibliography-pattern feature this
        # spec adds) - shown as a cascade since Tk renders it with its own
        # submenu arrow, so the two are never visually ambiguous.
        auto_zone_menu = tk.Menu(self.file_menu, tearoff=0)
        paragraph_menu = tk.Menu(auto_zone_menu, tearoff=0)
        paragraph_menu.add_command(label="Current Page", command=app.auto_zone_paragraph_current_page)
        paragraph_menu.add_command(label="Entire File", command=app.auto_zone_paragraph_entire_file)
        auto_zone_menu.add_cascade(label="Paragraph", menu=paragraph_menu)
        auto_zone_menu.add_command(label="Bibliography / Auto Zone by Pattern",
                                    command=app.auto_zone_bibliography_by_pattern)
        self.file_menu.add_cascade(label="Auto Zone", menu=auto_zone_menu)
        # CUPEPUB layout + semantic Auto Zone / Auto Tag engine
        # (auto_zoning/smart_auto_zone.py, gui/smart_auto_zone_ui.py).
        smart = app.smart_az
        smart_menu = tk.Menu(self.file_menu, tearoff=0)
        smart_menu.add_command(label="Auto Zone Page", command=smart.auto_zone_page)
        smart_menu.add_command(label="Re-analyse Page (replace automatic zones)", command=smart.reanalyse_page)
        smart_menu.add_command(label="Auto Zone Document...", command=smart.auto_zone_document)
        smart_menu.add_command(label="Link Page Continuations (whole document)",
                               command=smart.link_continuations_document)
        smart_menu.add_separator()
        smart_menu.add_command(label="Auto Tag Page", command=smart.auto_tag_page)
        smart_menu.add_command(label="Auto Tag Document", command=smart.auto_tag_document)
        smart_menu.add_separator()
        smart_menu.add_command(label="Next Zone Needing Review (F7)", command=smart.next_review_zone)
        smart_menu.add_command(label="Validation Report...", command=smart.validation_report)
        smart_menu.add_command(label="Live XML Preview...", command=smart.open_preview)
        smart_menu.add_separator()
        smart_menu.add_command(label="Tag Knowledge Model...", command=smart.show_knowledge)
        smart_menu.add_command(label="Load Project DTD...", command=smart.load_dtd)
        smart_menu.add_command(label="Load Reference XML Corpus...", command=smart.load_reference_corpus)
        overlay_menu = tk.Menu(smart_menu, tearoff=0)
        smart.build_overlay_menu(overlay_menu)
        smart_menu.add_cascade(label="Debug Overlays", menu=overlay_menu)
        self.file_menu.add_cascade(label="Auto Zone / Auto Tag (CUPEPUB)", menu=smart_menu)
        self.file_menu.add_command(label="Auto Detect (OCR)...", command=app.auto_detect_ocr)
        self.file_menu.add_command(label="Prepare OCR Cache...", command=app.prepare_ocr_cache)
        self.file_menu.add_command(label="OCR Entire Document...", command=app.ocr_entire_document)
        self.file_menu.add_command(label="OCR Settings...", command=app.open_ocr_settings)
        self.file_menu.add_command(label="Save Corrections as Template", command=app.save_corrections_as_template)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Fit Width", command=lambda: app.set_zoom_mode("fit_width"))
        self.file_menu.add_command(label="Fit Page", command=lambda: app.set_zoom_mode("fit_page"))

        self.rotate_menu = tk.Menu(self.file_menu, tearoff=0)
        self.rotate_menu.add_command(label="Rotate 90° Clockwise", command=app.rotate_page_cw)
        self.rotate_menu.add_command(label="Rotate 90° Counter-Clockwise", command=app.rotate_page_ccw)
        self.rotate_menu.add_command(label="Reset Rotation", command=app.reset_page_rotation)
        self.rotate_menu.add_separator()
        self._rotation_readout_index = 4
        self.rotate_menu.add_command(label="Rotation: 0°", state="disabled")
        self.file_menu.add_cascade(label="Rotate Page", menu=self.rotate_menu)

        self.file_menu.add_separator()
        self.file_menu.add_command(label="Settings", command=app.open_settings)
        self.file_menu.add_command(label="Theme: Toggle Light/Dark", command=app.toggle_theme)
        self.file_menu.add_command(label="CUPEPUB Config Status...", command=app.show_cup_diagnostics)
        self.file_menu.add_command(label="Help", command=app.show_help)

        btn("Load Project", app.load_project, tooltip="Open a previously saved EPUBForge project")
        sep()

        # ---------------- NAVIGATION ----------------
        group_label("Navigation")
        btn("←", app.prev_page, tooltip="Previous page  (Left Arrow)", width=3)
        self.page_var = tk.StringVar(value="1")
        page_entry = tk.Entry(row1, textvariable=self.page_var, width=5, justify="center",
                               bg=palette["surface"], fg=palette["text"], relief="flat",
                               highlightthickness=1, highlightbackground=palette["border"], font=theme.FONT_BODY)
        page_entry.pack(side=tk.LEFT, padx=2)
        page_entry.bind("<Return>", lambda e: app.goto_page(self.page_var.get()))
        self.page_total_label = tk.Label(row1, text="/ 0", bg=palette["toolbar_bg"], fg=palette["text_muted"],
                                          font=theme.FONT_BODY)
        self.page_total_label.pack(side=tk.LEFT, padx=2)
        btn("→", app.next_page, tooltip="Next page  (Right Arrow)", width=3)
        sep()

        # ---------------- EDIT ----------------
        group_label("Edit")
        self.undo_btn = btn("↶ Undo", app.undo, tooltip="Undo last zone operation  (Ctrl+Z)")
        self.redo_btn = btn("↷ Redo", app.redo, tooltip="Redo last zone operation  (Ctrl+Y)")
        sep()

        # ---------------- VIEW ----------------
        group_label("View")
        btn("−", app.zoom_out, tooltip="Zoom out", width=3)
        self.zoom_label = tk.Label(row1, text="100%", bg=palette["toolbar_bg"], fg=palette["text"],
                                    font=theme.FONT_BODY, width=5)
        self.zoom_label.pack(side=tk.LEFT, padx=2)
        btn("+", app.zoom_in, tooltip="Zoom in", width=3)
        btn("100%", app.zoom_reset, tooltip="Reset zoom to 100%")

        # Quick "Show Labels" checkbox (spec 22/23/33) plus a fuller "View"
        # menu (spec 24) sharing the SAME BooleanVars, so toggling either
        # one keeps the other in sync - never two independent states for
        # the same setting.
        self.show_borders_var = tk.BooleanVar(value=app.settings.get("show_zone_borders", True))
        self.show_labels_var = tk.BooleanVar(value=app.settings.get("show_tag_labels", True))
        self.show_ro_var = tk.BooleanVar(value=app.settings.get("show_reading_order", True))

        labels_chk = tk.Checkbutton(
            row1, text="Show Labels", variable=self.show_labels_var, bg=palette["toolbar_bg"],
            fg=palette["text"], selectcolor=palette["surface"], font=theme.FONT_BODY, activebackground=palette["toolbar_bg"],
            command=lambda: app.set_display_toggle("show_tag_labels", self.show_labels_var.get()))
        labels_chk.pack(side=tk.LEFT, padx=(8, 2))
        self._tooltips.append(theme.Tooltip(labels_chk, "Show/hide the [RO:N] tag text over zones - "
                                                          "the zone rectangle, selection and handles are unaffected"))

        view_mb = tk.Menubutton(row1, text="View ▾", relief="flat", bg=palette["button_bg"],
                                  fg=palette["text"], font=theme.FONT_BODY, cursor="hand2",
                                  bd=1, highlightthickness=1, highlightbackground=palette["border"], padx=10, pady=3)
        view_mb.pack(side=tk.LEFT, padx=2)
        view_menu = tk.Menu(view_mb, tearoff=0)
        view_menu.add_checkbutton(label="Show Zone Borders", variable=self.show_borders_var,
                                   command=lambda: app.set_display_toggle("show_zone_borders", self.show_borders_var.get()))
        view_menu.add_checkbutton(label="Show Tag Labels", variable=self.show_labels_var,
                                   command=lambda: app.set_display_toggle("show_tag_labels", self.show_labels_var.get()))
        view_menu.add_checkbutton(label="Show Reading Order", variable=self.show_ro_var,
                                   command=lambda: app.set_display_toggle("show_reading_order", self.show_ro_var.get()))
        view_mb.config(menu=view_menu)
        self.toggle_right_panel_btn = btn("▶ Collapse Panel", app.toggle_right_panel,
                                           tooltip="Collapse/expand the Zone Hierarchy panel "
                                                    "to give the PDF viewer more width")
        sep()

        # ---------------- ZONES ----------------
        group_label("Zones", parent=row2)
        btn("Split", app.start_horizontal_split, parent=row2,
            tooltip="Horizontal Split - divide the selected zone")

        # Automatic numbered-notes splitter. Detection is performed only
        # inside the selected zone; the App delegates mutation to the
        # existing ZoneManager split/undo/reading-order pipeline.
        btn("Auto Split Numbers", app.auto_split_based_on_numbers, parent=row2,
            tooltip="Automatically split a selected notes/endnotes zone by numbered notes")

        merge_mb = tk.Menubutton(row2, text="Merge Previous ▾", relief="flat", bg=palette["button_bg"],
                                   fg=palette["text"], font=theme.FONT_BODY, cursor="hand2",
                                   bd=1, highlightthickness=1, highlightbackground=palette["border"], padx=10, pady=3)
        merge_mb.pack(side=tk.LEFT, padx=2)
        merge_menu = tk.Menu(merge_mb, tearoff=0)
        merge_menu.add_command(label="Merge with Space", command=lambda: app.merge_selected_with_previous_mode(" "))
        merge_menu.add_command(label="Merge without Space", command=lambda: app.merge_selected_with_previous_mode(""))
        merge_mb.config(menu=merge_menu)
        self._tooltips.append(theme.Tooltip(merge_mb, "Merge the selected zone's text into the previous zone"))
        sep(parent=row2)

        # ---------------- PROFILE ----------------
        group_label("Profile", parent=row2)
        self.profile_var = tk.StringVar(value="XML")
        self.profile_combo = ttk.Combobox(row2, textvariable=self.profile_var,
                                           values=profile_manager.list_profile_names(),
                                           width=9, state="readonly", font=theme.FONT_BODY)
        self.profile_combo.pack(side=tk.LEFT, padx=2)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda e: app.set_profile(self.profile_var.get()))
        sep(parent=row2)

        # ---------------- OUTPUT ----------------
        group_label("Output", parent=row2)

        # "Generation Type" (spec: EPUBForge Part 7) - gates Generate XML
        # vs Generate XHTML IN ADDITION TO (never instead of) the existing
        # Profile-driven xhtml_enabled gating above: CUPEPUB enables XML/
        # disables XHTML (the existing CUPEPUB XML path is untouched by
        # this - see App.generate_xml), XHTML-EPUB enables XHTML via the
        # existing unchanged Mapping.xml pipeline, Client XHTML enables
        # XHTML via the new profiles/xhtml/*.json-driven generator
        # (core/client_xhtml_generator.py) instead.
        self.generation_type_var = tk.StringVar(value=app.settings.get("generation_type", "CUPEPUB"))
        gen_type_combo = ttk.Combobox(row2, textvariable=self.generation_type_var, values=GENERATION_TYPES,
                                       width=12, state="readonly", font=theme.FONT_BODY)
        gen_type_combo.pack(side=tk.LEFT, padx=2)
        gen_type_combo.bind("<<ComboboxSelected>>", lambda e: app.set_generation_type(self.generation_type_var.get()))
        self._tooltips.append(theme.Tooltip(gen_type_combo, "Generation Type - which output pipeline "
                                                              "Generate XML/XHTML uses"))

        # "XHTML Profile" (spec Part 8) - the 21 client document-structure
        # profiles (profiles/xhtml/*.json), only meaningful for Client
        # XHTML generation - see set_xhtml_profile_enabled.
        self.xhtml_profile_var = tk.StringVar(value=app.settings.get("xhtml_profile_label", ""))
        self._xhtml_profile_labels = {}  # label -> key
        self.xhtml_profile_combo = ttk.Combobox(row2, textvariable=self.xhtml_profile_var,
                                                  width=16, state="readonly", font=theme.FONT_BODY)
        self.xhtml_profile_combo.pack(side=tk.LEFT, padx=2)
        self.xhtml_profile_combo.bind("<<ComboboxSelected>>",
                                       lambda e: app.set_xhtml_profile(self._xhtml_profile_labels.get(
                                           self.xhtml_profile_var.get())))
        self._tooltips.append(theme.Tooltip(self.xhtml_profile_combo,
                                             "XHTML Profile - document structure for Client XHTML generation"))
        self.refresh_xhtml_profiles()
        sep(parent=row2)

        # ---------------- TYPE ----------------
        # (spec: "EPUBForge - TYPE Dropdown, CUPEPUB Output Behavior") - a
        # content/generation-structure selector, DISTINCT from the "XHTML
        # Profile" dropdown above (which is Client-XHTML-only). Reuses the
        # EXISTING component_type concept (core.profile_manager's per-
        # profile component_types, already read by App.generate_xhtml()/
        # generate_xml() as settings["epub_component_type"]) - see core/
        # generation_types.py's own docstring. Always enabled, and
        # deliberately NEVER a precondition for Generate XHTML - see
        # App.update_generation_controls(), the single function that
        # decides Generate XML/XHTML state from Profile + Output only.
        group_label("Type", parent=row2)
        self._HEADER_PREFIX = "── "  # "── FRONT MATTER ──" - a non-selectable-looking group header
        self.SELECT_TYPE_PLACEHOLDER = "Select Type"
        self.type_var = tk.StringVar(value=self.SELECT_TYPE_PLACEHOLDER)
        self._type_display_to_key = {}  # display label -> real component_type key (None for the placeholder)
        self.type_combo = ttk.Combobox(row2, textvariable=self.type_var, width=18,
                                        state="readonly", font=theme.FONT_BODY)
        self.type_combo.pack(side=tk.LEFT, padx=2)
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_selected)
        self._tooltips.append(theme.Tooltip(
            self.type_combo, "Type - optional content-structure configuration for generation. "
                               "Leaving this as \"Select Type\" does not block Generate XHTML."))

        self.generate_xml_btn = btn("Generate XML", app.generate_xml, parent=row2, kind="primary",
                                     tooltip="Generate BITS-style XML from the current zoning")
        self.verify_btn = btn("Verify", app.open_verification_window, parent=row2, kind="secondary",
                               tooltip="Review OCR/extraction accuracy, formatting, spelling, and grammar "
                                        "before generating XHTML (optional - existing workflow is unaffected "
                                        "unless you open this)")
        self.generate_xhtml_btn = btn("Generate XHTML", app.generate_xhtml, parent=row2, kind="primary",
                                       tooltip="Generate XHTML via Mapping.xml (EPUB/CUPEPUB) or, in Client "
                                                "XHTML mode, via the selected XHTML Profile")
        # Initial enable/disable state is NOT computed here: `app.toolbar`
        # doesn't exist yet at this point (we're still inside its own
        # constructor) - App.__init__ calls self.set_profile(...) itself
        # immediately after constructing this Toolbar, which calls
        # App.update_generation_controls() and establishes the real
        # initial state exactly once, never duplicated here.

    def _on_type_selected(self, _event=None):
        """A category header ("── FRONT MATTER ──") is present in the
        dropdown's own values list purely for visual grouping - ttk.
        Combobox has no native optgroup concept, so headers are plain,
        technically-selectable strings; selecting one is treated as a
        no-op (reverts to the placeholder) rather than a real Type choice."""
        selected = self.type_var.get()
        if selected.startswith(self._HEADER_PREFIX):
            self.type_var.set(self.SELECT_TYPE_PLACEHOLDER)
            return
        key = self._type_display_to_key.get(selected)  # None for the placeholder itself
        self.app.set_generation_component_type(key)

    def refresh_type_dropdown(self, component_types: list):
        """Rebuilds the TYPE dropdown for the ACTIVE profile's own
        component_types (spec: "Update available TYPE values" on every
        Profile change) - grouped FRONT MATTER / BODY / BACK MATTER per
        core.generation_types.available_types. Never disturbs the
        currently-selected value if it's still valid for the new list;
        falls back to the placeholder otherwise (e.g. switching to a
        profile that doesn't declare the previously-selected type)."""
        current_key = self._type_display_to_key.get(self.type_var.get())
        grouped = generation_types.available_types(component_types)
        values = [self.SELECT_TYPE_PLACEHOLDER]
        self._type_display_to_key = {self.SELECT_TYPE_PLACEHOLDER: None}
        still_valid_display = self.SELECT_TYPE_PLACEHOLDER
        for category, entries in grouped:
            values.append(f"{self._HEADER_PREFIX}{category} {self._HEADER_PREFIX}")
            for key, label in entries:
                values.append(label)
                self._type_display_to_key[label] = key
                if key == current_key:
                    still_valid_display = label
        self.type_combo.config(values=values)
        self.type_var.set(still_valid_display)

    def set_xml_enabled(self, enabled: bool):
        self.generate_xml_btn.config(state="normal" if enabled else "disabled")

    def set_xhtml_enabled(self, enabled: bool):
        self.generate_xhtml_btn.config(state="normal" if enabled else "disabled")

    def set_xhtml_profile_combo_enabled(self, enabled: bool):
        self.xhtml_profile_combo.config(state="readonly" if enabled else "disabled")

    def refresh_xhtml_profiles(self):
        """Rebuilds the XHTML Profile dropdown from profiles/xhtml/*.json
        (configured profiles first, then placeholders - matches
        xhtml_profile_manager.list_profiles's own ordering) - callable
        again after Settings > XHTML Profiles edits a file."""
        items = xhtml_profile_manager.list_profiles()
        self._xhtml_profile_labels = {}
        display_values = []
        for key, label, configured in items:
            display = label if configured else f"{label} (not configured)"
            self._xhtml_profile_labels[display] = key
            display_values.append(display)
        self.xhtml_profile_combo.config(values=display_values)

    def set_profile_options(self, profile_name: str):
        """Reflects the active project's saved profile (project load / new
        project) back onto the dropdown without re-triggering set_profile
        (which would otherwise re-run its own side effects redundantly)."""
        self.profile_combo.set(profile_name)

    def sync_display_toggles(self, settings: dict):
        """Reflects a just-loaded project's own show_zone_borders/
        show_tag_labels/show_reading_order values onto the checkbox/menu
        BooleanVars, without re-triggering set_display_toggle (the values
        are already correct in self.settings - this only updates what the
        controls DISPLAY)."""
        self.show_borders_var.set(settings.get("show_zone_borders", True))
        self.show_labels_var.set(settings.get("show_tag_labels", True))
        self.show_ro_var.set(settings.get("show_reading_order", True))

    def set_undo_redo_enabled(self, can_undo: bool, can_redo: bool):
        self.undo_btn.config(state="normal" if can_undo else "disabled")
        self.redo_btn.config(state="normal" if can_redo else "disabled")

    def set_page_info(self, page: int, total: int):
        self.page_var.set(str(page))
        self.page_total_label.config(text=f"/ {total}")

    def set_zoom_label(self, zoom: float):
        self.zoom_label.config(text=f"{int(round(zoom * 100))}%")

    def set_auto_zone_enabled(self, enabled: bool):
        self.file_menu.entryconfig(self._auto_zone_index, state="normal" if enabled else "disabled")

    def set_rotation_label(self, rotation: int):
        self.rotate_menu.entryconfig(self._rotation_readout_index, label=f"Rotation: {rotation}°")

    def set_right_panel_collapsed(self, collapsed: bool):
        self.toggle_right_panel_btn.configure(text="◀ Expand Panel" if collapsed else "▶ Collapse Panel")
