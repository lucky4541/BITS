# """Left tag-button panel (TAG TOOLBOX) and right zone-hierarchy tree panel."""
# import tkinter as tk
# from tkinter import ttk

# from core.constants import TAG_BUTTONS, DEFAULT_TAG_COLORS
# from core import reading_order, hierarchy, debug_log
# from gui import theme
# # Reuses the SAME centralized scroll-speed constants gui/pdf_viewer.py's own
# # wheel/touchpad fix already established (never a second, duplicated set of
# # magic numbers) - the Tag Toolbox had the identical "too fast" root cause:
# # no yscrollincrement set (Tk's own default unit size grows with the
# # widget's height) plus a flat "1 unit per event" that ignored a real
# # wheel notch's/touchpad sample's own magnitude entirely.
# from gui.pdf_viewer import SCROLL_UNIT_PX, normalize_wheel_units, accumulate_scroll_units


# def _format_shortcut(seq: str) -> str:
#     """Tk bind-sequence syntax -> a friendly display string for the tag
#     toolbox ("<Control-Alt-Key-1>" -> "Ctrl+Alt+1") - best-effort only
#     (falls back to the raw sequence, stripped of angle brackets, for
#     anything it doesn't recognize); this is a display concern only, never
#     used for the actual binding (see gui/main_window.py's
#     App._apply_index_tag_shortcuts, which binds the raw setting value
#     directly)."""
#     if not seq:
#         return ""
#     inner = seq.strip("<>")
#     replacements = {"Control": "Ctrl", "Shift": "Shift", "Alt": "Alt"}
#     parts = [replacements.get(p, p) for p in inner.split("-") if p != "Key"]
#     return "+".join(parts) if parts else inner


# class PropertiesInspector(tk.Frame):
#     """Compact bottom drawer under the Zone Hierarchy (spec 66.13) showing
#     the selected zone's own already-computed fields - no new computation,
#     purely a read-only display of what ZoneManager/hierarchy already track.
#     Collapsible (header click) so it never permanently consumes large
#     screen space when the user doesn't need it.

#     Also hosts CUPEPUB's manual PageNum entry (spec: OCR/PDF extraction
#     cannot always correctly read a Roman numeral or printed page label -
#     the user must be able to type the correct value directly). Shown only
#     when the selected zone IS the active profile's own page-marker zone
#     (app.active_profile["cup_pagenum_name"]) - every other profile/zone
#     sees the exact same read-only panel as before this existed."""
#     _FIELDS = ("Zone", "Page", "Reading Order", "Level", "Parent", "Text", "Coordinates")

#     def __init__(self, parent, app, palette):
#         super().__init__(parent, bg=palette["panel_header_bg"], highlightthickness=1,
#                           highlightbackground=palette["border"])
#         self.app = app
#         self.palette = palette
#         self._expanded = True
#         self._current_zone_id = None

#         header = tk.Frame(self, bg=palette["panel_header_bg"], cursor="hand2")
#         header.pack(fill="x")
#         self._arrow_lbl = tk.Label(header, text="▼  PROPERTIES", bg=palette["panel_header_bg"],
#                                      fg=palette["text_muted"], font=theme.FONT_SMALL_BOLD, anchor="w")
#         self._arrow_lbl.pack(side="left", padx=10, pady=6)
#         for w in (header, self._arrow_lbl):
#             w.bind("<Button-1>", lambda e: self._toggle())

#         self._body = tk.Frame(self, bg=palette["surface"])
#         self._body.pack(fill="x")
#         self._value_labels = {}
#         self._field_label_widgets = {}
#         for i, field in enumerate(self._FIELDS):
#             if field == "Text":
#                 continue  # built as an editable widget below instead of a read-only Label
#             lbl = tk.Label(self._body, text=field.upper(), bg=palette["surface"], fg=palette["text_faint"],
#                             font=theme.FONT_SMALL, anchor="w", width=13)
#             lbl.grid(row=i, column=0, sticky="w", padx=(10, 4), pady=2)
#             self._field_label_widgets[field] = lbl
#             val = tk.Label(self._body, text="—", bg=palette["surface"], fg=palette["text"],
#                             font=theme.FONT_BODY, anchor="w", justify="left", wraplength=260)
#             val.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=2)
#             self._value_labels[field] = val

#         # Generic editable zone text (spec: OCR text must remain editable,
#         # RULE 5/6 - manual correction always overrides OCR/extracted text,
#         # for ANY zone, not just PageNum). Reuses the exact same
#         # zone_manager.set_zone_text() call the PageNum row below already
#         # uses, so Undo/Redo and dirty-marking work identically for both.
#         # Hidden for the profile's own PageNum zone (row 62 below already
#         # gives that one a dedicated, purpose-built single-line editor with
#         # empty/"(empty)" handling PageNum needs that a generic multi-line
#         # box shouldn't duplicate)."""
#         text_row = self._FIELDS.index("Text")
#         self._text_label_widget = tk.Label(self._body, text="TEXT", bg=palette["surface"],
#                                              fg=palette["text_faint"], font=theme.FONT_SMALL, anchor="w", width=13)
#         self._text_label_widget.grid(row=text_row, column=0, sticky="nw", padx=(10, 4), pady=2)
#         text_edit = tk.Frame(self._body, bg=palette["surface"])
#         text_edit.grid(row=text_row, column=1, sticky="w", padx=(0, 10), pady=2)
#         self._text_box = tk.Text(text_edit, width=32, height=3, wrap="word", bg=palette["app_bg"],
#                                    fg=palette["text"], relief="flat", highlightthickness=1,
#                                    highlightbackground=palette["border"], font=theme.FONT_BODY)
#         self._text_box.pack(side="top", fill="x")
#         self._text_apply_btn = tk.Button(text_edit, text="Apply Text", command=self._apply_text)
#         theme.style_button(self._text_apply_btn, palette, kind="secondary")
#         self._text_apply_btn.pack(side="top", anchor="e", pady=(4, 0))
#         self._text_widgets = (self._text_label_widget, text_edit)

#         # PageNum manual-entry row - gridded into the SAME body, hidden by
#         # default (grid_remove), only re-gridded when show_zone() detects
#         # the selected zone is the profile's own page-marker zone. The
#         # "Page" field above is relabeled "PHYSICAL PDF PAGE" (instead of
#         # the generic "PAGE" every other zone type sees) whenever this row
#         # is visible, so the two numbers can never be mistaken for each
#         # other (spec: physical PDF location vs. printed EPUB page value).
#         self._pagenum_row = len(self._FIELDS)
#         tk.Label(self._body, text="PRINTED PAGE NUMBER", bg=palette["surface"], fg=palette["text_faint"],
#                   font=theme.FONT_SMALL, anchor="w", width=13).grid(
#             row=self._pagenum_row, column=0, sticky="w", padx=(10, 4), pady=(8, 2))
#         pagenum_edit = tk.Frame(self._body, bg=palette["surface"])
#         pagenum_edit.grid(row=self._pagenum_row, column=1, sticky="w", padx=(0, 10), pady=(8, 2))
#         self._pagenum_var = tk.StringVar()
#         self._pagenum_var.trace_add("write", lambda *_: self._sync_pagenum_button())
#         pagenum_entry = tk.Entry(pagenum_edit, textvariable=self._pagenum_var, width=14,
#                                   bg=palette["app_bg"], fg=palette["text"], relief="flat",
#                                   highlightthickness=1, highlightbackground=palette["border"], font=theme.FONT_BODY)
#         pagenum_entry.pack(side="left", ipady=2)
#         pagenum_entry.bind("<Return>", lambda e: self._apply_pagenum())
#         self._pagenum_apply_btn = tk.Button(pagenum_edit, text="Apply", command=self._apply_pagenum)
#         theme.style_button(self._pagenum_apply_btn, palette, kind="primary")
#         self._pagenum_apply_btn.pack(side="left", padx=(6, 0))
#         self._pagenum_empty_hint = tk.Label(pagenum_edit, text="(empty)", bg=palette["surface"],
#                                               fg=palette["warning"], font=theme.FONT_SMALL)
#         self._pagenum_empty_hint.pack(side="left", padx=(6, 0))
#         self._pagenum_widgets = (self._body.grid_slaves(row=self._pagenum_row, column=0)[0], pagenum_edit)
#         self._set_pagenum_row_visible(False)

#         # Index Level indicator + shortcut hint (spec 5: "When an Index
#         # zone is selected, show its current level clearly... This makes
#         # it obvious what the shortcut will change") - gridded the same
#         # hidden-by-default way as the PageNum row above, shown only when
#         # show_zone() detects the selected zone's tag is one of the active
#         # profile's own index_hierarchy_tags.
#         self._index_level_row = self._pagenum_row + 1
#         tk.Label(self._body, text="INDEX LEVEL", bg=palette["surface"], fg=palette["text_faint"],
#                   font=theme.FONT_SMALL, anchor="w", width=13).grid(
#             row=self._index_level_row, column=0, sticky="w", padx=(10, 4), pady=(8, 2))
#         self._index_level_value = tk.Label(self._body, text="—", bg=palette["surface"], fg=palette["text"],
#                                              font=theme.FONT_BODY, anchor="w")
#         self._index_level_value.grid(row=self._index_level_row, column=1, sticky="w", padx=(0, 10), pady=(8, 2))
#         self._index_shortcuts_row = self._index_level_row + 1
#         tk.Label(self._body, text="SHORTCUTS", bg=palette["surface"], fg=palette["text_faint"],
#                   font=theme.FONT_SMALL, anchor="nw", width=13).grid(
#             row=self._index_shortcuts_row, column=0, sticky="nw", padx=(10, 4), pady=(0, 8))
#         self._index_shortcuts_value = tk.Label(self._body, text="—", bg=palette["surface"], fg=palette["text_muted"],
#                                                  font=theme.FONT_SMALL, anchor="w", justify="left")
#         self._index_shortcuts_value.grid(row=self._index_shortcuts_row, column=1, sticky="w",
#                                           padx=(0, 10), pady=(0, 8))
#         self._index_widgets = (
#             self._body.grid_slaves(row=self._index_level_row, column=0)[0], self._index_level_value,
#             self._body.grid_slaves(row=self._index_shortcuts_row, column=0)[0], self._index_shortcuts_value,
#         )
#         self._set_index_rows_visible(False)

#         # Table Draw split controls (spec: "Add Row Split"/"Delete Row
#         # Split"/"Add Column Split"/"Delete Column Split" UI buttons) -
#         # shown only when the selected zone is tagged "table" (the Table
#         # Draw tag - profiles/epub_profile.json/xml_profile.json). Every
#         # button below delegates to an EXISTING gui/pdf_viewer.py
#         # primitive (start_region_split_mode/delete_selected_region_split)
#         # - no new split/persist/generate logic, only a discoverable UI
#         # entry point next to the keyboard shortcuts (Ctrl+Shift+Alt+6/7,
#         # see gui/main_window.py._start_table_draw_split).
#         self._table_draw_row = self._index_shortcuts_row + 1
#         tk.Label(self._body, text="TABLE DRAW", bg=palette["surface"], fg=palette["text_faint"],
#                   font=theme.FONT_SMALL, anchor="nw", width=13).grid(
#             row=self._table_draw_row, column=0, sticky="nw", padx=(10, 4), pady=(8, 8))
#         table_draw_box = tk.Frame(self._body, bg=palette["surface"])
#         table_draw_box.grid(row=self._table_draw_row, column=1, sticky="w", padx=(0, 10), pady=(8, 8))
#         row_frame = tk.Frame(table_draw_box, bg=palette["surface"])
#         row_frame.pack(side="top", anchor="w")
#         col_frame = tk.Frame(table_draw_box, bg=palette["surface"])
#         col_frame.pack(side="top", anchor="w", pady=(4, 0))
#         self._add_row_split_btn = tk.Button(row_frame, text="Add Row Split", command=self._add_row_split)
#         self._delete_row_split_btn = tk.Button(row_frame, text="Delete Row Split", command=self._delete_row_split)
#         self._add_col_split_btn = tk.Button(col_frame, text="Add Column Split", command=self._add_column_split)
#         self._delete_col_split_btn = tk.Button(col_frame, text="Delete Column Split",
#                                                  command=self._delete_column_split)
#         for btn in (self._add_row_split_btn, self._delete_row_split_btn,
#                     self._add_col_split_btn, self._delete_col_split_btn):
#             theme.style_button(btn, palette, kind="secondary")
#             btn.pack(side="left", padx=(0, 6))
#         self._table_draw_widgets = (
#             self._body.grid_slaves(row=self._table_draw_row, column=0)[0], table_draw_box,
#         )
#         self._set_table_draw_row_visible(False)

#     def _sync_pagenum_button(self):
#         """Set Page Number (empty - spec 29) vs Apply (correcting an
#         existing value - spec 2/6) - a purely cosmetic distinction, same
#         underlying action either way."""
#         is_empty = not self._pagenum_var.get().strip()
#         self._pagenum_apply_btn.configure(text="Set Page Number" if is_empty else "Apply")
#         if is_empty:
#             self._pagenum_empty_hint.pack(side="left", padx=(6, 0))
#         else:
#             self._pagenum_empty_hint.pack_forget()

#     def _set_pagenum_row_visible(self, visible: bool):
#         for w in self._pagenum_widgets:
#             if visible:
#                 w.grid()
#             else:
#                 w.grid_remove()

#     def _set_index_rows_visible(self, visible: bool):
#         for w in self._index_widgets:
#             if visible:
#                 w.grid()
#             else:
#                 w.grid_remove()

