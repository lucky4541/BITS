"""Modal dialogs: Settings, split child-tag selection, zone info/edit,
Hyphen Normalization Review."""
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from core.constants import TAG_BUTTONS, SPLIT_CHILD_CHOICES, SPLIT_CHILD_TAG
from core import text_extractor
from core.project_manager import DEFAULT_SETTINGS


DPI_PRESETS = ["72", "100", "150", "200", "250", "300", "400", "600"]


class SettingsDialog(tk.Toplevel):
    """Resizable, scrollable Settings dialog (a fixed-size, non-resizable
    single Frame previously let the content run below the screen on a
    small/laptop display with NO way to reach Save/Cancel - confirmed: the
    button row was gridded as just the last row of the same flat form
    Frame, so it scrolled off with everything else). The fix is the
    standard Tkinter scrollable-container pattern: a Canvas + vertical
    Scrollbar holding an inner Frame (`form`, below - every setting widget
    is built into it exactly as before, so _ok()'s logic and every
    settings key/value are completely unchanged), with the OK/Cancel
    button row built into a SEPARATE frame packed at the bottom of the
    Toplevel OUTSIDE the canvas - always visible regardless of scroll
    position, dialog size, or content height."""

    def __init__(self, parent, settings: dict, epub_profile: dict = None):  # epub_profile: unused, kept for callers
        super().__init__(parent)
        self.title("Settings")
        self.resizable(True, True)
        self.result = None

        # Button row FIRST and packed to the bottom, so it always claims
        # its own space before the scrollable content area gets whatever
        # is left - this is what keeps it "outside" the canvas and always
        # visible no matter how tall the settings content grows.
        btns = tk.Frame(self, padx=12, pady=10)
        btns.pack(side="bottom", fill="x")
        ttk.Separator(btns, orient="horizontal").pack(fill="x", pady=(0, 10))
        btn_row = tk.Frame(btns)
        btn_row.pack()
        tk.Button(btn_row, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        tk.Button(btn_row, text="Cancel", width=10, command=lambda: self._on_close()).pack(side="left", padx=4)

        # Scrollable content area: canvas + vertical scrollbar, with the
        # settings form built inside an inner Frame embedded in the canvas.
        canvas_holder = tk.Frame(self)
        canvas_holder.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(canvas_holder, highlightthickness=0)
        vsb = ttk.Scrollbar(canvas_holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        form = tk.Frame(canvas, padx=12, pady=12)
        form_window = canvas.create_window((0, 0), window=form, anchor="nw")

        def _sync_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_form_width(event):
            # Keeps the inner form exactly as wide as the visible canvas
            # (never wider, never left narrower with a stray gap) as the
            # dialog is resized - only the HEIGHT should ever need
            # scrolling, not the width.
            canvas.itemconfig(form_window, width=event.width)

        form.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_form_width)

        # Mouse-wheel scrolling while the cursor is over the settings
        # content (spec req. 8). Windows/macOS report event.delta in
        # multiples of 120 (Windows) - dividing by 120 gives one scroll
        # "click" per notch; bound only while this modal dialog is open
        # (bind_all, undone in _on_close) rather than a permanent
        # application-wide binding, since grab_set() already means no
        # other window can receive input during that window anyway.
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_mousewheel_linux(event):
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        self.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind_all("<Button-4>", _on_mousewheel_linux)
        self.bind_all("<Button-5>", _on_mousewheel_linux)

        # Keyboard scrolling (spec req. 9) - bound to the canvas itself
        # (not bind_all/the whole dialog), so PageUp/PageDown/arrow keys
        # scroll the content only when the canvas area has focus, and
        # never hijack normal text-cursor movement inside an Entry/
        # Spinbox/Combobox elsewhere in the form.
        canvas.bind("<Prior>", lambda e: canvas.yview_scroll(-1, "pages"))
        canvas.bind("<Next>", lambda e: canvas.yview_scroll(1, "pages"))
        canvas.bind("<Up>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Down>", lambda e: canvas.yview_scroll(1, "units"))
        canvas.configure(takefocus=True)
        canvas.bind("<Enter>", lambda e: canvas.focus_set())

        def _on_close():
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")
            self.destroy()

        self.protocol("WM_DELETE_WINDOW", _on_close)
        self._on_close = _on_close

        row = 0

        tk.Label(form, text="ID / filename prefix (e.g. bornemann-ch001):").grid(row=row, column=0, sticky="w")
        self.prefix_var = tk.StringVar(value=settings.get("prefix", "document"))
        tk.Entry(form, textvariable=self.prefix_var, width=30).grid(row=row, column=1, pady=3)
        row += 1

        tk.Label(form, text="XML root tag:").grid(row=row, column=0, sticky="w")
        self.root_tag_var = tk.StringVar(value=settings.get("root_tag", "book"))
        tk.Entry(form, textvariable=self.root_tag_var, width=30).grid(row=row, column=1, pady=3)
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        tk.Label(form, text="PDF Viewer", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        self.zoom_mode_var = tk.StringVar(value=settings.get("viewer_zoom_mode", "fit_width"))
        for label, value in (("Manual", "manual"), ("Fit Width", "fit_width"), ("Fit Page", "fit_page")):
            tk.Radiobutton(form, text=label, variable=self.zoom_mode_var, value=value).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=12)
            row += 1

        tk.Label(form, text="Viewer DPI (rendering quality - independent of zoom):").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self.viewer_dpi_var = tk.StringVar(value=str(settings.get("viewer_dpi", 200)))
        ttk.Combobox(form, textvariable=self.viewer_dpi_var,
                     values=["72", "96", "120", "150", "200", "300", "400"], width=10).grid(
            row=row, column=0, sticky="w", pady=3)
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        tk.Label(form, text="Image Extraction", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w")
        row += 1

        tk.Label(form, text="Image DPI (figures/equations only - never affects viewer zoom):").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self.dpi_var = tk.StringVar(value=str(settings.get("image_dpi", 200)))
        ttk.Combobox(form, textvariable=self.dpi_var, values=DPI_PRESETS, width=10).grid(
            row=row, column=0, sticky="w", pady=3)
        row += 1

        tk.Label(form, text="JPEG quality (1-100):").grid(row=row, column=0, sticky="w")
        self.quality_var = tk.IntVar(value=settings.get("jpeg_quality", 95))
        tk.Spinbox(form, from_=1, to=100, textvariable=self.quality_var, width=10).grid(row=row, column=1, sticky="w", pady=3)
        row += 1

        self.bg_remove_var = tk.BooleanVar(value=settings.get("remove_image_background", False))
        tk.Checkbutton(form, text="Automatically remove image background (replace with white)",
                       variable=self.bg_remove_var).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        tk.Label(form, text="Auto Zone confidence thresholds (%)", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        az_thresholds = settings.get("auto_zone_thresholds", {"high": 90, "medium": 75})
        tk.Label(form, text="High confidence at or above:").grid(row=row, column=0, sticky="w")
        self.az_high_var = tk.IntVar(value=az_thresholds.get("high", 90))
        tk.Spinbox(form, from_=0, to=100, textvariable=self.az_high_var, width=10).grid(
            row=row, column=1, sticky="w", pady=3)
        row += 1
        tk.Label(form, text="Medium confidence at or above:").grid(row=row, column=0, sticky="w")
        self.az_medium_var = tk.IntVar(value=az_thresholds.get("medium", 75))
        tk.Spinbox(form, from_=0, to=100, textvariable=self.az_medium_var, width=10).grid(
            row=row, column=1, sticky="w", pady=3)
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        tk.Label(form, text="BITS / JATS validation", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        tk.Label(form, text="BITS 2.2 DTD file (blank = profiles/BITS/dtd/):").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self.bits_dtd_var = tk.StringVar(value=settings.get("bits_dtd_path", ""))
        tk.Entry(form, textvariable=self.bits_dtd_var, width=42).grid(row=row, column=0, sticky="w", pady=3)

        def _browse_dtd():
            from tkinter import filedialog
            path = filedialog.askopenfilename(title="Choose the BITS book DTD",
                                              filetypes=[("DTD files", "*.dtd"), ("All files", "*.*")])
            if path:
                self.bits_dtd_var.set(path)

        tk.Button(form, text="Browse...", command=_browse_dtd).grid(row=row, column=1, sticky="w", pady=3)
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        self.debug_var = tk.BooleanVar(value=settings.get("debug_logging", False))
        tk.Checkbutton(form, text="Debug logging (console)", variable=self.debug_var).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        tk.Label(form, text="Index Tag Shortcuts", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        tk.Label(form, text="Retags the SELECTED Index zone. Format: <Control-Key-1>",
                 fg="#666666").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1
        self.index_shortcut_vars = {}
        for label, key in (("Index Primary", "index_shortcut_primary"),
                            ("Index Secondary", "index_shortcut_secondary"),
                            ("Index Tertiary", "index_shortcut_tertiary"),
                            ("Reset (if supported)", "index_shortcut_reset")):
            tk.Label(form, text=label + ":").grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=settings.get(key, DEFAULT_SETTINGS.get(key, "")))
            tk.Entry(form, textvariable=var, width=24).grid(row=row, column=1, sticky="w", pady=3)
            self.index_shortcut_vars[key] = var
            row += 1

        # Size to content, but never larger than the available screen (req.
        # 6/7) - measured AFTER every widget above is built, so this
        # reflects the real required height, then clamped the same way
        # gui/main_window.py's own App window already clamps itself
        # against a small display (same established pattern, not a new
        # one). minsize keeps the dialog from ever being shrunk below a
        # usable width/height once the user resizes it, while still
        # allowing scrolling to reach content the window itself is too
        # short to show in full.
        self.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        content_w = max(form.winfo_reqwidth() + vsb.winfo_reqwidth() + 24, 420)
        content_h = form.winfo_reqheight() + btns.winfo_reqheight() + 20
        win_w = min(content_w, screen_w - 80)
        win_h = min(content_h, screen_h - 80)
        self.geometry(f"{int(win_w)}x{int(win_h)}")
        self.minsize(360, 240)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        try:
            image_dpi = max(1, int(float(self.dpi_var.get())))
        except (TypeError, ValueError):
            image_dpi = 200
        try:
            viewer_dpi = max(1, int(float(self.viewer_dpi_var.get())))
        except (TypeError, ValueError):
            viewer_dpi = 200
        self.result = {
            "prefix": self.prefix_var.get().strip() or "document",
            "jpeg_quality": max(1, min(100, int(self.quality_var.get() or 95))),
            "root_tag": self.root_tag_var.get().strip() or "book",
            "image_dpi": image_dpi,
            "viewer_dpi": viewer_dpi,
            "viewer_zoom_mode": self.zoom_mode_var.get(),
            "remove_image_background": bool(self.bg_remove_var.get()),
            "debug_logging": bool(self.debug_var.get()),
            "auto_zone_thresholds": {
                "high": max(0, min(100, int(self.az_high_var.get() or 90))),
                "medium": max(0, min(100, int(self.az_medium_var.get() or 75))),
            },
            "bits_dtd_path": self.bits_dtd_var.get().strip(),
        }
        for key, var in self.index_shortcut_vars.items():
            value = var.get().strip()
            # An invalid/malformed Tk bind sequence is validated at BIND
            # time (App._apply_index_tag_shortcuts already catches
            # tk.TclError there and simply skips it, never fatal) - kept
            # here too, so a typo'd sequence never gets silently swapped
            # back to the default without the user noticing why.
            self.result[key] = value or DEFAULT_SETTINGS.get(key, "")
        self._on_close()


class SplitChildTagDialog(tk.Toplevel):
    """Shown when Horizontal Split's parent tag has ambiguous child type."""
    def __init__(self, parent, parent_tag: str):
        super().__init__(parent)
        self.title("Select Split Child Tag")
        self.resizable(False, False)
        self.result = None

        choices = SPLIT_CHILD_CHOICES.get(parent_tag) or [SPLIT_CHILD_TAG.get(parent_tag, "p")]
        tk.Label(self, text=f"Parent zone tag: {parent_tag}\nChoose the child tag to create:",
                 padx=12, pady=8, justify="left").pack(anchor="w")

        self.choice_var = tk.StringVar(value=choices[0])
        for c in choices:
            tk.Radiobutton(self, text=c, variable=self.choice_var, value=c).pack(anchor="w", padx=20)

        btns = tk.Frame(self, pady=10)
        btns.pack()
        tk.Button(btns, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        self.result = self.choice_var.get()
        self.destroy()


_INVALID_FOLDER_CHARS = '<>:"/\\|?*'


class OutputFolderNameDialog(tk.Toplevel):
    """Shown at Open-PDF time, BEFORE the PDF file-selection dialog (spec:
    "1. User clicks Open PDF. 2. Ask for output folder name. 3. User
    enters folder name. 4. Then allow PDF selection.") - one folder name
    per PDF/project, so every component (frontmatter section, chapter,
    ...) later generated from this PDF writes into the SAME shared
    output/{name}/ folder with a single shared images/ subfolder, instead
    of each component creating its own top-level folder (the confirmed
    real bug this whole feature exists to fix - see
    core.component_output.ComponentOutputManager's own docstring).

    Offers two modes (spec: "opening multiple PDFs must not automatically
    create unnecessary new folders... provide [Create New Folder] /
    [Select Existing Folder]"): the original free-text "Create New
    Folder" entry, or a "Select Existing Folder" listbox of this app's own
    output_root subfolders - picking one reuses it directly (self.result
    is just the folder-NAME string either way, so the caller's existing
    ComponentOutputManager(output_root / name, ...) + ensure_dirs()
    already reuses it safely with no other code change needed).

    self.result is the resulting folder-name string, or None if the user
    cancelled - the caller (App.open_pdf) treats a None result as
    aborting Open PDF entirely, since a folder choice is a hard
    precondition, never an optional/skippable step."""

    def __init__(self, parent, initial: str = "", output_root=None):
        super().__init__(parent)
        self.title("Output Folder")
        self.resizable(False, False)
        self.result = None
        self._output_root = output_root

        self.mode_var = tk.StringVar(value="new")
        mode_frame = tk.Frame(self, padx=12)
        mode_frame.pack(anchor="w", fill="x", pady=(10, 4))
        tk.Radiobutton(mode_frame, text="Create New Folder", variable=self.mode_var, value="new",
                       command=self._on_mode_change).pack(anchor="w")
        tk.Radiobutton(mode_frame, text="Select Existing Folder", variable=self.mode_var, value="existing",
                       command=self._on_mode_change).pack(anchor="w")

        self._new_frame = tk.Frame(self, padx=12)
        tk.Label(self._new_frame, text="Enter output folder name:", justify="left").pack(anchor="w", pady=(2, 4))
        tk.Label(self._new_frame, text="All XHTML files and images for this PDF will be saved directly\n"
                                        "inside this one folder (with a single shared images/ subfolder).",
                 justify="left", fg="gray30").pack(anchor="w", pady=(0, 6))
        self.name_var = tk.StringVar(value=initial)
        self._name_entry = tk.Entry(self._new_frame, textvariable=self.name_var, width=36)
        self._name_entry.pack(fill="x", pady=(0, 4))
        self._name_entry.bind("<Return>", lambda _e: self._ok())

        self._existing_frame = tk.Frame(self, padx=12)
        tk.Label(self._existing_frame, text="Select an existing output folder to reuse:",
                 justify="left").pack(anchor="w", pady=(2, 4))
        existing_names = []
        if output_root is not None and os.path.isdir(output_root):
            existing_names = sorted(p.name for p in Path(output_root).iterdir() if p.is_dir())
        list_row = tk.Frame(self._existing_frame)
        list_row.pack(fill="both", expand=True, pady=(0, 6))
        self._listbox = tk.Listbox(list_row, height=6, exportselection=False)
        self._listbox.pack(side="left", fill="both", expand=True)
        list_scroll = tk.Scrollbar(list_row, command=self._listbox.yview)
        list_scroll.pack(side="right", fill="y")
        self._listbox.configure(yscrollcommand=list_scroll.set)
        self._has_existing = bool(existing_names)
        if self._has_existing:
            for name in existing_names:
                self._listbox.insert("end", name)
        else:
            self._listbox.insert("end", "(no existing folders yet)")
            self._listbox.configure(state="disabled")

        # Establish the on-screen ordering (mode toggle, then whichever
        # mode's own controls, then error/buttons below) before the
        # error label/button row are packed, so switching modes later
        # never reorders anything already visible beneath them.
        self._on_mode_change()

        self.error_label = tk.Label(self, text="", fg="red", padx=12, justify="left")
        self.error_label.pack(anchor="w")

        btns = tk.Frame(self, pady=10)
        btns.pack()
        tk.Button(btns, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self._name_entry.focus_set()
        self._name_entry.select_range(0, "end")
        self.bind("<Escape>", lambda _e: self.destroy())

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _on_mode_change(self):
        if self.mode_var.get() == "new":
            self._existing_frame.pack_forget()
            self._new_frame.pack(anchor="w", fill="x")
        else:
            self._new_frame.pack_forget()
            self._existing_frame.pack(anchor="w", fill="both", expand=True)
        if hasattr(self, "error_label"):
            self.error_label.configure(text="")

    def _ok(self):
        if self.mode_var.get() == "new":
            name = self.name_var.get().strip()
            if not name:
                self.error_label.configure(text="Please enter a folder name.")
                return
            if any(c in _INVALID_FOLDER_CHARS for c in name):
                self.error_label.configure(text=f"Folder name cannot contain: {_INVALID_FOLDER_CHARS}")
                return
            self.result = name
        else:
            if not self._has_existing:
                self.error_label.configure(text="No existing folders yet - choose Create New Folder instead.")
                return
            sel = self._listbox.curselection()
            if not sel:
                self.error_label.configure(text="Please select an existing folder.")
                return
            self.result = self._listbox.get(sel[0])
        self.destroy()


class MergeModeDialog(tk.Toplevel):
    """Shown by Merge Previous (toolbar button and context menu) - spec
    98.17-98.19: lets the user choose whether the previous zone's text and
    this zone's text should be joined with a space ("James" + "Joyce" ->
    "James Joyce") or without one ("inter" + "national" -> "international").
    self.result is " ", "" or None (cancelled - no merge happens).

    Also shows WHICH zone was found as the continuation candidate and WHY
    (spec: "Find the previous logical continuation candidate. Show the
    candidate. Show why it was selected. Allow the user to confirm.") -
    page/tag/reading-order for both zones, plus a plain-language reason,
    not just a bare truncated text preview - so the user can catch a wrong
    candidate (e.g. an unrelated zone the search landed on) before
    confirming, rather than only after generating and re-reading output.

    confidence (spec: "EPUBForge - URGENT ZONING FIX - PREVIOUS-PARAGRAPH
    MERGE PROBLEM", sections 11/12/30 - core.paragraph_merge.
    continuation_confidence, the same text-continuity evidence the
    automatic engine uses): when None, the dialog behaves exactly as
    before (both Merge buttons always enabled - used for a tag/attribute
    context where confidence isn't meaningful). When given and BELOW
    LOW_CONFIDENCE_THRESHOLD, a warning replaces the normal join buttons
    with FORCE MERGE equivalents (spec: "If confidence is very low: Do
    not merge automatically. Show: 'Previous zone appears to be a
    separate paragraph.' The user can then explicitly force the merge if
    desired.") - the underlying core.zone_manager.ZoneManager.
    merge_with_previous call is IDENTICAL either way (tag compatibility
    is its own separate, always-enforced precondition); this dialog only
    ever adds or removes UI friction before making that same call."""
    LOW_CONFIDENCE_THRESHOLD = 0.60

    def __init__(self, parent, prev_preview: str = "", current_preview: str = "",
                 prev_info: str = "", current_info: str = "", reason: str = "",
                 confidence: float = None):
        super().__init__(parent)
        self.title("Merge Previous")
        self.resizable(False, False)
        self.result = None
        low_confidence = confidence is not None and confidence < self.LOW_CONFIDENCE_THRESHOLD

        header = f"Merge with:\n  {prev_info}\n  {prev_preview[:60]!r}\n\n" if prev_info else \
            f"Previous: {prev_preview[:40]!r}\n"
        header += f"Current:\n  {current_info}\n  {current_preview[:60]!r}\n\n" if current_info else \
            f"Current:  {current_preview[:40]!r}\n\n"
        if reason:
            header += f"Reason: {reason}\n\n"
        if confidence is not None:
            header += f"Confidence: {confidence:.0%}\n\n"
        if low_confidence:
            header += "Previous zone appears to be a separate paragraph.\n\n"
        label_kwargs = {"fg": "#B00000"} if low_confidence else {}
        tk.Label(self, text=header + "Join the previous zone's text with this zone's text:",
                 padx=12, pady=8, justify="left", **label_kwargs).pack(anchor="w")

        btns = tk.Frame(self, pady=10)
        btns.pack()
        space_label = "Force Merge (Space)" if low_confidence else "Merge with Space"
        nospace_label = "Force Merge (No Space)" if low_confidence else "Merge without Space"
        tk.Button(btns, text=space_label, width=20, command=lambda: self._choose(" ")).pack(side="left", padx=4)
        tk.Button(btns, text=nospace_label, width=22, command=lambda: self._choose("")).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=8, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _choose(self, join: str):
        self.result = join
        self.destroy()


class ZoneInfoDialog(tk.Toplevel):
    """Section 36: shows tag/page/bbox/parent/level/text/formatting; allows
    changing tag or parent (right-click 'Change Tag' / 'Change Parent').

    Fixed/maximum-reasonable size (capped to the screen, never grows with
    zone text length - see __init__'s geometry calc), with the
    informational/edit rows inside a vertically scrollable Canvas+Frame
    (same established pattern gui/zone_panel.py's TagPanel already uses
    for its own scrollable tag-button list, reused here rather than a
    second/different scrolling implementation), and the action buttons in
    their own fixed bottom bar OUTSIDE the scroll area so they're always
    reachable regardless of how much the zone's text scrolls. The zone
    text itself is a real read-only Text widget (word-wrap + its own
    vertical scrollbar) instead of an unbounded Label, so a very long
    zone's text can never be what makes the dialog itself grow."""
    def __init__(self, parent, app, zone_id: str):
        super().__init__(parent)
        self.app = app
        self.zone_id = zone_id
        self.title(f"Zone {zone_id}")

        zm = app.zone_manager
        zone = zm.zones[zone_id]

        # Fixed/maximum-reasonable size, capped to the actual screen so it
        # stays fully on-screen and usable on smaller displays (1366x768)
        # while giving more room on larger ones (1920x1080) - resizable
        # (not a hard-locked size) so the user can still shrink/grow it,
        # but it never auto-grows past this just because a zone's text is
        # long, since the text lives in its own scrollable widget below.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(560, screen_w - 80)
        height = min(640, screen_h - 100)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(420, 340)
        self.resizable(True, True)

        # ---- fixed bottom button bar (packed FIRST so it reserves its own
        # space before the expanding scroll area below claims the rest -
        # same ordering reason gui/zone_panel.py's TagPanel documents for
        # its own bottom-packed "Deselect Tag" button) ----
        btns = tk.Frame(self)
        btns.pack(side="bottom", fill="x", pady=8)
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        tk.Button(btns, text="Apply", width=10, command=self._apply).pack(side="left", padx=4)
        tk.Button(btns, text="Duplicate", width=10, command=self._duplicate).pack(side="left", padx=4)
        tk.Button(btns, text="Delete", width=10, command=self._delete).pack(side="left", padx=4)
        tk.Button(btns, text="Close", width=10, command=self.destroy).pack(side="left", padx=4)

        # ---- scrollable content area ----
        outer = tk.Frame(self)
        outer.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        form = tk.Frame(canvas, padx=12, pady=12)
        window_id = canvas.create_window((0, 0), window=form, anchor="nw")

        def _on_form_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(window_id, width=event.width)

        form.bind("<Configure>", _on_form_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        # Bound on the canvas/frame/each info label (NOT on the Text
        # widget below, which has its own native wheel-scroll for its own
        # content) - deliberately no nested/competing scroll handling.
        canvas.bind("<MouseWheel>", _on_wheel)
        form.bind("<MouseWheel>", _on_wheel)
        form.grid_columnconfigure(1, weight=1)

        def row(r, label, value):
            tk.Label(form, text=label, anchor="w", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w")
            lbl = tk.Label(form, text=value, anchor="w", wraplength=380, justify="left")
            lbl.grid(row=r, column=1, sticky="w")
            lbl.bind("<MouseWheel>", _on_wheel)
            return lbl

        row(0, "Tag:", zone.tag)
        row(1, "Page:", zone.page)
        row(2, "BBox:", "{:.1f}, {:.1f}, {:.1f}, {:.1f}".format(*zone.bbox))
        row(3, "Parent:", zone.parent_id or "None")
        row(4, "Level:", zone.level)
        row(5, "Reading Order:", zone.serial if zone.serial is not None else "-")

        text_label = tk.Label(form, text="Text:", anchor="w", font=("Segoe UI", 9, "bold"))
        text_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 2))
        text_label.bind("<MouseWheel>", _on_wheel)
        text_frame = tk.Frame(form)
        text_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        text_widget = tk.Text(text_frame, height=8, wrap="word", font=("Segoe UI", 9))
        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text_widget.yview)
        text_widget.configure(yscrollcommand=text_scroll.set)
        text_widget.insert("1.0", zone.text or "(none)")
        text_widget.configure(state="disabled")
        text_widget.pack(side="left", fill="both", expand=True)
        text_scroll.pack(side="right", fill="y")

        fmt = {}
        try:
            if app.pdf_document and zone.tag not in ("figure", "graphic", "equation"):
                from core import text_extractor
                page = app.pdf_document.get_page(zone.page)
                fmt = text_extractor.detect_zone_formatting(page, zone.bbox)
        except Exception:
            fmt = {}
        fmt_text = ", ".join(f"{k}={'Yes' if v else 'No'}" for k, v in fmt.items()) or "n/a"
        row(8, "Formatting:", fmt_text)

        change_tag_label = tk.Label(form, text="Change Tag:", font=("Segoe UI", 9, "bold"))
        change_tag_label.grid(row=9, column=0, sticky="w", pady=(10, 0))
        change_tag_label.bind("<MouseWheel>", _on_wheel)
        # Profile-aware (see gui/main_window.py App.set_profile): offers
        # whatever tag set the currently active Profile defines, not always
        # the XML profile's constants.TAG_BUTTONS - falls back to that
        # import only if the app somehow has no active list yet.
        active_buttons = getattr(app, "active_tag_buttons", None) or TAG_BUTTONS
        tag_names = [t for _, t, _ in active_buttons]
        self.tag_var = tk.StringVar(value=zone.tag)
        ttk.Combobox(form, textvariable=self.tag_var, values=sorted(set(tag_names)), width=20, state="readonly").grid(
            row=9, column=1, sticky="w", pady=(10, 0))

        change_parent_label = tk.Label(form, text="Change Parent (zone id or blank):", font=("Segoe UI", 9, "bold"))
        change_parent_label.grid(row=10, column=0, sticky="w")
        change_parent_label.bind("<MouseWheel>", _on_wheel)
        self.parent_var = tk.StringVar(value=zone.parent_id or "")
        tk.Entry(form, textvariable=self.parent_var, width=22).grid(row=10, column=1, sticky="w")

        self.transient(parent)
        self.grab_set()

    def _apply(self):
        zm = self.app.zone_manager
        new_tag = self.tag_var.get()
        if new_tag != zm.zones[self.zone_id].tag:
            page_marker_tags = self.app.active_profile.get("page_marker_tags")
            zm.set_tag(self.zone_id, new_tag, page_marker_tags=page_marker_tags)
        new_parent = self.parent_var.get().strip() or None
        parent_changed = new_parent != zm.zones[self.zone_id].parent_id
        if parent_changed:
            if new_parent is None or new_parent in zm.zones:
                if not zm.set_parent(self.zone_id, new_parent):
                    from tkinter import messagebox
                    messagebox.showerror(
                        "Change Parent", "That would make the zone its own ancestor (a cycle) - rejected.")
            else:
                from tkinter import messagebox
                messagebox.showerror("Change Parent", f"No such zone: {new_parent}")
        # A tag-only change must NOT disturb Reading Order (spec: "ZoneTool
        # - Final Fix - Preserve Column Zoning Sequence During Manual
        # Zoning": "Changing tag ... must NOT change the physical Reading
        # Order unless the user explicitly requests a Reading Order
        # recalculation") - a zone's logical/hierarchy tag has no bearing
        # on its physical page position. A PARENT change still gets the
        # full recompute (it can change which zones count as top-level).
        self.app.on_zones_changed(recompute_reading_order=parent_changed)
        self.destroy()

    def _duplicate(self):
        self.app.zone_manager.duplicate_zone(self.zone_id)
        self.app.on_zones_changed()
        self.destroy()

    def _delete(self):
        self.app.zone_manager.delete_zone(self.zone_id)
        self.app.on_zones_changed()
        self.destroy()


class HyphenReviewDialog(tk.Toplevel):
    """Post-zoning "Hyphen Normalization Review" - shown (by
    gui/main_window.py, right before XML generation) only when
    core.text_extractor.find_all_hyphen_candidates found at least one
    genuine line-break-hyphen candidate anywhere in the project. Each
    candidate is a specific (zone, boundary_index) pair - see
    text_extractor.dehyphenate_join's `keep_at` parameter - already
    filtered to exclude same-line compound hyphens (those never form a
    fragment boundary at all, so text_extractor never even considers
    them). CHECKED (the default, matching the prior automatic-join
    behavior) means "remove this line-break hyphen"; UNCHECKED means
    "keep it" - written into that zone's own
    attributes["hyphen_keep_boundaries"] only when Apply is pressed
    (self.result becomes True); Cancel (self.result stays False) discards
    any checkbox edits made in this dialog session, leaving already-
    persisted overrides untouched either way."""

    def __init__(self, parent, zone_manager, pdf_document):
        super().__init__(parent)
        self.title("Hyphen Normalization Review")
        self.geometry("620x480")
        self.result = False
        self.zm = zone_manager

        self.candidates = text_extractor.find_all_hyphen_candidates(zone_manager, pdf_document)
        self.vars = []  # (candidate, tk.BooleanVar) in display order

        tk.Label(self, text="Uncheck any word that is NOT a real line-break hyphen "
                             "(it will be kept as written).",
                 anchor="w", justify="left", wraplength=590).pack(fill="x", padx=10, pady=(10, 4))

        list_frame = tk.Frame(self)
        list_frame.pack(side="top", fill="both", expand=True, padx=10, pady=4)
        canvas = tk.Canvas(list_frame, highlightthickness=0)
        vbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        container = tk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=container, anchor="nw")

        def _on_container_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(window_id, width=event.width)

        container.bind("<Configure>", _on_container_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        canvas.bind("<MouseWheel>", _on_wheel)
        container.bind("<MouseWheel>", _on_wheel)

        for cand in self.candidates:
            zone = zone_manager.zones.get(cand["zone_id"])
            if cand.get("chain_boundary"):
                already_kept = zone is not None and bool(zone.attributes.get("hyphen_keep_chain_boundary"))
            else:
                already_kept = zone is not None and cand["boundary_index"] in \
                    set(zone.attributes.get("hyphen_keep_boundaries", []))
            var = tk.BooleanVar(value=not already_kept)  # checked = remove hyphen (default)
            row = tk.Frame(container)
            row.pack(fill="x", padx=2, pady=1)
            text = f'{cand["prefix_word"]} + {cand["suffix_word"]}  →  {cand["joined_preview"]}'
            cb = tk.Checkbutton(row, text=text, variable=var, anchor="w", justify="left")
            cb.pack(side="left", fill="x", expand=True)
            tag = zone.tag if zone is not None else "?"
            tk.Label(row, text=f'p.{cand["page"]}  [{tag}]', fg="#666666",
                     font=("Segoe UI", 8)).pack(side="right", padx=(4, 8))
            cb.bind("<MouseWheel>", _on_wheel)
            row.bind("<MouseWheel>", _on_wheel)
            self.vars.append((cand, var))

        if not self.candidates:
            tk.Label(container, text="No line-break-hyphen candidates found.",
                     fg="#666666").pack(padx=4, pady=8)

        btns = tk.Frame(self)
        btns.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        tk.Button(btns, text="Select All", command=self._select_all).pack(side="left", padx=2)
        tk.Button(btns, text="Unselect All", command=self._unselect_all).pack(side="left", padx=2)
        tk.Button(btns, text="Apply", width=10, command=self._apply).pack(side="right", padx=2)
        tk.Button(btns, text="Cancel", width=10, command=self._cancel).pack(side="right", padx=2)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _select_all(self):
        for _, var in self.vars:
            var.set(True)

    def _unselect_all(self):
        for _, var in self.vars:
            var.set(False)

    def _apply(self):
        # Group edits per zone so each zone's attributes dict is only
        # rewritten once, in boundary_index order, never leaving a stale
        # duplicate or losing an index from a previous review session that
        # this dialog run didn't touch (a zone with candidates from an
        # earlier PDF layout change keeps whatever wasn't re-detected now).
        per_zone_keep = {}
        for cand, var in self.vars:
            zone = self.zm.zones.get(cand["zone_id"])
            if zone is None:
                continue
            if cand.get("chain_boundary"):
                # A boolean flag on the LATER zone - it can be the "second
                # half" of at most one merge-chain boundary, so no index
                # set is needed here (contrast the within-zone case below).
                if var.get():
                    zone.attributes.pop("hyphen_keep_chain_boundary", None)  # checked = remove hyphen = not kept
                else:
                    zone.attributes["hyphen_keep_chain_boundary"] = True  # unchecked = keep hyphen
                continue
            keep = per_zone_keep.setdefault(
                zone.zone_id, set(zone.attributes.get("hyphen_keep_boundaries", [])))
            if var.get():
                keep.discard(cand["boundary_index"])  # checked = remove hyphen = not kept
            else:
                keep.add(cand["boundary_index"])  # unchecked = keep hyphen
        for zone_id, keep in per_zone_keep.items():
            zone = self.zm.zones[zone_id]
            if keep:
                zone.attributes["hyphen_keep_boundaries"] = sorted(keep)
            else:
                zone.attributes.pop("hyphen_keep_boundaries", None)
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()


# Ordered (label, tag) pairs for the Auto Analyse preview count table -
# mirrors the exact example layout in the feature request (Headings /
# Paragraphs / Numbered Lists / Bullet Lists / List Items / Tables /
# Table Captions / Figures / Page Number). "Headings" sums h1..h6 so the
# preview stays readable even when a page has several heading levels.
_AUTO_ANALYSE_COUNT_ROWS = [
    ("Headings", ("h1", "h2", "h3", "h4", "h5", "h6")),
    ("Paragraphs", ("p",)),
    ("Numbered/Alpha Lists", ("list",)),
    ("Bullet Lists", ("list-bullet",)),
    ("List Items", ("list-item",)),
    ("Tables", ("table",)),
    ("Table Captions", ("table-caption",)),
    ("Figures", ("figure",)),
    ("Page Number", ("pagenumber",)),
]


class AutoAnalysePreviewDialog(tk.Toplevel):
    """Auto Analyse's "preview before creating anything" step (gui/
    main_window.py's App.auto_analyse): shows the counts
    auto_zoning.page_analyzer.analyze_page found on the CURRENT page only,
    with [Cancel] [Apply] - nothing is written to the ZoneManager unless
    Apply is pressed (self.result becomes True; Cancel leaves it False and
    the caller creates nothing). Same established modal Toplevel pattern
    every other dialog in this file uses."""

    def __init__(self, parent, page_num: int, counts: dict):
        super().__init__(parent)
        self.title("Auto Analyse")
        self.resizable(False, False)
        self.result = False

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Auto Analysis Result - Page {page_num}",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        row = 1
        for label, tags in _AUTO_ANALYSE_COUNT_ROWS:
            total = sum(counts.get(t, 0) for t in tags)
            tk.Label(form, text=f"{label}:", anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 24))
            tk.Label(form, text=str(total), anchor="e").grid(row=row, column=1, sticky="e")
            row += 1

        if not counts:
            tk.Label(form, text="Nothing recognizable was found on this page.",
                      fg="#B71C1C").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1

        btns = tk.Frame(form)
        btns.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btns, text="Apply", width=10, command=self._apply,
                  state="normal" if counts else "disabled").pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _apply(self):
        self.result = True
        self.destroy()


class AutoAnalyseConflictDialog(tk.Toplevel):
    """Shown by App.auto_analyse() only when the current page already has
    zones and EVERY one of them is a prior Auto Analyse/Auto Zone/OCR
    result (attributes["source"] in ("auto","ocr")) - never for manual zones, which
    App.auto_analyse() refuses to touch outright before this dialog is
    even reached. self.result is one of "cancel" (default), "keep", or
    "replace"."""

    def __init__(self, parent, page_num: int, existing_count: int):
        super().__init__(parent)
        self.title("Auto Analyse")
        self.resizable(False, False)
        self.result = "cancel"

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Automatic zones already exist on page {page_num} "
                             f"({existing_count} zone(s)). Replace them?",
                 wraplength=340, justify="left").pack(anchor="w")

        btns = tk.Frame(form)
        btns.pack(pady=(14, 0))
        tk.Button(btns, text="Cancel", width=14, command=self.destroy).pack(side="left", padx=4)
        tk.Button(btns, text="Keep Existing", width=14, command=self._keep).pack(side="left", padx=4)
        tk.Button(btns, text="Replace Auto Zones", width=16, command=self._replace).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _keep(self):
        self.result = "keep"
        self.destroy()

    def _replace(self):
        self.result = "replace"
        self.destroy()


class IndexAutoZonePreviewDialog(tk.Toplevel):
    """Auto Zone Index's "preview before creating anything" step (spec 13 -
    "INDEX AUTO-ZONE PREVIEW... Do not modify the project until Apply is
    clicked"), shown by gui/main_window.py's App.auto_zone_index. Same
    established "nothing is written to ZoneManager unless Apply is pressed"
    Toplevel pattern as AutoAnalysePreviewDialog above - self.result stays
    False (the default) unless the user explicitly clicks Apply."""

    def __init__(self, parent, page_num: int, result):
        super().__init__(parent)
        self.title("Auto Zone Index Preview")
        self.resizable(False, False)
        self.result = False

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Index Auto-Zone Preview - Page {page_num}",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        rows = [("Columns detected", str(result.columns_detected)),
                ("Entries detected", str(result.total_entries))]
        for level in sorted(result.entries_by_level):
            rows.append((f"Level {level}", str(result.entries_by_level[level])))
        rows.append(("Wrapped entries", str(result.wrapped_count)))
        rows.append(("Page references", str(result.page_reference_count)))
        if result.cross_reference_count:
            rows.append(("Cross references (see / see also)", str(result.cross_reference_count)))
        if result.outlier_count:
            rows.append(("Outlier entries (folded into nearest level)", str(result.outlier_count)))
        if result.alphabetic_headings_skipped:
            rows.append(("Alphabetic section headings skipped (A/B/C...)",
                          str(result.alphabetic_headings_skipped)))
        rows.append(("Average confidence", f"{result.avg_confidence:.0f}%"))

        row = 1
        for label, value in rows:
            tk.Label(form, text=f"{label}:", anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 24), pady=1)
            tk.Label(form, text=value, anchor="e").grid(row=row, column=1, sticky="e", pady=1)
            row += 1

        if result.indentation_by_level:
            tk.Label(form, text="Detected indentation:", anchor="w",
                     font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
            row += 1
            for level, x0 in result.indentation_by_level.items():
                tk.Label(form, text=f"    Level {level}", anchor="w").grid(row=row, column=0, sticky="w")
                tk.Label(form, text=f"x≈{x0:.0f}", anchor="e").grid(row=row, column=1, sticky="e")
                row += 1

        if result.warnings:
            for w in result.warnings:
                tk.Label(form, text=w, fg="#B71C1C", wraplength=340, justify="left").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
                row += 1

        btns = tk.Frame(form)
        btns.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btns, text="Apply", width=10, command=self._apply,
                  state="normal" if result.predicted_zones else "disabled").pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _apply(self):
        self.result = True
        self.destroy()


