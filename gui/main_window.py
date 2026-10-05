"""Assembles toolbar + tag panel + PDF viewer + zone tree + status bar, and
owns all shared application state and menu/keyboard wiring."""
import os
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

from core.pdf_loader import PDFDocument
from core.zone_manager import ZoneManager
from core import project_manager, xml_generator, validation, debug_log, text_extractor, reading_order
from core.bits import pipeline as bits_pipeline
from core import paragraph_merge
from core import autosave_service
from core import table_extractor
from core import profile_manager
from core import tag_normalizer
from gui.smart_auto_zone_ui import SmartAutoZoneUI
from core.component_output import ComponentOutputManager
from core import component_output
from core.resource_path import writable_root
from core.constants import DEFAULT_ZOOM, SPLIT_CHILD_CHOICES

from gui.toolbar import Toolbar
from gui.zone_panel import TagPanel, ZoneTreePanel
from gui.pdf_viewer import PDFViewerPanel
from gui.dialogs import (SettingsDialog, SplitChildTagDialog, ZoneInfoDialog, HyphenReviewDialog,
                          AutoAnalysePreviewDialog, AutoAnalyseConflictDialog, MergeModeDialog,
                          IndexAutoZonePreviewDialog, IndexAutoZoneConflictDialog,
                          ParagraphAutoZonePreviewDialog, BibliographySampleSelectDialog,
                          BibliographyScopeDialog, BibliographyAutoZonePreviewDialog,
                          TableGeneratorPreviewDialog, OutputFolderNameDialog)
from gui.ocr_dialogs import OCRProgressDialog, OCRSettingsDialog
from gui import theme
from core import ui_prefs

from auto_zoning import reference_loader, reference_analyzer, auto_zone_engine, layout_template
from auto_zoning import page_analyzer, hierarchy_builder, reading_order_engine
from auto_zoning import index_auto_zone
from auto_zoning import paragraph_auto_zone, bibliography_auto_zone

from core.ocr import ocr_service, page_classifier
from core.ocr.ocr_cache import OCRCache
from core.ocr import paddle_engine  # noqa: F401 - import-time @register call is the only thing needed from this

# Persistent user data (projects/output/assets/ocr_cache) always lives
# beside the real EXE (core.resource_path.writable_root - the same
# frozen-aware logic this module used to compute inline, now centralized
# there so core/profile_manager.py, core/cup_config.py, and core/ui_prefs.py
# all resolve their own frozen/dev paths through the same single place
# instead of each re-deriving it). Source/dev runs (python main.py) are
# completely unaffected.
APP_ROOT = Path(writable_root())