#     def _set_table_draw_row_visible(self, visible: bool):
#         for w in self._table_draw_widgets:
#             if visible:
#                 w.grid()
#             else:
#                 w.grid_remove()

#     def _add_row_split(self):
#         if not self._current_zone_id:
#             return
#         self.app.viewer.start_region_split_mode(self._current_zone_id, "row")

#     def _add_column_split(self):
#         if not self._current_zone_id:
#             return
#         self.app.viewer.start_region_split_mode(self._current_zone_id, "col")

#     def _delete_row_split(self):
#         if self.app.viewer.mode != "region_split_h":
#             self.app.set_status("Click Add Row Split, then click a guide line to select it before deleting.")
#             return
#         self.app.viewer.delete_selected_region_split()

#     def _delete_column_split(self):
#         if self.app.viewer.mode != "region_split_v":
#             self.app.set_status("Click Add Column Split, then click a guide line to select it before deleting.")
#             return
#         self.app.viewer.delete_selected_region_split()

#     def _apply_pagenum(self):
#         """Ctrl+Z/Ctrl+Y already work here for free - set_zone_text pushes
#         a normal undo snapshot like every other zone mutation, and
#         on_zones_changed() (a) refreshes the hierarchy/canvas/status, (b)
#         marks the project dirty via auto_save_project - no separate
#         plumbing needed for either requirement."""
#         if not self._current_zone_id:
#             return
#         self.app.zone_manager.set_zone_text(self._current_zone_id, self._pagenum_var.get())
#         self.app.on_zones_changed()
#         self.app.notify("Page number updated", kind="success")

#     def _apply_text(self):
#         """Same reuse as _apply_pagenum - one call into the existing
#         zone_manager.set_zone_text() gives Undo/Redo, dirty-marking, and
#         on_zones_changed() refresh for free, identical to every other zone
#         mutation in the app."""
#         if not self._current_zone_id:
#             return
#         new_text = self._text_box.get("1.0", "end-1c")
#         self.app.zone_manager.set_zone_text(self._current_zone_id, new_text)
#         self.app.on_zones_changed()
#         self.app.notify("Zone text updated", kind="success")

#     def _set_text_row_visible(self, visible: bool):
#         for w in self._text_widgets:
#             if visible:
#                 w.grid()
#             else:
#                 w.grid_remove()

#     def _toggle(self):
#         self._expanded = not self._expanded
#         if self._expanded:
#             self._body.pack(fill="x")
#             self._arrow_lbl.configure(text="▼  PROPERTIES")
#         else:
#             self._body.pack_forget()
#             self._arrow_lbl.configure(text="▶  PROPERTIES")

#     def show_zone(self, zone):
#         if zone is None:
#             self._current_zone_id = None
#             for lbl in self._value_labels.values():
#                 lbl.configure(text="—")
#             self._text_box.delete("1.0", "end")
#             self._text_box.configure(state="disabled")
#             self._set_pagenum_row_visible(False)
#             self._set_index_rows_visible(False)
#             self._set_table_draw_row_visible(False)
#             self._set_text_row_visible(True)
#             return
#         self._current_zone_id = zone.zone_id
#         self._value_labels["Zone"].configure(text=zone.tag)
#         self._value_labels["Page"].configure(text=str(zone.page))
#         self._value_labels["Reading Order"].configure(text=str(zone.serial) if zone.serial is not None else "—")
#         self._value_labels["Level"].configure(text=str(zone.level))
#         self._value_labels["Parent"].configure(text=zone.parent_id or "—")
#         x0, y0, x1, y1 = zone.bbox
#         self._value_labels["Coordinates"].configure(
#             text=f"X {x0:.0f}  Y {y0:.0f}  W {x1 - x0:.0f}  H {y1 - y0:.0f}")

#         pagenum_name = self.app.active_profile.get("cup_pagenum_name")
#         is_pagenum = bool(pagenum_name) and zone.attributes.get("cup_name") == pagenum_name
#         self._field_label_widgets["Page"].configure(text="PHYSICAL PDF PAGE" if is_pagenum else "PAGE")
#         self._set_pagenum_row_visible(is_pagenum)
#         if is_pagenum:
#             self._pagenum_var.set(zone.text or "")
#             self._sync_pagenum_button()

#         index_hierarchy_tags = {int(k): v for k, v in (self.app.active_profile.get("index_hierarchy_tags")
#                                                           or {}).items()}
#         current_level = next((lvl for lvl, t in index_hierarchy_tags.items() if t == zone.tag), None)
#         self._set_index_rows_visible(current_level is not None)
#         if current_level is not None:
#             self._index_level_value.configure(text=self.app.index_level_name(current_level))
#             hint_lines = []
#             for lvl, setting_key in ((1, "index_shortcut_primary"), (2, "index_shortcut_secondary"),
#                                       (3, "index_shortcut_tertiary")):
#                 if lvl not in index_hierarchy_tags:
#                     continue
#                 seq = self.app.settings.get(setting_key)
#                 if not seq:
#                     continue
#                 marker = "→ " if lvl == current_level else "   "
#                 hint_lines.append(f"{marker}{_format_shortcut(seq)}  {self.app.index_level_name(lvl)}")
#             self._index_shortcuts_value.configure(text="\n".join(hint_lines) or "—")

#         self._set_table_draw_row_visible(zone.tag == "table")

#         # The dedicated PageNum row above already covers editing for that
#         # one zone - showing the generic multi-line box too would just be a
#         # second, redundant editor for the exact same value.
#         self._set_text_row_visible(not is_pagenum)
#         if not is_pagenum:
#             self._text_box.configure(state="normal")
#             self._text_box.delete("1.0", "end")
#             self._text_box.insert("1.0", zone.text or "")


# class TagPanel(tk.Frame):
#     def __init__(self, parent, app, tag_buttons=None, tag_colors=None, tag_groups=None):
#         palette = theme.current.palette
#         super().__init__(parent, bg=palette["panel_bg"])
#         self.app = app
#         self.palette = palette
#         self.buttons = {}
#         self._tag_lookup = {}
#         self._collapsed_groups = set()
#         # Data from the most recent rebuild() - kept around so the search
#         # box (spec 98.27-98.32) can re-filter/re-render without needing a
#         # fresh rebuild() call from the profile-switch code path, and so
#         # switching profiles clears any leftover search term automatically.
#         self._all_tag_buttons = []
#         self._all_tag_colors = {}
#         self._all_tag_groups = []
#         # Category tab bar (spec: "ZONETOOL - MASTER PRODUCTION FIX..." -
#         # "Make tags panel exact like above image", a reference screenshot
#         # of FM/BODY/BM/FLOATS/LEVEL tabs with a per-tag Ctrl+Shift+<key>
#         # shortcut shown on each row) - self._active_group_label is which
#         # tab is currently selected; only meaningful when self._all_tag_
#         # groups is non-empty (CUPEPUB is the only profile that declares
#         # tag_groups at all - see core/cup_config.py), so this is purely
#         # additive for XML/EPUB, which render exactly as before.
#         self._active_group_label = None
#         self._tab_buttons = {}
#         # Independent tag-button highlight state (separate from the tab
#         # highlight above, and separate from each other) - _manual_tag_label
#         # is the label of whichever tag button the user last explicitly
#         # clicked (mirrors self.app.active_tag, the tag a NEWLY DRAWN zone
#         # will get); _active_tag_label is the label currently PAINTED
#         # highlighted, normally equal to _manual_tag_label but temporarily
#         # overridden by highlight_tag_for_zone() while an existing zone
#         # with a different tag is selected (display-only - never touches
#         # self.app.active_tag, so inspecting an existing zone can never
#         # silently change what a newly-drawn zone would be tagged).
#         self._manual_tag_label = None
#         self._active_tag_label = None

#         header = tk.Frame(self, bg=palette["panel_header_bg"])
#         header.pack(side="top", fill="x")
#         tk.Label(header, text="TAG TOOLBOX", bg=palette["panel_header_bg"], fg=palette["text"],
#                   font=theme.FONT_PANEL_TITLE).pack(side="top", anchor="w", padx=10, pady=(8, 6))

#         search_row = tk.Frame(header, bg=palette["panel_header_bg"])
#         search_row.pack(side="top", fill="x", padx=10, pady=(0, 8))
#         self._search_var = tk.StringVar()
#         self._search_var.trace_add("write", lambda *_: self._render(self._search_var.get()))
#         search_box = tk.Frame(search_row, bg=palette["surface"], highlightthickness=1,
#                                 highlightbackground=palette["border"])
#         search_box.pack(fill="x")
#         tk.Label(search_box, text="\U0001F50D", bg=palette["surface"], fg=palette["text_muted"],
#                   font=theme.FONT_SMALL).pack(side="left", padx=(6, 2))
#         self._search_entry = tk.Entry(search_box, textvariable=self._search_var, bg=palette["surface"],
#                                        fg=palette["text"], relief="flat", font=theme.FONT_BODY,
#                                        insertbackground=palette["text"])
#         self._search_entry.pack(side="left", fill="x", expand=True, ipady=3)
#         clear_btn = tk.Label(search_box, text="✕", bg=palette["surface"], fg=palette["text_faint"],
#                               font=theme.FONT_SMALL, cursor="hand2")
#         clear_btn.pack(side="left", padx=(2, 6))
#         clear_btn.bind("<Button-1>", lambda e: self._search_var.set(""))

#         # Category tab bar - built/rebuilt by _rebuild_tabs() (called from
#         # rebuild() whenever the active profile's tag_groups change), never
#         # torn down and rebuilt on every keystroke the way the scrollable
#         # button list below is - a persistent widget, exactly like the
#         # search box above it, so switching tabs never flickers unrelated UI.
#         self._tab_bar = tk.Frame(header, bg=palette["panel_header_bg"])
#         self._tab_bar.pack(side="top", fill="x", padx=10, pady=(0, 8))

#         # Packed before the canvas (side="bottom") so it reserves its space
#         # first - a later bottom-pack after an expand=True fill would get none.
#         deselect_btn = tk.Button(self, text="Deselect Tag", command=self._deselect)
#         theme.style_button(deselect_btn, palette, kind="secondary")
#         deselect_btn.pack(side="bottom", fill="x", padx=8, pady=8)

#         # Scrollable button list: a Canvas + inner Frame, so all tags stay
#         # reachable via scrolling instead of shrinking the buttons to fit.
#         self._canvas = tk.Canvas(self, bg=palette["panel_bg"], highlightthickness=0,
#                                   yscrollincrement=SCROLL_UNIT_PX)
#         self._vbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
#         self._canvas.configure(yscrollcommand=self._vbar.set)
#         self._touchpad_accum_y = 0.0  # fractional leftover between TouchpadScroll samples - see _on_touchpad_scroll
#         self._canvas.pack(side="left", fill="both", expand=True)
#         self._vbar.pack(side="right", fill="y")

#         self._container = tk.Frame(self._canvas, bg=palette["panel_bg"])
#         self._window_id = self._canvas.create_window((0, 0), window=self._container, anchor="nw")

#         def _on_container_configure(event):
#             self._canvas.configure(scrollregion=self._canvas.bbox("all"))

#         def _on_canvas_configure(event):
#             self._canvas.itemconfigure(self._window_id, width=event.width)

#         self._container.bind("<Configure>", _on_container_configure)
#         self._canvas.bind("<Configure>", _on_canvas_configure)
#         # Bound on the canvas, its own scrollbar, AND the whole panel Frame
#         # (covers the header/search-box area and any background padding
#         # strip) - not just the canvas - a real touchpad gesture landing
#         # on the thin scrollbar strip previously reached no binding at all
#         # and silently did nothing (confirmed via Settings > Debug
#         # logging's [SCROLL] entries showing zero events for a gesture
#         # that visually targeted this panel).
#         for widget in (self._canvas, self._vbar, self):
#             widget.bind("<MouseWheel>", self._on_wheel)
#             widget.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"))
#             widget.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"))
#             # Tk 9's own separate high-resolution touchpad event (TIP 684)
#             # - see theme.bind_touchpad_scroll's own docstring. A no-op on
#             # any older Tk build.
#             theme.bind_touchpad_scroll(widget, self._on_touchpad_scroll)

#         self.rebuild(tag_buttons if tag_buttons is not None else TAG_BUTTONS,
#                      tag_colors if tag_colors is not None else DEFAULT_TAG_COLORS,
#                      tag_groups)

#     def focus_search(self):
#         """Ctrl+F (gui/main_window.py, only bound when no active CUP
#         shortcut already claims it)."""
#         self._search_entry.focus_set()
#         self._search_entry.selection_range(0, "end")

#     def _on_wheel(self, event):
#         # Reuses gui.pdf_viewer.normalize_wheel_units - the SAME "too fast"
#         # root cause (no yscrollincrement set, so Tk's own default unit
#         # size grows with the widget's height, plus a flat 1-unit-per-event
#         # step that ignored a real notch's own magnitude) that PDFViewerPanel
#         # already had, fixed the same way here rather than a second,
#         # independently-tuned implementation.
#         units = normalize_wheel_units(event.delta)
#         if units:
#             self._canvas.yview_scroll(units, "units")
#         debug_log.log("SCROLL", f"widget=TagPanel.{event.widget} event=<MouseWheel> delta={event.delta} "
#                                  f"widget_xy=({event.x},{event.y}) action=yview_scroll(canvas) units={units} handled=True")