class IndexAutoZoneConflictDialog(tk.Toplevel):
    """Shown by App.auto_zone_index() only when the target page already has
    zones whose attributes["source"] == "auto_index" (a prior Auto Zone
    Index run) - never for a manual/foreign zone, which App.auto_zone_index
    refuses to touch outright before this dialog is even reached (spec 11/
    12). self.result is one of "cancel" (default), "keep", or "replace";
    Replace only ever removes the auto_index zones themselves (spec:
    "remove ONLY source='auto_index' zones... Never remove manual zones...
    unrelated zones... page-number zones... image zones... other tags")."""

    def __init__(self, parent, page_num: int, existing_count: int):
        super().__init__(parent)
        self.title("Auto Zone Index")
        self.resizable(False, False)
        self.result = "cancel"

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Index zones already exist on page {page_num} "
                             f"({existing_count} zone(s)). Replace existing Auto Index zones?",
                 wraplength=340, justify="left").pack(anchor="w")

        btns = tk.Frame(form)
        btns.pack(pady=(14, 0))
        tk.Button(btns, text="Cancel", width=14, command=self.destroy).pack(side="left", padx=4)
        tk.Button(btns, text="Keep Existing", width=14, command=self._keep).pack(side="left", padx=4)
        tk.Button(btns, text="Replace", width=14, command=self._replace).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _keep(self):
        self.result = "keep"
        self.destroy()

    def _replace(self):
        self.result = "replace"
        self.destroy()