PROJECTS_DIR = APP_ROOT / "projects"
OUTPUT_DIR = APP_ROOT / "output"
ASSETS_DIR = APP_ROOT / "assets"
OCR_CACHE_DIR = APP_ROOT / "ocr_cache"
for _d in (PROJECTS_DIR, OUTPUT_DIR, ASSETS_DIR, OCR_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MIN_ZOOM, MAX_ZOOM, ZOOM_STEP = 0.25, 4.0, 1.15


class App:
    def __init__(self, root: tk.Tk, on_home=None):
        # `on_home`: optional callback wired by app.launcher.launcher_window
        # when Zoning is opened as one of the EPUBForge Feature Hub's three
        # modules (spec: "each reachable via [<- Home] returning to the
        # launcher without restarting") - unset for the plain `python
        # main.py` / run() entry point, which still works exactly as
        # before (no launcher involved, no Home button shown).
        self._on_home = on_home
        # Theme (spec 66.25) - loaded/applied before any widget exists, so
        # every panel's own __init__ reads the right palette from
        # theme.current on its first construction. The toggle itself
        # (App.toggle_theme) only persists the choice and asks for a
        # restart rather than re-theming a fully-built widget tree live -
        # see toggle_theme's own docstring for why.
        theme.current.set_mode(ui_prefs.load_ui_prefs().get("theme", "light"))
        theme.apply_ttk_style(root, theme.current.palette)

        self.root = root
        self.root.title("BITS Tool - PDF Zoning & BITS / JATS XML")
        # Responsive desktop layout (spec 66.24 - 1366x768 up to 2560x1440):
        # a hardcoded 1400x900 request is WIDER than the spec's own stated
        # minimum resolution and was confirmed (via direct screenshot
        # testing) to hang off the right/bottom edge of any display smaller
        # than that, silently clipping real content (including the header's
        # own Project/Profile badges) with no visual indication anything
        # was cut off. Capped to the actual screen size, with a small
        # margin so the window never exactly touches the screen edge.
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        win_w, win_h = min(1400, screen_w - 40), min(900, screen_h - 80)
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.minsize(1024, 640)
        self.root.configure(bg=theme.current.palette["app_bg"])

        self.pdf_document: PDFDocument | None = None
        self.pdf_path = None
        self.project_path = None
        self.page_count = 0
        self.current_page = 1
        self.zoom = DEFAULT_ZOOM
        self.zoom_mode = "fit_width"  # "manual" | "fit_width" | "fit_page" - independent of image_dpi
        self.zone_manager = ZoneManager()
        # Verification workflow (core/verification/) - None until the operator
        # actually opens the Verify window for this project (see
        # open_verification_window/generate_xhtml below). Existing projects,
        # and any project whose operator never uses this feature, behave
        # with ZERO change: generate_xhtml()'s own soft-gate only ever
        # triggers when this is not None.
        self.verification_session = None
        self.verification_window = None
        self.active_tag = None  # (tag, attrs) or None
        self.selected_zone_id = None
        self.settings = dict(project_manager.DEFAULT_SETTINGS)
        # Active Profile (Profile dropdown, toolbar) - see core/profile_manager.py.
        # active_tag_buttons/active_tag_colors are what TagPanel/ZoneInfoDialog's
        # "Change Tag" combobox actually read; switching profiles never deletes
        # or retags any existing zone, only which buttons are offered next.
        self.active_profile = profile_manager.get_profile(self.settings.get("profile", profile_manager.DEFAULT_PROFILE_NAME))
        self.active_tag_buttons = profile_manager.tag_buttons_of(self.active_profile)
        # Centralized, debounced, crash-safe autosave (spec: "ZONING -
        # CUPPEUB DEFAULT PROFILE + CONTINUOUS AUTOSAVE") - ONE service for
        # every project mutation, reused via self.mark_dirty(reason) below.
        # No-op until self.autosave.start(path) is called (open_pdf/
        # load_project/save_project), same "no path yet -> nothing to do"
        # guard the old auto_save_project() used to have.
        self.autosave = autosave_service.AutoSaveService(
            self.root, self._build_autosave_snapshot, self._on_autosave_status)
        # CUPEPUB-only: Tk bind sequences currently registered for its
        # keyboard shortcuts (core/cup_config.py's cup_shortcuts), so
        # set_profile can cleanly unbind them when switching to a different
        # profile instead of leaving stale bindings active.
        self._cup_shortcut_seqs = []
        # Index Primary/Secondary/Tertiary retag shortcuts (spec 1/7) - Tk
        # bind sequences currently registered from self.settings, tracked
        # the same way as _cup_shortcut_seqs above so they can be cleanly
        # unbound and rebound when the user changes them in Settings.
        self._index_shortcut_seqs = []
        self._resize_after_id = None
        # Transient UI-only validation state (see generate_xml/_clear_overlap_highlight)
        # - zone_ids currently flagged by validation.find_overlapping_zone_ids()
        # for the red highlight in the PDF viewer. Never written to zone
        # data or the project JSON, never affects XML generation - purely
        # what _draw_zone reads to decide whether to draw the highlight.
        self.overlapping_zone_ids = set()
        # Per-page VIEW/EDIT rotation (Page Rotation feature) - {page_number
        # (int): 0|90|180|270}, defaults to 0 for any page not present.
        # Purely a display/layout preference - never written into zone.bbox
        # (see gui/pdf_viewer.py's rotation-aware pdf_to_screen/
        # screen_to_pdf) or read by XML generation, so it never affects
        # generated output. Reset on open_pdf (a different document's pages
        # start fresh), restored from the project file on load_project,
        # persisted by save_project/auto_save_project.
        self.page_rotations = {}
        # Auto-Zoning reference state (see load_reference_project/auto_zone
        # below) - a list of per-reference-project layout_template.
        # LayoutTemplate objects (one per "Load Reference Project" click)
        # plus their merge (reference_template), recomputed on every load.
        # Deliberately NOT reset by open_pdf/load_project: a loaded
        # reference is meant to carry over to whatever new target PDF is
        # opened next - it is the whole point of the feature.
        self.reference_templates = []
        self.reference_template = None

        # OCR (core/ocr package) - settings persisted app-level like theme
        # (core.ui_prefs), never per-project. The cache directory follows
        # THIS module's own frozen-aware APP_ROOT (a packaged EXE's own
        # folder), not core.profile_manager's non-frozen-aware one, so
        # cached OCR results survive next to projects/output/assets in a
        # packaged build instead of landing inside the wiped PyInstaller
        # temp extraction directory.
        _prefs = ui_prefs.load_ui_prefs()
        self.ocr_settings = {
            "engine": _prefs.get("ocr_engine", "PaddleOCR"),
            "mode": _prefs.get("ocr_mode", "auto"),
            "language": _prefs.get("ocr_language", "en"),
            "dpi": _prefs.get("ocr_dpi", 300),
            "preprocessing": dict(_prefs.get("ocr_preprocessing", {})),
        }
        self.ocr_cache = OCRCache(str(OCR_CACHE_DIR))
        self._ocr_job_running = False

        palette = theme.current.palette

        # ---------------- header (spec 66.2) ----------------
        header = tk.Frame(root, bg=palette["header_bg"], height=40)
        header.pack(side=tk.TOP, fill=tk.X)
        header.pack_propagate(False)
        left = tk.Frame(header, bg=palette["header_bg"])
        left.pack(side=tk.LEFT, padx=14)
        if self._on_home is not None:
            home_btn = tk.Label(left, text="← Home", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                                 font=theme.FONT_BODY_BOLD, cursor="hand2", padx=6)
            home_btn.pack(side=tk.LEFT, padx=(0, 10))
            home_btn.bind("<Button-1>", lambda e: self._go_home())
            home_btn.bind("<Enter>", lambda e: home_btn.config(fg=palette["accent"]))
            home_btn.bind("<Leave>", lambda e: home_btn.config(fg=palette["header_fg_muted"]))
        tk.Label(left, text="◈ BITS Tool", bg=palette["header_bg"], fg=palette["header_fg"],
                  font=theme.FONT_APP_TITLE).pack(side=tk.LEFT)
        tk.Label(left, text="  PDF Zoning & Conversion", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                  font=theme.FONT_BODY).pack(side=tk.LEFT)
        right = tk.Frame(header, bg=palette["header_bg"])
        right.pack(side=tk.RIGHT, padx=14)
        self.profile_badge = tk.Label(right, text=profile_manager.DEFAULT_PROFILE_NAME, bg=palette["accent"], fg=palette["accent_fg"],
                                        font=theme.FONT_SMALL_BOLD, padx=8, pady=2)
        self.profile_badge.pack(side=tk.RIGHT, padx=(10, 0))
        tk.Label(right, text="Profile:", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                  font=theme.FONT_BODY).pack(side=tk.RIGHT)
        self.project_badge = tk.Label(right, text="No project loaded", bg=palette["header_bg"],
                                        fg=palette["header_fg"], font=theme.FONT_BODY)
        self.project_badge.pack(side=tk.RIGHT, padx=(0, 24))
        tk.Label(right, text="Project:", bg=palette["header_bg"], fg=palette["header_fg_muted"],
                  font=theme.FONT_BODY).pack(side=tk.RIGHT)

        # CUPEPUB Auto Zone / Auto Tag engine UI (gui/smart_auto_zone_ui.py) -
        # created before the toolbar, whose menus call into it.
        self.smart_az = SmartAutoZoneUI(self)

        self.toolbar = Toolbar(root, self)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)

        body = tk.Frame(root, bg=palette["app_bg"])
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Tags | PDF | Zone Hierarchy - ALL THREE resizable via the same
        # draggable-sash PanedWindow (spec 66.24), not just the right two as
        # before (Tags used to be a fixed-width side frame).
        self.paned = tk.PanedWindow(body, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=6,
                                     bg=palette["app_bg"])
        self.paned.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        paned = self.paned

        self.tag_panel = TagPanel(paned, self, self.active_tag_buttons, self.active_profile.get("tag_colors", {}),
                                   self.active_profile.get("tag_groups"))
        paned.add(self.tag_panel, minsize=180, width=220)

        self.viewer = PDFViewerPanel(paned, self)
        paned.add(self.viewer, stretch="always", minsize=300)

        self.zone_tree = ZoneTreePanel(paned, self)
        paned.add(self.zone_tree, stretch="always", minsize=240)
        self.right_panel_collapsed = False

        self.split_bar = tk.Frame(root, bg=palette["warning_tint"], highlightthickness=1,
                                    highlightbackground=palette["warning"])
        tk.Label(self.split_bar, text="Split: click inside the zone to add boundaries "
                                       "(Ctrl+Shift+V toggles horizontal/vertical).",
                 bg=palette["warning_tint"], fg=palette["text"], font=theme.FONT_BODY).pack(side=tk.LEFT, padx=6, pady=4)
        for txt, cmd in (("Confirm Split (Enter)", self._confirm_split), ("Clear Last (Backspace)", self._clear_last_split),
                          ("Cancel (Esc)", self._cancel_split)):
            b = tk.Button(self.split_bar, text=txt, command=cmd)
            theme.style_button(b, palette, kind="secondary")
            b.pack(side=tk.LEFT, padx=4, pady=4)

        status_frame = tk.Frame(root, bg=palette["panel_header_bg"], highlightthickness=1,
                                  highlightbackground=palette["border"])
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bar = tk.Label(status_frame, text="No PDF loaded", anchor="w", bg=palette["panel_header_bg"],
                                    fg=palette["text_muted"], font=theme.FONT_SMALL, padx=8, pady=4)
        self.status_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # UI-only branding - never written to the project JSON or the
        # generated XML. Packed after (and on the opposite side from) the
        # status label within the same bottom frame, so it stays pinned to
        # the bottom-right on any window resize without ever overlapping
        # or truncating the status text (status_bar's own fill=X/expand=True
        # simply shrinks to make room for it).
        self.branding_label = tk.Label(status_frame, text="Developed By Syncronic IT Solutions Pvt Ltd | Version 1.0.0",
                                        anchor="e", bg=palette["panel_header_bg"], fg=palette["text_faint"],
                                        font=theme.FONT_SMALL)
        self.branding_label.pack(side=tk.RIGHT, padx=(4, 8))
        # Autosave status indicator (spec section 33) - "Saved"/"Saving..."/
        # "Unsaved changes"/"Error", updated by AutoSaveService's own status
        # callback. Never a popup (spec: "Normal autosaves must not produce
        # popup dialogs").
        self.autosave_status_label = tk.Label(status_frame, text="Autosave: -", anchor="e",
                                               bg=palette["panel_header_bg"], fg=palette["text_muted"],
                                               font=theme.FONT_SMALL, padx=8)
        self.autosave_status_label.pack(side=tk.RIGHT, padx=(4, 8))

        self._bind_keys()
        self._apply_index_tag_shortcuts()
        self._bind_global_scroll_diagnostic()
        self.toolbar.set_undo_redo_enabled(False, False)
        self._build_context_menu()
        self.viewer.canvas.bind("<Configure>", self._on_viewer_resize)
        self.set_profile(self.settings.get("profile", profile_manager.DEFAULT_PROFILE_NAME), persist=False)
        # Standalone entry point (run() below) closes via the window's own
        # [X] button - flush any pending autosave first (spec section 28).
        # The Launcher's embedded entry point (app/launcher/launcher_window.
        # py._open_zoning) overrides this immediately after constructing
        # App with its own WM_DELETE_WINDOW -> _go_home, which also flushes
        # (see _go_home below) - so both entry points are covered either way.
        self.root.protocol("WM_DELETE_WINDOW", self._on_app_close)

        # Every keyboard shortcut above (_bind_keys' Ctrl+Z/Y/Delete/Escape,
        # and _apply_index_tag_shortcuts' Index Primary/Secondary/Tertiary/
        # Reset) is bound on/via self.root, which only receives key events
        # when self.root itself is Tk's currently-focused toplevel. A plain
        # tk.Tk() root (the old, direct `python main.py` entry point) gets
        # OS keyboard focus automatically on creation; a tk.Toplevel()
        # opened from the EPUBForge Launcher (app/launcher/launcher_window.
        # py's _open_zoning, AFTER withdrawing the launcher's own root)
        # does NOT reliably inherit that focus - confirmed as the real
        # cause of a reported regression where Index-tag shortcuts (and
        # every other root-bound shortcut) silently stopped firing after
        # the Launcher was introduced, even though the window was visibly
        # on screen and clickable. lift()+focus_force() (not focus_set()
        # alone - already confirmed unreliable for event_generate/real
        # input in tests/test_index_tag_shortcuts.py's own setup) makes
        # self.root the actual OS-focused window regardless of which of
        # the two construction paths created it.
        self.root.lift()
        self.root.focus_force()

    # ---------------- key bindings ----------------

    def _set_project_ocr_cache(self, project_path=None):
        """Point OCR storage at the current project's own folder."""
        path = project_path or self.project_path
        if path:
            cache_dir = Path(path).resolve().parent / "ocr_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.ocr_cache = OCRCache(str(cache_dir))
        else:
            self.ocr_cache = OCRCache(str(OCR_CACHE_DIR))


    def _bind_keys(self):
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-y>", lambda e: self.redo())
        for seq in ("<Control-s>", "<Control-S>"):
            self.root.bind(seq, lambda e: self._manual_save())
        self.root.bind("<Delete>", lambda e: self._on_delete_key())
        self.root.bind("<F7>", lambda e: self.smart_az.next_review_zone())
        self.root.bind("<Return>", lambda e: self._confirm_split() if self.viewer.mode == "split" else None)
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<BackSpace>", lambda e: self._clear_last_split() if self.viewer.mode == "split" else None)
        for seq in ("<Control-Shift-N>", "<Control-Shift-n>"):
            self.root.bind(seq, lambda e: self.auto_split_based_on_numbers())
        for seq in ("<Control-Shift-V>", "<Control-Shift-v>"):
            self.root.bind(seq, lambda e: self._toggle_split_axis())
        # Manual Region Split guides - generic, works for ANY zone/tag
        # (not just Table) - keyboard-only by design, no Tags-panel
        # buttons. Bound both cased forms since Tkinter's Shift-modifier
        # keysym casing for letter keys isn't fully consistent across
        # platforms.
        for seq in ("<Control-Shift-R>", "<Control-Shift-r>"):
            self.root.bind(seq, lambda e: self.start_horizontal_region_split())
        for seq in ("<Control-Shift-C>", "<Control-Shift-c>"):
            self.root.bind(seq, lambda e: self.start_vertical_region_split())
        for seq in ("<Control-Shift-X>", "<Control-Shift-x>"):
            self.root.bind(seq, lambda e: self._clear_region_splits())
        # Index-only physical merge: selected index zone + compatible index zone above.
        for seq in ("<Control-Shift-I>", "<Control-Shift-i>"):
            self.root.bind(seq, lambda e: self._index_merge_shortcut())
        # Table Draw-only Horizontal/Vertical Split shortcuts - distinct
        # from the generic Ctrl+Shift+R/C above (which stay completely
        # unchanged, still working for any zone/tag). Ctrl+Shift+Alt+6/7
        # confirmed genuinely unused: no other binding anywhere in this
        # app (this method, core.cup_config's dynamically-built CUP
        # shortcuts, or the configurable Index shortcuts) claims a
        # Ctrl+Shift+Alt+<digit> sequence past 1-5 (CUPEPUB's own list-type
        # tag shortcuts).
        self.root.bind("<Control-Shift-Alt-6>", lambda e: self._start_table_draw_split("row"))
        self.root.bind("<Control-Shift-Alt-7>", lambda e: self._start_table_draw_split("col"))
        # Page navigation (spec 66.22) - guarded against focus currently
        # being in a text entry (page-number box, tag search, zone search),
        # where Left/Right must move the text cursor instead of flipping
        # pages; Tk dispatches to the focused widget's own class binding
        # first, but that binding doesn't stop propagation, so an unguarded
        # global bind here would ALSO change pages while the user is simply
        # editing text.
        self.root.bind("<Left>", lambda e: None if self._focus_in_entry() else self.prev_page())
        self.root.bind("<Right>", lambda e: None if self._focus_in_entry() else self.next_page())

    def _focus_in_entry(self) -> bool:
        return isinstance(self.root.focus_get(), (tk.Entry, tk.Spinbox))

    def _bind_global_scroll_diagnostic(self):
        """A pure, read-only diagnostic tap (Settings > Debug logging) -
        bind_all on every wheel-ish sequence, logging every one that
        reaches ANY widget anywhere in the app, in addition to (never
        instead of) whatever widget-specific handler (gui/pdf_viewer.py,
        gui/zone_panel.py) also runs for the same event. Deliberately
        never returns "break" and never calls yview_scroll/xview_scroll
        itself - it changes NOTHING about actual scrolling behavior,
        purely observes.

        The reason this exists: a real user report confirmed their
        touchpad scrolls correctly in OTHER Windows applications but
        produces ZERO [SCROLL] log lines in ZoneTool specifically, even
        after the widget-specific bindings were made as broad as
        reasonably possible (canvas + scrollbars + panel background). That
        rules out "wrong widget under the cursor" as the cause and points
        at something upstream - either this Tcl/Tk build's own low-level
        mouse-wheel hook not being invoked for this specific touchpad
        driver's message delivery, or the event arriving somewhere this
        app's widget tree doesn't yet cover. Logging via bind_all (the
        LOWEST-priority bindtag, "all") answers exactly that: if a
        [SCROLL_RAW] line ever appears, Tk received the event somewhere
        and it's a routing/binding problem still fixable in this file; if
        it never appears no matter where in the app window the gesture
        happens, the event is not reaching Tk's own event system at all
        for this hardware, which is a Tcl/Tk-level or driver-level
        limitation no amount of additional .bind() calls here can fix."""
        def _log_raw(event, seq_name):
            if not debug_log.is_enabled():
                return  # skip the widget/class introspection cost entirely when logging is off
            try:
                widget_class = event.widget.winfo_class()
            except Exception:
                widget_class = "?"
            debug_log.log(
                "SCROLL_RAW",
                f"widget={event.widget} class={widget_class} seq={seq_name} "
                f"delta={getattr(event, 'delta', None)} state={getattr(event, 'state', None)} "
                f"root_xy=({event.x_root},{event.y_root})")

        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Control-MouseWheel>",
                    "<Button-4>", "<Button-5>", "<Shift-Button-4>", "<Shift-Button-5>"):
            self.root.bind_all(seq, lambda e, s=seq: _log_raw(e, s), add="+")

        # <TouchpadScroll> (Tk 9's own separate high-resolution touchpad
        # event, TIP 684) needs its own true "any widget" catch-all too -
        # theme.bind_touchpad_scroll binds to one SPECIFIC widget's own
        # Tcl path, not the "all" bindtag bind_all uses, so the global tap
        # here binds directly to the literal Tcl target "all" (the exact
        # same bindtag every widget's own bindtags list already includes,
        # which is what bind_all itself is a thin Python wrapper around)
        # rather than reusing that helper as-is.
        def _log_raw_touchpad(dx, dy):
            if not debug_log.is_enabled():
                return
            debug_log.log("SCROLL_RAW", f"widget=(any) seq=<TouchpadScroll> dx={dx} dy={dy}")

        cmd_name = self.root.register(lambda dx, dy: _log_raw_touchpad(int(dx), int(dy)))
        script = f"set _ztGD [tk::PreciseScrollDeltas %D]; {cmd_name} [lindex $_ztGD 0] [lindex $_ztGD 1]"
        try:
            self.root.tk.call("info", "commands", "tk::PreciseScrollDeltas")
            self.root.tk.call("bind", "all", "<TouchpadScroll>", "+" + script)
        except tk.TclError:
            pass  # Tk < 9.0 - this event doesn't exist at all, nothing to bind

    def _log_scroll_diagnostic_env(self):
        """Logs the Tcl/Tk build ("info patchlevel") + Python's own
        tkinter.TkVersion the moment Debug logging turns on - a real
        confirmed touchpad-scroll report showed scrolling working
        correctly in every OTHER Windows application but producing zero
        [SCROLL]/[SCROLL_RAW] lines in ZoneTool specifically, which points
        at this SPECIFIC Tcl/Tk build's own low-level mouse-wheel hook
        rather than an OS/driver setting - this line answers "which Tk
        build" without a separate round-trip asking the user to run
        anything themselves."""
        if not debug_log.is_enabled():
            return
        try:
            patchlevel = self.root.tk.call("info", "patchlevel")
        except tk.TclError:
            patchlevel = "unknown"
        debug_log.log("ENV", f"Tcl/Tk patchlevel={patchlevel} tkinter.TkVersion={tk.TkVersion}")

    def notify(self, message: str, kind: str = "success"):
        """Compact toast (spec 66.15) for a routine, non-blocking
        confirmation - anchored to the PDF workspace so it never appears
        somewhere the user isn't already looking. Reserved for brief
        confirmations only; Generate XML/XHTML's own multi-line summaries
        (counts, warnings) stay as messagebox dialogs since a toast that
        auto-dismisses in ~3 seconds isn't appropriate for content the user
        may need to actually read and act on."""
        theme.show_toast(self.viewer, message, kind=kind)

    def _go_home(self):
        """"<- Home" (spec: EPUBForge Feature Hub navigation) - returns to
        the Launcher without restarting the process. Best-effort PDF
        cleanup only (mirrors the existing load_project()/new project
        pattern at self.pdf_document.close() - never fatal if there's
        nothing open yet or the close itself raises)."""
        self._flush_autosave_before_close()
        try:
            if getattr(self, "pdf_document", None) is not None:
                self.pdf_document.close()
        except Exception:
            pass
        self._on_home()

    def _on_app_close(self):
        """WM_DELETE_WINDOW for the standalone (non-Launcher) entry point -
        see run() below. Same "flush any pending autosave, then close" as
        _go_home above (spec section 28: "If project is dirty: save_now().
        Only then close.")."""
        self._flush_autosave_before_close()
        try:
            if getattr(self, "pdf_document", None) is not None:
                self.pdf_document.close()
        except Exception:
            pass
        self.root.destroy()

    def _flush_autosave_before_close(self):
        try:
            self.autosave.shutdown()
        except Exception:
            pass

    def toggle_theme(self):
        """Persists the Light/Dark choice (spec 66.25) and asks for a
        restart rather than re-theming the already-built widget tree live -
        every panel reads gui.theme.current.palette exactly once, at its
        own construction time, so a genuinely live in-place re-theme would
        need every one of them to expose (and this method to call) its own
        re-color method; simpler and far lower-risk to apply the new
        palette cleanly on the next launch, same as most desktop apps'
        theme settings."""
        new_mode = "dark" if theme.current.palette["mode"] == "light" else "light"
        ui_prefs.save_ui_prefs({"theme": new_mode})
        messagebox.showinfo("Theme", f"Switched to {new_mode.title()} theme.\nRestart the BITS Tool to apply it.")

    def set_display_toggle(self, key: str, value: bool):
        """Show Zone Borders / Show Tag Labels / Show Reading Order (spec
        22-24) - purely visual (gui/pdf_viewer.py._draw_zone reads these
        same self.settings keys), never touches zone data, selection, or
        generation. Persisted through the existing per-project settings
        round-trip (core/project_manager.py.DEFAULT_SETTINGS), same as
        every other setting - no separate preference store needed."""
        self.settings[key] = value
        self.viewer.redraw()
        self.mark_dirty("display_toggle_changed")

    def toggle_right_panel(self):
        """Collapse/expand the Zone Hierarchy + Properties panel (spec:
        "RIGHT-SIDE ZONE HIERARCHY / PROPERTIES PANEL... maximize the PDF
        working area"). Reuses the SAME tk.PanedWindow the three panels
        already live in (spec: "use the existing splitter mechanism rather
        than creating a completely separate window system") - forget()
        actually UNMANAGES the pane (not just hides its contents), so the
        space it held is immediately reclaimed by the PDF viewer's own
        stretch="always" pane, which is exactly what makes the viewer
        genuinely widen rather than leaving a blank gap. add() re-inserts
        it at the end (its only possible position, being the rightmost
        pane) with the identical stretch/minsize it was originally created
        with, so repeated collapse/expand cycles always return to the same
        layout behavior. self.zone_tree itself (the Treeview, its data, its
        current selection/search state) is never destroyed or rebuilt by
        this - only its PARENT pane management changes, so nothing about
        zone data, reading order, or any other state is touched."""
        if self.right_panel_collapsed:
            self.paned.add(self.zone_tree, stretch="always", minsize=240)
        else:
            self.paned.forget(self.zone_tree)
        self.right_panel_collapsed = not self.right_panel_collapsed
        self.toolbar.set_right_panel_collapsed(self.right_panel_collapsed)

    def _on_escape(self, event):
        if self.viewer.mode == "split":
            self._cancel_split()
        elif self.viewer.mode in ("region_split_h", "region_split_v"):
            self._exit_region_split_mode()

    def _on_delete_key(self):
        """<Delete>: while in Row/Column Split Mode with a guide selected
        (clicked, not dragged), removes that guide. Otherwise falls
        through to the normal "delete selected zone" behavior."""
        if self.viewer.mode in ("region_split_h", "region_split_v") \
                and self.viewer.region_split_state \
                and self.viewer.region_split_state.get("selected_index") is not None:
            self.viewer.delete_selected_region_split()
        else:
            self.delete_selected()

    def _build_context_menu(self):
        self.context_menu = tk.Menu(self.root, tearoff=0)

    def show_zone_context_menu(self, event, zone_id):
        self.context_menu.delete(0, "end")
        self.context_menu.add_command(label="Change Tag / Info...", command=lambda: self.on_zone_edit_requested(zone_id))
        self.context_menu.add_command(label="Set Parent...", command=lambda: self._prompt_set_parent(zone_id))
        self.context_menu.add_command(label="Remove Parent", command=lambda: self._remove_parent(zone_id))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Move Up", command=lambda: self._move_zone(zone_id, -1))
        self.context_menu.add_command(label="Move Down", command=lambda: self._move_zone(zone_id, 1))
        self.context_menu.add_command(label="Set Reading Order...", command=lambda: self._prompt_set_reading_order(zone_id))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Horizontal Split", command=lambda: self.start_horizontal_split(zone_id))
        self.context_menu.add_command(label="Auto Split Based on Numbers", command=lambda: self.auto_split_based_on_numbers(zone_id))
        self.context_menu.add_command(label="Merge Previous", command=lambda: self._prompt_and_merge_with_previous(zone_id))
        zone = self.zone_manager.zones.get(zone_id)
        # Index Merge is intentionally separate from generic Merge Previous.
        if zone and self._is_index_zone(zone):
            self.context_menu.add_command(
                label="Index Merge (with Above)",
                command=lambda zid=zone_id: self._index_merge_zone(zid),
            )
        if zone and zone.tag == "table":
            # Table Draw split actions, now ALSO reachable via right-click
            # (previously only in the Properties panel's own "TABLE DRAW"
            # section + the Ctrl+Shift+Alt+6/7 shortcuts - a real,
            # reported discoverability gap: reported as "row and column
            # split not able to find" via right-click). Delegates to the
            # EXACT SAME gui.pdf_viewer.start_region_split_mode/delete_
            # selected_region_split primitives the Properties panel's own
            # Add/Delete Row/Column Split buttons already call - no new
            # split/persist/generate logic, only another entry point into
            # the same, already-correct mechanism.
            self.context_menu.add_separator()
            self.context_menu.add_command(
                label="Split Row", command=lambda zid=zone_id: self.viewer.start_region_split_mode(zid, "row"))
            self.context_menu.add_command(
                label="Split Column", command=lambda zid=zone_id: self.viewer.start_region_split_mode(zid, "col"))
            if self.viewer.mode in ("region_split_h", "region_split_v"):
                self.context_menu.add_command(
                    label=("Delete Selected Row Split" if self.viewer.mode == "region_split_h"
                           else "Delete Selected Column Split"),
                    command=self.viewer.delete_selected_region_split)
                # "Finish Split" - a clickable, discoverable equivalent of
                # pressing Esc while mid-split. Every line drawn is ALREADY
                # saved the instant it's added (see start_region_split_
                # mode's own docstring) - there is no separate confirm/
                # apply step to perform. This item exists only because an
                # operator coming from the OLDER Horizontal Split feature
                # (which genuinely DOES need Enter to confirm) reasonably
                # expected the same here and had no clickable action
                # matching that expectation - functionally identical to
                # Esc, given a label that reads as "I'm done" instead.
                self.context_menu.add_command(label="Finish Split (already saved)",
                                               command=self._exit_region_split_mode)
        if zone and self._is_bibliography_zone(zone):
            # "Merge with Previous Reference" - a specialized, additional
            # entry point for bibliography/reference zones, alongside
            # (never replacing) the generic "Merge Previous" item above.
            # Calls the EXACT SAME _prompt_and_merge_with_previous /
            # ZoneManager.merge_with_previous engine every other Merge
            # Previous entry point already uses - one merge mechanism,
            # not a second competing one (spec: "Do NOT remove existing
            # Merge Previous... Add this as an additional context option
            # specifically for bibliography/reference zones"). Already
            # exactly one undo transaction, for free, since it's a single
            # merge_with_previous call underneath.
            self.context_menu.add_command(
                label="Merge with Previous Reference",
                command=lambda zid=zone_id: self._prompt_and_merge_with_previous(zid),
            )
        if zone and zone.attributes.get("merged_with_previous"):
            self.context_menu.add_command(label="Unmerge", command=lambda: self._unmerge(zone_id))
        if zone:
            # Auto Zone / Auto Tag engine (CUPEPUB): lock, review, explain.
            self.context_menu.add_separator()
            self.context_menu.add_command(label="Unlock Zone" if zone.locked else "Lock Zone",
                                          command=lambda: self.smart_az.toggle_lock(zone_id))
            if zone.attributes.get("auto_role") or zone.attributes.get("confidence") is not None:
                self.context_menu.add_command(label="Show Auto Tag Decision...",
                                              command=lambda: self.smart_az.explain_zone(zone_id))
            if zone.needs_review:
                self.context_menu.add_command(label="Mark as Reviewed",
                                              command=lambda: self.smart_az.mark_reviewed(zone_id))
            self.context_menu.add_separator()
        self.context_menu.add_command(label="Duplicate", command=lambda: self._duplicate_zone(zone_id))
        self.context_menu.add_command(label="Delete", command=lambda: self._delete_zone(zone_id))
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def merge_selected_with_previous(self):
        if not self.selected_zone_id:
            messagebox.showinfo("Merge Previous", "Select a zone first.")
            return
        self._prompt_and_merge_with_previous(self.selected_zone_id)

    def merge_selected_with_previous_mode(self, join: str):
        """Toolbar's "Merge Previous ▾" dropdown (spec 66.20) - the mode is
        already known from which menu item was clicked, so this skips
        MergeModeDialog and merges directly. Same underlying engine call
        (ZoneManager.merge_with_previous) as every other Merge Previous
        entry point - no separate UI-only merge mechanism."""
        if not self.selected_zone_id:
            messagebox.showinfo("Merge Previous", "Select a zone first.")
            return
        self._merge_with_previous(self.selected_zone_id, join)

    # ---------------- index-only merge ----------------
    @staticmethod
    def _is_index_zone(zone):
        """True only for the three EPUB index hierarchy levels."""
        return getattr(zone, "tag", "").strip().lower() in {
            "indexprimary", "indexsecondary", "indextertiary", "indexterritory"
        }

    @staticmethod
    def _is_bibliography_zone(zone):
        """True for a bibliography/reference-entry zone - reuses auto_
        zoning.bibliography_auto_zone's own REFN_TAG/REFD_TAG literals
        (the same tags Auto Bibliography's own detection assigns) rather
        than a second, separately-maintained tag set."""
        from auto_zoning.bibliography_auto_zone import REFN_TAG, REFD_TAG
        return getattr(zone, "tag", "").strip().lower() in {REFN_TAG, REFD_TAG}

    def _index_merge_zone(self, zone_id):
        """Physically merge an Index zone with its compatible Index zone above.

        This is deliberately independent of the generic Merge Previous workflow.
        """
        zone = self.zone_manager.zones.get(zone_id)
        if zone is None or not self._is_index_zone(zone):
            self.notify("Select an Index zone first", kind="warning")
            return

        merger = getattr(self.zone_manager, "merge_index_with_previous", None)
        if not callable(merger):
            self.notify("Index Merge is not available in ZoneManager", kind="warning")
            return

        try:
            result = merger(zone_id)
        except Exception as exc:
            debug_log.log("INDEX_MERGE", f"failed for {zone_id}: {exc}")
            self.notify(f"Index Merge failed: {exc}", kind="warning")
            return

        if isinstance(result, tuple):
            ok = bool(result[0])
            message = result[1] if len(result) > 1 else ("Index zones merged" if ok else "No compatible Index zone above found.")
            merged_id = result[2] if len(result) > 2 else zone_id
        else:
            ok = bool(result)
            message = "Index zones merged" if ok else "No compatible Index zone above found."
            merged_id = zone_id

        if ok:
            self.on_zones_changed()
            if merged_id in self.zone_manager.zones:
                self.on_zone_selected(merged_id)
            else:
                self.on_zone_selected(zone_id if zone_id in self.zone_manager.zones else None)
            self.set_status("Index zones merged")
            self.notify("Index zones merged", kind="success")
        else:
            self.notify(message, kind="warning")

    def _index_merge_shortcut(self):
        """Ctrl+Shift+I: Index Merge only; never invokes Merge Previous."""
        if self._focus_in_entry() or not self.selected_zone_id:
            return "break"
        self._index_merge_zone(self.selected_zone_id)
        return "break"

    def _merge_skip_tags(self):
        """The active profile's own classification for Merge Previous's
        "find previous compatible content" search (core/zone_manager.py.
        _find_previous_in_reading_order) - page markers (CUPEPUB's
        PageNum, XML's pagenumber) are always skipped over, footnote-flow
        tags (fn/en) are skipped whenever they don't match the searching
        zone's own flow. "Previous zone" means the previous compatible
        CONTENT zone in the same logical reading flow, never simply the
        immediately preceding zone."""
        return (self.active_profile.get("page_marker_tags"), self.active_profile.get("footnote_flow_tags"),
                self.active_profile.get("non_flow_tags"))

    def _prompt_and_merge_with_previous(self, zone_id):
        """Shared by the toolbar button and the context menu (spec
        98.19 - one UI mechanism, not two): asks the user whether the
        previous zone's text should join this zone's text with a space or
        without one (spec 98.17/98.18) before actually merging. Previews
        use each zone's own already-extracted .text, so no extra PDF
        re-extraction is needed just to show the dialog. Also shows page/
        tag/reading-order for both zones and a plain-language reason for
        the candidate found (spec: "show the candidate, show why it was
        selected, allow the user to confirm" - see MergeModeDialog)."""
        page_marker_tags, footnote_flow_tags, non_flow_tags = self._merge_skip_tags()
        prev_id = self.zone_manager._find_previous_in_reading_order(
            zone_id, page_marker_tags, footnote_flow_tags, non_flow_tags)
        current_zone = self.zone_manager.zones.get(zone_id)
        prev_zone = self.zone_manager.zones.get(prev_id) if prev_id else None
        prev_preview = prev_zone.text if prev_zone else ""
        current_preview = current_zone.text if current_zone else ""

        def _info(z):
            if z is None:
                return ""
            ro = z.serial if z.serial is not None else "?"
            return f"{z.tag} — Page {z.page} — RO:{ro}"

        reason = ""
        confidence = None
        if prev_zone is not None and current_zone is not None:
            if prev_zone.page != current_zone.page:
                reason = (f"cross-page continuation - the last compatible content zone "
                          f"found on page {prev_zone.page}, skipping past any page-marker/"
                          f"footnote/image-figure zones in between")
            else:
                reason = "same page, previous compatible content zone in reading order"
            # Spec: "EPUBForge - URGENT ZONING FIX - PREVIOUS-PARAGRAPH MERGE
            # PROBLEM", section 30 - a real, evidence-based confidence score
            # (text continuity - the same signal the automatic engine uses),
            # never just "found a candidate = proceed". Only meaningful for
            # actual text-bearing content (a container/image tag has no
            # sentence-continuity concept), so it's left None otherwise -
            # MergeModeDialog shows no confidence line at all in that case.
            confidence = paragraph_merge.continuation_confidence(prev_preview, current_preview)

        dlg = MergeModeDialog(self.root, prev_preview, current_preview, _info(prev_zone), _info(current_zone),
                               reason, confidence)
        if dlg.result is None:
            return
        self._merge_with_previous(zone_id, dlg.result)

    def _merge_with_previous(self, zone_id, join: str = " "):
        # Logical merge only - see ZoneManager.merge_with_previous. Both zones
        # stay independent physical zones with untouched bboxes; zone_id just
        # gets flagged (and turns yellow) as a continuation of the previous
        # zone. Nothing is deleted, so the same zone_id stays selected.
        page_marker_tags, footnote_flow_tags, non_flow_tags = self._merge_skip_tags()
        ok, message, _ = self.zone_manager.merge_with_previous(
            zone_id, join, page_marker_tags, footnote_flow_tags, non_flow_tags)
        if ok:
            # Full canonical recompute (spec: "EPUBForge - Fix Canonical
            # Reading Order for Multi-Column, Split and Merged Zones" -
            # "After merge: recalculate_reading_order(page)"). Merging
            # never touches any zone's bbox (see this method's own comment
            # above - both zones keep their exact physical position), so
            # this reproduces the identical column/order result the
            # zones already had, just via the one canonical code path
            # instead of a second, parallel "trust the existing order"
            # shortcut.
            self.on_zones_changed()
            self.on_zone_selected(zone_id)
            self.set_status("Merged with previous content")
            self.notify("Merged with previous content", kind="success")
        else:
            # Non-blocking (spec: "show a small non-blocking notification")
            # - never the old "Cannot merge across pages" framing merely
            # because the pages differ; message is already one of the two
            # exact required strings ("No compatible previous content zone
            # found." / "Previous content is not compatible with this
            # zone.") from ZoneManager.merge_with_previous.
            self.notify(message, kind="warning")

    def _unmerge(self, zone_id):
        if self.zone_manager.unmerge(zone_id):
            self.on_zones_changed()
            self.on_zone_selected(zone_id)
            self.set_status("Unmerged")

    def _remove_parent(self, zone_id):
        self.zone_manager.set_parent(zone_id, None)
        self.on_zones_changed()

    _INDEX_LEVEL_NAMES = {1: "Primary", 2: "Secondary", 3: "Tertiary"}

    def index_level_name(self, level: int) -> str:
        return self._INDEX_LEVEL_NAMES.get(level, f"Level {level}")

    def _index_hierarchy_tags(self) -> dict:
        """{level:int -> tag:str} from the active profile, normalized -
        the single source of truth every Index-shortcut method below reads
        from (spec: "Do not hard-code tag names if the existing CUPEPUB
        profile already defines them")."""
        raw = self.active_profile.get("index_hierarchy_tags") or {}
        return {int(k): v for k, v in raw.items()}

    def _retag_index_zone(self, zone_id, level: int):
        """Index Primary/Secondary/Tertiary retag shortcuts - a single,
        minimal operation: ONLY zone.tag (and the level it implies) changes.
        Reuses core.zone_manager.ZoneManager.set_tag - the SAME method the
        "Change Tag" dialog/context menu already calls, zero new retag
        mechanism - which already pushes its own undo snapshot, so Undo/
        Redo works with no extra plumbing.

        Deliberately does NOT touch parent_id, bbox, text, page, or
        reading order (spec: "DO NOT... change its parent... Only the
        Index tag/level should change" / "must NEVER automatically alter
        reading order, coordinates, parent, page, hierarchy" - unless a
        profile explicitly requires otherwise, which none currently does).
        Only fires for a zone that is ALREADY one of the profile's own
        index-hierarchy tags (spec: "Shortcuts should only activate...
        AND the active zone is an Index zone") - pressing Ctrl+2 on an
        ordinary Paragraph zone does nothing, by design; converting a
        non-index zone into an index one is still the Tag Toolbox's job.
        Tag names come ENTIRELY from the active profile's own
        index_hierarchy_tags - never assumes IndexPE/SE/TE literally."""
        tag_for_level = self._index_hierarchy_tags()
        if not tag_for_level:
            return
        zone = self.zone_manager.zones.get(zone_id)
        if not zone or zone.tag not in tag_for_level.values():
            return
        target_tag = tag_for_level.get(level)
        if not target_tag:
            return
        if zone.tag == target_tag:
            return  # already at this level - nothing to do, nothing to undo-snapshot

        current_level = next((lvl for lvl, t in tag_for_level.items() if t == zone.tag), None)
        self.zone_manager.set_tag(zone_id, target_tag)
        # recompute_reading_order=False: retagging never moves this zone's
        # bbox and never touches parent_id, so its reading-order position
        # must stay exactly where it was (spec 30) - never let the very
        # refresh this triggers second-guess that, matching _move_zone's
        # own reasoning for the identical situation.
        self.on_zones_changed(recompute_reading_order=False)
        from_name = self.index_level_name(current_level) if current_level else zone.tag
        self.notify(f"Index level changed: {from_name} → {self.index_level_name(level)}", kind="success")

    def _reset_index_zone(self, zone_id):
        """Ctrl+0 (spec 2, optional): "Reset/return to normal Index entry
        if supported by the existing profile." A flat, non-hierarchical
        "Index Entry" tag (profiles/epub_profile.json's own "indexentry",
        distinct from the nested indexprimary/secondary/territory family)
        exists for the EPUB profile but NOT for CUPEPUB (confirmed by
        inspecting profiles/CUPEPUB/CUPLookup.xml - no such tag is defined
        there) - never invents one; simply reports (via the same
        non-blocking toast every other shortcut result uses, never a
        popup) that reset isn't available for whichever profile is
        currently active, exactly as the spec's own "if supported"
        qualifier anticipates. Same "only an existing Index zone" gate and
        "tag only, nothing else" discipline as the Level 1/2/3 shortcuts."""
        tag_for_level = self._index_hierarchy_tags()
        zone = self.zone_manager.zones.get(zone_id)
        if not zone or not tag_for_level or zone.tag not in tag_for_level.values():
            return
        flat_tag = next((t for _l, t, _a in self.active_tag_buttons if t == "indexentry"), None)
        if not flat_tag:
            self.notify("Reset is not supported for the active profile.", kind="info")
            return
        if zone.tag == flat_tag:
            return
        self.zone_manager.set_tag(zone_id, flat_tag)
        self.on_zones_changed(recompute_reading_order=False)
        self.notify("Index entry reset to normal.", kind="success")

    def _apply_index_tag_shortcuts(self):
        """Registers/unregisters the Index Primary/Secondary/Tertiary(/
        reset) retag shortcuts (spec 1/2/7) from self.settings - same
        unbind-then-rebind-from-scratch pattern App._apply_cup_shortcuts
        already uses for the identical "user-configurable keybinding, may
        change at runtime" situation, so switching the shortcut in
        Settings never leaves a stale binding active. bind_all (not
        root.bind) so the shortcut fires regardless of which widget
        currently has focus - EXCEPT a text entry, guarded explicitly
        inside the handler itself (spec 9), the same self._focus_in_entry()
        check Left/Right arrow navigation already relies on."""
        for seq in getattr(self, "_index_shortcut_seqs", []):
            try:
                self.root.unbind_all(seq)
            except tk.TclError:
                pass
        self._index_shortcut_seqs = []

        def _make_handler(action):
            def handler(event):
                if self._focus_in_entry():
                    return None
                if not self.selected_zone_id:
                    return None
                action(self.selected_zone_id)
                return "break"
            return handler

        bindings = (
            ("index_shortcut_primary", lambda zid: self._retag_index_zone(zid, 1)),
            ("index_shortcut_secondary", lambda zid: self._retag_index_zone(zid, 2)),
            ("index_shortcut_tertiary", lambda zid: self._retag_index_zone(zid, 3)),
            ("index_shortcut_reset", self._reset_index_zone),
        )
        for key, action in bindings:
            seq = self.settings.get(key)
            if not seq:
                continue
            try:
                self.root.bind_all(seq, _make_handler(action))
                self._index_shortcut_seqs.append(seq)
            except tk.TclError:
                pass  # an invalid/reserved sequence (e.g. mistyped in Settings) is skipped, never fatal

    def _prompt_set_parent(self, zone_id):
        from tkinter import simpledialog
        current = self.zone_manager.zones[zone_id].parent_id or ""
        new_parent = simpledialog.askstring(
            "Set Parent", "Parent zone id (blank to clear):", initialvalue=current, parent=self.root)
        if new_parent is None:
            return
        new_parent = new_parent.strip() or None
        if new_parent is not None and new_parent not in self.zone_manager.zones:
            messagebox.showerror("Set Parent", f"No such zone: {new_parent}")
            return
        if not self.zone_manager.set_parent(zone_id, new_parent):
            messagebox.showerror("Set Parent", "That would make the zone its own ancestor (a cycle) - rejected.")
            return
        self.on_zones_changed()

    def _move_zone(self, zone_id, direction):
        # recompute_reading_order=False: the user just explicitly chose this
        # exact position via Move Up/Move Down - the automatic geometric
        # recalculation must not immediately overwrite that choice on the
        # very refresh this triggers (see on_zones_changed's own comment).
        moved = self.zone_manager.move_up(zone_id) if direction < 0 else self.zone_manager.move_down(zone_id)
        if moved:
            self.on_zones_changed(recompute_reading_order=False)

    def _prompt_set_reading_order(self, zone_id):
        from tkinter import simpledialog
        zone = self.zone_manager.zones[zone_id]
        if not self.zone_manager._counts_in_reading_order(zone):
            messagebox.showinfo(
                "Set Reading Order",
                "This zone doesn't hold its own Reading Order position - it's either a "
                "Horizontal Split parent (its pieces occupy the sequence instead) or "
                "merged into a previous zone (Unmerge it first).")
            return
        # Reading Order is per-page - the valid position range is scoped to
        # this zone's OWN page, not the whole project.
        n = sum(1 for z in self.zone_manager.zones.values()
                if z.page == zone.page and self.zone_manager._counts_in_reading_order(z))
        current = zone.serial if zone.serial is not None else n
        value = simpledialog.askinteger(
            "Set Reading Order",
            f"Move this zone to Reading Order position on page {zone.page} (1-{n}):\n"
            "All zones between the old and new position shift automatically.",
            initialvalue=current, minvalue=1, maxvalue=n, parent=self.root)
        if value is None:
            return
        self.zone_manager.set_reading_order(zone_id, value)
        # recompute_reading_order=False - see _move_zone's comment: this IS
        # the manual override, it must not immediately overwrite itself.
        self.on_zones_changed(recompute_reading_order=False)

    # ---------------- PDF / project I/O ----------------
    def open_pdf(self):
        # Output folder name is asked BEFORE PDF selection (spec: "1. User
        # clicks Open PDF. 2. Ask for output folder name. 3. User enters
        # folder name. 4. Then allow PDF selection.") - a hard precondition,
        # never optional: cancelling here aborts Open PDF entirely, exactly
        # like cancelling the file-selection dialog itself does just below.
        # If this PDF turns out to already have a saved project (the
        # existing-project-restore branch further down), that project's own
        # previously-saved output_folder_name (or lack of one, for an older
        # project saved before this feature existed) takes over anyway via
        # self.settings' own wholesale replacement in _load_project_from_data
        # - this prompt's value is simply what a genuinely NEW project uses.
        folder_dialog = OutputFolderNameDialog(self.root, initial=self.settings.get("output_folder_name", ""),
                                                output_root=OUTPUT_DIR)
        if not folder_dialog.result:
            return
        output_folder_name = folder_dialog.result

        path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        if self.zone_manager.zones and path != self.pdf_path:
            if not messagebox.askyesno("Open PDF", "This will discard the current zoning session. Continue?"):
                return
            self.zone_manager = ZoneManager()
            self.smart_az.on_document_changed()
        if path == self.pdf_path and self.pdf_document:
            # Already the currently-open document - reloading it from its
            # own autosaved project.json here could clobber an edit still
            # sitting in the debounce window that hasn't hit disk yet.
            # Nothing to do beyond reclaiming focus (see the identical
            # lift()/focus_force() call at the end of this method).
            self.root.lift()
            self.root.focus_force()
            return

        # Automatic project association (spec: "ZONING - CUPPEUB DEFAULT
        # PROFILE + CONTINUOUS AUTOSAVE" sections 2/7) - a PDF opened before
        # from this same Projects/ root already has a project.json tracking
        # it; reopening it must restore that EXISTING project (profile,
        # zones, OCR, merges, everything) rather than starting blank, per
        # priority rule #1 ("Existing saved project profile" beats both the
        # user's current in-session selection and the CUPEPUB default).
        # Keyed by PDF filename stem under the SAME projects/ root the
        # manual Save/Load dialogs already default to (APP_ROOT / "projects")
        # - never a second, incompatible project-directory convention. The
        # file itself is named after the PDF's own stem (e.g.
        # "08_466AR_ch1.json"), never the generic "project.json" - a real,
        # confirmed complaint (every project used the same meaningless
        # filename). An OLD project saved under the previous literal
        # "project.json" name is untouched and still loads correctly
        # through the separate, unchanged manual Load Project menu action.
        default_project_path = str(PROJECTS_DIR / Path(path).stem / f"{Path(path).stem}.json")
        if os.path.isfile(default_project_path):
            try:
                data = project_manager.load_project(default_project_path)
            except (OSError, ValueError) as e:
                # Corrupt/unreadable project.json - atomic writes should
                # prevent this, but this path now runs automatically on
                # every matching PDF open (never just on an explicit user
                # action), so it must degrade to "treat as a brand-new
                # project" rather than crash the app. The .bak rotation
                # (spec section 16) is the user's own manual recovery path
                # from here if the recovery file is ALSO bad - never
                # silently overwritten by what follows below.
                data = None
                self.notify(f"Could not read the existing project for this PDF ({e}) - "
                            f"starting a new one.", kind="warning")
            if data and data.get("pdf") and os.path.exists(data["pdf"]):
                self._load_project_from_data(default_project_path, data)
                self.root.lift()
                self.root.focus_force()
                return
            # The project file's own recorded PDF path is stale/missing (or
            # the file itself was unreadable) - fall through and treat this
            # exactly like a brand-new project for the PDF the user just
            # picked (never silently overwrite an existing project.json in
            # that case either - a fresh save at the SAME default_project_
            # path only happens once the user actually makes a change, via
            # the normal autosave path below).

        if self.pdf_document:
            self.pdf_document.close()
        self.pdf_document = PDFDocument(path)
        self.zone_manager.set_pdf_document(self.pdf_document)
        self.smart_az.on_document_changed()
        self.pdf_path = path
        self.page_count = self.pdf_document.page_count
        self.current_page = 1
        self.overlapping_zone_ids = set()  # never carries over to a different document
        self.page_rotations = {}  # never carries over to a different document
        self.toolbar.set_page_info(self.current_page, self.page_count)
        # settings["prefix"] is the OUTPUT FILENAME/folder identity (spec:
        # "ZONETOOL - MASTER PRODUCTION FIX" sections 2/44/74/76 - "the
        # uploaded/input XHTML filename must remain EXACTLY the filename
        # provided" / "Filename and ID prefix are TWO DIFFERENT things") -
        # the FULL original PDF stem, e.g. "23_980AR_bm3", never narrowed.
        # The separate, SHORT element-ID prefix ("bm3") is derived from
        # THIS value on demand, only at generation time, via
        # core.component_output.derive_id_prefix - see generate_xml/
        # generate_xhtml/_generate_client_xhtml below. (An earlier revision
        # of this line stored the already-shortened id-prefix directly
        # here, which silently renamed every generated output file too -
        # exactly the regression this comment now guards against.)
        self.settings["prefix"] = Path(path).stem
        # The single shared output folder name for this WHOLE PDF project
        # (spec: one folder + one images/ subfolder per PDF, never per
        # component) - entered once, above, before the file dialog even
        # opened. Distinct from settings["prefix"], which still drives only
        # FILENAMES/element ids, never the output folder path anymore - see
        # core.component_output.ComponentOutputManager.
        self.settings["output_folder_name"] = output_folder_name
        # BITS is the default profile for every brand-new project - the
        # "already has a project.json" branch above is what makes this apply
        # ONLY to a genuinely new project, never to one with an existing
        # saved profile.
        self.settings["profile"] = profile_manager.DEFAULT_PROFILE_NAME
        self.set_profile(profile_manager.DEFAULT_PROFILE_NAME, persist=False)
        self.viewer.load_page(self.current_page)
        self.zone_tree.refresh()
        self.toolbar.set_rotation_label(self.page_rotations.get(self.current_page, 0))
        # Default view per Settings > PDF Viewer > Zoom Mode (Fit Width unless
        # the user has set a different preference).
        self.set_zoom_mode(self.settings.get("viewer_zoom_mode", "fit_width"))
        self.toolbar.set_auto_zone_enabled(bool(self.reference_templates))
        # Automatic project creation + immediate first autosave (spec
        # sections 7/8) - the user must never need to press Save before
        # their first change is protected. critical=True collapses the
        # debounce to ~150ms instead of the normal 700ms, matching "the
        # project JSON must be created immediately".
        self.project_path = default_project_path
        self._set_project_ocr_cache(self.project_path)
        self.autosave.start(self.project_path)
        self.mark_dirty("project_created", critical=True)
        self.set_status(f"Loaded {os.path.basename(path)}")
        # filedialog.askopenfilename is a NATIVE OS dialog, not a Tk
        # window - closing it is not guaranteed to hand OS keyboard focus
        # back to self.root the way closing a Tk Toplevel (grab_set/
        # release) reliably does. Explicitly reclaiming it here means
        # every root-bound shortcut (Ctrl+Z/Y, the Index Primary/
        # Secondary/Tertiary retag shortcuts, ...) works immediately after
        # opening a PDF, with no extra click needed first - same reasoning
        # as App.__init__'s own lift()+focus_force() call.
        self.root.lift()
        self.root.focus_force()

    def save_project(self):
        if not self.pdf_path:
            messagebox.showwarning("Save Project", "Open a PDF first.")
            return
        path = filedialog.asksaveasfilename(
            initialdir=str(APP_ROOT / "projects"), defaultextension=".json",
            filetypes=[("BITS Tool project", "*.json")])
        if not path:
            return
        project_manager.save_project(
            path, self.pdf_path, 200, self.zoom, self.zone_manager,
            settings=self.settings, page_count=self.page_count, page_rotations=self.page_rotations,
            verification_data=self.verification_session.to_dict() if self.verification_session else None)
        self.project_path = path
        self._set_project_ocr_cache(self.project_path)
        # Save As moves autosave association to the new file (spec section
        # 36) - the OLD project_path stops receiving changes automatically,
        # since self.autosave now only ever writes to this new one.
        self.autosave.start(path)
        self.set_status(f"Project saved: {os.path.basename(path)}")
        self.notify(f"Project saved: {os.path.basename(path)}", kind="success")

    def load_project(self):
        path = filedialog.askopenfilename(
            initialdir=str(APP_ROOT / "projects"), filetypes=[("BITS Tool project", "*.json")])
        if not path:
            return
        data = project_manager.load_project(path)
        pdf_path = data.get("pdf")
        if not pdf_path or not os.path.exists(pdf_path):
            messagebox.showerror("Load Project", f"PDF not found: {pdf_path}")
            return
        self._load_project_from_data(path, data)
        # See open_pdf()'s identical call for why: a native OS file dialog
        # closing doesn't reliably hand OS keyboard focus back to
        # self.root on its own, which would otherwise leave every root-
        # bound shortcut (Index Primary/Secondary/Tertiary retag, Ctrl+Z/
        # Y, ...) unresponsive until the user clicks somewhere first.
        self.root.lift()
        self.root.focus_force()

    def _load_project_from_data(self, path: str, data: dict):
        """Shared restore body for load_project() (explicit menu action)
        and open_pdf()'s own "a project already exists for this PDF"
        branch (spec section 2: "EXISTING PROJECT WITH SAVED PROFILE ->
        restore saved profile") - exactly one place turns a loaded project
        dict into live app state, never two divergent copies of this
        logic. Also runs the crash-recovery check (spec sections 17/29)
        before committing to `data`, since both entry points need it."""
        recovered = autosave_service.check_recovery(path)
        used_recovery = False
        if recovered is not None:
            saved_at = (recovered.get("autosave_meta") or {}).get("saved_at", "?")
            clean_at = (data.get("autosave_meta") or {}).get("saved_at", "unknown")
            if messagebox.askyesno(
                    "Recover Project",
                    "Recovered autosaved changes are available for this project.\n\n"
                    f"Recovered save: {saved_at}\nLast clean save: {clean_at}\n\n"
                    "Recover the newer autosaved changes now?\n"
                    "(Choosing No keeps the last clean save.)"):
                data = recovered
                used_recovery = True
                debug_log.log("AUTOSAVE", "Recovery loaded")

        pdf_path = data.get("pdf")
        if self.pdf_document:
            self.pdf_document.close()
        self.pdf_document = PDFDocument(pdf_path)
        self.pdf_path = pdf_path
        self.page_count = self.pdf_document.page_count
        self.zone_manager = ZoneManager(self.pdf_document)
        self.smart_az.on_document_changed()
        project_manager.load_into_zone_manager(data, self.zone_manager)
        # Repair Reading Order on load (spec: "EPUBForge - Final Reading
        # Order Fix" section 24 / an earlier spec's "Existing project
        # loading": "If the existing project has... wrong column order...
        # repair Reading Order automatically... Only repair Reading
        # Order" - never zone data). A project saved while an earlier,
        # now-fixed column-detection bug was still active (e.g. the
        # narrow-gutter rejection fixed in core/column_detector.py) would
        # otherwise silently keep replaying that exact wrong order forever
        # on every future reopen, since loading a project never used to
        # recompute anything - the user would have no way to know a fix
        # even existed without knowing to click "Recalculate Reading
        # Order" first. This never touches bbox/tag/text/hierarchy/OCR -
        # only zone.serial (Reading Order) values, via the SAME one
        # canonical engine every other zoning operation already uses.
        reading_order.recompute_all_pages(self.zone_manager)
        self.settings = data.get("settings", dict(project_manager.DEFAULT_SETTINGS))
        debug_log.set_enabled(self.settings.get("debug_logging", False))
        self._log_scroll_diagnostic_env()
        self.toolbar.sync_display_toggles(self.settings)
        self.set_profile(self.settings.get("profile", profile_manager.DEFAULT_PROFILE_NAME), persist=False)
        self.zoom = data.get("zoom", DEFAULT_ZOOM) or DEFAULT_ZOOM
        self.current_page = 1
        self.overlapping_zone_ids = set()  # never carries over from a previous project
        # Restored from the project file - int() the keys back (JSON object
        # keys are always strings; project_manager.save_project relies on
        # json.dump to stringify them on the way out). Absent entirely in a
        # project saved before this feature existed -> {} -> every page
        # reads as 0deg via .get(page, 0), no error.
        self.page_rotations = {int(k): v for k, v in data.get("page_rotations", {}).items()}
        from core.verification.verification_session import session_from_project_data
        self.verification_session = session_from_project_data(data) if data.get("verification") else None
        self.project_path = path
        self._set_project_ocr_cache(self.project_path)
        self.autosave.start(path)
        if used_recovery:
            # Persist the recovered state as the new clean project.json
            # immediately (spec: recovering must not leave the project
            # permanently stuck offering the same recovery prompt forever).
            self.mark_dirty("recovery_loaded", critical=True)
        self.toolbar.set_page_info(self.current_page, self.page_count)
        self.viewer.load_page(self.current_page)
        self.zone_tree.refresh()
        self.toolbar.set_rotation_label(self.page_rotations.get(self.current_page, 0))
        saved_mode = self.settings.get("viewer_zoom_mode", "fit_width")
        if saved_mode == "manual":
            self.zoom_mode = "manual"
            self._apply_zoom()
        else:
            self.set_zoom_mode(saved_mode)
        self.toolbar.set_auto_zone_enabled(bool(self.reference_templates))
        self.set_status(f"Project loaded: {os.path.basename(path)}")
        # Source PDF validation (spec section 30) - never blocks or
        # discards zoning, just a heads-up that the PDF backing this
        # project has changed on disk since the project was last saved.
        try:
            current_size, current_mtime = os.path.getsize(pdf_path), os.path.getmtime(pdf_path)
        except OSError:
            current_size = current_mtime = None
        stored_size, stored_mtime = data.get("pdf_size"), data.get("pdf_mtime")
        if current_size is not None and stored_size is not None and (
                current_size != stored_size
                or (stored_mtime is not None and abs(current_mtime - stored_mtime) > 1)):
            self.notify("The source PDF has changed since this project was saved.", kind="warning")

    # ---------------- auto-zoning ----------------
    def load_reference_project(self):
        """"Load Reference Project" - reads an existing, already-manually-
        zoned ZoneTool project JSON (the exact same file format Save
        Project produces - no new file type) as a layout/semantic template.
        Read-only: never touches the live zone_manager or the reference
        file itself. Clicking this more than once ("Add Reference")
        accumulates templates - self.reference_template is their merge
        (layout_template.merge_templates), recomputed each time."""
        path = filedialog.askopenfilename(
            initialdir=str(APP_ROOT / "projects"), filetypes=[("BITS Tool project", "*.json")],
            title="Load Reference Project")
        if not path:
            return
        try:
            raw_ref = reference_loader.load_reference(path)
        except FileNotFoundError:
            relocated = filedialog.askopenfilename(
                title="Reference PDF not found - locate it", filetypes=[("PDF files", "*.pdf")])
            if not relocated:
                return
            try:
                raw_ref = reference_loader.load_reference(path, pdf_path_override=relocated)
            except Exception as e:
                messagebox.showerror("Load Reference Project", f"Could not load reference: {e}")
                return
        except Exception as e:
            messagebox.showerror("Load Reference Project", f"Could not load reference: {e}")
            return
        template = reference_analyzer.analyze(raw_ref)
        raw_ref.pdf_document.close()
        self.reference_templates.append(template)
        self.reference_template = layout_template.merge_templates(self.reference_templates)
        self.toolbar.set_auto_zone_enabled(bool(self.pdf_document) and bool(self.reference_templates))
        self.set_status(f"Reference loaded: {os.path.basename(path)}  "
                         f"({len(self.reference_templates)} reference project(s) total)")

    def auto_zone(self):
        """"Auto Zone" - proposes zones for the CURRENT PDF's pages that
        have no zones yet (any page with at least one existing zone is
        skipped entirely, guaranteeing this never touches manual zoning and
        never creates a duplicate zone), using the merged reference
        template built by load_reference_project(). Creates real zones via
        the normal zone_manager.add_zone path (same ID scheme, undo,
        containment auto-parenting as manual zoning), then triggers exactly
        ONE on_zones_changed() (one redraw, one auto-save) for the whole
        run rather than one per zone."""
        if not self.pdf_document:
            messagebox.showwarning("Auto Zone", "Open a PDF first.")
            return
        if not self.reference_templates:
            messagebox.showwarning("Auto Zone", "Load at least one Reference Project first.")
            return
        result = auto_zone_engine.run_auto_zone(self.pdf_document, self.zone_manager,
                                                 self.reference_template, settings=self.settings)
        self.on_zones_changed()
        summary = (f"Auto Zone Complete\n\n"
                   f"Zones created: {result.zones_created}\n"
                   f"High confidence: {result.high}\n"
                   f"Medium confidence: {result.medium}\n"
                   f"Low confidence: {result.low}\n"
                   f"Possible overlaps: {result.overlaps}\n\n"
                   f"Pages processed: {result.pages_processed}\n"
                   f"Pages skipped (already zoned): {result.pages_skipped}")
        messagebox.showinfo("Auto Zone", summary)
        self.set_status(f"Auto Zone: {result.zones_created} zone(s) created")

    def auto_analyse(self):
        """"Auto Analyse" - a SEPARATE, no-reference-required capability
        from Auto Zone above: analyzes ONLY self.current_page's own PDF
        structure (fonts, geometry, columns, list markers, table geometry -
        see auto_zoning.page_analyzer) and proposes zones for it, with a
        preview (AutoAnalysePreviewDialog) the user must explicitly Apply
        before anything is created. Never loops over any other page.

        Duplicate-zone safety: a page with zero zones runs normally; a page
        whose zones are ALL previous auto-generated results
        (attributes["source"] == "auto" - the exact same marker
        hierarchy_builder.create_page already sets, reused unchanged, so no
        new Zone field was needed) offers AutoAnalyseConflictDialog
        (Cancel / Keep Existing / Replace); a page with ANY manual zone is
        refused outright - manually-zoned work is never silently touched.
        A zone also counts as "manual" here the moment its text has been
        hand-corrected (attributes["manual_text"]) even if it was ORIGINALLY
        auto/OCR-created - a user's explicit correction must never be
        silently wiped out by a later Replace (spec: "do not silently
        overwrite manual zones")."""
        if not self.pdf_document:
            messagebox.showwarning("Auto Analyse", "Open a PDF first.")
            return
        page = self.current_page
        existing = self.zone_manager.zones_on_page(page)
        if existing:
            manual = [z for z in existing if z.attributes.get("manual_text")
                      or z.attributes.get("source") not in ("auto", "ocr")]
            if manual:
                messagebox.showwarning(
                    "Auto Analyse",
                    f"Page {page} already has {len(manual)} manually created zone(s). "
                    f"Auto Analyse only runs on a page with no existing manual zones, "
                    f"to avoid creating duplicate or conflicting zones over your work. "
                    f"Remove or move the existing zone(s) first if you want to re-run "
                    f"Auto Analyse here.")
                return
            conflict = AutoAnalyseConflictDialog(self.root, page, len(existing))
            if conflict.result == "cancel" or conflict.result == "keep":
                return
            for z in existing:
                self.zone_manager.delete_zone(z.zone_id, cascade=True)

        result = page_analyzer.analyze_page(self.pdf_document, page)
        preview = AutoAnalysePreviewDialog(self.root, page, result.counts)
        if not preview.result:
            return

        created_ids = hierarchy_builder.create_page(self.zone_manager, page, result.predicted_zones)
        reading_order_engine.order_page(self.zone_manager, page, created_ids)
        self.on_zones_changed()
        self.set_status(f"Auto Analyse: {sum(result.counts.values())} zone(s) created on page {page}")

    def recalculate_reading_order(self):
        """"Recalculate Reading Order" (spec: "ZoneTool - Fix Multi-Column
        Reading Order") - explicit, on-demand re-run of the SAME column-
        aware core.reading_order.recompute_all_pages() every ordinary
        zoning edit already triggers automatically via on_zones_changed().
        Exists because loading a project never auto-recomputes (a saved
        project's zone.serial values are trusted as-is, so a user's own
        prior manual reorder is never silently overwritten just by
        reopening the file) - this gives a way to apply a NEWER/corrected
        algorithm to an OLDER project without editing every zone by hand.
        Never deletes/moves/retags a single zone - only zone.serial values
        (Reading Order) are recalculated; text, bboxes, tags, parent/
        child/split/merge relationships are completely untouched."""
        if not self.zone_manager.zones:
            messagebox.showinfo("Recalculate Reading Order", "No zones to recalculate.")
            return
        reading_order.recompute_all_pages(self.zone_manager)
        self.viewer.redraw()
        self.zone_tree.refresh()
        self.set_status()
        self.mark_dirty("reading_order_changed", critical=True)
        self.notify("Reading Order recalculated (column-aware)", kind="success")

    def auto_zone_index(self):
        """"Auto Zone Index" (spec: "AUTO ZONE INDEX") - detects Index
        (back-of-book) entries and their indentation-derived hierarchy on
        self.current_page ONLY (mirrors Auto Analyse's own single-page
        scope, triggered by the Auto Zone Index button gui/zone_panel.py's
        TagPanel shows contextually next to the active profile's own Index
        Primary/Secondary/Territory tags), with a preview the user must
        explicitly Apply before anything is created - see
        auto_zoning/index_auto_zone.py for the actual detection pipeline,
        kept completely separate from this UI-orchestration method.

        Manual-zone protection (spec 11) is PARTIAL/per-entry, not an
        all-or-nothing page refusal like Auto Analyse's: any detected entry
        that overlaps an existing manually-created/corrected zone (or any
        zone from a DIFFERENT auto feature - Auto Zone/Auto Analyse/OCR -
        which this feature has no business silently covering either) is
        simply never proposed in the first place, with a warning; every
        non-overlapping entry is still offered normally. A prior Auto Zone
        Index run's OWN zones (attributes["source"] == "auto_index", never
        manually corrected) are handled separately (spec 12): Replace/Keep
        Existing/Cancel, and Replace removes ONLY those specific zones -
        never any manual or unrelated zone on the same page."""
        if not self.pdf_document:
            messagebox.showwarning("Auto Zone Index", "Open a PDF first.")
            return
        index_hierarchy_tags = self.active_profile.get("index_hierarchy_tags")
        if not index_hierarchy_tags:
            messagebox.showwarning("Auto Zone Index",
                                    "The active profile has no Index hierarchy tags configured.")
            return
        page = self.current_page

        existing = self.zone_manager.zones_on_page(page)
        existing_auto_index = [z for z in existing
                                if z.attributes.get("source") == "auto_index" and not z.attributes.get("manual_text")]
        if existing_auto_index:
            conflict = IndexAutoZoneConflictDialog(self.root, page, len(existing_auto_index))
            if conflict.result in ("cancel", "keep"):
                return
            for z in existing_auto_index:
                self.zone_manager.delete_zone(z.zone_id, cascade=True)
            existing = self.zone_manager.zones_on_page(page)

        tolerance = self.settings.get("index_auto_zone_tolerance")
        tolerance = float(tolerance) if isinstance(tolerance, (int, float)) else None
        lines = self._index_lines_for_page(page)
        result = index_auto_zone.detect_index_zones(self.pdf_document, page, index_hierarchy_tags,
                                                      tolerance=tolerance, lines=lines)
        if not result.predicted_zones:
            messagebox.showinfo("Auto Zone Index",
                                 result.warnings[0] if result.warnings
                                 else "No index entries could be detected on this page.")
            return

        manual_zones = [z for z in existing
                         if z.attributes.get("manual_text") or z.attributes.get("source") != "auto_index"]
        if manual_zones:
            result.predicted_zones, protected_count = index_auto_zone.prune_protected_zones(
                result.predicted_zones, [z.bbox for z in manual_zones])
            if protected_count:
                messagebox.showwarning(
                    "Auto Zone Index",
                    "Some index zones contain manual corrections.\nThese zones were protected.\n\n"
                    f"{protected_count} detected {'entry was' if protected_count == 1 else 'entries were'} skipped.")
            if not result.predicted_zones:
                messagebox.showinfo("Auto Zone Index", "No index entries could be detected on this page.")
                return

        preview = IndexAutoZonePreviewDialog(self.root, page, result)
        if not preview.result:
            return

        created_ids = hierarchy_builder.create_page(self.zone_manager, page, result.predicted_zones)
        # Must run BEFORE order_page: reparenting the carried-over entry
        # removes it from this page's own top-level Reading Order entirely
        # (it becomes a CHILD of a previous-page ancestor), so it must also
        # be dropped from the id list order_page assigns page-local serials
        # to - otherwise it would be left with a stale, meaningless serial
        # value from a slot it no longer actually occupies.
        created_ids = self._carry_index_hierarchy_from_previous_page(page, created_ids, index_hierarchy_tags)
        reading_order_engine.order_page(self.zone_manager, page, created_ids)
        self.on_zones_changed()
        self.set_status(f"Auto Zone Index: {result.total_entries} entr{'y' if result.total_entries == 1 else 'ies'} "
                         f"created on page {page}")

    def table_generator(self):
        """Table Generator (spec: "EPUBForge - Complete Semantic Auto-Zone +
        Table Generator") - previews the row/column/cell grid core.
        table_extractor.analyze_table ALREADY detects for the SELECTED
        Table zone - the exact same call core/xml_generator.py's own
        _zone_table makes at Generate XML time, so detection itself is
        never duplicated here, only surfaced for review before generation
        (spec: "Draw/select table region... Detect table rows... columns...
        cells... Allow user to correct the table grid"). Read-only: never
        creates, modifies, or deletes any zone. Correction happens entirely
        through the EXISTING Row/Column Split Mode (Ctrl+Shift+R/C - already
        stored on the zone's own horizontal_splits/vertical_splits
        attributes (gui/pdf_viewer.py's start_region_split_mode/
        _persist_region_splits - a real, confirmed key-name mismatch was
        fixed here: this method used to read "row_splits"/"column_splits",
        keys nothing ever actually writes, so this preview could never see
        splits the user had genuinely drawn even though generation itself
        already read the correct keys and used them correctly), and
        already the only thing analyze_table's manual_row_splits/manual_
        column_splits parameters consume) - spec Part AL/BA: "reuse existing
        controls... do not create a duplicate editing architecture."

        Scoped to the literal "table" tag - the XML/BITS profile's semantic
        Table element, the only one with a real row/column/cell grid
        pipeline today (core/xml_generator.py's _zone_table). CUPEPUB's own
        Table tag ("tblimage") represents a table as a rasterized image via
        profiles/CUPEPUB/Mapping.xml, which has no grid/tr/td markup
        configured at all (confirmed by inspection) - Table Generator
        refuses cleanly for that case with a clear message (spec: "If a
        client structure has not yet been supplied... do not invent a fake
        one") rather than guessing at an unconfigured structure; CUPEPUB's
        existing image-based table behavior is completely unchanged."""
        if not self.pdf_document:
            messagebox.showwarning("Table Generator", "Open a PDF first.")
            return
        zone_id = self.selected_zone_id
        if not zone_id or zone_id not in self.zone_manager.zones:
            messagebox.showwarning("Table Generator", "Select a Table zone first.")
            return
        zone = self.zone_manager.zones[zone_id]
        if zone.tag != "table":
            messagebox.showwarning(
                "Table Generator",
                "The selected zone is not the semantic Table element.\n\n"
                "Table Generator currently supports the XML/BITS profile's Table tag, "
                "which has a real row/column/cell grid structure. The active profile's "
                f"'{zone.tag}' zone type does not have a configured grid structure to preview.")
            return
        page = self.pdf_document.get_page(zone.page)
        manual_row_splits = zone.attributes.get("horizontal_splits")
        manual_column_splits = zone.attributes.get("vertical_splits")
        try:
            structure = table_extractor.analyze_table(
                page, zone.bbox, manual_row_splits=manual_row_splits, manual_column_splits=manual_column_splits)
        except Exception as e:  # noqa: BLE001 - detection failure must never crash the app or touch the zone
            messagebox.showerror("Table Generator", f"Table detection failed:\n{e}")
            return
        TableGeneratorPreviewDialog(self.root, structure,
                                     has_manual_splits=bool(manual_row_splits or manual_column_splits))

    def _index_lines_for_page(self, page):
        """Spec 24 ("OCR COMPATIBILITY"): a SCANNED/OCR_REQUIRED page has no
        digital text layer for auto_zoning.pdf_block_detector.detect_lines
        to read - Auto Zone Index must use this app's own EXISTING OCR
        output instead, never a second/duplicate OCR pipeline, and never
        trigger a fresh OCR run itself (spec: "Do not run OCR again
        unnecessarily... use the existing OCR output if available"; a
        synchronous OCR call here would also block the GUI thread, which
        the app's own Auto Detect/OCR entire document actions deliberately
        avoid via a background thread - re-running that same machinery
        inline here would reintroduce exactly the freeze this app already
        fixed elsewhere). Returns None (index_auto_zone.detect_index_zones'
        own default: extract fresh from the digital text layer) for a
        DIGITAL/MIXED/UNKNOWN page, or for a SCANNED/OCR_REQUIRED page with
        no cached OCR result yet for the CURRENT ocr_settings - in the
        latter case Auto Zone Index will simply detect nothing and report
        "No index entries could be detected", which is honest: the user
        needs to run Auto Detect (OCR) on this page first."""
        try:
            classification = page_classifier.classify_page(self.pdf_document, page)
        except Exception:
            return None
        if classification.category not in ("SCANNED", "OCR_REQUIRED"):
            return None
        settings = self.ocr_settings
        try:
            cached = self.ocr_cache.get_latest_for_page(
                self.pdf_document.path, page, settings["engine"], settings["language"],
                settings.get("preprocessing"))
        except Exception:
            return None
        if cached is None or not cached.blocks:
            return None
        return ocr_service._ocr_blocks_to_lines(cached.blocks)

    def _carry_index_hierarchy_from_previous_page(self, page, created_top_level_ids, index_hierarchy_tags):
        """Spec 18 ("PAGE-TO-PAGE INDEX CONTINUATION"): if this page's own
        FIRST detected entry is a deeper level (Secondary/Territory) with no
        Primary/Secondary of its own on THIS page to nest under - the index
        section itself continues from the previous page, only the physical
        column happened to break there - reparent it onto the nearest
        eligible ancestor on the immediately preceding page (the last
        surviving auto_index zone whose level is strictly lower), instead of
        leaving it stranded as a page-1 top-level entry. Column/indentation
        positions themselves are still always recalculated fresh for this
        page (see index_auto_zone.detect_index_zones, called with THIS
        page's own geometry only) - only the HIERARCHY link carries over,
        never an absolute X assumption. Returns created_top_level_ids with
        the reparented id (if any) removed, for the caller to pass to
        reading_order_engine.order_page."""
        if not created_top_level_ids:
            return created_top_level_ids
        first_id = created_top_level_ids[0]
        first_zone = self.zone_manager.zones.get(first_id)
        if first_zone is None or first_zone.parent_id is not None:
            return created_top_level_ids
        tag_to_level = {tag: int(level) for level, tag in index_hierarchy_tags.items()}
        first_level = tag_to_level.get(first_zone.tag)
        if not first_level or first_level <= 1:
            return created_top_level_ids  # a genuine Level-1 entry always legitimately starts fresh, on any page
        prev_page_zones = [z for z in self.zone_manager.zones_on_page(page - 1)
                            if z.attributes.get("source") == "auto_index" and z.tag in tag_to_level]
        candidates = [z for z in prev_page_zones if tag_to_level[z.tag] < first_level]
        if not candidates:
            return created_top_level_ids
        ancestor = max(candidates, key=lambda z: (tag_to_level[z.tag], z.created_order))
        self.zone_manager.set_parent(first_id, ancestor.zone_id)
        return [zid for zid in created_top_level_ids if zid != first_id]

    def auto_zone_paragraph_current_page(self):
        """"Auto Zone -> Paragraph -> Current Page" (spec: "Paragraph Auto
        Zone"). Detects individual paragraph boundaries on self.current_page
        ONLY via auto_zoning.paragraph_auto_zone.detect_paragraph_zones,
        with a preview the user must explicitly Apply before anything is
        created - mirrors Auto Analyse/Auto Zone Index's own single-page
        scope and preview-first pattern exactly.

        Existing zones are never touched: every line already covered by an
        existing zone on this page is excluded from detection entirely
        (spec 3), so re-running this is always safe (never creates a
        duplicate), and the whole batch of created zones is ONE undoable
        operation (spec 11 - ZoneManager.begin_batch/end_batch)."""
        if not self.pdf_document:
            messagebox.showwarning("Paragraph Auto Zone", "Open a PDF first.")
            return
        page = self.current_page
        existing_bboxes = [z.bbox for z in self.zone_manager.zones_on_page(page)]
        lines = self._index_lines_for_page(page)
        result = paragraph_auto_zone.detect_paragraph_zones(
            self.pdf_document, page, existing_bboxes=existing_bboxes, lines=lines)
        if not result.predicted_zones:
            messagebox.showinfo("Paragraph Auto Zone",
                                 result.warnings[0] if result.warnings
                                 else "No paragraphs could be detected on this page.")
            return
        preview = ParagraphAutoZonePreviewDialog(self.root, page, result)
        if not preview.result:
            return
        self.zone_manager.begin_batch()
        try:
            created_ids = hierarchy_builder.create_page(self.zone_manager, page, result.predicted_zones)
            reading_order_engine.order_page(self.zone_manager, page, created_ids)
        finally:
            self.zone_manager.end_batch()
        self.on_zones_changed()
        self.set_status(f"Paragraph Auto Zone: {result.paragraphs_detected} zone(s) created on page {page}")

    def auto_zone_paragraph_entire_file(self):
        """"Auto Zone -> Paragraph -> Entire File" (spec 2): runs the SAME
        per-page detection across every page of the document, skipping any
        text already covered by an existing zone (spec 3) and never
        re-running OCR (spec 13, via self._index_lines_for_page - the SAME
        cached-OCR-or-digital-text helper Auto Zone Index already uses, so
        this feature never re-parses OCR results itself). Mirrors OCR
        Entire Document's own whole-document confirm-then-summarize UX (a
        per-page preview across potentially hundreds of pages would defeat
        the point of a batch action) - the ENTIRE run is still just ONE
        undoable operation (spec 11)."""
        if not self.pdf_document:
            messagebox.showwarning("Paragraph Auto Zone", "Open a PDF first.")
            return
        total_pages = self.page_count
        if not messagebox.askyesno(
                "Paragraph Auto Zone - Entire File",
                f"This will detect paragraphs across all {total_pages} page(s).\n\n"
                f"Existing zones are never touched - any text already covered by an "
                f"existing zone is skipped. Continue?"):
            return

        self.zone_manager.begin_batch()
        total_created = 0
        pages_with_zones = 0
        try:
            for page in range(1, total_pages + 1):
                existing_bboxes = [z.bbox for z in self.zone_manager.zones_on_page(page)]
                lines = self._index_lines_for_page(page)
                result = paragraph_auto_zone.detect_paragraph_zones(
                    self.pdf_document, page, existing_bboxes=existing_bboxes, lines=lines)
                if not result.predicted_zones:
                    continue
                created_ids = hierarchy_builder.create_page(self.zone_manager, page, result.predicted_zones)
                reading_order_engine.order_page(self.zone_manager, page, created_ids)
                total_created += len(created_ids)
                pages_with_zones += 1
        finally:
            self.zone_manager.end_batch()
        self.on_zones_changed()
        messagebox.showinfo("Paragraph Auto Zone - Entire File",
                             f"Paragraph Auto Zone complete.\n\n"
                             f"Zones created: {total_created}\n"
                             f"Pages with new zones: {pages_with_zones} / {total_pages}")
        self.set_status(f"Paragraph Auto Zone: {total_created} zone(s) created across {pages_with_zones} page(s)")

    def auto_zone_bibliography_by_pattern(self):
        """Automatically detect bibliography/reference entries.

        IMPORTANT: this workflow must NOT require manually created sample
        zones.  The bibliography detector learns the page/document layout
        automatically when ``learn_pattern`` is called with no samples.

        Existing zones are preserved: their bounding boxes are supplied to
        the detector so candidates already covered by manual/project zones
        are skipped.  The user still gets the existing scope and preview
        dialogs before any new zones are created.
        """
        if not self.pdf_document:
            messagebox.showwarning("Bibliography Auto Zone", "Open a PDF first.")
            return

        page = self.current_page

        # NO BibliographySampleSelectDialog here.  The fixed bibliography
        # detector supports automatic pattern discovery when sample_zones is
        # empty.  This is the critical fix for the old "Select 3-4 samples"
        # dialog appearing on a page with zero zones.
        pattern, error = bibliography_auto_zone.learn_pattern(
            self.pdf_document,
            sample_zones=[],
        )
        if error:
            messagebox.showwarning("Bibliography Auto Zone", error)
            return

        scope = BibliographyScopeDialog(self.root)
        if scope.result == "cancel":
            return

        if scope.result == "current":
            existing_bboxes = [z.bbox for z in self.zone_manager.zones_on_page(page)]
            result = bibliography_auto_zone.detect_bibliography_zones(
                self.pdf_document,
                page,
                pattern,
                existing_bboxes=existing_bboxes,
            )
            results = [(page, result)]
            page_label = f"Page {page}"
        else:
            results = []
            for p in range(1, self.page_count + 1):
                existing_bboxes = [
                    z.bbox for z in self.zone_manager.zones_on_page(p)
                ]
                result = bibliography_auto_zone.detect_bibliography_zones(
                    self.pdf_document,
                    p,
                    pattern,
                    existing_bboxes=existing_bboxes,
                )
                if result.entries_detected or result.pattern_changed or result.predicted_zones:
                    results.append((p, result))
            page_label = "Entire File"

        if not results:
            messagebox.showinfo(
                "Bibliography Auto Zone",
                "No bibliography/reference entries were detected.",
            )
            return

        preview = BibliographyAutoZonePreviewDialog(
            self.root, page_label, results
        )
        if not preview.result:
            return

        self.zone_manager.begin_batch()
        total_created = 0
        try:
            for p, result in results:
                if not result.predicted_zones:
                    continue
                created_ids = hierarchy_builder.create_page(
                    self.zone_manager, p, result.predicted_zones
                )
                if created_ids:
                    reading_order_engine.order_page(
                        self.zone_manager, p, created_ids
                    )
                    total_created += len(created_ids)
        finally:
            self.zone_manager.end_batch()

        self.on_zones_changed()
        self.set_status(
            f"Bibliography Auto Zone: {total_created} zone(s) created"
        )

    def show_empty_canvas_context_menu(self, event):
        """Right-click on EMPTY PDF viewer canvas (no zone under the
        cursor) - spec: "Also add to the PDF Viewer right-click menu: Auto
        Zone -> Paragraph -> Current Page" / "Auto Zone -> Bibliography /
        Auto Zone by Pattern". A brand-new menu, never the existing
        self.context_menu built by _build_context_menu/shown by
        show_zone_context_menu above - that one is shown only when an
        existing zone IS hit under the cursor and must keep behaving
        exactly as before (gui/pdf_viewer.py's _on_right_click routes to
        one or the other based on that hit-test, never both)."""
        if not self.pdf_document:
            return
        menu = tk.Menu(self.root, tearoff=0)
        paragraph_menu = tk.Menu(menu, tearoff=0)
        paragraph_menu.add_command(label="Current Page", command=self.auto_zone_paragraph_current_page)
        menu.add_cascade(label="Auto Zone: Paragraph", menu=paragraph_menu)
        menu.add_command(label="Auto Zone: Bibliography / Auto Zone by Pattern",
                          command=self.auto_zone_bibliography_by_pattern)
        menu.add_separator()
        menu.add_command(label="Insert Page Number...", command=lambda e=event: self._prompt_insert_page_number(e))
        menu.tk_popup(event.x_root, event.y_root)

    def _prompt_insert_page_number(self, event):
        """Right-click empty canvas -> Insert Page Number. Creates REAL
        pagebreak/page-number zone metadata - never normal body text,
        never a superscript appended to a nearby paragraph (spec:
        "must NOT simply create normal body text"). Reuses the exact
        tag/attrs the active profile's own "Page Number" toolbox button
        would use (found via page_marker_tags - never hardcoded per-
        profile, since CUPEPUB's own page-marker tag literal, "pagenum",
        differs from the XML/EPUB profiles' "pagenumber"), and the exact
        existing manual-PageNum-correction mechanism (ZoneManager.
        set_zone_text, which marks manual_text=True so a later re-
        extraction never silently overwrites the typed value) the
        Properties panel's own PageNum entry field already uses - no
        second, competing page-number creation path. Purely local: no
        OCR, no re-verification, no PDF reload - just one new zone plus
        the existing autosave/undo wiring every add_zone call already
        has."""
        from tkinter import simpledialog
        if not self.pdf_document:
            return
        page_marker_tags = set(self.active_profile.get("page_marker_tags") or [])
        entry = next((e for e in self.active_tag_buttons if e[1] in page_marker_tags), None)
        if entry is None:
            messagebox.showinfo("Insert Page Number", "The active profile has no Page Number tag configured.")
            return
        _label, tag, attrs = entry
        value = simpledialog.askstring("Insert Page Number", "Page Number:", parent=self.root)
        if value is None or not value.strip():
            return
        x, y = self.viewer.canvas_point(event)
        # A small, fixed-size bbox centered on the click point - large
        # enough to be individually selectable, small enough to never
        # visually swallow a real body zone the operator can still see
        # around it. Independent of body-text reading order by
        # construction: it is its own zone, never appended into a
        # paragraph's own text.
        half_h, half_w = 10, 30
        bbox = self.viewer.screen_to_pdf((x - half_w, y - half_h, x + half_w, y + half_h))
        zone = self.zone_manager.add_zone(self.current_page, tag, bbox, attributes=dict(attrs))
        self.zone_manager.set_zone_text(zone.zone_id, value.strip())
        # add_zone always places a brand-new zone at the END of its own
        # page's Reading Order (its own documented, shared default for
        # every zone type - never changed here). Position-aware insertion
        # (spec: "If the user inserts page number between two body
        # zones... preserve that reading order") reuses the EXISTING
        # set_reading_order mechanism (the same one the "Set Reading
        # Order..." context menu item already calls) to move it to where
        # its own Y-coordinate actually falls among the page's other
        # top-level zones - never a second reading-order algorithm.
        page_zones = sorted(
            (z for z in self.zone_manager.zones.values()
             if z.page == self.current_page and z.zone_id != zone.zone_id
             and self.zone_manager._counts_in_reading_order(z)),
            key=lambda z: z.serial or 0)
        insert_at = 1 + sum(1 for z in page_zones if z.bbox[1] < bbox[1])
        self.zone_manager.set_reading_order(zone.zone_id, insert_at)
        self.on_zones_changed()
        self.set_status(f"Inserted page number: {value.strip()}")

    def open_ocr_settings(self):
        dlg = OCRSettingsDialog(self.root, self.ocr_settings)
        if dlg.result is None:
            return
        self.ocr_settings = dlg.result
        ui_prefs.save_ui_prefs({
            "ocr_engine": self.ocr_settings["engine"],
            "ocr_mode": self.ocr_settings["mode"],
            "ocr_language": self.ocr_settings["language"],
            "ocr_dpi": self.ocr_settings["dpi"],
            "ocr_preprocessing": self.ocr_settings["preprocessing"],
        })
        self.notify("OCR settings saved", kind="success")

    def auto_detect_ocr(self):
        """"Auto Detect (OCR)" - core.ocr.ocr_service.analyze_page_with_ocr
        run for self.current_page only (RULE 3: page-by-page, never a
        whole-document decision), on a background thread so a real OCR call
        never blocks the GUI thread (spec: OCR must never freeze the app).
        Reuses the EXACT same manual-zone-conflict guard, preview dialog,
        and zone-creation/reading-order calls Auto Analyse already uses
        above - the only difference is what produces the predicted zones
        and that it may need to wait for a background OCR call first.

        Not reentrant: a second click while a job is already running is
        ignored (self._ocr_job_running), since pdf_document/PyMuPDF page
        objects are read from the worker thread and are not safe to also
        read concurrently from a second overlapping job."""
        if not self.pdf_document:
            messagebox.showwarning("Auto Detect", "Open a PDF first.")
            return
        if self._ocr_job_running:
            messagebox.showinfo("Auto Detect", "An OCR job is already running - wait for it to finish.")
            return
        page = self.current_page
        existing = self.zone_manager.zones_on_page(page)
        if existing:
            manual = [z for z in existing if z.attributes.get("manual_text")
                      or z.attributes.get("source") not in ("auto", "ocr")]
            if manual:
                messagebox.showwarning(
                    "Auto Detect",
                    f"Page {page} already has {len(manual)} manually created zone(s). "
                    f"Auto Detect only runs on a page with no existing manual zones, "
                    f"to avoid creating duplicate or conflicting zones over your work.")
                return
            conflict = AutoAnalyseConflictDialog(self.root, page, len(existing))
            if conflict.result == "cancel" or conflict.result == "keep":
                return
            for z in existing:
                self.zone_manager.delete_zone(z.zone_id, cascade=True)

        self._ocr_job_running = True
        result_queue = queue.Queue()
        progress = OCRProgressDialog(self.root, page)

        pdf_document, settings = self.pdf_document, self.ocr_settings

        # Pre-flight classification message (spec: tell the user up front
        # which path will be taken) - classify_page only inspects the
        # existing digital text/image layers already on the page (cheap,
        # no OCR call), so this is safe to run synchronously before
        # starting the background worker.
        mode = settings.get("mode", "auto")
        if mode == "digital_only":
            progress.set_status(f"Digital text detected - existing extractor will be used (page {page}).")
        elif mode == "force_ocr":
            progress.set_status(f"Using prepared OCR cache - no new OCR will run (page {page}).")
        else:
            category = page_classifier.classify_page(pdf_document, page).category
            if category in ("DIGITAL", "MIXED"):
                progress.set_status(f"Digital text detected - existing extractor will be used (page {page}).")
            else:
                progress.set_status(f"Using prepared OCR cache - no new OCR will run (page {page}).")

        def worker():
            # Zoning is strictly cache-only.  It must never start PaddleOCR
            # for the page or for individual zones.  Prepare OCR Cache is the
            # only operation that performs the expensive OCR inference.
            analysis = ocr_service.analyze_page_with_cached_ocr(
                pdf_document, page, engine_name=settings["engine"], language=settings["language"],
                dpi=settings["dpi"], preprocessing_settings=settings["preprocessing"], cache=self.ocr_cache)
            result_queue.put(analysis)

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(150, lambda: self._poll_ocr_job(page, progress, result_queue))

    def _poll_ocr_job(self, page: int, progress: OCRProgressDialog, result_queue: queue.Queue):
        try:
            analysis = result_queue.get_nowait()
        except queue.Empty:
            self.root.after(150, lambda: self._poll_ocr_job(page, progress, result_queue))
            return

        self._ocr_job_running = False
        cancelled = progress.cancelled
        progress.close()
        if cancelled:
            self.set_status("Auto Detect cancelled - result discarded.")
            return

        if analysis.error:
            messagebox.showerror("Auto Detect", f"OCR failed on page {page}:\n\n{analysis.error}")
            return

        engine_note = f" via {analysis.ocr_result.engine}" if analysis.used_ocr and analysis.ocr_result else ""
        preview = AutoAnalysePreviewDialog(self.root, page, analysis.counts)
        if not preview.result:
            return

        created_ids = hierarchy_builder.create_page(self.zone_manager, page, analysis.predicted_zones)
        reading_order_engine.order_page(self.zone_manager, page, created_ids)
        self.on_zones_changed()
        self.set_status(
            f"Auto Detect ({analysis.classification.category}{engine_note}): "
            f"{sum(analysis.counts.values())} zone(s) created on page {page}")

    def prepare_ocr_cache(self):
        """Prepare OCR once per page and store it in the current project's
        ocr_cache folder. This action does not create/change/delete zones."""
        if not self.pdf_document:
            messagebox.showwarning("Prepare OCR Cache", "Open a PDF first.")
            return
        if self._ocr_job_running:
            messagebox.showinfo("Prepare OCR Cache", "An OCR job is already running - wait for it to finish.")
            return
        if not self.project_path:
            messagebox.showwarning("Prepare OCR Cache", "Save/create the project first.")
            return
        total = self.page_count
        if not messagebox.askyesno(
                "Prepare OCR Cache",
                f"Prepare OCR cache for all {total} PDF page(s)?\n\n"
                "OCR is performed once per eligible page and stored locally in this project.\n"
                "Verify and Generate XHTML will reuse the stored results and will not OCR again.\n\n"
                "The first run may take time; later Verify/Generate runs will be much faster."):
            return
        self._ocr_job_running = True
        result_queue = queue.Queue()
        progress = OCRProgressDialog(self.root, total_pages=total)
        pdf_document, settings, cache = self.pdf_document, self.ocr_settings, self.ocr_cache
        pages = list(range(1, total + 1))

        def progress_cb(current, total_pages, page_number):
            result_queue.put(("progress", current, total_pages, page_number))

        def worker():
            try:
                results = ocr_service.cache_document_with_ocr(
                    pdf_document, pages, engine_name=settings["engine"], language=settings["language"],
                    dpi=settings["dpi"], preprocessing_settings=settings["preprocessing"],
                    cache=cache,
                    # Explicit "Prepare OCR Cache" means PREPARE OCR for
                    # every PDF page, including pages classified as DIGITAL.
                    # Auto Detect keeps its existing automatic classification;
                    # this operation is intentionally different: it creates
                    # the persistent OCR dataset that Verify/Generate can
                    # reuse later without ever starting OCR themselves.
                    mode="force_ocr",
                    progress_callback=progress_cb, should_cancel=lambda: progress.cancelled)
                result_queue.put(("done", results))
            except Exception as exc:
                result_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                item = result_queue.get_nowait()
            except queue.Empty:
                self.root.after(150, poll)
                return
            if item[0] == "progress":
                _, current, total_pages, page_number = item
                progress.set_progress(current, total_pages, page_number)
                self.root.after(50, poll)
                return
            self._ocr_job_running = False
            progress.close()
            if item[0] == "error":
                messagebox.showerror("Prepare OCR Cache", f"OCR cache preparation failed:\n{item[1]}")
                return
            results = item[1]
            failed = [(r.page_number, r.error) for r in results if r.error]
            # Count actual persistent cache entries, not merely pages that
            # were classified as OCR-eligible.  Prepare OCR Cache always
            # uses force_ocr, so a successful page here must have a reusable
            # OCR result on disk.
            cached_ok = 0
            for r in results:
                if r.error or r.ocr_result is None:
                    continue
                try:
                    engine_probe = ocr_service.get_engine(settings["engine"])
                    cached = cache.get(
                        pdf_document.path, r.page_number, settings["engine"],
                        engine_probe.version, settings["language"],
                        settings["preprocessing"]
                    )
                    if cached is not None:
                        cached_ok += 1
                except Exception:
                    pass
            summary = (f"OCR cache prepared.\n\nPages processed: {len(results)}/{len(pages)}\n"
                       f"Cached OCR pages: {cached_ok}\nFailed: {len(failed)}\n\n"
                       "Verify and Generate XHTML will now reuse the stored OCR results.")
            if failed:
                summary += "\n\nFailed pages:\n" + "\n".join(f"- {p}: {e}" for p, e in failed[:15])
                messagebox.showwarning("Prepare OCR Cache", summary)
            else:
                messagebox.showinfo("Prepare OCR Cache", summary)
            self.set_status(f"OCR cache ready: {cached_ok} page(s)")

        poll()

    def ocr_entire_document(self):
        """"OCR Entire Document" - the whole-book batch counterpart to
        single-page Auto Detect (spec 24: large books must process
        incrementally with "Page 24/471" progress, never load every page
        as an image into memory at once). Runs
        core.ocr.ocr_service.analyze_document_with_ocr on a background
        thread (a real multi-page OCR run can take minutes), applying each
        page's result to the SAME zone-creation path Auto Detect already
        uses (hierarchy_builder.create_page + reading_order_engine.
        order_page) - no second, batch-specific zoning path.

        Unlike single-page Auto Detect, a per-page preview/approve dialog
        for every one of potentially hundreds of pages would defeat the
        point of a batch action - pages with any manual zone (including a
        hand-corrected OCR/auto zone - attributes["manual_text"]) are
        silently SKIPPED (never touched), and every other eligible page's
        existing auto/OCR zones are replaced outright. This is disclosed
        up front in the confirmation prompt, not decided silently."""
        if not self.pdf_document:
            messagebox.showwarning("OCR Entire Document", "Open a PDF first.")
            return
        if self._ocr_job_running:
            messagebox.showinfo("OCR Entire Document", "An OCR job is already running - wait for it to finish.")
            return
        total_pages = self.page_count
        if not messagebox.askyesno(
                "OCR Entire Document",
                f"This will run OCR analysis across all {total_pages} page(s) using "
                f"{self.ocr_settings['engine']} (mode: {self.ocr_settings.get('mode', 'auto')}).\n\n"
                f"Pages with any manually created or hand-corrected zone will be SKIPPED. "
                f"Every other page's existing auto/OCR zones will be REPLACED.\n\n"
                f"This can take a long time for a large book. Continue?"):
            return

        self._ocr_job_running = True
        result_queue = queue.Queue()
        progress = OCRProgressDialog(self.root, total_pages=total_pages)
        pdf_document, settings, zone_manager = self.pdf_document, self.ocr_settings, self.zone_manager

        eligible_pages = []
        skipped_manual = 0
        for page in range(1, total_pages + 1):
            existing = zone_manager.zones_on_page(page)
            manual = [z for z in existing if z.attributes.get("manual_text")
                      or z.attributes.get("source") not in ("auto", "ocr")]
            if manual:
                skipped_manual += 1
                continue
            eligible_pages.append(page)

        def progress_cb(current, total, page_number):
            result_queue.put(("progress", current, total, page_number))

        def worker():
            results = ocr_service.analyze_document_with_ocr(
                pdf_document, eligible_pages, engine_name=settings["engine"], language=settings["language"],
                dpi=settings["dpi"], preprocessing_settings=settings["preprocessing"], cache=self.ocr_cache,
                mode=settings.get("mode", "auto"), progress_callback=progress_cb,
                should_cancel=lambda: progress.cancelled)
            result_queue.put(("done", results))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_ocr_document_job(progress, result_queue, skipped_manual, len(eligible_pages))

    def _poll_ocr_document_job(self, progress: OCRProgressDialog, result_queue: queue.Queue,
                                 skipped_manual: int, eligible_count: int):
        try:
            item = result_queue.get_nowait()
        except queue.Empty:
            self.root.after(150, lambda: self._poll_ocr_document_job(
                progress, result_queue, skipped_manual, eligible_count))
            return

        if item[0] == "progress":
            _, current, total, page_number = item
            progress.set_progress(current, total, page_number)
            self.root.after(50, lambda: self._poll_ocr_document_job(
                progress, result_queue, skipped_manual, eligible_count))
            return

        _, results = item
        self._ocr_job_running = False
        progress.close()

        zones_created = 0
        pages_failed = []
        for analysis in results:
            page = analysis.page_number
            if analysis.error:
                pages_failed.append((page, analysis.error))
                debug_log.log("OCR", f"Page {page} failed in batch: {analysis.error}")
                continue
            existing = self.zone_manager.zones_on_page(page)
            for z in existing:
                self.zone_manager.delete_zone(z.zone_id, cascade=True)
            created_ids = hierarchy_builder.create_page(self.zone_manager, page, analysis.predicted_zones)
            reading_order_engine.order_page(self.zone_manager, page, created_ids)
            zones_created += sum(analysis.counts.values())

        self.on_zones_changed()
        summary = (f"OCR Entire Document{' (cancelled)' if progress.cancelled else ''}\n\n"
                   f"Pages processed: {len(results)}/{eligible_count}\n"
                   f"Pages skipped (manual zones present): {skipped_manual}\n"
                   f"Pages failed: {len(pages_failed)}\n"
                   f"Zones created: {zones_created}")
        if pages_failed:
            shown = "\n".join(f"- Page {p}: {e}" for p, e in pages_failed[:15])
            more = f"\n... and {len(pages_failed) - 15} more" if len(pages_failed) > 15 else ""
            messagebox.showwarning("OCR Entire Document - completed with errors",
                                    summary + "\n\nFailed pages:\n" + shown + more)
        else:
            messagebox.showinfo("OCR Entire Document", summary)
        self.set_status(f"OCR Entire Document: {zones_created} zone(s) created across {len(results)} page(s)")

    def save_corrections_as_template(self):
        """"Save Corrections as Template" - saves the CURRENT project (after
        Auto Zone + any manual corrections) as a brand-new project JSON via
        the same project_manager.save_project() Save Project already uses -
        never overwrites the original reference file, and the result is
        directly usable as a future "Load Reference Project" input, since a
        reference IS just a normal project file."""
        if not self.pdf_path:
            messagebox.showwarning("Save Corrections as Template", "Open a PDF first.")
            return
        path = filedialog.asksaveasfilename(
            initialdir=str(APP_ROOT / "projects"), defaultextension=".json",
            filetypes=[("BITS Tool project", "*.json")],
            title="Save Corrections as Template (new file - never overwrites the original reference)")
        if not path:
            return
        project_manager.save_project(path, self.pdf_path, 200, self.zoom, self.zone_manager,
                                      settings=self.settings, page_count=self.page_count,
                                      page_rotations=self.page_rotations)
        self.set_status(f"Corrections saved as template: {os.path.basename(path)}")

    # ---------------- page nav ----------------
    def prev_page(self):
        self.goto_page(str(self.current_page - 1))

    def next_page(self):
        self.goto_page(str(self.current_page + 1))

    def goto_page(self, value):
        if not self.pdf_document:
            return
        try:
            page = int(value)
        except (TypeError, ValueError):
            return
        page = max(1, min(self.page_count, page))
        self.current_page = page
        self.toolbar.set_page_info(page, self.page_count)
        self.viewer.load_page(page)
        self.toolbar.set_rotation_label(self.page_rotations.get(page, 0))
        self.set_status()

    # ---------------- rotation ----------------
    def _set_page_rotation(self, new_rotation):
        """Applies a new rotation value for the CURRENT page only (each page
        keeps its own independent rotation - see self.page_rotations) and
        fully refreshes the display. Deliberately does NOT go through the
        normal on_zones_changed() path: rotation never touches any zone's
        bbox/tag/parent/reading-order (see gui/pdf_viewer.py's rotation-aware
        pdf_to_screen/screen_to_pdf - zone.bbox always stays in the PDF's own
        native, unrotated coordinate space), so there is nothing for the
        automatic Reading Order recalculation to legitimately recompute -
        triggering it anyway would violate the explicit "rotation must never
        change Reading Order" requirement, even though the values would
        happen to come out identical."""
        self.page_rotations[self.current_page] = new_rotation % 360
        self.viewer.load_page(self.current_page)
        self.toolbar.set_rotation_label(self.page_rotations[self.current_page])
        self.zone_tree.refresh()
        self.set_status()
        self.mark_dirty("page_rotation_changed")

    def rotate_page_cw(self):
        if not self.pdf_document:
            return
        current = self.page_rotations.get(self.current_page, 0)
        self._set_page_rotation(current + 90)

    def rotate_page_ccw(self):
        if not self.pdf_document:
            return
        current = self.page_rotations.get(self.current_page, 0)
        self._set_page_rotation(current - 90)

    def reset_page_rotation(self):
        if not self.pdf_document:
            return
        self._set_page_rotation(0)

    # ---------------- zoom ----------------
    # Three independent concepts, per spec: PDF coordinates (zone.bbox, never
    # touched here), viewer zoom/fit mode (this section - screen display
    # only), and image extraction DPI (Settings > Image DPI, core/
    # image_extractor.py - crop/asset rendering only). None of the methods
    # below ever reads or writes self.settings["image_dpi"].
    def zoom_in(self):
        self.zoom_mode = "manual"
        self.zoom = min(MAX_ZOOM, self.zoom * ZOOM_STEP)
        self._apply_zoom()

    def zoom_out(self):
        self.zoom_mode = "manual"
        self.zoom = max(MIN_ZOOM, self.zoom / ZOOM_STEP)
        self._apply_zoom()

    def zoom_reset(self):
        self.zoom_mode = "manual"
        self.zoom = 1.0
        self._apply_zoom()

    def _compute_fit_zoom(self, mode):
        """mode: 'fit_width' or 'fit_page'. Returns None if there's nothing to fit yet."""
        if not self.viewer.base_image:
            return None
        cw = self.viewer.canvas.winfo_width()
        ch = self.viewer.canvas.winfo_height()
        iw, ih = self.viewer.base_image.width, self.viewer.base_image.height
        if cw <= 1 or ch <= 1 or iw <= 0 or ih <= 0:
            return None
        if mode == "fit_page":
            return max(MIN_ZOOM, min(MAX_ZOOM, min(cw / iw, ch / ih)))
        return max(MIN_ZOOM, min(MAX_ZOOM, cw / iw))  # fit_width

    def set_zoom_mode(self, mode):
        self.zoom_mode = mode
        self.settings["viewer_zoom_mode"] = mode
        if mode != "manual":
            new_zoom = self._compute_fit_zoom(mode)
            if new_zoom:
                self.zoom = new_zoom
        self._apply_zoom()

    def _on_viewer_resize(self, event=None):
        if self.zoom_mode == "manual":
            return
        # Debounced: a window drag fires many Configure events, and each
        # recompute re-renders the page image (PIL resize) - coalesce them.
        if self._resize_after_id:
            self.root.after_cancel(self._resize_after_id)
        self._resize_after_id = self.root.after(120, self._apply_fit_zoom_now)

    def _apply_fit_zoom_now(self):
        self._resize_after_id = None
        if self.zoom_mode == "manual":
            return
        new_zoom = self._compute_fit_zoom(self.zoom_mode)
        if new_zoom and abs(new_zoom - self.zoom) > 1e-6:
            self.zoom = new_zoom
            self._apply_zoom()

    def _apply_zoom(self):
        self.toolbar.set_zoom_label(self.zoom)
        self.viewer.redraw()

    # ---------------- profile ----------------
    def set_profile(self, name: str, persist: bool = True):
        """Profile dropdown (toolbar) / project load. Reloads the profile's
        JSON fresh from disk (profile_manager.reload_profile) so hand-edited
        tag lists/mapping paths take effect immediately, rebuilds the Tag
        Panel's button set, and updates which Generate button is enabled -
        NEVER touches any existing zone. A zone tagged under the previous
        profile keeps its exact tag string; it just won't have a
        highlighted button in the panel until the profile is switched back,
        and still shows normally (tag name as plain text) in the Zone
        Hierarchy tree per the spec's "keep zones, warn, never silently
        delete" requirement."""
        try:
            profile = profile_manager.reload_profile(name)
        except profile_manager.ProfileLoadError as e:
            messagebox.showerror("Profile", f"Could not load the {name} profile:\n{e}")
            return
        self.active_profile = profile
        self.active_tag_buttons = profile_manager.tag_buttons_of(profile)
        self.settings["profile"] = profile.get("name", name).upper()
        self.tag_panel.rebuild(self.active_tag_buttons, profile.get("tag_colors", {}), profile.get("tag_groups"))
        self.toolbar.set_profile_options(self.settings["profile"])
        self.update_generation_controls()
        self._apply_cup_shortcuts(profile)
        self.smart_az.on_profile_changed()
        self.set_status(f"Profile: {self.settings['profile']}")
        if persist:
            # Critical (spec section 13/38) - never triggers regeneration,
            # only persists the new active profile setting.
            self.mark_dirty("profile_changed", critical=True)

    def update_generation_controls(self):
        """Generate XML produces the active profile's output: BITS 2.2 book
        (profile BITS) or JATS 1.4 article (profile JATS)."""
        kind = "JATS" if (self.active_profile.get("name") or "").upper() == "JATS" else "BITS"
        self.toolbar.set_xml_enabled(True)
        self.toolbar.set_output_label(f"Output: {kind} XML")

    def _apply_cup_shortcuts(self, profile):
        """Registers/unregisters CUPEPUB's keyboard shortcuts (core/
        cup_config.py's cup_shortcuts, themselves read live from
        CUPEPUB_Zoning.xml's shortcutKey/usectrl/useshift/usealt) on every
        profile switch - unbinding whatever this method bound last time
        first, so switching away from CUPEPUB never leaves a stray shortcut
        active for a profile that doesn't define it, and switching back
        (e.g. after CUPEPUB_Zoning.xml was hand-edited and the profile
        reloaded) always reflects the current configuration, never a stale
        one."""
        for seq in self._cup_shortcut_seqs:
            try:
                # bind_all registers on the "all" bindtag, not root's own -
                # plain unbind() would silently no-op and leak every one of
                # these across every subsequent profile switch.
                self.root.unbind_all(seq)
            except tk.TclError:
                pass
        self._cup_shortcut_seqs = []
        claimed_seqs = set()
        for entry in profile.get("cup_shortcuts") or []:
            def handler(event, label=entry["cup_name"]):
                self.tag_panel.select_label(label)
                return "break"

            for seq in entry.get("seqs", []):
                try:
                    self.root.bind_all(seq, handler)
                    self._cup_shortcut_seqs.append(seq)
                    claimed_seqs.add(seq.lower())
                except tk.TclError:
                    pass  # an invalid/reserved sequence is skipped, never fatal

        # Ctrl+F focuses the Tag Search box (spec 66.6/66.22) - but ONLY if
        # the active profile doesn't already claim it as a tag shortcut
        # (CUPEPUB's "TblImage" does - spec 66.22 explicitly requires never
        # overriding an existing CUPEPUB shortcut). Re-evaluated on every
        # profile switch, so it activates automatically for XML/EPUB (which
        # have no shortcuts at all) and steps aside whenever CUPEPUB's own
        # config claims it.
        if "<control-f>" not in claimed_seqs:
            search_seq = "<Control-f>"
            self.root.bind_all(search_seq, lambda e: self.tag_panel.focus_search() or "break")
            self._cup_shortcut_seqs.append(search_seq)

    # ---------------- tag / zone selection ----------------
    def set_active_tag(self, tag, attrs):
        self.active_tag = (tag, attrs) if tag else None

    def on_zone_selected(self, zone_id):
        self.selected_zone_id = zone_id
        self.zone_tree.select(zone_id)
        self.tag_panel.highlight_tag_for_zone(self.zone_manager.zones.get(zone_id))
        self.set_status()

    def on_zone_created(self, zone_id):
        """Handle creation of one interactive zone without doing a
        project-wide Reading Order recomputation.

        ZoneManager.add_zone() already maintains the canonical Reading
        Order for the affected page. Calling on_zones_changed() with its
        default argument here used to call reading_order.recompute_all_pages(),
        which recalculated every page after drawing just one zone and caused
        the 5-10 second UI freeze on larger PDFs.

        Keep the normal UI refresh and autosave, but explicitly skip the
        redundant full-document Reading Order pass.
        """
        self.viewer.selected_zone_id = zone_id
        self.on_zones_changed(recompute_reading_order=False)
        self.on_zone_selected(zone_id)

    def on_zones_changed(self, recompute_reading_order=True, pages=None):
        # Reading Order is normally maintained by ZoneManager during the
        # individual mutation.  This flag is retained for operations that
        # explicitly require a full canonical recomputation (for example
        # Undo/Redo or the dedicated Recalculate Reading Order command).
        # Interactive zone creation deliberately passes False because
        # ZoneManager.add_zone() has already normalized that page.
        # recompute_reading_order=False is used ONLY by the explicit manual
        # reorder controls (_move_zone/_prompt_set_reading_order) so their
        # own just-applied choice isn't immediately overwritten by the very
        # refresh they themselves trigger here - any OTHER zoning change on
        # the page (add/move/resize/delete/split/merge/retag) still fully
        # recomputes on top of it, same as always.
        if recompute_reading_order:
            if pages:
                # the caller knows exactly which page(s) it changed (e.g.
                # Split) - the other pages' order cannot have changed, and
                # recomputing all of them made every split stall on big books
                if self.zone_manager.pdf_document is not None:
                    for page in sorted(set(pages)):
                        reading_order.recompute_page_order(self.zone_manager, page)
            else:
                reading_order.recompute_all_pages(self.zone_manager)
        self.viewer.redraw()
        self.zone_tree.refresh()
        self.set_status()
        self.toolbar.set_undo_redo_enabled(self.zone_manager.can_undo(), self.zone_manager.can_redo())
        self.smart_az.notify_changed()
        # UI refresh/autosave remains centralized here for every mutation.
        # Reading Order itself is maintained by ZoneManager for local
        # mutations; callers that need a full recomputation explicitly pass
        # recompute_reading_order=True.
        # spec section 13's "critical structural changes" list is exactly
        # what on_zones_changed already covers, so critical=True (a very
        # short debounce, not a hard-coded per-call-site reason) satisfies
        # both "every change autosaves" (section 9) and "critical changes
        # save immediately or with a very short delay" (section 13) without
        # threading a reason string through 20+ call sites across three
        # files that all already exist for a different purpose.
        self.mark_dirty("zone_changed", critical=True)

    def _repair_broken_merges(self):
        """Before validating for Generate/Verify: a Merge Previous link whose
        target zone was deleted (projects edited before this fix) is fixed
        in place instead of blocking generation with an "orphaned
        continuation fragment" error."""
        try:
            if self.zone_manager.repair_orphan_merges():
                self.on_zones_changed(recompute_reading_order=False)
        except Exception as exc:  # never block generation on the repair itself
            debug_log.log("MERGE", f"repair_orphan_merges failed: {exc}")

    def on_zones_reindexed(self):
        self.set_status()

    # ---------------- autosave ----------------
    def mark_dirty(self, reason: str, critical: bool = False):
        """THE single entry point every project-state mutation routes
        through (spec section 10: "Do not put separate autosave logic
        inside every UI component... UI components should notify the
        project state manager") - forwards to the one, centralized
        AutoSaveService (spec section 11). No-op until a project actually
        exists yet (self.autosave.project_path unset) - see open_pdf/
        load_project/save_project, the only three places that call
        self.autosave.start()."""
        self.autosave.mark_dirty(reason, critical=critical)

    def _build_autosave_snapshot(self) -> dict:
        """The snapshot-capture AutoSaveService calls on the MAIN thread
        before handing the result to its background writer (spec section
        14) - identical shape to what the manual Save Project button
        writes (core.project_manager.build_project_data is the ONE
        function both paths call), just invoked automatically instead of
        from a Save button click."""
        return project_manager.build_project_data(
            self.pdf_path, 200, self.zoom, self.zone_manager, settings=self.settings,
            page_count=self.page_count, page_rotations=self.page_rotations,
            verification_data=self.verification_session.to_dict() if self.verification_session else None)

    def _on_autosave_status(self, text: str):
        """AutoSaveService's status callback (spec section 33) - called
        from the Tk main thread only (AutoSaveService marshals background-
        thread results back via root.after itself), so touching a widget
        here directly is safe."""
        self.autosave_status_label.config(text=f"Autosave: {text}")

    def _manual_save(self):
        """Ctrl+S (spec section 35) - forces the existing continuous
        autosave to write RIGHT NOW instead of waiting out its debounce.
        Deliberately NOT the "Save Project" (Save As) file-picker action
        above - the project is already continuously autosaved to its own
        file the moment it exists, so Ctrl+S has nothing else to ask the
        user."""
        if not self.autosave.project_path:
            return
        self.autosave.save_now()
        self.set_status("Saved")

    def select_zone_from_tree(self, zone_id):
        zone = self.zone_manager.zones.get(zone_id)
        if not zone:
            return
        if zone.page != self.current_page:
            self.goto_page(str(zone.page))
        self.viewer.selected_zone_id = zone_id
        self.viewer.redraw_selection_only()
        self.on_zone_selected(zone_id)

    def on_zone_edit_requested(self, zone_id):
        ZoneInfoDialog(self.root, self, zone_id)
        self.on_zones_changed()

    # ---------------- edit ops ----------------
    def undo(self):
        if self.zone_manager.undo():
            self.on_zones_changed()
            self.set_status("Undo")

    def redo(self):
        if self.zone_manager.redo():
            self.on_zones_changed()
            self.set_status("Redo")

    def delete_selected(self):
        if self.selected_zone_id:
            self._delete_zone(self.selected_zone_id)

    def _delete_zone(self, zone_id):
        zone = self.zone_manager.zones.get(zone_id)
        cascade = True
        if zone and zone.children:
            n = len(zone.children)
            answer = messagebox.askyesnocancel(
                "Delete Zone",
                f"This zone has {n} child zone(s).\n\n"
                "Yes = delete this zone AND all its children (default)\n"
                "No = delete only this zone (children are kept, reparented up)\n"
                "Cancel = don't delete anything",
                default=messagebox.YES)
            if answer is None:
                return
            cascade = bool(answer)
        self.zone_manager.delete_zone(zone_id, cascade=cascade)
        if self.selected_zone_id == zone_id:
            self.selected_zone_id = None
            self.viewer.selected_zone_id = None
            self.tag_panel.highlight_tag_for_zone(None)
        # Full canonical recompute (spec: "EPUBForge - Fix Canonical
        # Reading Order for Multi-Column, Split and Merged Zones" -
        # delete is explicitly listed among the operations that must call
        # the canonical Reading Order function). Deleting a zone can only
        # ever remove a zone from consideration, never move a surviving
        # one, so this reliably reproduces the same column/order result
        # the remaining zones already had.
        self.on_zones_changed()

    def _duplicate_zone(self, zone_id):
        z = self.zone_manager.duplicate_zone(zone_id)
        if z:
            self.on_zone_created(z.zone_id)

    # ---------------- automatic numbered-notes split ----------------
    def auto_split_based_on_numbers(self, zone_id=None):
        """Auto-split one selected notes/endnotes zone by numbered notes.

        Detection is performed only inside the selected zone.  The actual
        mutation is delegated to ZoneManager's existing split_zone() pipeline.
        """
        zone_id = zone_id or self.selected_zone_id
        if not zone_id:
            messagebox.showinfo("Auto Split Based on Numbers", "Select a zone first.")
            return

        zone = self.zone_manager.zones.get(zone_id)
        if not zone:
            messagebox.showerror("Auto Split Based on Numbers", "The selected zone no longer exists.")
            return

        profile = self.active_profile or {}
        configured_flow_tags = list(profile.get("footnote_flow_tags", []) or [])
        if not configured_flow_tags:
            messagebox.showwarning(
                "Auto Split Based on Numbers",
                "The active profile has no footnote/endnote flow tags configured."
            )
            return

        # Preserve an existing fn/en tag when the selected parent is already
        # one of the configured note-flow tags. Otherwise use the first
        # configured note tag rather than inventing a profile vocabulary.
        note_tag = zone.tag if zone.tag in configured_flow_tags else configured_flow_tags[0]
        page_marker_tags = set(profile.get("page_marker_tags", []) or [])

        result = self.zone_manager.auto_split_numbered_notes(
            zone_id,
            note_tag=note_tag,
            page_marker_tags=page_marker_tags,
        )

        if not result.get("ok"):
            detail = result.get("error", "No changes were made.")
            warnings = result.get("warnings") or []
            if warnings:
                detail += "\n\n" + "\n".join(f"• {w}" for w in warnings)
            messagebox.showinfo("Auto Split Based on Numbers", detail)
            return

        # One centralized refresh: canonical Reading Order, viewer, tree,
        # undo/redo state and autosave all remain on the normal App path.
        self.on_zones_changed()

        created = result.get("created", [])
        msg = (
            f"Auto split completed.\n\n"
            f"Numbered notes detected: {result.get('note_count', 0)}\n"
            f"Zones created: {len(created)}\n"
            f"Heading detected: {'Yes' if result.get('heading') else 'No'}"
        )
        if result.get("page_number"):
            if result.get("page_number_mapped"):
                msg += f"\nPage number detected and tagged: {result['page_number']}"
            else:
                msg += (
                    f"\nPage number detected: {result['page_number']}"
                    "\nThe active profile has no page-marker tag, so it was left unmapped."
                )
        if result.get("warnings"):
            msg += "\n\n" + "\n".join(f"• {w}" for w in result["warnings"])

        self.set_status("Auto Split Based on Numbers applied")
        messagebox.showinfo("Auto Split Based on Numbers", msg)

    # ---------------- horizontal split ----------------
    def start_horizontal_split(self, zone_id=None):
        zone_id = zone_id or self.selected_zone_id
        if not zone_id:
            messagebox.showinfo("Horizontal Split", "Select a zone first.")
            return
        self.viewer.start_split(zone_id)
        self.split_bar.pack(side=tk.TOP, fill=tk.X, after=self.toolbar)

    def _confirm_split(self):
        if self.viewer.mode != "split" or not self.viewer.split_state:
            return
        zone_id = self.viewer.split_state["zone_id"]
        parent_tag = self.zone_manager.zones[zone_id].tag
        child_tag = None
        if parent_tag in SPLIT_CHILD_CHOICES:
            dlg = SplitChildTagDialog(self.root, parent_tag)
            if dlg.result is None:
                return
            child_tag = dlg.result
        self.viewer.confirm_split(child_tag=child_tag)
        self.split_bar.pack_forget()
        self.set_status("Split applied")

    def _cancel_split(self):
        self.viewer.cancel_split()
        self.split_bar.pack_forget()
        self.set_status("Split cancelled")

    def _clear_last_split(self):
        self.viewer.clear_last_split_boundary()

    def _toggle_split_axis(self):
        if self.viewer.mode != "split":
            return
        self.viewer.toggle_split_axis()

    # ---------------- generic manual region-split guides (any tag) ----------------
    def start_horizontal_region_split(self):
        """Ctrl+Shift+R - enters horizontal Region Split guide mode for
        the selected zone, REGARDLESS OF TAG. No Tags-panel button by
        design - this is a keyboard-only workflow, same as Horizontal
        Split's own Enter/Escape/Backspace shortcuts. See
        core.xml_generator.split_zone_into_regions for how the resulting
        guides are consumed generically at Generate XML time, whatever
        the zone's tag is (a row x column grid for a Table zone
        specifically, N sibling elements of the zone's own tag for
        everything else)."""
        self._start_region_split_mode("row")

    def start_vertical_region_split(self):
        """Ctrl+Shift+C - enters vertical Region Split guide mode, for any
        zone/tag (see start_horizontal_region_split)."""
        self._start_region_split_mode("col")

    def _start_region_split_mode(self, axis):
        zone_id = self.selected_zone_id
        if not zone_id or self.zone_manager.zones.get(zone_id, None) is None:
            messagebox.showinfo("Region Split", "Select a zone first.")
            return
        self.viewer.start_region_split_mode(zone_id, axis)

    def _exit_region_split_mode(self):
        if self.viewer.mode not in ("region_split_h", "region_split_v"):
            return
        axis = self.viewer.region_split_state.get("axis") if self.viewer.region_split_state else None
        self.viewer.exit_region_split_mode()
        label = "Horizontal" if axis == "row" else "Vertical"
        self.set_status(f"{label} Split guide mode exited")

    def _clear_region_splits(self):
        if self.viewer.mode not in ("region_split_h", "region_split_v"):
            return
        self.viewer.clear_region_splits()

    def _start_table_draw_split(self, axis):
        """Ctrl+Shift+Alt+6 (row) / +7 (col) - Table Draw-only Horizontal/
        Vertical Split shortcuts (spec: dedicated shortcuts scoped ONLY to
        when a Table Draw zone is selected - must NOT fire for Para/
        PageNum/Figure/Caption/TOC/no-selection). Delegates to the exact
        same start_region_split_mode primitive the generic, any-zone
        Ctrl+Shift+R/C shortcuts already use above - Table Draw's own
        split/persist/generate mechanism is already correct and needs no
        changes, only this additional tag-gated entry point into it."""
        zone_id = self.selected_zone_id
        zone = self.zone_manager.zones.get(zone_id) if zone_id else None
        if zone is None or zone.tag != "table":
            return
        self.viewer.start_region_split_mode(zone_id, axis)

    # ---------------- help ----------------
    def show_help(self):
        HELP_TEXT = (
            "HORIZONTAL / VERTICAL SPLIT (toolbar)\n\n"
            "Select a zone, click \"Horizontal Split\" on the toolbar.\n\n"
            "Horizontal Split\n"
            "    Splits the selected zone into top/bottom regions.\n\n"
            "Ctrl+Shift+V (while Split mode is active)\n"
            "    Toggles the current split to Vertical/Column Split -\n"
            "    splits the selected zone into left/right regions\n"
            "    instead. Any boundaries already drawn are cleared\n"
            "    when you toggle.\n\n"
            "Click inside the zone\n"
            "    Add a split boundary.\n\n"
            "Click an existing line (no drag)\n"
            "    Delete it. Drag it to move it.\n\n"
            "Enter\n"
            "    Confirm the split.\n\n"
            "Backspace\n"
            "    Remove the last boundary.\n\n"
            "Escape\n"
            "    Cancel the split.\n\n"
            "IMPORTANT\n\n"
            "Split replaces the original zone: once confirmed, the\n"
            "split pieces are what Generate XML uses - the original,\n"
            "pre-split zone is kept only as split history (visible in\n"
            "Zone Hierarchy as SPLIT-PARENT) and never generates its\n"
            "own extra XML element alongside its pieces.\n\n"
            "Only ACTIVE / leaf zones are exported - a SPLIT-PARENT\n"
            "zone is skipped, its split children are used instead.\n\n"
            "Generate XML never performs automatic analysis - it reads\n"
            "exactly the zones, splits, and manual Reading Order you\n"
            "created, nothing more.\n\n"
            "Reading order follows manual zone order - split pieces\n"
            "take the original zone's position, top-to-bottom for a\n"
            "Horizontal Split or left-to-right for a Vertical Split.\n\n"
            "Split works for ANY tag - Paragraph, Title, Subtitle,\n"
            "Section heading, Boxed Text, Table, Figure, Caption,\n"
            "List Item, and every other supported tag.\n\n"
            "------------------------------------------------------------\n\n"
            "TABLE ZONING SHORTCUTS\n\n"
            "Table Caption\n"
            "    Select \"Table Caption\" from Tags and draw the caption zone.\n\n"
            "Table\n"
            "    Select \"Table\" from Tags and draw the complete table zone.\n\n"
            "Table Wrap Foot\n"
            "    Select \"Table Wrap Foot\" and draw the table foot zone.\n\n"
            "------------------------------------------------------------\n\n"
            "REGION SPLIT SHORTCUTS (ANY ZONE, ANY TAG)\n\n"
            "Region Split works on the SELECTED zone regardless of its\n"
            "tag - a Paragraph, Caption, Boxed Text, List Item,\n"
            "Reference, Heading, Figure, Table, or any other tag.\n\n"
            "Ctrl+Shift+R\n"
            "    Horizontal Split Mode\n\n"
            "    Select a zone, press Ctrl+Shift+R, then draw\n"
            "    horizontal boundaries. For a Table zone these become\n"
            "    row boundaries; for any other tag they split the\n"
            "    zone into that many separate elements of the SAME\n"
            "    tag, stacked top to bottom.\n\n"
            "Ctrl+Shift+C\n"
            "    Vertical Split Mode\n\n"
            "    Select a zone, press Ctrl+Shift+C, then draw\n"
            "    vertical boundaries. For a Table zone these become\n"
            "    column boundaries; for any other tag they split the\n"
            "    zone side by side, left to right.\n\n"
            "Escape\n"
            "    Cancel/deselect. Exits split mode.\n\n"
            "Click a guide line (no drag)\n"
            "    Select that split guide (highlighted red).\n\n"
            "Delete\n"
            "    Delete the currently selected split guide.\n\n"
            "Drag a guide line\n"
            "    Move that split guide.\n\n"
            "Ctrl+Shift+X\n"
            "    Clear all split guides for the selected zone.\n\n"
            "------------------------------------------------------------\n\n"
            "IMPORTANT\n\n"
            "Manual splits override automatic analysis for that zone\n"
            "(automatic table row/column detection for a Table zone;\n"
            "for any other tag, a split zone is simply extracted region\n"
            "by region - no automatic paragraph/line grouping runs on\n"
            "a split zone).\n\n"
            "A zone with NO manual splits generates exactly one XML\n"
            "element, same as before this feature existed.\n"
            "A zone WITH manual splits generates one element per\n"
            "region instead - the original, unsplit zone never ALSO\n"
            "generates its own extra element.\n\n"
            "Use manual splits when PDF text extraction causes:\n"
            "- merged rows/columns (Table)\n"
            "- incorrect columns\n"
            "- missing rows\n"
            "- incorrect cell assignment\n"
            "- text flowing into neighboring cells or regions\n"
            "- content that should be several separate elements\n"
            "  but was drawn as a single zone\n\n"
            "The original PDF is never modified.\n\n"
            "The zone itself is never resized by split guides.\n\n"
            "Split guides are only analysis boundaries.\n\n"
            "------------------------------------------------------------\n\n"
            "ROTATED TABLES\n\n"
            "The TableAnalyzer automatically detects 90/270 degree\n"
            "rotated tables and normalizes their coordinates.\n\n"
            "The PDF page itself is not rotated.\n\n"
            "------------------------------------------------------------\n\n"
            "TABLE LISTS\n\n"
            "Lists detected inside a table cell remain inside\n"
            "that cell.\n\n"
            "------------------------------------------------------------\n\n"
            "TABLE FOOTNOTES\n\n"
            "Table Wrap Foot is generated after </table>.\n\n"
            "------------------------------------------------------------\n\n"
            "AUTO ZONE\n\n"
            "Load Reference Project\n"
            "    Pick an already-correctly-zoned project JSON (a normal\n"
            "    Save Project file). the BITS Tool learns its layout/semantic\n"
            "    patterns (normalized position, size, font, list markers,\n"
            "    text patterns) - never raw coordinates. Click it again\n"
            "    with a different project to add more references; their\n"
            "    patterns combine.\n\n"
            "Auto Zone\n"
            "    Open a NEW, similar PDF, then click Auto Zone. Every page\n"
            "    that has NO zones yet gets proposed zones (tag, bbox,\n"
            "    reading order, hierarchy) based on the loaded reference(s).\n"
            "    A page that already has at least one zone is always\n"
            "    skipped - Auto Zone never touches existing manual zoning\n"
            "    and never creates a duplicate zone.\n\n"
            "    Auto-created zones are shown with a dashed blue (high\n"
            "    confidence), orange (medium), or purple (low confidence)\n"
            "    outline and an \"(auto NN%)\" label instead of the normal\n"
            "    tag color - purely visual, never written to the XML.\n"
            "    Confidence thresholds are configurable in Settings.\n\n"
            "    Every auto-created zone is a completely normal zone -\n"
            "    select, retag, move, resize, split, merge, or delete it\n"
            "    exactly like a manually drawn one.\n\n"
            "Save Corrections as Template\n"
            "    After Auto Zone plus any manual corrections, saves the\n"
            "    current project as a NEW project file (never overwrites\n"
            "    the original reference) - usable as a future reference.\n\n"
            "IMPORTANT\n\n"
            "Auto Zone never copies coordinates from the reference PDF -\n"
            "it only reuses learned normalized patterns, adapted to the\n"
            "new PDF's own actual layout, margins, and page size."
        )
        dlg = tk.Toplevel(self.root)
        dlg.title("Help")
        dlg.geometry("560x600")
        dlg.transient(self.root)
        text = tk.Text(dlg, wrap="word", padx=10, pady=10)
        text.insert("1.0", HELP_TEXT)
        text.config(state="disabled")
        text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        tk.Button(dlg, text="Close", command=dlg.destroy).pack(side=tk.BOTTOM, pady=8)
        dlg.grab_set()

    # ---------------- settings ----------------
    def open_settings(self):
        old_viewer_dpi = self.settings.get("viewer_dpi", 200)
        # Generalized from a literal `== "EPUB"` check: any XHTML-producing
        # profile (EPUB, CUPEPUB) gets its own component_types list offered
        # as a dropdown; XML (xhtml_enabled False) keeps the plain text entry.
        epub_profile = self.active_profile if self.active_profile.get("xhtml_enabled") else None
        dlg = SettingsDialog(self.root, self.settings, epub_profile=epub_profile)
        if dlg.result:
            self.settings.update(dlg.result)
            debug_log.set_enabled(self.settings.get("debug_logging", False))
            self._log_scroll_diagnostic_env()
            if self.pdf_document and self.settings.get("viewer_dpi", 200) != old_viewer_dpi:
                # DPI controls rasterization quality, not zone positions - zone
                # bboxes stay in PDF coordinates and remain correctly aligned
                # after reloading the page at the new render resolution.
                self.viewer.load_page(self.current_page)
            self._apply_index_tag_shortcuts()  # pick up a possibly-changed Index shortcut immediately
            self.tag_panel.rebuild(self.active_tag_buttons, self.active_profile.get("tag_colors", {}),
                                    self.active_profile.get("tag_groups"))
            self.set_status("Settings updated")
        # Defensive, same reasoning as open_pdf()/load_project() - makes
        # sure every root-bound shortcut works immediately after closing
        # Settings, whether or not anything was actually changed.
        self.root.lift()
        self.root.focus_force()

    # ---------------- XML generation ----------------
    def _refresh_overlap_highlight(self, page_marker_tags):
        """Shared by generate_xml/generate_xhtml/_generate_client_xhtml
        (spec: "URGENT - FIX OVERLAPPING ZONES + HIGHLIGHT THEM") - a
        single place that recomputes the transient overlap-highlight set
        on EVERY validation run, regardless of outcome, so a highlight
        from a previous failed attempt disappears the instant the user's
        fix makes it stop existing (never left stale), and jumps the
        canvas to the lowest-numbered page that actually has a flagged
        zone, so the conflicting rectangles are already visible without
        the user having to go hunting for the right page first - the
        exact gap reported: Generate XHTML already produced the correct
        error text (validation.validate() already includes the overlap/
        split-parent messages), it just never populated
        self.overlapping_zone_ids or focused the affected page the way
        Generate XML's own already-working highlight did.

        Purely a transient UI read of validation's own geometry
        (validation.find_overlapping_zone_ids reuses the exact same
        algorithm validate()'s own overlap error text uses) - never
        written to zone data, reading order, or the project file, and
        never auto-selects, merges, deletes, or moves any zone; the user
        still clicks a highlighted zone themselves to inspect/fix it."""
        self.overlapping_zone_ids = validation.find_overlapping_zone_ids(self.zone_manager, page_marker_tags)
        if self.overlapping_zone_ids:
            pages = {self.zone_manager.zones[zid].page for zid in self.overlapping_zone_ids
                     if zid in self.zone_manager.zones}
            if pages:
                target_page = min(pages)
                if target_page != self.current_page:
                    self.goto_page(str(target_page))
        self.viewer.redraw()

    def generate_xml(self):
        if not self.pdf_document:
            messagebox.showwarning("Generate XML", "Open a PDF first.")
            return
        if not self.zone_manager.zones:
            messagebox.showwarning("Generate XML", "No zones to generate from.")
            return
        page_marker_tags = self.active_profile.get("page_marker_tags")
        footnote_flow_tags = self.active_profile.get("footnote_flow_tags")
        non_flow_tags = self.active_profile.get("non_flow_tags")
        self._repair_broken_merges()
        errors = validation.validate(self.zone_manager, page_marker_tags, footnote_flow_tags, non_flow_tags)
        self._refresh_overlap_highlight(page_marker_tags)
        if errors:
            messagebox.showerror("Generate XML", "Fix these issues before generating:\n\n" + "\n".join(errors[:20]))
            return
        # Post-zoning Hyphen Normalization Review (workflow: zoning
        # completed -> review -> Generate XML) - only opened when there's
        # actually something to review; skipped entirely (zero behavior
        # change) for a project with no detected line-break-hyphen
        # candidates. Cancel aborts XML generation - "keep the original
        # extracted text" means nothing from this review session is
        # applied, and the user returns to zoning rather than proceeding
        # with an unreviewed generation.
        if text_extractor.find_all_hyphen_candidates(self.zone_manager, self.pdf_document):
            dlg = HyphenReviewDialog(self.root, self.zone_manager, self.pdf_document)
            if not dlg.result:
                return
        prefix = self.settings.get("prefix", "document")
        # Output filename/folder = the FULL prefix (never narrowed - spec:
        # "Filename... must remain EXACTLY the filename provided"); element
        # IDs get the separately-derived SHORT id_prefix below.
        id_prefix = component_output.derive_id_prefix(prefix)
        output_dir = APP_ROOT / "output" / (self.settings.get("output_folder_name") or prefix)
        assets_dir = output_dir / "images"
        output_path = output_dir / f"{prefix}.xml"
        kind = bits_pipeline.kind_of(self.active_profile)
        self.set_status(f"Generating {kind} XML...")
        try:
            res = bits_pipeline.generate(
                self.zone_manager, self.pdf_document, kind, str(output_path), str(assets_dir), prefix=id_prefix,
                settings=dict(self.settings.get("bits_meta") or {},
                              bits_dtd_path=self.settings.get("bits_dtd_path", "")),
                jpeg_quality=self.settings.get("jpeg_quality", 95),
                image_dpi=self.settings.get("image_dpi", 200),
                remove_image_background=self.settings.get("remove_image_background", False))
        except Exception as e:
            messagebox.showerror("Generate XML", f"Failed to generate {kind} XML:\n{e}")
            self.set_status("XML generation failed")
            return
        self.zone_tree.refresh()
        self.viewer.redraw()
        summary = (res.summary() + f"\n\nSections: {res.counters.get('section', 0)}  "
                   f"Figures: {res.asset_counters.get('figure', 0)}  "
                   f"Equations: {res.asset_counters.get('equation', 0)}\nReport: {res.report_path}")
        if not res.text_preserved:
            messagebox.showwarning("Generate XML - check the text", summary)
        elif res.errors_after:
            messagebox.showwarning("Generate XML - needs review",
                                   summary + "\n\nRemaining DTD errors:\n" + "\n".join(res.errors_after[:8]))
        else:
            messagebox.showinfo("Generate XML", summary)
        self.set_status(f"{kind} XML: {res.status} - {res.output_path}")

    # ---------------- verification (core/verification/) ----------------
    def open_verification_window(self):
        """Opens (or raises, if already open) the Verification window for
        the current project - an entirely optional, additive step between
        OCR/Extraction and Generate XHTML. Requires an open PDF with at
        least one zone, exactly like every other zone-dependent action in
        this app; never mutates zoning/generation state by itself."""
        if not self.pdf_document:
            messagebox.showwarning("Verify", "Open a PDF first.")
            return
        if not self.zone_manager.zones:
            messagebox.showwarning("Verify", "No zones to verify yet.")
            return
        if self.verification_window is not None and self.verification_window.winfo_exists():
            self.verification_window.lift()
            self.verification_window.focus_force()
            return
        from gui.verification_window import VerificationWindow
        self.verification_window = VerificationWindow(self)

    # ---------------- status ----------------
    def set_status(self, message: str = None):
        page_info = f"Page {self.current_page}/{self.page_count}" if self.pdf_document else "No PDF loaded"
        zoom_info = f"Zoom {int(round(self.zoom * 100))}%"
        n_zones = len(self.zone_manager.zones)
        sel = self.zone_manager.zones.get(self.selected_zone_id) if self.selected_zone_id else None
        multi_selected = getattr(self.viewer, "selected_zone_ids", None) or set()
        if len(multi_selected) > 1:
            sel_info = f"Selected: {len(multi_selected)} zones selected"
        elif sel:
            ro = sel.serial if sel.serial is not None else "-"
            sel_info = f"Selected: {sel.zone_id} ({sel.tag}) | Page {sel.page} | Reading Order: {ro}"
        else:
            sel_info = "Selected: none"
        profile_name = self.settings.get('profile', 'XML')
        profile_info = f"Profile: {profile_name}"
        parts = [page_info, zoom_info, f"Zones: {n_zones}", sel_info, profile_info]
        if message:
            parts.append(message)
        self.status_bar.config(text="   ·   ".join(parts))
        # Header badges (spec 66.2/66.19) - refreshed here rather than at
        # every individual project-load/profile-switch call site, since
        # set_status() already runs after both.
        self.profile_badge.config(text=profile_name)
        self.project_badge.config(
            text=os.path.basename(self.project_path) if self.project_path else "No project loaded")


def run():
    root = tk.Tk()
    app = App(root)
    root.mainloop()