#     def _on_touchpad_scroll(self, dx: int, dy: int):
#         """Tk 9's <TouchpadScroll> (see theme.bind_touchpad_scroll and gui/
#         pdf_viewer.py's identical handler for the full explanation of why
#         this exists as a SEPARATE event from <MouseWheel>). Fractional
#         accumulation (gui.pdf_viewer.accumulate_scroll_units) - a lone
#         small sample does not force a full canvas unit of movement, unlike
#         the previous flat "1 unit per event" behavior."""
#         if dy:
#             self._touchpad_accum_y, units = accumulate_scroll_units(self._touchpad_accum_y, dy)
#             if units:
#                 self._canvas.yview_scroll(units, "units")
#         debug_log.log("SCROLL", f"widget=TagPanel event=<TouchpadScroll> dx={dx} dy={dy} "
#                                  f"action=yview_scroll(canvas) handled=True")

#     def _bind_wheel_recursive(self, widget):
#         """Wheel scrolling must work with the pointer ANYWHERE over the tag
#         list (spec: "the pointer may be over a group header, a tag button,
#         empty space between rows, or a scrollbar"), not just over the bare
#         Canvas background - _render() rebuilds this entire widget subtree
#         (group headers, row frames, color chips, buttons) from scratch on
#         every keystroke/rebuild, so binding is applied recursively, once,
#         after each render, rather than hand-wiring every individual widget
#         creation call site (fragile against a future new widget type being
#         added without remembering to also bind it there)."""
#         widget.bind("<MouseWheel>", self._on_wheel, add="+")
#         widget.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"), add="+")
#         widget.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"), add="+")
#         theme.bind_touchpad_scroll(widget, self._on_touchpad_scroll)
#         for child in widget.winfo_children():
#             self._bind_wheel_recursive(child)

#     def rebuild(self, tag_buttons, tag_colors=None, tag_groups=None):
#         """Swaps the button list for a different Profile's tag set (Profile
#         dropdown, toolbar) without rebuilding the panel itself - existing
#         zones already tagged from the previous profile are never touched by
#         this (see gui/main_window.py App.set_profile): only which buttons
#         are offered to draw NEW zones with changes.

#         tag_groups (optional, only CUPEPUB sets it - see core/cup_config.py)
#         is a list of {"label": "Frontmatter", "first": "<label of the first
#         button in this group>"} - a collapsible section header is inserted
#         right before that button. None/omitted (XML, EPUB) renders the
#         exact same flat button list as before this parameter existed.

#         Also clears any active search term (spec 98.30 - search always
#         operates on THIS profile's own loaded tags, never a stale filter
#         left over from a different profile)."""
#         self._all_tag_buttons = list(tag_buttons)
#         self._all_tag_colors = tag_colors or DEFAULT_TAG_COLORS
#         self._all_tag_groups = list(tag_groups or [])
#         self._collapsed_groups = set()
#         self._active_group_label = self._all_tag_groups[0]["label"] if self._all_tag_groups else None
#         self._rebuild_tabs()
#         # Stable across search filtering (unlike self.buttons, which only
#         # ever holds currently-VISIBLE button widgets) - a keyboard
#         # shortcut (gui/main_window.py._apply_cup_shortcuts) must still be
#         # able to select a tag that the search box is currently hiding.
#         self._tag_lookup = {label: (tag, attrs) for label, tag, attrs in self._all_tag_buttons}
#         if self._search_var.get():
#             self._search_var.set("")  # triggers _render via the trace below
#         else:
#             self._render("")

#     def _toggle_group(self, group_label):
#         if group_label in self._collapsed_groups:
#             self._collapsed_groups.discard(group_label)
#         else:
#             self._collapsed_groups.add(group_label)
#         self._render(self._search_var.get())

#     # Short, high-contrast display labels for the Frontmatter/Backmatter
#     # tabs (spec: "group tags into FM/BM with clear, high-contrast,
#     # professional-colored headings") - internal group_label strings
#     # ("Frontmatter"/"Backmatter", set by core/cup_config.py) are left
#     # completely unchanged, since _render's own tab-filter matching
#     # (label_to_group/group_starts, above) keys off that same string;
#     # only what's actually painted on the button changes. Body/Float are
#     # deliberately absent here - not mentioned by the spec, so they keep
#     # their existing group_label.upper() display exactly as before.
#     _GROUP_DISPLAY_LABEL = {"Frontmatter": "FM", "Backmatter": "BM"}
#     # Distinct accent color per group, always visible (active or not) via
#     # a colored border - reuses two of theme.py's own existing, already-
#     # defined TAG_CATEGORY_PALETTE entries (never a new color added to
#     # gui/theme.py, per explicit instruction). This palette is otherwise
#     # unused anywhere in the codebase today.
#     _GROUP_ACCENT_INDEX = {"Frontmatter": 0, "Backmatter": 4}

#     def _rebuild_tabs(self):
#         """Category tab bar (FM/BODY/BM/FLOATS for CUPEPUB) - rebuilt only
#         when the tag set itself changes (profile switch), not on every
#         keystroke. Empty/hidden entirely for a profile with no tag_groups
#         (XML/EPUB) - identical to this panel's behavior before tabs existed."""
#         for child in list(self._tab_bar.winfo_children()):
#             child.destroy()
#         self._tab_buttons = {}
#         if not self._all_tag_groups:
#             return
#         for group in self._all_tag_groups:
#             group_label = group["label"]
#             display_text = self._GROUP_DISPLAY_LABEL.get(group_label, group_label.upper())
#             btn = tk.Button(self._tab_bar, text=display_text,
#                              command=lambda gl=group_label: self._select_tab(gl))
#             btn.pack(side="left", padx=(0, 4))
#             self._tab_buttons[group_label] = btn
#         self._update_tab_styles()

#     def _select_tab(self, group_label):
#         self._active_group_label = group_label
#         self._update_tab_styles()
#         self._render(self._search_var.get())

#     def _style_one_tab(self, group_label, btn, active):
#         if active:
#             btn.configure(bg=self.palette["accent"], fg=self.palette["accent_fg"])
#         else:
#             theme.style_button(btn, self.palette, kind="secondary")
#         accent_idx = self._GROUP_ACCENT_INDEX.get(group_label)
#         if accent_idx is None:
#             return
#         accent_color = theme.TAG_CATEGORY_PALETTE[accent_idx]
#         # FM/BM keep their own identity color as a bold border/heading-text
#         # accent in BOTH states (a thicker ring while active, a colored
#         # label while inactive) so they read as distinct, professional
#         # section headings rather than plain neutral tab buttons - never
#         # overriding the accent-filled active bg/fg pairing above, which
#         # already carries the strongest contrast this theme defines.
#         btn.configure(font=theme.FONT_BODY_BOLD, highlightthickness=2,
#                       highlightbackground=accent_color, highlightcolor=accent_color)
#         if not active:
#             btn.configure(fg=accent_color)

#     def _update_tab_styles(self):
#         for group_label, btn in self._tab_buttons.items():
#             self._style_one_tab(group_label, btn, group_label == self._active_group_label)

#     def _render(self, filter_text: str = ""):
#         """Renders the (possibly filtered) button list. Case-insensitive,
#         partial-match against each button's own label (spec 98.28/98.29) -
#         searches whatever tag_buttons this profile actually loaded, never a
#         separate hardcoded list (spec 98.30). A group header is shown only
#         if at least one of its member buttons currently matches (spec
#         98.32), and is skipped entirely while manually collapsed (search
#         active always force-expands, so typing never hides a real match
#         behind a collapsed section). An empty filter with zero tags at all
#         (shouldn't happen) or a filter matching nothing shows a clear
#         "No matching tags" state instead of a blank panel (spec 66.6)."""
#         query = filter_text.strip().lower()
#         for child in list(self._container.winfo_children()):
#             child.destroy()
#         self.buttons = {}
#         group_starts = {g["first"]: g["label"] for g in self._all_tag_groups}
#         # Which group each button belongs to (based on group_starts'
#         # boundaries) - used to filter by the active tab below. A profile
#         # with no tag_groups (XML/EPUB) never populates this, so every
#         # label maps to None and the tab filter is simply never applied.
#         label_to_group = {}
#         _current_group = None
#         for label, _tag, _attrs in self._all_tag_buttons:
#             if label in group_starts:
#                 _current_group = group_starts[label]
#             label_to_group[label] = _current_group
#         # Tabs (spec: "Make tags panel exact like above image") replace the
#         # OLDER collapsible-inline-header UI for a profile that declares
#         # tag_groups (CUPEPUB is the only one - see core/cup_config.py):
#         # searching still searches every category regardless of the active
#         # tab (same precedent as the old collapsed-group-auto-expands-on-
#         # search behavior this replaces), so a query always overrides the
#         # tab filter rather than being scoped by it.
#         tab_filter_active = bool(self._all_tag_groups) and not query
#         matches = {
#             label: (not query or query in label.lower())
#             and (not tab_filter_active or label_to_group.get(label) == self._active_group_label)
#             for label, _tag, _attrs in self._all_tag_buttons
#         }
#         pending_group_label = None
#         skip_until_next_group = False
#         any_visible = False
#         # Auto Zone Index button (spec: "ADD 'AUTO ZONE' BUTTON TO INDEX TAG
#         # PANEL") - shown right after the run of VISIBLE index-hierarchy-
#         # level buttons (IndexPE/IndexSE/IndexTE for CUPEPUB, "Index Primary/
#         # Secondary/Territory (nested)" for EPUB - whatever the active
#         # profile's own index_hierarchy_tags actually names them), whether
#         # that run is the normal Backmatter group listing or the result of
#         # the user searching "index" (spec: "When the user searches/selects
#         # Index-related tags... show an additional button" - both cases are
#         # just "these tags are currently visible", handled identically
#         # here). Never auto-runs anything by itself - only wiring a button
#         # the user must still click (spec: "Do not automatically execute...
#         # The user must click the button").
#         index_hierarchy_tags = self.app.active_profile.get("index_hierarchy_tags") or {}
#         index_tags = set(index_hierarchy_tags.values())
#         # spec 11 ("Show the assigned shortcut next to the corresponding
#         # Index tags in the tag toolbox when possible") - {tag: "Ctrl+Alt+1"},
#         # built once per render from whichever of the three configured
#         # shortcuts (self.app.settings) actually resolves to a real,
#         # currently-active profile tag; a level with no configured/valid
#         # shortcut simply shows no suffix, never a placeholder.
#         index_tag_shortcuts = {}
#         for level, setting_key in ((1, "index_shortcut_primary"), (2, "index_shortcut_secondary"),
#                                     (3, "index_shortcut_tertiary")):
#             tag = index_hierarchy_tags.get(level) or index_hierarchy_tags.get(str(level))
#             seq = self.app.settings.get(setting_key)
#             if tag and seq:
#                 index_tag_shortcuts[tag] = _format_shortcut(seq)
#         # Per-tag "apply this tag" keyboard shortcut (spec: "Make tags panel
#         # exact like above image" - every row in the reference screenshot
#         # shows its own Ctrl+Shift+<key>). Reuses core.cup_config.
#         # build_cup_profile's own already-computed cup_shortcuts list (the
#         # SAME data gui/main_window.py._apply_cup_shortcuts already binds
#         # for real - this only adds a DISPLAY of it, never a second shortcut
#         # system) - {} for XML/EPUB, which don't declare cup_shortcuts at
#         # all. Deliberately does not override index_tag_shortcuts above (a
#         # DIFFERENT action - retagging an already-selected zone's level -
#         # kept exactly as before); only fills in a label for a tag that
#         # doesn't already have one.
#         cup_tag_shortcuts = {}
#         for entry in (self.app.active_profile.get("cup_shortcuts") or []):
#             tag = entry.get("tag")
#             seqs = entry.get("seqs") or []
#             if not tag or not seqs or tag in cup_tag_shortcuts:
#                 continue
#             # core.cup_config._shortcut_seqs binds BOTH letter casings from
#             # a Python set (unordered) for a letter key - display the
#             # uppercase form deterministically (matching CUPEPUB_Zoning.xml's
#             # own shortcutKey="A" convention) rather than whichever casing
#             # the set happened to iterate first.
#             display_seq = next((s for s in seqs if s[-2:-1].isupper() or not s[-2:-1].isalpha()), seqs[0])
#             cup_tag_shortcuts[tag] = _format_shortcut(display_seq)
#         pending_index_button = False
#         # Table Generator was removed from the visible TAG TOOLBOX - it is
#         # not an XML/CUPEPUB zoning tag, and its own read-only preview
#         # dialog (App.table_generator / TableGeneratorPreviewDialog in
#         # gui/dialogs.py) requires an EXISTING "table"-tagged zone rather
#         # than creating one, which made it confusing alongside the actual
#         # zoning tag buttons. That underlying method/dialog is left
#         # completely intact (still reachable programmatically) - only this
#         # toolbox injection is gone. Table zones are now created via the
#         # new "Table Draw" tag button (core/constants.py's own TAG_BUTTONS,
#         # reusing the existing TAG_TABLE tag - no new zone type).

#         def _insert_auto_zone_index_button():
#             ttk.Separator(self._container, orient="horizontal").pack(fill="x", padx=8, pady=(6, 3))
#             btn_row = tk.Frame(self._container, bg=self.palette["panel_bg"])
#             btn_row.pack(fill="x", padx=8, pady=(0, 3))
#             azi_btn = tk.Button(btn_row, text="Auto Zone Index", command=self.app.auto_zone_index)
#             theme.style_button(azi_btn, self.palette, kind="primary")
#             azi_btn.pack(fill="x")
#             ttk.Separator(self._container, orient="horizontal").pack(fill="x", padx=8, pady=(3, 6))