class ParagraphAutoZonePreviewDialog(tk.Toplevel):
    """Paragraph Auto Zone's "preview before creating anything" step (gui/
    main_window.py's App.auto_zone_paragraph_current_page), same established
    "nothing is written to ZoneManager unless Apply is pressed" Toplevel
    pattern as AutoAnalysePreviewDialog/IndexAutoZonePreviewDialog above."""

    def __init__(self, parent, page_num: int, result):
        super().__init__(parent)
        self.title("Paragraph Auto Zone Preview")
        self.resizable(False, False)
        self.result = False

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Paragraph Auto Zone - Page {page_num}",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        row = 1
        tk.Label(form, text="Paragraphs detected:", anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 24))
        tk.Label(form, text=str(result.paragraphs_detected), anchor="e").grid(row=row, column=1, sticky="e")
        row += 1
        if result.lines_skipped_existing:
            tk.Label(form, text="Lines skipped (already zoned):", anchor="w").grid(
                row=row, column=0, sticky="w", padx=(0, 24))
            tk.Label(form, text=str(result.lines_skipped_existing), anchor="e").grid(row=row, column=1, sticky="e")
            row += 1

        for w in result.warnings:
            tk.Label(form, text=w, fg="#B71C1C", wraplength=340, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1

        btns = tk.Frame(form)
        btns.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btns, text="Apply", width=10, command=self._apply,
                  state="normal" if result.predicted_zones else "disabled").pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _apply(self):
        self.result = True
        self.destroy()


class BibliographySampleSelectDialog(tk.Toplevel):
    """Bibliography Auto Zone's own sample-selection step (spec: "User
    manually zones 3-4 bibliography entries... User selects those 3-4
    zones"). A dedicated, standalone multi-select list rather than
    switching the main zone tree's own selectmode to allow multi-select
    (gui/zone_panel.py's Treeview is "browse"/single-select everywhere
    else in the app - changing that shared, heavily-used widget's selection
    behavior for one feature risks subtly changing every other zone-
    selection interaction) - this dialog touches nothing but its own
    Listbox. Lists every zone on the given page; self.selected_zone_ids is
    a list of zone_id strings (empty/None on Cancel)."""

    def __init__(self, parent, page_num: int, zones: list):
        super().__init__(parent)
        self.title("Bibliography Auto Zone - Select Sample Entries")
        self.resizable(False, False)
        self.selected_zone_ids = None
        self._zones = zones

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Select 3-4 manually-zoned bibliography entries on page {page_num}\n"
                             f"to learn the pattern from (Ctrl/Shift-click for multiple):",
                 justify="left", anchor="w").pack(anchor="w", pady=(0, 8))

        list_frame = tk.Frame(form)
        list_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, width=70, height=min(12, max(4, len(zones))),
                                   yscrollcommand=scrollbar.set, exportselection=False)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for z in zones:
            snippet = (z.text or "").strip().replace("\n", " ")[:60]
            self.listbox.insert("end", f"[{z.tag}] {snippet}")

        if not zones:
            tk.Label(form, text="No zones exist on this page yet.", fg="#B71C1C").pack(anchor="w", pady=(8, 0))

        btns = tk.Frame(form)
        btns.pack(pady=(14, 0))
        tk.Button(btns, text="Use Selected", width=14, command=self._use_selected,
                  state="normal" if zones else "disabled").pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _use_selected(self):
        indices = self.listbox.curselection()
        self.selected_zone_ids = [self._zones[i].zone_id for i in indices]
        self.destroy()