#         for label, tag, attrs in self._all_tag_buttons:
#             if label in group_starts:
#                 if pending_index_button:
#                     _insert_auto_zone_index_button()
#                     pending_index_button = False
#                 group_label = group_starts[label]
#                 if self._all_tag_groups:
#                     # The tab bar (_rebuild_tabs) IS the category selector
#                     # now - no inline collapsible header needed, and
#                     # label_to_group-based filtering in `matches` above
#                     # already hides every other category's buttons, so
#                     # there's nothing left to collapse here.
#                     skip_until_next_group = False
#                 else:
#                     collapsed = (not query) and group_label in self._collapsed_groups
#                     arrow = "▶" if collapsed else "▼"
#                     header_row = tk.Frame(self._container, bg=self.palette["panel_header_bg"], cursor="hand2")
#                     header_row.pack(fill="x", padx=0, pady=(8, 1))
#                     header_lbl = tk.Label(header_row, text=f"{arrow}  {group_label.upper()}",
#                                            bg=self.palette["panel_header_bg"], fg=self.palette["text_muted"],
#                                            font=theme.FONT_SMALL_BOLD, anchor="w")
#                     header_lbl.pack(fill="x", padx=8, pady=4)
#                     for w in (header_row, header_lbl):
#                         w.bind("<Button-1>", lambda e, gl=group_label: self._toggle_group(gl))
#                     skip_until_next_group = collapsed
#                 pending_group_label = None
#             if not matches[label] or skip_until_next_group:
#                 continue
#             if pending_index_button and tag not in index_tags:
#                 _insert_auto_zone_index_button()
#                 pending_index_button = False
#             any_visible = True
#             color = self._all_tag_colors.get(tag, self.palette["text"])
#             row = tk.Frame(self._container, bg=self.palette["panel_bg"])
#             row.pack(fill="x", padx=8, pady=1)
#             # A slim color chip to the left of the button communicates
#             # tag-category color (spec 66.7/66.27) without turning the
#             # whole button into a "colorful website chip" - the button
#             # itself stays neutral gray/white.
#             chip = tk.Frame(row, bg=color, width=3)
#             chip.pack(side="left", fill="y")
#             if tag in index_tag_shortcuts:
#                 shortcut_suffix = f"   [{index_tag_shortcuts[tag]}]"
#             elif tag in cup_tag_shortcuts:
#                 shortcut_suffix = f"   [{cup_tag_shortcuts[tag]}]"
#             else:
#                 shortcut_suffix = ""
#             b = tk.Button(row, text=label + shortcut_suffix, anchor="w",
#                           command=lambda t=tag, a=attrs, l=label: self._select(t, a, l))
#             b.configure(bg=self.palette["surface"], fg=self.palette["text"], activebackground=self.palette["hover_bg"],
#                         relief="flat", bd=1, highlightthickness=1, highlightbackground=self.palette["border"],
#                         font=theme.FONT_BODY, cursor="hand2", padx=8, pady=4)
#             b.pack(side="left", fill="x", expand=True)
#             self.buttons[label] = b
#             pending_index_button = tag in index_tags
#         if pending_index_button:
#             _insert_auto_zone_index_button()
#         if not any_visible:
#             tk.Label(self._container, text="No matching tags", bg=self.palette["panel_bg"],
#                       fg=self.palette["text_faint"], font=theme.FONT_BODY).pack(pady=24)
#         self._bind_wheel_recursive(self._container)
#         # Scroll-reset (real, confirmed bug): this whole button list is
#         # destroyed and rebuilt on every rebuild()/tab-switch/search
#         # keystroke, but the canvas never reset its scroll position - if
#         # the user was scrolled down in a long category and then switches
#         # to a shorter one (or a search narrows the list), the newly
#         # rendered content can sit entirely above/below the surviving
#         # scroll offset, making the panel LOOK completely empty even
#         # though tags did render. Always snapping back to the top means
#         # whatever just rendered (including "No matching tags") is always
#         # immediately visible.
#         self._canvas.yview_moveto(0)
#         # Real, confirmed bug: this used to unconditionally clear both the
#         # visual highlight AND self.app.active_tag on every render - which
#         # fires on a plain search keystroke or tab switch, not just an
#         # actual deselect. Now: only truly stale state (the manually-
#         # selected tag no longer exists at all, e.g. after a genuine
#         # profile change) clears app.active_tag; otherwise the current
#         # effective highlight (manual click OR a zone-derived override -
#         # see highlight_tag_for_zone) is simply re-painted onto the fresh
#         # button widgets, leaving app.active_tag completely untouched.
#         if self._manual_tag_label and self._manual_tag_label not in self._tag_lookup:
#             self._manual_tag_label = None
#             self._active_tag_label = None
#             self.app.set_active_tag(None, None)
#         else:
#             self._paint_active_tag(self._active_tag_label)

#     def _paint_active_tag(self, label):
#         """Shared highlight-painting primitive - clears every button back
#         to its normal style, then (if `label` is currently rendered)
#         applies the highlighted style to that one button. Purely visual:
#         never touches self.app.active_tag itself - callers decide whether
#         that also needs updating (see _select/_deselect vs
#         highlight_tag_for_zone)."""
#         for b in self.buttons.values():
#             b.configure(bg=self.palette["surface"], highlightbackground=self.palette["border"])
#         self._active_tag_label = label
#         # The button may not currently be rendered (a search/tab filter is
#         # hiding it, or a zone-derived tag from a different category) -
#         # the label is still tracked either way, just without a visible
#         # painted button to show for it right now.
#         if label in self.buttons:
#             self.buttons[label].configure(bg=self.palette["accent_tint"], highlightbackground=self.palette["accent"])

#     def _select(self, tag, attrs, label):
#         self._manual_tag_label = label
#         self._paint_active_tag(label)
#         self.app.set_active_tag(tag, attrs)

#     def select_label(self, label):
#         """Activates a tag by its button label (the CUP tag name, e.g.
#         "H1"/"Para_NoIndent") - used by CUPEPUB's keyboard-shortcut handler
#         (gui/main_window.py) to mirror exactly what clicking that button
#         does, including the visual pressed/selected state. Looked up from
#         the STABLE, unfiltered _tag_lookup (see rebuild()), so a shortcut
#         still works even while the search box is hiding that button."""
#         entry = self._tag_lookup.get(label)
#         if entry:
#             self._select(entry[0], entry[1], label)

#     def _deselect(self):
#         self._manual_tag_label = None
#         self._paint_active_tag(None)
#         self.app.set_active_tag(None, None)

#     def _label_for_zone(self, zone):
#         """Resolves the Tag Panel button label whose (tag, attrs) matches
#         this zone, for the display-only zone-selection highlight (see
#         highlight_tag_for_zone). Several labels can share the same
#         underlying `tag` string with different `attrs` (e.g. CUPEPUB's
#         Para/Para_NoIndent/Para_Center/ParaHang all use tag="p",
#         distinguished by attrs["cup_name"]/attrs["type"]) - a button only
#         matches if EVERY one of its own attrs is present with the same
#         value on the zone's own attributes, and the most specific
#         (largest attrs) match wins, mirroring the cup_name-based
#         disambiguation core/cup_config.py already relies on elsewhere."""
#         best_label, best_attrs = None, None
#         for label, tag, attrs in self._all_tag_buttons:
#             if tag != zone.tag:
#                 continue
#             if not all(zone.attributes.get(k) == v for k, v in attrs.items()):
#                 continue
#             if best_attrs is None or len(attrs) > len(best_attrs):
#                 best_label, best_attrs = label, attrs
#         return best_label

#     def highlight_tag_for_zone(self, zone):
#         """Display-only (confirmed with the user): selecting an existing
#         zone highlights ITS tag in the panel purely for information -
#         never calls self.app.set_active_tag, so simply inspecting an
#         existing zone can never silently change what tag a NEWLY DRAWN
#         zone would get (that stays whatever the user last explicitly
#         clicked - self._manual_tag_label). `zone=None` (nothing selected,
#         or the selected zone was just deleted) falls back to re-showing
#         that last manually-clicked tag instead."""
#         label = self._label_for_zone(zone) if zone is not None else self._manual_tag_label
#         self._paint_active_tag(label)


# class ZoneTreePanel(tk.Frame):
#     def __init__(self, parent, app):
#         palette = theme.current.palette
#         super().__init__(parent, width=340, bg=palette["panel_bg"])
#         self.app = app
#         self.palette = palette

#         header = tk.Frame(self, bg=palette["panel_header_bg"])
#         header.pack(side="top", fill="x")
#         tk.Label(header, text="ZONE HIERARCHY", bg=palette["panel_header_bg"], fg=palette["text"],
#                   font=theme.FONT_PANEL_TITLE).pack(side="top", anchor="w", padx=10, pady=(8, 6))

#         search_row = tk.Frame(header, bg=palette["panel_header_bg"])
#         search_row.pack(side="top", fill="x", padx=10, pady=(0, 8))
#         self._search_var = tk.StringVar()
#         search_box = tk.Frame(search_row, bg=palette["surface"], highlightthickness=1,
#                                 highlightbackground=palette["border"])
#         search_box.pack(fill="x")
#         tk.Label(search_box, text="\U0001F50D", bg=palette["surface"], fg=palette["text_muted"],
#                   font=theme.FONT_SMALL).pack(side="left", padx=(6, 2))
#         search_entry = tk.Entry(search_box, textvariable=self._search_var, bg=palette["surface"],
#                                  fg=palette["text"], relief="flat", font=theme.FONT_BODY,
#                                  insertbackground=palette["text"])
#         search_entry.pack(side="left", fill="x", expand=True, ipady=3)
#         search_entry.bind("<Return>", lambda e: self._jump_to_next_match())
#         clear_btn = tk.Label(search_box, text="✕", bg=palette["surface"], fg=palette["text_faint"],
#                               font=theme.FONT_SMALL, cursor="hand2")
#         clear_btn.pack(side="left", padx=(2, 6))
#         clear_btn.bind("<Button-1>", lambda e: self._search_var.set(""))
#         # Searching a Treeview means finding matches and JUMPING/scrolling to
#         # them (Enter cycles to the next match), not hiding non-matching
#         # rows - hiding rows would mean rebuilding the tree's own parent/
#         # child structure outside of core.hierarchy.build_document_tree,
#         # which owns that logic and must stay the single source of truth
#         # for it (spec 66.32 - the UI is presentation only).
#         self._match_iids = []
#         self._match_index = -1

#         tree_area = tk.Frame(self, bg=palette["panel_bg"])
#         tree_area.pack(side="top", fill="both", expand=True)

#         columns = ("tag", "status", "page", "level", "parent", "serial", "bbox", "text")
#         self.tree = ttk.Treeview(tree_area, columns=columns, show="tree headings", selectmode="browse")
#         self.tree.heading("#0", text="Zone")
#         self.tree.heading("tag", text="Tag")
#         self.tree.heading("status", text="Status")
#         self.tree.heading("page", text="Page")
#         self.tree.heading("level", text="Level")
#         self.tree.heading("parent", text="Parent")
#         self.tree.heading("serial", text="Reading Order")
#         self.tree.heading("bbox", text="BBox")
#         self.tree.heading("text", text="Text Preview")
#         self.tree.column("#0", width=110)
#         self.tree.column("tag", width=70)
#         self.tree.column("status", width=90, anchor="center")
#         self.tree.column("page", width=40, anchor="center")
#         self.tree.column("level", width=40, anchor="center")
#         self.tree.column("parent", width=70, anchor="center")
#         self.tree.column("serial", width=90, anchor="center")
#         self.tree.column("bbox", width=140)
#         self.tree.column("text", width=220)
#         # A SPLIT-PARENT row is never used by Generate XML (see
#         # xml_generator._gen_zone_multi's flatten logic, mirrored by
#         # _zone_status below) - only its own split children are - so it's
#         # shown dimmed/gray to make that immediately visible at a glance,
#         # matching the spec's "clearly distinguish ACTIVE/LEAF vs
#         # SPLIT-PARENT" requirement.
#         self.tree.tag_configure("split_parent", foreground=palette["text_faint"])
#         self.tree.tag_configure("search_match", background=palette["warning_tint"])

#         vbar = ttk.Scrollbar(tree_area, orient="vertical", command=self.tree.yview)
#         self.tree.configure(yscrollcommand=vbar.set)
#         self.tree.pack(side="left", fill="both", expand=True)
#         vbar.pack(side="right", fill="y")
#         # ttk.Treeview already scrolls natively with the mouse wheel
#         # (confirmed directly) - this is only the same defensive extra
#         # applied to the OTHER panels' scrollbars/background strips above,
#         # for the thin vbar/tree_area area the Treeview's own native
#         # binding doesn't cover. <TouchpadScroll> (Tk 9's own separate
#         # high-resolution touchpad event, TIP 684 - see theme.
#         # bind_touchpad_scroll) is bound on self.tree too, not just the
#         # scrollbar/background - ttk's own built-in Treeview bindings
#         # predate this Tk 9 event entirely, so unlike <MouseWheel> there
#         # is no native handling of it to fall back on here at all.
#         for widget in (vbar, tree_area):
#             widget.bind("<MouseWheel>", lambda e: self.tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))
#         for widget in (self.tree, vbar, tree_area):
#             theme.bind_touchpad_scroll(
#                 widget, lambda dx, dy: self.tree.yview_scroll(-1 if dy > 0 else 1, "units") if dy else None)

#         self.tree.bind("<<TreeviewSelect>>", self._on_select)
#         self.tree.bind("<Double-Button-1>", self._on_double_click)
#         self._suppress_select_event = False
#         self._zm = None

#         self.inspector = PropertiesInspector(self, app, palette)
#         self.inspector.pack(side="bottom", fill="x")

#         self._search_var.trace_add("write", lambda *_: self._recompute_matches())

#     # ---------------- search ----------------
#     def _recompute_matches(self):
#         query = self._search_var.get().strip().lower()
#         # Clears any previous match highlight by re-walking all rows - cheap
#         # (a project's zone count is at most a few thousand rows, not a
#         # performance concern - spec 66.29).
#         self._apply_row_tags_recursive("")
#         self._match_iids = []
#         if not query:
#             self._match_index = -1
#             return
#         self._collect_matches_recursive("", query)
#         self._match_index = -1
#         if self._match_iids:
#             self._jump_to_next_match()

#     def _apply_row_tags_recursive(self, iid):
#         for child in self.tree.get_children(iid):
#             tags = tuple(t for t in self.tree.item(child, "tags") if t != "search_match")
#             self.tree.item(child, tags=tags)
#             self._apply_row_tags_recursive(child)

#     def _collect_matches_recursive(self, iid, query):
#         for child in self.tree.get_children(iid):
#             values = self.tree.item(child, "values")
#             text = self.tree.item(child, "text") or ""
#             haystack = (text + " " + " ".join(str(v) for v in values)).lower()
#             if query in haystack:
#                 self._match_iids.append(child)
#                 tags = tuple(self.tree.item(child, "tags")) + ("search_match",)
#                 self.tree.item(child, tags=tags)
#             self._collect_matches_recursive(child, query)

#     def _jump_to_next_match(self):
#         if not self._match_iids:
#             return
#         self._match_index = (self._match_index + 1) % len(self._match_iids)
#         iid = self._match_iids[self._match_index]
#         # Expand every ancestor so a match nested deep in the hierarchy is
#         # actually visible once scrolled to, not just selected off-screen.
#         parent = self.tree.parent(iid)
#         while parent:
#             self.tree.item(parent, open=True)
#             parent = self.tree.parent(parent)
#         self.tree.see(iid)
#         self.tree.selection_set(iid)

#     def _on_double_click(self, _event):
#         """Double-click to navigate (spec 66.11) - jumps the PDF viewer to
#         the selected zone's own page, reusing the exact same selection path
#         a single click already takes (select_zone_from_tree), just also
#         changing pages first if the zone lives on a different one."""
#         sel = self.tree.selection()
#         if not sel or sel[0] not in self._zm.zones:
#             return
#         zone = self._zm.zones[sel[0]]
#         if zone.page != self.app.current_page:
#             self.app.goto_page(str(zone.page))
#         self.app.select_zone_from_tree(sel[0])

#     def refresh(self):
#         zm = self.app.zone_manager
#         self._zm = zm
#         self.tree.delete(*self.tree.get_children())
#         if not zm.zones:
#             self.inspector.show_zone(None)
#             return
#         page_order = reading_order.compute_page_order(zm)
#         box_container_tags = self.app.active_profile.get("box_container_tags")
#         extra_markers = [tuple(box_container_tags)] if box_container_tags else None
#         doc_tree, boundary_issues = hierarchy.build_document_tree(
#             zm, page_order, extra_markers,
#             page_marker_tags=self.app.active_profile.get("page_marker_tags"),
#             footnote_flow_tags=self.app.active_profile.get("footnote_flow_tags"),
#             non_flow_tags=self.app.active_profile.get("non_flow_tags"))
#         hierarchy.finalize_order(zm, doc_tree)
#         if boundary_issues:
#             self.app.set_status("; ".join(boundary_issues))
#         for node in doc_tree["children"]:
#             self._insert_node("", node)
#         if self._search_var.get():
#             self._recompute_matches()
#         self.app.on_zones_reindexed()

#     @staticmethod
#     def _bbox_str(bbox):
#         return "{:.0f}, {:.0f}, {:.0f}, {:.0f}".format(*bbox)

#     def _zone_status(self, zone):
#         """ACTIVE (used by Generate XML) or SPLIT-PARENT (superseded by
#         its own split children, kept only as split-history metadata) -
#         mirrors xml_generator._gen_zone_multi's own flatten condition
#         exactly, so the tree always agrees with what Generate XML will
#         actually emit; never a separately-maintained rule."""
#         zm = self._zm
#         if zone.children:
#             children = [zm.zones[c] for c in zone.children if c in zm.zones]
#             if children and all(c.is_split and c.source_zone_id == zone.zone_id and c.tag == zone.tag
#                                  for c in children):
#                 return "SPLIT-PARENT"
#         return "ACTIVE"

#     def _row_values(self, zone):
#         return (zone.tag, self._zone_status(zone), zone.page, zone.level, zone.parent_id or "-",
#                 zone.serial or "", self._bbox_str(zone.bbox), (zone.text or "")[:60])

#     def _row_tags(self, zone):
#         return ("split_parent",) if self._zone_status(zone) == "SPLIT-PARENT" else ()

#     def _insert_node(self, parent_iid, node):
#         zm = self._zm
#         if node["type"] == "sec":
#             zone = zm.zones[node["zone_id"]]
#             label = f"H{node['level']}: {(zone.text or '')[:30]}"
#             iid = self.tree.insert(parent_iid, "end", iid=zone.zone_id, text=label, values=self._row_values(zone),
#                                     tags=self._row_tags(zone))
#             for child in node["children"]:
#                 self._insert_node(iid, child)
#         elif node["type"] == "boxed-text":
#             start_zone = zm.zones[node["start_zone_id"]]
#             end_ok = "OK" if node.get("end_zone_id") else "UNCLOSED"
#             label = f"boxed-text [{end_ok}]"
#             iid = self.tree.insert(parent_iid, "end", iid=start_zone.zone_id, text=label,
#                                     values=self._row_values(start_zone), tags=self._row_tags(start_zone))
#             for child in node["children"]:
#                 self._insert_node(iid, child)
#             if node.get("end_zone_id"):
#                 end_zone = zm.zones[node["end_zone_id"]]
#                 self.tree.insert(iid, "end", iid=end_zone.zone_id, text="boxed-text-end",
#                                   values=self._row_values(end_zone), tags=self._row_tags(end_zone))
#         elif node["type"] == "merged_p":
#             zone_ids = node["zone_ids"]
#             first = zm.zones[zone_ids[0]]
#             # Synthetic wrapper row (not a real zone) so the user can see WHY
#             # a page-number-flanked paragraph pair became one <p> - each real
#             # constituent zone is still shown, selectable, underneath it.
#             wrapper_iid = "merge_" + "_".join(zone_ids)
#             label = f"[merged p x{sum(1 for z in zone_ids if zm.zones[z].tag == 'p')}]"
#             self.tree.insert(parent_iid, "end", iid=wrapper_iid, text=label,
#                               values=("p (merged)", "ACTIVE", first.page, first.level, "-", first.serial, "", ""))
#             for zid in zone_ids:
#                 z = zm.zones[zid]
#                 self.tree.insert(wrapper_iid, "end", iid=zid, text=z.tag, values=self._row_values(z),
#                                   tags=self._row_tags(z))
#         else:
#             zone = zm.zones[node["zone_id"]]
#             self._insert_zone_recursive(parent_iid, zone)

#     def _insert_zone_recursive(self, parent_iid, zone):
#         zm = self._zm
#         status = self._zone_status(zone)
#         base_label = zone.tag if not zone.text else f"{zone.tag}: {zone.text[:30]}"
#         label = f"[SPLIT-PARENT] {base_label}" if status == "SPLIT-PARENT" else base_label
#         iid = self.tree.insert(parent_iid, "end", iid=zone.zone_id, text=label, values=self._row_values(zone),
#                                 tags=self._row_tags(zone))
#         for cid in reading_order.sort_children_ids(zm, zone.zone_id):
#             self._insert_zone_recursive(iid, zm.zones[cid])

#     def select(self, zone_id):
#         if not zone_id or not self.tree.exists(zone_id):
#             self.inspector.show_zone(None)
#             return
#         self.inspector.show_zone(self._zm.zones.get(zone_id))
#         current = self.tree.selection()
#         if current and current[0] == zone_id:
#             # Already selected - typically means this call originated from the
#             # tree's own <<TreeviewSelect>> handler. Calling selection_set again
#             # here would reenter the Treeview's selection machinery while it is
#             # still dispatching that same event, which hangs ttk on this platform.
#             return
#         self._suppress_select_event = True
#         self.tree.selection_set(zone_id)
#         self.tree.see(zone_id)
#         # ttk generates <<TreeviewSelect>> as a DEFERRED virtual event, not
#         # synchronously inside selection_set() - update_idletasks() alone
#         # does NOT drain it (it only services the idle queue, and this
#         # virtual event sits on the general Tcl event queue), so clearing
#         # the guard flag right after selection_set() does not actually
#         # guarantee _on_select's own firing (for THIS selection_set call)
#         # has already been seen and ignored. A full nested update() forces
#         # every pending event, deferred virtual events included, to be
#         # dispatched now, while the guard is still up - instead of possibly
#         # later (e.g. during the next canvas click's own handling, or even
#         # after a page switch), which was silently queuing a redundant,
#         # stale after_idle(select_zone_from_tree(...)) for every single
#         # canvas zone selection, capable of snapping the viewer back to an
#         # old page if it fired after the user had already navigated away.
#         self.tree.update()
#         self._suppress_select_event = False

#     def _on_select(self, event):
#         if self._suppress_select_event:
#             return
#         sel = self.tree.selection()
#         if sel:
#             self.inspector.show_zone(self._zm.zones.get(sel[0]))
#             # Deferred via after_idle: mutating the Treeview's own selection
#             # (which select_zone_from_tree -> zone_tree.select does) from
#             # directly inside its <<TreeviewSelect>> handler is reentrant and
#             # can hang ttk on some platforms. Let this event dispatch finish first.
#             self.after_idle(lambda zid=sel[0]: self.app.select_zone_from_tree(zid))

"""Left tag-button panel (TAG TOOLBOX) and right zone-hierarchy tree panel."""
import tkinter as tk
from tkinter import ttk

from core.constants import TAG_BUTTONS, DEFAULT_TAG_COLORS
from core import reading_order, hierarchy, debug_log
from gui import theme
from gui.pdf_viewer import SCROLL_UNIT_PX, normalize_wheel_units, accumulate_scroll_units


# ---------------------------------------------------------------------------
# Palette safety net
# ---------------------------------------------------------------------------
# theme.current.palette is a module-level global read DIRECTLY by TagPanel and
# ZoneTreePanel (not passed in). If the launcher's set_mode()/apply_ttk_style()
# path ever produces a palette missing a key, every tk.Label(bg=palette["..."])
# below would raise a TclError deep inside tkinter. This helper guarantees a
# usable color for every key this module touches, so a palette gap degrades to
# a sane default instead of crashing the Zoning window at construction time.
_PALETTE_DEFAULTS = {
    "app_bg":           "#f5f5f5",
    "panel_bg":         "#ffffff",
    "panel_header_bg":  "#eef1f5",
    "surface":          "#ffffff",
    "border":           "#d0d0d0",
    "text":             "#1a1a1a",
    "text_muted":       "#666666",
    "text_faint":       "#999999",
    "accent":           "#2563eb",
    "accent_fg":        "#ffffff",
    "accent_hover":     "#1d4ed8",
    "accent_tint":      "#dbeafe",
    "hover_bg":         "#e5e7eb",
    "success":          "#16a34a",
    "warning":          "#d97706",
    "warning_tint":     "#fef3c7",
    "danger":           "#dc2626",
    "header_bg":        "#1e293b",
    "header_fg":        "#ffffff",
    "header_fg_muted":  "#94a3b8",
}


def _p(palette, key):
    """Never return None/'': Tk rejects both for bg/fg and raises TclError."""
    val = palette.get(key) if palette else None
    if not val:
        return _PALETTE_DEFAULTS.get(key, "#ffffff")
    return val


def _safe_palette(palette):
    """Returns a shallow copy of `palette` with every key this module reads
    guaranteed non-empty. Preserves whatever real values are present."""
    safe = dict(palette or {})
    for k, v in _PALETTE_DEFAULTS.items():
        if not safe.get(k):
            safe[k] = v
    return safe


def _format_shortcut(seq: str) -> str:
    """Tk bind-sequence syntax -> a friendly display string for the tag
    toolbox ("<Control-Alt-Key-1>" -> "Ctrl+Alt+1")."""
    if not seq:
        return ""
    inner = seq.strip("<>")
    replacements = {"Control": "Ctrl", "Shift": "Shift", "Alt": "Alt"}
    parts = [replacements.get(p, p) for p in inner.split("-") if p != "Key"]
    return "+".join(parts) if parts else inner