class BibliographyScopeDialog(tk.Toplevel):
    """Shown after a bibliography pattern is successfully learned (spec 8:
    "After learning the bibliography pattern, provide: Current Page or
    Entire File"). self.result is one of "cancel" (default), "current",
    or "entire"."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Bibliography Auto Zone")
        self.resizable(False, False)
        self.result = "cancel"

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text="Pattern learned. Apply it to:", anchor="w").pack(anchor="w")

        btns = tk.Frame(form)
        btns.pack(pady=(14, 0))
        tk.Button(btns, text="Current Page", width=14, command=self._current).pack(side="left", padx=4)
        tk.Button(btns, text="Entire File", width=14, command=self._entire).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _current(self):
        self.result = "current"
        self.destroy()

    def _entire(self):
        self.result = "entire"
        self.destroy()


class BibliographyAutoZonePreviewDialog(tk.Toplevel):
    """Bibliography Auto Zone's "preview before creating anything" step
    (spec 10: "Before committing generated zones: show a visual preview...
    Detected zones / High-confidence zones / Review-required zones...
    [Apply] [Cancel]"). Same established Toplevel pattern as every other
    preview dialog in this file - self.result stays False unless Apply is
    explicitly pressed."""

    def __init__(self, parent, page_label: str, results: list):
        """`results`: list of (page_num, BibliographyAutoZoneResult) - one
        entry for Current Page scope, one per scanned page for Entire
        File scope."""
        super().__init__(parent)
        self.title("Bibliography Auto Zone Preview")
        self.resizable(False, False)
        self.result = False

        total_entries = sum(r.entries_detected for _, r in results)
        total_high = sum(r.high_confidence for _, r in results)
        total_review = sum(r.review_required for _, r in results)
        total_low = sum(r.low_confidence for _, r in results)
        pattern_changed_pages = [p for p, r in results if r.pattern_changed]

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)
        tk.Label(form, text=f"Bibliography Auto Zone - {page_label}",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # spec 34/51 (3-tier confidence): LOW-confidence candidates were
        # never added to predicted_zones at all (core.auto_zoning.
        # bibliography_auto_zone's own "never create a potentially
        # incorrect zone" rule) - shown here purely for visibility/review,
        # never counted into "Entries detected" (which only ever counts
        # zones actually about to be created).
        rows = [("Entries detected", str(total_entries)),
                ("High-confidence zones (>= 85%)", str(total_high)),
                ("Review-required zones (< 85%)", str(total_review))]
        if total_low:
            rows.append(("Low-confidence — not zoned, review manually", str(total_low)))
        row = 1
        for label, value in rows:
            tk.Label(form, text=f"{label}:", anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 24), pady=1)
            tk.Label(form, text=value, anchor="e").grid(row=row, column=1, sticky="e", pady=1)
            row += 1

        if pattern_changed_pages:
            pages_str = ", ".join(str(p) for p in pattern_changed_pages)
            tk.Label(form, text=f"Pattern changed — manual review required (page(s) {pages_str}).",
                     fg="#B71C1C", wraplength=360, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1

        if not total_entries:
            tk.Label(form, text="No remaining bibliography entries could be detected.",
                     fg="#B71C1C").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1

        btns = tk.Frame(form)
        btns.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btns, text="Apply", width=10, command=self._apply,
                  state="normal" if total_entries else "disabled").pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)

    def _apply(self):
        self.result = True
        self.destroy()


class TableGeneratorPreviewDialog(tk.Toplevel):
    """Table Generator (spec: "EPUBForge - Complete Semantic Auto-Zone +
    Table Generator" Part AC/BA) - shows the grid core.table_extractor.
    analyze_table ALREADY detects for the selected Table zone (the exact
    same call core/xml_generator.py's own _zone_table makes at Generate
    XML time - this dialog never re-implements detection, only previews
    its result before generation so a bad detection can be caught and
    corrected early, rather than only discovered afterward).

    READ-ONLY by design - this dialog never writes anything itself. The
    zone's own horizontal_splits/vertical_splits attributes (gui.pdf_viewer's
    existing Row/Column Split Mode, Ctrl+Shift+R/C) are the ONLY
    correction mechanism (spec Part AL/BA: "reuse existing controls... do
    not create a duplicate editing architecture") - this dialog just
    points the user at that EXISTING mode rather than reimplementing
    cell-boundary editing a second time. There is no Apply/Cancel -
    nothing here is committed; Close is the only outcome."""

    def __init__(self, parent, table_structure, has_manual_splits: bool):
        super().__init__(parent)
        self.title("Table Generator")
        self.resizable(False, False)

        form = tk.Frame(self, padx=16, pady=12)
        form.pack(fill="both", expand=True)

        rows = table_structure.rows
        n_rows = len(rows)
        n_cols = max((sum(c.colspan for c in row) for row in rows), default=0)
        source = "manually corrected (Row/Column Split Mode)" if has_manual_splits else "automatically detected"
        tk.Label(form, text=f"Detected Table Structure ({source})",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(form, text=f"{n_rows} row(s) x {n_cols} column(s)"
                             f"{'  (header row detected)' if table_structure.header_row_count else ''}",
                 anchor="w").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        grid_frame = tk.Frame(form, bd=1, relief="solid")
        grid_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        if not rows:
            tk.Label(grid_frame, text="No cells could be detected in this table zone.",
                     fg="#B71C1C", padx=8, pady=8).grid(row=0, column=0)
        for r, row in enumerate(rows):
            c = 0
            for cell in row:
                text = (cell.text or "").strip().replace("\n", " ")
                if len(text) > 24:
                    text = text[:21] + "..."
                span_note = f" [colspan={cell.colspan}]" if cell.colspan > 1 else ""
                lbl = tk.Label(grid_frame, text=(text or "(empty)") + span_note, anchor="w", justify="left",
                                wraplength=140, bd=1, relief="solid", padx=4, pady=3,
                                bg="#FFF8E1" if r < table_structure.header_row_count else "#FFFFFF")
                lbl.grid(row=r, column=c, columnspan=cell.colspan, sticky="nsew")
                c += cell.colspan

        tk.Label(form, text="To correct row/column boundaries, use the existing Row/Column Split "
                             "Mode (Ctrl+Shift+R / Ctrl+Shift+C) on this zone, then reopen Table Generator.",
                 fg="#666666", wraplength=420, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

        btns = tk.Frame(form)
        btns.grid(row=4, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btns, text="Close", width=10, command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()
        self.wait_window(self)