class PropertiesInspector(tk.Frame):
    """Compact bottom drawer under the Zone Hierarchy showing the selected
    zone's already-computed fields - read-only display of what
    ZoneManager/hierarchy already track. Also hosts CUPEPUB's manual
    PageNum entry, the generic editable zone text box, index level
    indicator, and Table Draw split controls."""
    _FIELDS = ("Zone", "Page", "Reading Order", "Level", "Parent", "Text", "Coordinates")

    def __init__(self, parent, app, palette):
        palette = _safe_palette(palette)
        super().__init__(parent, bg=_p(palette, "panel_header_bg"), highlightthickness=1,
                          highlightbackground=_p(palette, "border"))
        self.app = app
        self.palette = palette
        self._expanded = True
        self._current_zone_id = None

        header = tk.Frame(self, bg=_p(palette, "panel_header_bg"), cursor="hand2")
        header.pack(fill="x")
        self._arrow_lbl = tk.Label(header, text="▼  PROPERTIES", bg=_p(palette, "panel_header_bg"),
                                    fg=_p(palette, "text_muted"), font=theme.FONT_SMALL_BOLD, anchor="w")
        self._arrow_lbl.pack(side="left", padx=10, pady=6)
        for w in (header, self._arrow_lbl):
            w.bind("<Button-1>", lambda e: self._toggle())

        self._body = tk.Frame(self, bg=_p(palette, "surface"))
        self._body.pack(fill="x")
        self._value_labels = {}
        self._field_label_widgets = {}
        for i, field in enumerate(self._FIELDS):
            if field == "Text":
                continue
            lbl = tk.Label(self._body, text=field.upper(), bg=_p(palette, "surface"),
                            fg=_p(palette, "text_faint"), font=theme.FONT_SMALL, anchor="w", width=13)
            lbl.grid(row=i, column=0, sticky="w", padx=(10, 4), pady=2)
            self._field_label_widgets[field] = lbl
            val = tk.Label(self._body, text="—", bg=_p(palette, "surface"), fg=_p(palette, "text"),
                            font=theme.FONT_BODY, anchor="w", justify="left", wraplength=260)
            val.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=2)
            self._value_labels[field] = val

        text_row = self._FIELDS.index("Text")
        self._text_label_widget = tk.Label(self._body, text="TEXT", bg=_p(palette, "surface"),
                                             fg=_p(palette, "text_faint"), font=theme.FONT_SMALL,
                                             anchor="w", width=13)
        self._text_label_widget.grid(row=text_row, column=0, sticky="nw", padx=(10, 4), pady=2)
        text_edit = tk.Frame(self._body, bg=_p(palette, "surface"))
        text_edit.grid(row=text_row, column=1, sticky="w", padx=(0, 10), pady=2)
        self._text_box = tk.Text(text_edit, width=32, height=3, wrap="word",
                                   bg=_p(palette, "app_bg"), fg=_p(palette, "text"),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=_p(palette, "border"), font=theme.FONT_BODY)
        self._text_box.pack(side="top", fill="x")
        self._text_apply_btn = tk.Button(text_edit, text="Apply Text", command=self._apply_text)
        theme.style_button(self._text_apply_btn, palette, kind="secondary")
        self._text_apply_btn.pack(side="top", anchor="e", pady=(4, 0))
        self._text_widgets = (self._text_label_widget, text_edit)

        # PageNum manual-entry row
        self._pagenum_row = len(self._FIELDS)
        tk.Label(self._body, text="PRINTED PAGE NUMBER", bg=_p(palette, "surface"),
                  fg=_p(palette, "text_faint"), font=theme.FONT_SMALL, anchor="w", width=13).grid(
            row=self._pagenum_row, column=0, sticky="w", padx=(10, 4), pady=(8, 2))
        pagenum_edit = tk.Frame(self._body, bg=_p(palette, "surface"))
        pagenum_edit.grid(row=self._pagenum_row, column=1, sticky="w", padx=(0, 10), pady=(8, 2))
        self._pagenum_var = tk.StringVar()
        self._pagenum_var.trace_add("write", lambda *_: self._sync_pagenum_button())
        pagenum_entry = tk.Entry(pagenum_edit, textvariable=self._pagenum_var, width=14,
                                  bg=_p(palette, "app_bg"), fg=_p(palette, "text"),
                                  relief="flat", highlightthickness=1,
                                  highlightbackground=_p(palette, "border"), font=theme.FONT_BODY)
        pagenum_entry.pack(side="left", ipady=2)
        pagenum_entry.bind("<Return>", lambda e: self._apply_pagenum())
        self._pagenum_apply_btn = tk.Button(pagenum_edit, text="Apply", command=self._apply_pagenum)
        theme.style_button(self._pagenum_apply_btn, palette, kind="primary")
        self._pagenum_apply_btn.pack(side="left", padx=(6, 0))
        self._pagenum_empty_hint = tk.Label(pagenum_edit, text="(empty)", bg=_p(palette, "surface"),
                                              fg=_p(palette, "warning"), font=theme.FONT_SMALL)
        self._pagenum_empty_hint.pack(side="left", padx=(6, 0))
        self._pagenum_widgets = (self._body.grid_slaves(row=self._pagenum_row, column=0)[0], pagenum_edit)
        self._set_pagenum_row_visible(False)

        # Index Level indicator + shortcut hint
        self._index_level_row = self._pagenum_row + 1
        tk.Label(self._body, text="INDEX LEVEL", bg=_p(palette, "surface"), fg=_p(palette, "text_faint"),
                  font=theme.FONT_SMALL, anchor="w", width=13).grid(
            row=self._index_level_row, column=0, sticky="w", padx=(10, 4), pady=(8, 2))
        self._index_level_value = tk.Label(self._body, text="—", bg=_p(palette, "surface"),
                                             fg=_p(palette, "text"), font=theme.FONT_BODY, anchor="w")
        self._index_level_value.grid(row=self._index_level_row, column=1, sticky="w", padx=(0, 10), pady=(8, 2))
        self._index_shortcuts_row = self._index_level_row + 1
        tk.Label(self._body, text="SHORTCUTS", bg=_p(palette, "surface"), fg=_p(palette, "text_faint"),
                  font=theme.FONT_SMALL, anchor="nw", width=13).grid(
            row=self._index_shortcuts_row, column=0, sticky="nw", padx=(10, 4), pady=(0, 8))
        self._index_shortcuts_value = tk.Label(self._body, text="—", bg=_p(palette, "surface"),
                                                 fg=_p(palette, "text_muted"), font=theme.FONT_SMALL,
                                                 anchor="w", justify="left")
        self._index_shortcuts_value.grid(row=self._index_shortcuts_row, column=1, sticky="w",
                                          padx=(0, 10), pady=(0, 8))
        self._index_widgets = (
            self._body.grid_slaves(row=self._index_level_row, column=0)[0], self._index_level_value,
            self._body.grid_slaves(row=self._index_shortcuts_row, column=0)[0], self._index_shortcuts_value,
        )
        self._set_index_rows_visible(False)

        # Table Draw split controls
        self._table_draw_row = self._index_shortcuts_row + 1
        tk.Label(self._body, text="TABLE DRAW", bg=_p(palette, "surface"), fg=_p(palette, "text_faint"),
                  font=theme.FONT_SMALL, anchor="nw", width=13).grid(
            row=self._table_draw_row, column=0, sticky="nw", padx=(10, 4), pady=(8, 8))
        table_draw_box = tk.Frame(self._body, bg=_p(palette, "surface"))
        table_draw_box.grid(row=self._table_draw_row, column=1, sticky="w", padx=(0, 10), pady=(8, 8))
        row_frame = tk.Frame(table_draw_box, bg=_p(palette, "surface"))
        row_frame.pack(side="top", anchor="w")
        col_frame = tk.Frame(table_draw_box, bg=_p(palette, "surface"))
        col_frame.pack(side="top", anchor="w", pady=(4, 0))
        self._add_row_split_btn = tk.Button(row_frame, text="Add Row Split", command=self._add_row_split)
        self._delete_row_split_btn = tk.Button(row_frame, text="Delete Row Split", command=self._delete_row_split)
        self._add_col_split_btn = tk.Button(col_frame, text="Add Column Split", command=self._add_column_split)
        self._delete_col_split_btn = tk.Button(col_frame, text="Delete Column Split",
                                                 command=self._delete_column_split)
        for btn in (self._add_row_split_btn, self._delete_row_split_btn,
                    self._add_col_split_btn, self._delete_col_split_btn):
            theme.style_button(btn, palette, kind="secondary")
            btn.pack(side="left", padx=(0, 6))
        self._table_draw_widgets = (
            self._body.grid_slaves(row=self._table_draw_row, column=0)[0], table_draw_box,
        )
        self._set_table_draw_row_visible(False)

    def _sync_pagenum_button(self):
        is_empty = not self._pagenum_var.get().strip()
        self._pagenum_apply_btn.configure(text="Set Page Number" if is_empty else "Apply")
        if is_empty:
            self._pagenum_empty_hint.pack(side="left", padx=(6, 0))
        else:
            self._pagenum_empty_hint.pack_forget()

    def _set_pagenum_row_visible(self, visible: bool):
        for w in self._pagenum_widgets:
            if visible:
                w.grid()
            else:
                w.grid_remove()

    def _set_index_rows_visible(self, visible: bool):
        for w in self._index_widgets:
            if visible:
                w.grid()
            else:
                w.grid_remove()

    def _set_table_draw_row_visible(self, visible: bool):
        for w in self._table_draw_widgets:
            if visible:
                w.grid()
            else:
                w.grid_remove()

    def _add_row_split(self):
        if not self._current_zone_id:
            return
        self.app.viewer.start_region_split_mode(self._current_zone_id, "row")

    def _add_column_split(self):
        if not self._current_zone_id:
            return
        self.app.viewer.start_region_split_mode(self._current_zone_id, "col")

    def _delete_row_split(self):
        if self.app.viewer.mode != "region_split_h":
            self.app.set_status("Click Add Row Split, then click a guide line to select it before deleting.")
            return
        self.app.viewer.delete_selected_region_split()

    def _delete_column_split(self):
        if self.app.viewer.mode != "region_split_v":
            self.app.set_status("Click Add Column Split, then click a guide line to select it before deleting.")
            return
        self.app.viewer.delete_selected_region_split()

    def _apply_pagenum(self):
        if not self._current_zone_id:
            return
        self.app.zone_manager.set_zone_text(self._current_zone_id, self._pagenum_var.get())
        self.app.on_zones_changed()
        self.app.notify("Page number updated", kind="success")

    def _apply_text(self):
        if not self._current_zone_id:
            return
        new_text = self._text_box.get("1.0", "end-1c")
        self.app.zone_manager.set_zone_text(self._current_zone_id, new_text)
        self.app.on_zones_changed()
        self.app.notify("Zone text updated", kind="success")

    def _set_text_row_visible(self, visible: bool):
        for w in self._text_widgets:
            if visible:
                w.grid()
            else:
                w.grid_remove()

    def _toggle(self):
        self._expanded = not self._expanded
        if self._expanded:
            self._body.pack(fill="x")
            self._arrow_lbl.configure(text="▼  PROPERTIES")
        else:
            self._body.pack_forget()
            self._arrow_lbl.configure(text="▶  PROPERTIES")

    def show_zone(self, zone):
        if zone is None:
            self._current_zone_id = None
            for lbl in self._value_labels.values():
                lbl.configure(text="—")
            self._text_box.delete("1.0", "end")
            self._text_box.configure(state="disabled")
            self._set_pagenum_row_visible(False)
            self._set_index_rows_visible(False)
            self._set_table_draw_row_visible(False)
            self._set_text_row_visible(True)
            return
        self._current_zone_id = zone.zone_id
        self._value_labels["Zone"].configure(text=zone.tag)
        self._value_labels["Page"].configure(text=str(zone.page))
        self._value_labels["Reading Order"].configure(text=str(zone.serial) if zone.serial is not None else "—")
        self._value_labels["Level"].configure(text=str(zone.level))
        self._value_labels["Parent"].configure(text=zone.parent_id or "—")
        x0, y0, x1, y1 = zone.bbox
        self._value_labels["Coordinates"].configure(
            text=f"X {x0:.0f}  Y {y0:.0f}  W {x1 - x0:.0f}  H {y1 - y0:.0f}")

        pagenum_name = self.app.active_profile.get("cup_pagenum_name")
        is_pagenum = bool(pagenum_name) and zone.attributes.get("cup_name") == pagenum_name
        self._field_label_widgets["Page"].configure(text="PHYSICAL PDF PAGE" if is_pagenum else "PAGE")
        self._set_pagenum_row_visible(is_pagenum)
        if is_pagenum:
            self._pagenum_var.set(zone.text or "")
            self._sync_pagenum_button()

        index_hierarchy_tags = {int(k): v for k, v in (self.app.active_profile.get("index_hierarchy_tags")
                                                          or {}).items()}
        current_level = next((lvl for lvl, t in index_hierarchy_tags.items() if t == zone.tag), None)
        self._set_index_rows_visible(current_level is not None)
        if current_level is not None:
            self._index_level_value.configure(text=self.app.index_level_name(current_level))
            hint_lines = []
            for lvl, setting_key in ((1, "index_shortcut_primary"), (2, "index_shortcut_secondary"),
                                      (3, "index_shortcut_tertiary")):
                if lvl not in index_hierarchy_tags:
                    continue
                seq = self.app.settings.get(setting_key)
                if not seq:
                    continue
                marker = "→ " if lvl == current_level else "   "
                hint_lines.append(f"{marker}{_format_shortcut(seq)}  {self.app.index_level_name(lvl)}")
            self._index_shortcuts_value.configure(text="\n".join(hint_lines) or "—")

        self._set_table_draw_row_visible(zone.tag == "table")

        self._set_text_row_visible(not is_pagenum)
        if not is_pagenum:
            self._text_box.configure(state="normal")
            self._text_box.delete("1.0", "end")
            self._text_box.insert("1.0", zone.text or "")


class TagPanel(tk.Frame):
    def __init__(self, parent, app, tag_buttons=None, tag_colors=None, tag_groups=None):
        palette = _safe_palette(theme.current.snapshot())
        super().__init__(parent, bg=_p(palette, "panel_bg"))
        self.app = app
        self.palette = palette
        self.buttons = {}
        self._tag_lookup = {}
        self._collapsed_groups = set()
        self._all_tag_buttons = []
        self._all_tag_colors = {}
        self._all_tag_groups = []
        self._active_group_label = None
        self._tab_buttons = {}
        self._manual_tag_label = None
        self._active_tag_label = None

        header = tk.Frame(self, bg=_p(palette, "panel_header_bg"))
        header.pack(side="top", fill="x")
        tk.Label(header, text="TAG TOOLBOX", bg=_p(palette, "panel_header_bg"), fg=_p(palette, "text"),
                  font=theme.FONT_PANEL_TITLE).pack(side="top", anchor="w", padx=10, pady=(8, 6))

        search_row = tk.Frame(header, bg=_p(palette, "panel_header_bg"))
        search_row.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._render(self._search_var.get()))
        search_box = tk.Frame(search_row, bg=_p(palette, "surface"), highlightthickness=1,
                                highlightbackground=_p(palette, "border"))
        search_box.pack(fill="x")
        tk.Label(search_box, text="\U0001F50D", bg=_p(palette, "surface"), fg=_p(palette, "text_muted"),
                  font=theme.FONT_SMALL).pack(side="left", padx=(6, 2))
        self._search_entry = tk.Entry(search_box, textvariable=self._search_var, bg=_p(palette, "surface"),
                                       fg=_p(palette, "text"), relief="flat", font=theme.FONT_BODY,
                                       insertbackground=_p(palette, "text"))
        self._search_entry.pack(side="left", fill="x", expand=True, ipady=3)
        # Defensive lookup - see module docstring. Uses palette.get with a
        # fallback so a missing "surface"/"text_faint" key can never again
        # surface as a TclError from inside tk.Label().
        clear_btn = tk.Label(search_box, text="✕",
                              bg=_p(palette, "surface"),
                              fg=_p(palette, "text_faint"),
                              font=theme.FONT_SMALL, cursor="hand2")
        clear_btn.pack(side="left", padx=(2, 6))
        clear_btn.bind("<Button-1>", lambda e: self._search_var.set(""))

        self._tab_bar = tk.Frame(header, bg=_p(palette, "panel_header_bg"))
        self._tab_bar.pack(side="top", fill="x", padx=10, pady=(0, 8))

        deselect_btn = tk.Button(self, text="Deselect Tag", command=self._deselect)
        theme.style_button(deselect_btn, palette, kind="secondary")
        deselect_btn.pack(side="bottom", fill="x", padx=8, pady=8)

        self._canvas = tk.Canvas(self, bg=_p(palette, "panel_bg"), highlightthickness=0,
                                  yscrollincrement=SCROLL_UNIT_PX)
        self._vbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vbar.set)
        self._touchpad_accum_y = 0.0
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vbar.pack(side="right", fill="y")

        self._container = tk.Frame(self._canvas, bg=_p(palette, "panel_bg"))
        self._window_id = self._canvas.create_window((0, 0), window=self._container, anchor="nw")

        def _on_container_configure(event):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        def _on_canvas_configure(event):
            self._canvas.itemconfigure(self._window_id, width=event.width)

        self._container.bind("<Configure>", _on_container_configure)
        self._canvas.bind("<Configure>", _on_canvas_configure)
        for widget in (self._canvas, self._vbar, self):
            widget.bind("<MouseWheel>", self._on_wheel)
            widget.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"))
            theme.bind_touchpad_scroll(widget, self._on_touchpad_scroll)

        self.rebuild(tag_buttons if tag_buttons is not None else TAG_BUTTONS,
                     tag_colors if tag_colors is not None else DEFAULT_TAG_COLORS,
                     tag_groups)

    def focus_search(self):
        self._search_entry.focus_set()
        self._search_entry.selection_range(0, "end")

    def _on_wheel(self, event):
        units = normalize_wheel_units(event.delta)
        if units:
            self._canvas.yview_scroll(units, "units")
        debug_log.log("SCROLL", f"widget=TagPanel.{event.widget} event=<MouseWheel> delta={event.delta} "
                                 f"widget_xy=({event.x},{event.y}) action=yview_scroll(canvas) units={units} handled=True")

    def _on_touchpad_scroll(self, dx: int, dy: int):
        if dy:
            self._touchpad_accum_y, units = accumulate_scroll_units(self._touchpad_accum_y, dy)
            if units:
                self._canvas.yview_scroll(units, "units")
        debug_log.log("SCROLL", f"widget=TagPanel event=<TouchpadScroll> dx={dx} dy={dy} "
                                 f"action=yview_scroll(canvas) handled=True")

    def _bind_wheel_recursive(self, widget):
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        widget.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"), add="+")
        widget.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"), add="+")
        theme.bind_touchpad_scroll(widget, self._on_touchpad_scroll)
        for child in widget.winfo_children():
            self._bind_wheel_recursive(child)

    def rebuild(self, tag_buttons, tag_colors=None, tag_groups=None):
        self._all_tag_buttons = list(tag_buttons)
        self._all_tag_colors = tag_colors or DEFAULT_TAG_COLORS
        self._all_tag_groups = list(tag_groups or [])
        self._collapsed_groups = set()
        self._active_group_label = self._all_tag_groups[0]["label"] if self._all_tag_groups else None
        self._rebuild_tabs()
        self._tag_lookup = {label: (tag, attrs) for label, tag, attrs in self._all_tag_buttons}
        if self._search_var.get():
            self._search_var.set("")
        else:
            self._render("")

    def _toggle_group(self, group_label):
        if group_label in self._collapsed_groups:
            self._collapsed_groups.discard(group_label)
        else:
            self._collapsed_groups.add(group_label)
        self._render(self._search_var.get())

    _GROUP_DISPLAY_LABEL = {"Frontmatter": "FM", "Backmatter": "BM"}
    _GROUP_ACCENT_INDEX = {"Frontmatter": 0, "Backmatter": 4}

    def _rebuild_tabs(self):
        for child in list(self._tab_bar.winfo_children()):
            child.destroy()
        self._tab_buttons = {}
        if not self._all_tag_groups:
            return
        for group in self._all_tag_groups:
            group_label = group["label"]
            display_text = self._GROUP_DISPLAY_LABEL.get(group_label, group_label.upper())
            btn = tk.Button(self._tab_bar, text=display_text,
                             command=lambda gl=group_label: self._select_tab(gl))
            btn.pack(side="left", padx=(0, 4))
            self._tab_buttons[group_label] = btn
        self._update_tab_styles()

    def _select_tab(self, group_label):
        self._active_group_label = group_label
        self._update_tab_styles()
        self._render(self._search_var.get())

    def _style_one_tab(self, group_label, btn, active):
        if active:
            btn.configure(bg=_p(self.palette, "accent"), fg=_p(self.palette, "accent_fg"))
        else:
            theme.style_button(btn, self.palette, kind="secondary")
        accent_idx = self._GROUP_ACCENT_INDEX.get(group_label)
        if accent_idx is None:
            return
        accent_color = theme.TAG_CATEGORY_PALETTE[accent_idx]
        btn.configure(font=theme.FONT_BODY_BOLD, highlightthickness=2,
                      highlightbackground=accent_color, highlightcolor=accent_color)
        if not active:
            btn.configure(fg=accent_color)

    def _update_tab_styles(self):
        for group_label, btn in self._tab_buttons.items():
            self._style_one_tab(group_label, btn, group_label == self._active_group_label)

    def _render(self, filter_text: str = ""):
        query = filter_text.strip().lower()
        for child in list(self._container.winfo_children()):
            child.destroy()
        self.buttons = {}
        group_starts = {g["first"]: g["label"] for g in self._all_tag_groups}
        label_to_group = {}
        _current_group = None
        for label, _tag, _attrs in self._all_tag_buttons:
            if label in group_starts:
                _current_group = group_starts[label]
            label_to_group[label] = _current_group
        tab_filter_active = bool(self._all_tag_groups) and not query
        matches = {
            label: (not query or query in label.lower())
            and (not tab_filter_active or label_to_group.get(label) == self._active_group_label)
            for label, _tag, _attrs in self._all_tag_buttons
        }
        pending_group_label = None
        skip_until_next_group = False
        any_visible = False
        index_hierarchy_tags = self.app.active_profile.get("index_hierarchy_tags") or {}
        index_tags = set(index_hierarchy_tags.values())
        index_tag_shortcuts = {}
        for level, setting_key in ((1, "index_shortcut_primary"), (2, "index_shortcut_secondary"),
                                    (3, "index_shortcut_tertiary")):
            tag = index_hierarchy_tags.get(level) or index_hierarchy_tags.get(str(level))
            seq = self.app.settings.get(setting_key)
            if tag and seq:
                index_tag_shortcuts[tag] = _format_shortcut(seq)
        cup_tag_shortcuts = {}
        for entry in (self.app.active_profile.get("cup_shortcuts") or []):
            tag = entry.get("tag")
            seqs = entry.get("seqs") or []
            if not tag or not seqs or tag in cup_tag_shortcuts:
                continue
            display_seq = next((s for s in seqs if s[-2:-1].isupper() or not s[-2:-1].isalpha()), seqs[0])
            cup_tag_shortcuts[tag] = _format_shortcut(display_seq)
        pending_index_button = False

        def _insert_auto_zone_index_button():
            ttk.Separator(self._container, orient="horizontal").pack(fill="x", padx=8, pady=(6, 3))
            btn_row = tk.Frame(self._container, bg=_p(self.palette, "panel_bg"))
            btn_row.pack(fill="x", padx=8, pady=(0, 3))
            azi_btn = tk.Button(btn_row, text="Auto Zone Index", command=self.app.auto_zone_index)
            theme.style_button(azi_btn, self.palette, kind="primary")
            azi_btn.pack(fill="x")
            ttk.Separator(self._container, orient="horizontal").pack(fill="x", padx=8, pady=(3, 6))

        for label, tag, attrs in self._all_tag_buttons:
            if label in group_starts:
                if pending_index_button:
                    _insert_auto_zone_index_button()
                    pending_index_button = False
                group_label = group_starts[label]
                if self._all_tag_groups:
                    skip_until_next_group = False
                else:
                    collapsed = (not query) and group_label in self._collapsed_groups
                    arrow = "▶" if collapsed else "▼"
                    header_row = tk.Frame(self._container, bg=_p(self.palette, "panel_header_bg"),
                                           cursor="hand2")
                    header_row.pack(fill="x", padx=0, pady=(8, 1))
                    header_lbl = tk.Label(header_row, text=f"{arrow}  {group_label.upper()}",
                                           bg=_p(self.palette, "panel_header_bg"),
                                           fg=_p(self.palette, "text_muted"),
                                           font=theme.FONT_SMALL_BOLD, anchor="w")
                    header_lbl.pack(fill="x", padx=8, pady=4)
                    for w in (header_row, header_lbl):
                        w.bind("<Button-1>", lambda e, gl=group_label: self._toggle_group(gl))
                    skip_until_next_group = collapsed
                pending_group_label = None
            if not matches[label] or skip_until_next_group:
                continue
            if pending_index_button and tag not in index_tags:
                _insert_auto_zone_index_button()
                pending_index_button = False
            any_visible = True
            color = self._all_tag_colors.get(tag, _p(self.palette, "text"))
            row = tk.Frame(self._container, bg=_p(self.palette, "panel_bg"))
            row.pack(fill="x", padx=8, pady=1)
            chip = tk.Frame(row, bg=color, width=3)
            chip.pack(side="left", fill="y")
            if tag in index_tag_shortcuts:
                shortcut_suffix = f"   [{index_tag_shortcuts[tag]}]"
            elif tag in cup_tag_shortcuts:
                shortcut_suffix = f"   [{cup_tag_shortcuts[tag]}]"
            else:
                shortcut_suffix = ""
            b = tk.Button(row, text=label + shortcut_suffix, anchor="w",
                          command=lambda t=tag, a=attrs, l=label: self._select(t, a, l))
            b.configure(bg=_p(self.palette, "surface"), fg=_p(self.palette, "text"),
                        activebackground=_p(self.palette, "hover_bg"),
                        relief="flat", bd=1, highlightthickness=1,
                        highlightbackground=_p(self.palette, "border"),
                        font=theme.FONT_BODY, cursor="hand2", padx=8, pady=4)
            b.pack(side="left", fill="x", expand=True)
            self.buttons[label] = b
            pending_index_button = tag in index_tags
        if pending_index_button:
            _insert_auto_zone_index_button()
        if not any_visible:
            tk.Label(self._container, text="No matching tags", bg=_p(self.palette, "panel_bg"),
                      fg=_p(self.palette, "text_faint"), font=theme.FONT_BODY).pack(pady=24)
        self._bind_wheel_recursive(self._container)
        self._canvas.yview_moveto(0)
        if self._manual_tag_label and self._manual_tag_label not in self._tag_lookup:
            self._manual_tag_label = None
            self._active_tag_label = None
            self.app.set_active_tag(None, None)
        else:
            self._paint_active_tag(self._active_tag_label)

    def _paint_active_tag(self, label):
        for b in self.buttons.values():
            b.configure(bg=_p(self.palette, "surface"),
                        highlightbackground=_p(self.palette, "border"))
        self._active_tag_label = label
        if label in self.buttons:
            self.buttons[label].configure(bg=_p(self.palette, "accent_tint"),
                                           highlightbackground=_p(self.palette, "accent"))

    def _select(self, tag, attrs, label):
        self._manual_tag_label = label
        self._paint_active_tag(label)
        self.app.set_active_tag(tag, attrs)

    def select_label(self, label):
        entry = self._tag_lookup.get(label)
        if entry:
            self._select(entry[0], entry[1], label)

    def _deselect(self):
        self._manual_tag_label = None
        self._paint_active_tag(None)
        self.app.set_active_tag(None, None)

    def _label_for_zone(self, zone):
        best_label, best_attrs = None, None
        for label, tag, attrs in self._all_tag_buttons:
            if tag != zone.tag:
                continue
            if not all(zone.attributes.get(k) == v for k, v in attrs.items()):
                continue
            if best_attrs is None or len(attrs) > len(best_attrs):
                best_label, best_attrs = label, attrs
        return best_label

    def highlight_tag_for_zone(self, zone):
        label = self._label_for_zone(zone) if zone is not None else self._manual_tag_label
        self._paint_active_tag(label)


class ZoneTreePanel(tk.Frame):
    def __init__(self, parent, app):
        palette = _safe_palette(theme.current.snapshot())
        super().__init__(parent, width=340, bg=_p(palette, "panel_bg"))
        self.app = app
        self.palette = palette

        header = tk.Frame(self, bg=_p(palette, "panel_header_bg"))
        header.pack(side="top", fill="x")
        tk.Label(header, text="ZONE HIERARCHY", bg=_p(palette, "panel_header_bg"),
                  fg=_p(palette, "text"), font=theme.FONT_PANEL_TITLE).pack(side="top", anchor="w",
                                                                              padx=10, pady=(8, 6))

        search_row = tk.Frame(header, bg=_p(palette, "panel_header_bg"))
        search_row.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self._search_var = tk.StringVar()
        search_box = tk.Frame(search_row, bg=_p(palette, "surface"), highlightthickness=1,
                                highlightbackground=_p(palette, "border"))
        search_box.pack(fill="x")
        tk.Label(search_box, text="\U0001F50D", bg=_p(palette, "surface"), fg=_p(palette, "text_muted"),
                  font=theme.FONT_SMALL).pack(side="left", padx=(6, 2))
        search_entry = tk.Entry(search_box, textvariable=self._search_var, bg=_p(palette, "surface"),
                                 fg=_p(palette, "text"), relief="flat", font=theme.FONT_BODY,
                                 insertbackground=_p(palette, "text"))
        search_entry.pack(side="left", fill="x", expand=True, ipady=3)
        search_entry.bind("<Return>", lambda e: self._jump_to_next_match())
        clear_btn = tk.Label(search_box, text="✕",
                              bg=_p(palette, "surface"),
                              fg=_p(palette, "text_faint"),
                              font=theme.FONT_SMALL, cursor="hand2")
        clear_btn.pack(side="left", padx=(2, 6))
        clear_btn.bind("<Button-1>", lambda e: self._search_var.set(""))
        self._match_iids = []
        self._match_index = -1

        tree_area = tk.Frame(self, bg=_p(palette, "panel_bg"))
        tree_area.pack(side="top", fill="both", expand=True)

        columns = ("tag", "status", "page", "level", "parent", "serial", "bbox", "text")
        self.tree = ttk.Treeview(tree_area, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Zone")
        self.tree.heading("tag", text="Tag")
        self.tree.heading("status", text="Status")
        self.tree.heading("page", text="Page")
        self.tree.heading("level", text="Level")
        self.tree.heading("parent", text="Parent")
        self.tree.heading("serial", text="Reading Order")
        self.tree.heading("bbox", text="BBox")
        self.tree.heading("text", text="Text Preview")
        self.tree.column("#0", width=110)
        self.tree.column("tag", width=70)
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("page", width=40, anchor="center")
        self.tree.column("level", width=40, anchor="center")
        self.tree.column("parent", width=70, anchor="center")
        self.tree.column("serial", width=90, anchor="center")
        self.tree.column("bbox", width=140)
        self.tree.column("text", width=220)
        self.tree.tag_configure("split_parent", foreground=_p(palette, "text_faint"))
        self.tree.tag_configure("search_match", background=_p(palette, "warning_tint"))

        vbar = ttk.Scrollbar(tree_area, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        for widget in (vbar, tree_area):
            widget.bind("<MouseWheel>", lambda e: self.tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        for widget in (self.tree, vbar, tree_area):
            theme.bind_touchpad_scroll(
                widget, lambda dx, dy: self.tree.yview_scroll(-1 if dy > 0 else 1, "units") if dy else None)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        self._suppress_select_event = False
        self._zm = None

        self.inspector = PropertiesInspector(self, app, palette)
        self.inspector.pack(side="bottom", fill="x")

        self._search_var.trace_add("write", lambda *_: self._recompute_matches())

    def _recompute_matches(self):
        query = self._search_var.get().strip().lower()
        self._apply_row_tags_recursive("")
        self._match_iids = []
        if not query:
            self._match_index = -1
            return
        self._collect_matches_recursive("", query)
        self._match_index = -1
        if self._match_iids:
            self._jump_to_next_match()

    def _apply_row_tags_recursive(self, iid):
        for child in self.tree.get_children(iid):
            tags = tuple(t for t in self.tree.item(child, "tags") if t != "search_match")
            self.tree.item(child, tags=tags)
            self._apply_row_tags_recursive(child)

    def _collect_matches_recursive(self, iid, query):
        for child in self.tree.get_children(iid):
            values = self.tree.item(child, "values")
            text = self.tree.item(child, "text") or ""
            haystack = (text + " " + " ".join(str(v) for v in values)).lower()
            if query in haystack:
                self._match_iids.append(child)
                tags = tuple(self.tree.item(child, "tags")) + ("search_match",)
                self.tree.item(child, tags=tags)
            self._collect_matches_recursive(child, query)

    def _jump_to_next_match(self):
        if not self._match_iids:
            return
        self._match_index = (self._match_index + 1) % len(self._match_iids)
        iid = self._match_iids[self._match_index]
        parent = self.tree.parent(iid)
        while parent:
            self.tree.item(parent, open=True)
            parent = self.tree.parent(parent)
        self.tree.see(iid)
        self.tree.selection_set(iid)

    def _on_double_click(self, _event):
        sel = self.tree.selection()
        if not sel or sel[0] not in self._zm.zones:
            return
        zone = self._zm.zones[sel[0]]
        if zone.page != self.app.current_page:
            self.app.goto_page(str(zone.page))
        self.app.select_zone_from_tree(sel[0])

    # ---- incremental tree update -------------------------------------
    # The panel used to delete every row and re-insert the whole document
    # (one Tk call per zone) after EVERY edit - on a large project that
    # alone froze the window for about a second after drawing or splitting
    # a single zone. refresh() now builds the wanted rows in Python and
    # _apply_rows() only touches the rows that actually changed.
    def _emit_row(self, parent_iid, _index, iid, text="", values=(), tags=()):
        self._pending_rows.append((iid, parent_iid, text, tuple(values), tuple(tags)))
        return iid

    def _apply_rows(self, rows):
        try:
            self._apply_rows_diff(rows)
        except tk.TclError:
            # cache and widget out of step for any reason -> full rebuild
            self._row_cache = {}
            self._children_cache = {}
            self.tree.delete(*self.tree.get_children())
            self._apply_rows_diff(rows)

    def _apply_rows_diff(self, rows):
        tree = self.tree
        old_rows = getattr(self, "_row_cache", None) or {}
        old_children = getattr(self, "_children_cache", None) or {}
        if not old_rows and tree.get_children():
            tree.delete(*tree.get_children())
        new_rows = {}
        new_children = {}
        for iid, parent, text, values, tags in rows:
            new_rows[iid] = (parent, text, values, tags)
            new_children.setdefault(parent, []).append(iid)
            old = old_rows.get(iid)
            if old is None:
                tree.insert(parent, "end", iid=iid, text=text, values=values, tags=tags)
            elif old[1:] != (text, values, tags):
                tree.item(iid, text=text, values=values, tags=tags)
        # fix order / parent for every parent whose child list changed
        # (also re-parents surviving rows before stale rows are deleted)
        for parent, kids in new_children.items():
            if old_children.get(parent) != kids:
                tree.set_children(parent, *kids)
        for parent in old_children:
            if parent not in new_children and parent in new_rows:
                tree.set_children(parent)
        gone = [iid for iid in old_rows if iid not in new_rows]
        if gone:
            gone = [iid for iid in gone if tree.exists(iid)]
            if gone:
                tree.delete(*gone)
        self._row_cache = new_rows
        self._children_cache = new_children

    def refresh(self):
        zm = self.app.zone_manager
        self._zm = zm
        if not zm.zones:
            self._apply_rows([])
            self.inspector.show_zone(None)
            return
        page_order = reading_order.compute_page_order(zm)
        box_container_tags = self.app.active_profile.get("box_container_tags")
        extra_markers = [tuple(box_container_tags)] if box_container_tags else None
        doc_tree, boundary_issues = hierarchy.build_document_tree(
            zm, page_order, extra_markers,
            page_marker_tags=self.app.active_profile.get("page_marker_tags"),
            footnote_flow_tags=self.app.active_profile.get("footnote_flow_tags"),
            non_flow_tags=self.app.active_profile.get("non_flow_tags"))
        hierarchy.finalize_order(zm, doc_tree)
        if boundary_issues:
            self.app.set_status("; ".join(boundary_issues))
        self._pending_rows = []
        for node in doc_tree["children"]:
            self._insert_node("", node)
        rows, self._pending_rows = self._pending_rows, None
        self._apply_rows(rows)
        if self._search_var.get():
            self._recompute_matches()
        self.app.on_zones_reindexed()

    @staticmethod
    def _bbox_str(bbox):
        return "{:.0f}, {:.0f}, {:.0f}, {:.0f}".format(*bbox)

    def _zone_status(self, zone):
        zm = self._zm
        if zone.children:
            children = [zm.zones[c] for c in zone.children if c in zm.zones]
            if children and all(c.is_split and c.source_zone_id == zone.zone_id and c.tag == zone.tag
                                 for c in children):
                return "SPLIT-PARENT"
        return "ACTIVE"

    def _row_values(self, zone):
        return (zone.tag, self._zone_status(zone), zone.page, zone.level, zone.parent_id or "-",
                zone.serial or "", self._bbox_str(zone.bbox), (zone.text or "")[:60])

    def _row_tags(self, zone):
        return ("split_parent",) if self._zone_status(zone) == "SPLIT-PARENT" else ()

    def _insert_node(self, parent_iid, node):
        zm = self._zm
        if node["type"] == "sec":
            zone = zm.zones[node["zone_id"]]
            label = f"H{node['level']}: {(zone.text or '')[:30]}"
            iid = self._emit_row(parent_iid, "end", iid=zone.zone_id, text=label,
                                    values=self._row_values(zone), tags=self._row_tags(zone))
            for child in node["children"]:
                self._insert_node(iid, child)
        elif node["type"] == "boxed-text":
            start_zone = zm.zones[node["start_zone_id"]]
            end_ok = "OK" if node.get("end_zone_id") else "UNCLOSED"
            label = f"boxed-text [{end_ok}]"
            iid = self._emit_row(parent_iid, "end", iid=start_zone.zone_id, text=label,
                                    values=self._row_values(start_zone), tags=self._row_tags(start_zone))
            for child in node["children"]:
                self._insert_node(iid, child)
            if node.get("end_zone_id"):
                end_zone = zm.zones[node["end_zone_id"]]
                self._emit_row(iid, "end", iid=end_zone.zone_id, text="boxed-text-end",
                                  values=self._row_values(end_zone), tags=self._row_tags(end_zone))
        elif node["type"] == "merged_p":
            zone_ids = node["zone_ids"]
            first = zm.zones[zone_ids[0]]
            wrapper_iid = "merge_" + "_".join(zone_ids)
            label = f"[merged p x{sum(1 for z in zone_ids if zm.zones[z].tag == 'p')}]"
            self._emit_row(parent_iid, "end", iid=wrapper_iid, text=label,
                              values=("p (merged)", "ACTIVE", first.page, first.level, "-", first.serial, "", ""))
            for zid in zone_ids:
                z = zm.zones[zid]
                self._emit_row(wrapper_iid, "end", iid=zid, text=z.tag,
                                  values=self._row_values(z), tags=self._row_tags(z))
        else:
            zone = zm.zones[node["zone_id"]]
            self._insert_zone_recursive(parent_iid, zone)

    def _insert_zone_recursive(self, parent_iid, zone):
        zm = self._zm
        status = self._zone_status(zone)
        base_label = zone.tag if not zone.text else f"{zone.tag}: {zone.text[:30]}"
        label = f"[SPLIT-PARENT] {base_label}" if status == "SPLIT-PARENT" else base_label
        iid = self._emit_row(parent_iid, "end", iid=zone.zone_id, text=label,
                                values=self._row_values(zone), tags=self._row_tags(zone))
        for cid in reading_order.sort_children_ids(zm, zone.zone_id):
            self._insert_zone_recursive(iid, zm.zones[cid])

    def select(self, zone_id):
        if not zone_id or not self.tree.exists(zone_id):
            self.inspector.show_zone(None)
            return
        self.inspector.show_zone(self._zm.zones.get(zone_id))
        current = self.tree.selection()
        if current and current[0] == zone_id:
            return
        self._suppress_select_event = True
        self.tree.selection_set(zone_id)
        self.tree.see(zone_id)
        self.tree.update()
        self._suppress_select_event = False

    def _on_select(self, event):
        if self._suppress_select_event:
            return
        sel = self.tree.selection()
        if sel:
            self.inspector.show_zone(self._zm.zones.get(sel[0]))
            self.after_idle(lambda zid=sel[0]: self.app.select_zone_from_tree(zid))