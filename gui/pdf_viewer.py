"""Center PDF canvas: 200 DPI render scaled by GUI zoom, zone drawing, resize
handles, move, horizontal split, and PDF<->screen coordinate conversion."""
import tkinter as tk

from PIL import Image, ImageTk

from core.constants import DPI, DEFAULT_TAG_COLORS
from core import debug_log
from auto_zoning import confidence as az_confidence
from gui import theme

HANDLE_SIZE = 7
AUTO_ZONE_COLORS = {"high": "#1E88E5", "medium": "#FB8C00", "low": "#8E24AA"}
HANDLE_NAMES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"]
MIN_ZONE_SIZE = 10.0  # PDF-space units (points) - zoom-independent by construction
MERGED_ZONE_COLOR = "#FFD700"  # a zone produced by Merge Previous, regardless of tag

# ---------------- centralized scroll-speed configuration (spec: "EPUBForge
# - Fix Excessively Fast Mouse/Touchpad Scrolling and Zone-Draw Auto-
# Scroll") - every vertical scroll path below (wheel, touchpad, edge
# auto-scroll) shares these same values, never a scattered/duplicated
# magic number.
#
# SCROLL_UNIT_PX is the canvas's own yscrollincrement (applied at canvas
# creation below) - WITHOUT an explicit increment, Tk computes "one unit"
# from the widget's own current height (confirmed directly: ~69px on a
# 900px-tall canvas, growing on a larger/maximized window) - a real root
# cause of "the PDF jumps several lines" that has nothing to do with wheel/
# touchpad delta handling at all: even a single, correctly-normalized
# "1 unit" step was already a large, screen-size-dependent jump. Fixing
# this one value tightens EVERY "units"-based scroll call in this file
# (wheel, touchpad, edge auto-scroll) at once, consistently, regardless of
# window size.
SCROLL_UNIT_PX = 16
# Wheel/touchpad normalization - both are converted to the same "notches"
# scale (raw delta / 120, Windows' own convention; TIP 684's high-
# resolution TouchpadScroll deltas use the identical scale, just at finer
# granularity - see _accumulate_touchpad_units) before being turned into a
# controlled number of SCROLL_UNIT_PX-sized units, so a small delta stays
# small and a fast multi-notch flick is clamped rather than amplified.
WHEEL_UNITS_PER_NOTCH = 3        # ~48px per full wheel notch - several lines, never half a page
MAX_WHEEL_UNITS_PER_EVENT = 3    # clamp - one event/notch never exceeds this, however large its own delta

# Edge auto-scroll while drawing/resizing/moving a zone (spec: "AUTO
# VERTICAL SCROLL WHILE DRAGGING A ZONE") - widget-relative pixels from the
# canvas's own top/bottom edge that activates auto-scroll, and the tick
# interval/speed range used while the cursor stays inside that band. Speed
# scales with how deep into the band the cursor is (spec: "scrolling speed
# increases as the cursor gets closer to the edge"). MAX_UNITS lowered from
# an earlier 4 to 2 specifically because SCROLL_UNIT_PX above shrank from
# Tk's own ~69px default to 16px - keeping MAX_UNITS unchanged would have
# LOWERED the real on-screen edge-scroll speed already (an incidental,
# welcome side effect), but 2 keeps the ABSOLUTE max speed (2 * 16px /
# 40ms tick = 800px/sec at the very edge) explicitly "slow and controlled"
# by design, not merely as a side effect of the unit-size fix.
AUTO_SCROLL_EDGE_PX = 45
AUTO_SCROLL_MIN_UNITS = 1
AUTO_SCROLL_MAX_UNITS = 2
AUTO_SCROLL_INTERVAL_MS = 40


def normalize_wheel_units(delta: int) -> int:
    """Converts a raw <MouseWheel> delta into a small, controlled, SIGNED
    number of SCROLL_UNIT_PX-sized canvas units (spec: "raw event delta ->
    normalize -> clamp -> apply controlled scroll amount"). Scaled by
    NOTCHES (delta/120, Windows' own convention), not a bare direction-only
    sign - previously EVERY event moved a fixed 1 unit regardless of delta
    magnitude, which (a) never let a genuine multi-notch flick move any
    further than a gentle single notch, and (b), combined with Tk's own
    screen-size-dependent default unit size (see SCROLL_UNIT_PX's own
    comment), made that one fixed unit itself a large, uncontrolled jump.
    A genuine notch (delta == +/-120, by far the common real case) always
    produces AT LEAST 1 final unit of movement - never truncated to 0 by
    naive floor division on a delta that isn't an exact multiple of 120
    (precision-touchpad drivers commonly report smaller deltas like +/-40
    through this same legacy event) - the exact asymmetric bug an earlier
    fix in this file already addressed, preserved here by construction
    rather than reintroduced. A larger delta (a fast multi-notch flick, or
    an unusual driver value) is CLAMPED to MAX_WHEEL_UNITS_PER_EVENT, never
    amplified into an excessive jump.

    A module-level function (not a PDFViewerPanel-only method) so every
    Tk Canvas-based scrollable panel in the app can share the exact same
    wheel-normalization math - gui/zone_panel.py's own Tag Toolbox panel
    reuses this directly, rather than a second, duplicated implementation
    of the identical fix."""
    if delta == 0:
        return 0
    notches = delta / 120.0
    magnitude = max(1, round(abs(notches) * WHEEL_UNITS_PER_NOTCH))
    magnitude = min(magnitude, MAX_WHEEL_UNITS_PER_EVENT)
    return -magnitude if delta > 0 else magnitude


def accumulate_scroll_units(accum: float, delta: int) -> tuple:
    """Converts one high-resolution TouchpadScroll sample's delta into the
    running fractional accumulator (same notches-based scale normalize_
    wheel_units uses), returning (new_accum, units) - units is the SIGNED
    whole-unit amount actually ready to scroll THIS call (0 most of the
    time - most individual samples are too small to cross a whole unit on
    their own), new_accum is the leftover remainder the caller must persist
    and pass back in on the next sample. Unlike a genuine discrete wheel
    notch, a tiny continuous sample is NEVER force-rounded up to a full
    unit here - that would exactly reproduce the "every event is treated
    as a full click" bug this fix exists to remove. Pure/stateless: the
    caller owns its own accumulator float (e.g. PDFViewerPanel/TagPanel's
    own self._touchpad_accum_y), so multiple independent scrollable panels
    never share or clobber one another's running remainder."""
    accum += (delta / 120.0) * WHEEL_UNITS_PER_NOTCH
    units = int(accum)  # truncates toward 0, keeps the fractional remainder
    accum -= units
    units = max(-MAX_WHEEL_UNITS_PER_EVENT, min(MAX_WHEEL_UNITS_PER_EVENT, units))
    return accum, -units

# ---------------- page-rotation coordinate transform ----------------
# Page rotation (App.page_rotations, per page number, 0/90/180/270 -
# purely a VIEW/EDIT preference, never written into the PDF file itself)
# is handled ENTIRELY here, at the screen<->PDF conversion boundary.
# zone.bbox is ALWAYS stored in the PDF's own native, unrotated coordinate
# space (the same points page.rect/get_text/get_pixmap already use) -
# rotating the view never touches a single zone's bbox value. This is why
# XML generation, image/text extraction, hierarchy, reading order, split,
# merge, and Auto Zone all need ZERO rotation-awareness: they only ever
# see native-space bboxes, exactly as before this feature existed. Only
# pdf_to_screen/screen_to_pdf (below) and load_page's own rendering step
# need to know the current rotation.


def _rotate_point(x, y, pw, ph, rot):
    """A point (x, y) in the PDF's native (0deg) point space -> its
    position in the CLOCKWISE-by-`rot`-degrees rotated view, still in
    points (before DPI/zoom scaling). `pw`/`ph`: the native page size."""
    if rot == 90:
        return ph - y, x
    if rot == 180:
        return pw - x, ph - y
    if rot == 270:
        return y, pw - x
    return x, y


def _unrotate_point(nx, ny, pw, ph, rot):
    """Inverse of _rotate_point: a point in the rotated view -> its
    position in the PDF's native (0deg) point space."""
    if rot == 90:
        return ny, ph - nx
    if rot == 180:
        return pw - nx, ph - ny
    if rot == 270:
        return pw - ny, nx
    return nx, ny


def _rotate_bbox(bbox, pw, ph, rot):
    """Native-space bbox -> its axis-aligned bbox in the rotated view.
    Transforms all four corners (not just the two opposite ones) since a
    90/270 rotation can change which corner ends up top-left/bottom-right."""
    if rot == 0:
        return list(bbox)
    x0, y0, x1, y1 = bbox
    corners = [_rotate_point(x0, y0, pw, ph, rot), _rotate_point(x1, y0, pw, ph, rot),
               _rotate_point(x0, y1, pw, ph, rot), _rotate_point(x1, y1, pw, ph, rot)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _unrotate_bbox(bbox, pw, ph, rot):
    """Inverse of _rotate_bbox: a bbox in the rotated view -> its
    axis-aligned bbox in the PDF's native (0deg) coordinate space."""
    if rot == 0:
        return list(bbox)
    x0, y0, x1, y1 = bbox
    corners = [_unrotate_point(x0, y0, pw, ph, rot), _unrotate_point(x1, y0, pw, ph, rot),
               _unrotate_point(x0, y1, pw, ph, rot), _unrotate_point(x1, y1, pw, ph, rot)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


# PIL Image.transpose constants for a CLOCKWISE rotation by `rot` degrees -
# confirmed empirically (PIL's own ROTATE_90/270 constants name a
# COUNTER-clockwise angle, the opposite of what the name suggests at a
# glance): ROTATE_270 (270 deg CCW) = 90 deg CW; ROTATE_90 (90 deg CCW) =
# 270 deg CW. Deliberately reusing pdf_loader.PDFDocument.render_page_image
# UNCHANGED (still always renders at 0deg) and rotating its OUTPUT here via
# PIL's lossless, exact transpose - never re-rendering from the PDF a
# second, rotation-aware way, and never any interpolation/quality loss.
_PIL_ROTATE_FOR_CW = {0: None, 90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}


class PDFViewerPanel(tk.Frame):
    def __init__(self, parent, app):
        palette = theme.current.palette
        super().__init__(parent, bg=palette["canvas_bg"])
        self.app = app
        self.palette = palette

        self.canvas = tk.Canvas(self, bg=palette["canvas_bg"], highlightthickness=0,
                                 yscrollincrement=SCROLL_UNIT_PX)
        self.vbar = tk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.hbar = tk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Empty state (spec 66.18) - a Frame placed (not gridded) directly
        # on TOP of the canvas via .place(), independent of the canvas's
        # own item space, since canvas.delete("all") (every redraw()) would
        # otherwise destroy a canvas-embedded window item on the very first
        # page load. Shown until the first load_page() call, hidden (and
        # never shown again this session) the moment a page actually renders.
        self._empty_state = tk.Frame(self, bg=palette["canvas_bg"])
        tk.Label(self._empty_state, text="EPUBForge", bg=palette["canvas_bg"], fg=palette["text"],
                  font=(theme.FONT_FAMILY, 20, "bold")).pack(pady=(0, 4))
        tk.Label(self._empty_state, text="PDF Zoning & EPUB Production", bg=palette["canvas_bg"],
                  fg=palette["text_muted"], font=theme.FONT_BODY).pack(pady=(0, 18))
        tk.Label(self._empty_state, text="Load a project to begin zoning.", bg=palette["canvas_bg"],
                  fg=palette["text_muted"], font=theme.FONT_BODY).pack(pady=(0, 10))
        open_btn = tk.Button(self._empty_state, text="Open Project", command=lambda: app.load_project())
        theme.style_button(open_btn, palette, kind="primary")
        open_btn.pack(pady=(0, 14))
        tk.Label(self._empty_state, text="Digital PDF  ·  Scanned PDF  ·  Mixed PDF", bg=palette["canvas_bg"],
                  fg=palette["text_faint"], font=theme.FONT_SMALL).pack()
        self._empty_state.place(relx=0.5, rely=0.5, anchor="center")

        self.base_image = None       # PIL.Image, 200 DPI render of the current page
        self.tk_image = None         # displayed (zoomed) PhotoImage - keep a ref!
        self.image_item = None
        self.zone_items = {}         # zone_id -> (rect_item, top_left_label_item, end_edge_tag_label_item)
        self.handle_items = {}       # handle name -> item id
        self.selected_zone_id = None
        # Multi-zone selection (Ctrl+A / Ctrl+click) - additive to the
        # existing single-scalar selected_zone_id above, which stays the
        # "primary"/most-recently-clicked zone every EXISTING single-zone
        # consumer (move/resize/context menu/properties panel) already
        # keys off, completely unchanged. selected_zone_ids is purely new
        # state: which zones currently show the multi-select highlight
        # and count toward "N zones selected".
        self.selected_zone_ids = set()

        self.mode = "idle"           # idle | drawing | moving | resizing | split | region_split_h | region_split_v
        self.drag = {}
        self.split_state = None      # {"zone_id":..., "boundaries": [pdf_y,...], "guide_item":None}
        # Manual generic Region Split guides for ANY zone, any tag
        # (Ctrl+Shift+R / Ctrl+Shift+C) - {"zone_id":..., "axis":
        # "row"|"col", "values": [pdf_y_or_x,...], "committed_items": [...],
        # "guide_item": None, "dragging_index": None, "drag_moved": False}.
        # Unlike split_state, these persist DIRECTLY onto the zone's own
        # attributes ("horizontal_splits"/"vertical_splits") as they're
        # edited - see _persist_region_splits - since they're permanent
        # per-zone split metadata, never separate XML zones or a one-shot
        # split-into-new-zones action. core.xml_generator.
        # split_zone_into_regions turns them into one XML element per
        # region for every tag (a row x column grid for table
        # specifically) at generation time.
        self.region_split_state = None
        self._auto_scroll_job = None  # after() id while edge auto-scroll is active, else None
        self._touchpad_accum_y = 0.0  # fractional leftover between TouchpadScroll events - see _accumulate_touchpad_units

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        # Ctrl+A / Ctrl+click multi-zone selection - bound on the CANVAS
        # widget specifically (not root), so Tk's own focus-based dispatch
        # means this never fires while a Properties-panel Entry/Text field
        # has focus (that widget's own native Ctrl+A - select-all-text -
        # keeps working exactly as before; confirmed no pre-existing
        # <Control-a> binding exists anywhere in gui/ to collide with).
        self.canvas.bind("<Control-a>", lambda e: self.select_all_zones_on_page())
        self.canvas.bind("<Control-A>", lambda e: self.select_all_zones_on_page())
        self.canvas.bind("<Control-Button-1>", self._on_ctrl_click)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

        # Wheel/touchpad bindings are applied to the CANVAS *and* every
        # other widget occupying this panel (the scrollbars, and the panel
        # Frame itself, for the thin strip of background grid padding
        # around the canvas/scrollbars) - Tk delivers a wheel event to
        # whichever widget is directly under the cursor, so a real
        # touchpad gesture that happens to land on the vertical scrollbar
        # strip (or between the canvas and the scrollbar) previously never
        # reached the canvas's own binding at all and did nothing,
        # silently - confirmed as a real gap, not a hypothetical one, once
        # Settings > Debug logging's new [SCROLL] entries showed zero
        # events for a gesture the user could visually confirm targeted
        # this panel.
        for widget in (self.canvas, self.vbar, self.hbar, self):
            widget.bind("<MouseWheel>", self._on_wheel)          # Windows
            widget.bind("<Shift-MouseWheel>", self._on_wheel_h)
            # Ctrl+wheel = zoom (no pre-existing Ctrl+wheel behavior to
            # conflict with - confirmed by inspection), reusing the exact
            # same app.zoom_in()/zoom_out() the toolbar's +/- buttons
            # already call, never a second zoom mechanism. Bound as its
            # own (more specific) sequence, so plain <MouseWheel> above
            # still handles an un-modified wheel scroll exactly as before
            # - Tk dispatches the most specific matching binding, never
            # both at once.
            widget.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
            widget.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
            widget.bind("<Control-Button-4>", lambda e: self.app.zoom_in())
            widget.bind("<Control-Button-5>", lambda e: self.app.zoom_out())
            # Tk 9's own separate high-resolution touchpad event (TIP 684)
            # - see theme.bind_touchpad_scroll's own docstring for why a
            # real Windows Precision Touchpad can generate ONLY this and
            # never <MouseWheel> at all. A no-op on any older Tk build.
            theme.bind_touchpad_scroll(widget, self._on_touchpad_scroll)

    # ---------------- page loading / rendering ----------------
    def viewer_dpi(self):
        """PDF rendering DPI (Settings > Viewer DPI, default 200) - controls
        rasterization quality, NOT display scale (that's self.app.zoom) and
        NOT figure/equation crop quality (that's Settings > Image DPI,
        core/image_extractor.py). Three independent concepts, never mixed."""
        return self.app.settings.get("viewer_dpi", DPI)

    def load_page(self, page_number: int):
        self.base_image = self.app.pdf_document.render_page_image(page_number, dpi=self.viewer_dpi())
        rot = self.app.page_rotations.get(page_number, 0)
        pil_op = _PIL_ROTATE_FOR_CW.get(rot)
        if pil_op is not None:
            self.base_image = self.base_image.transpose(pil_op)
        self.selected_zone_id = None
        self.redraw()

    def redraw(self):
        self.canvas.delete("all")
        self.zone_items = {}
        self.handle_items = {}
        if self.base_image is None:
            self._empty_state.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._empty_state.place_forget()
        zoom = self.app.zoom
        w = max(1, int(self.base_image.width * zoom))
        h = max(1, int(self.base_image.height * zoom))
        resized = self.base_image.resize((w, h), Image.LANCZOS) if zoom != 1.0 else self.base_image
        self.tk_image = ImageTk.PhotoImage(resized)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)
        # Subtle page border (spec 66.8 - "clean page boundary") - the
        # closest honest equivalent to a drop shadow available on a plain
        # tk.Canvas (no compositor/blur), so a 1px darker outline is used
        # instead of a fabricated shadow effect.
        self.canvas.create_rectangle(0, 0, w, h, outline=self.palette["page_border"], width=1)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self._draw_all_zones()
        self._draw_region_splits()
        smart_az = getattr(self.app, "smart_az", None)
        if smart_az is not None:
            smart_az.draw_debug_overlays(self)

    def _draw_all_zones(self):
        if not self.app.pdf_document:
            return
        page = self.app.current_page
        for zone in self.app.zone_manager.zones_on_page(page):
            self._draw_zone(zone)
        # Resize handles must NOT be drawn during split mode (or the manual
        # region-split guide modes below, same reasoning): they sit
        # right on the zone's edges/corners, which is exactly where a user
        # needs to click to add a split/guide line near the top/bottom/side
        # - a handle's own tag_bind would hijack that click into
        # _start_resize(), silently switching mode away and making the
        # split/guide interaction seem to randomly stop working. This was
        # the main cause of unreliable splitting.
        if self.mode not in ("split", "region_split_h", "region_split_v") and self.selected_zone_id \
                and self.selected_zone_id in self.app.zone_manager.zones:
            self._draw_handles(self.selected_zone_id)

    def _tag_color(self, tag):
        # Project-level override, then the ACTIVE PROFILE's own tag_colors
        # (EPUB/CUPEPUB define their own full maps - see
        # profiles/epub_profile.json / core/cup_config.py), then the
        # XML-profile default map as the last resort. Previously this always
        # skipped straight to DEFAULT_TAG_COLORS, so most EPUB/CUPEPUB zones
        # drew with a plain black outline on the canvas even though their
        # profile defines real colors - fixed here rather than worked around,
        # since it's a plain lookup-order bug, not profile-specific behavior.
        project_override = self.app.settings.get("tag_colors", {}).get(tag)
        if project_override:
            return project_override
        profile_color = getattr(self.app, "active_profile", {}).get("tag_colors", {}).get(tag)
        if profile_color:
            return profile_color
        return DEFAULT_TAG_COLORS.get(tag, "#000000")

    def _zone_color(self, zone):
        # A zone flagged by Merge Previous (attributes["merged_with_previous"])
        # is visually distinguished in YELLOW, overriding its normal tag
        # color - purely a display decision driven live off that flag
        # (persisted with the zone through save/load, since it's just a
        # normal entry in the zone's own attributes dict), so it stays
        # correct across page changes, zoom, tag changes, and reload without
        # any cached state. The PREVIOUS/target zone keeps its normal color -
        # only the zone that declares itself merged turns yellow.
        if zone.attributes.get("merged_with_previous"):
            return MERGED_ZONE_COLOR
        return self._tag_color(zone.tag)

    def _draw_zone(self, zone):
        x0, y0, x1, y1 = self.pdf_to_screen(zone.bbox)
        is_selected = zone.zone_id == self.selected_zone_id
        # Overlap-validation highlight (App.overlapping_zone_ids, set by
        # generate_xml() from validation.find_overlapping_zone_ids() -
        # never touches zone.bbox/tag/attributes/data, purely a
        # transient UI read) overrides the normal tag-color border with
        # a red, thicker one plus a light stippled red fill, so an
        # overlapping zone is unmistakable regardless of its own tag
        # color. Selection keeps its own existing width bump on top, so
        # a selected+overlapping zone still reads as both at once.
        auto_conf = zone.attributes.get("confidence") if zone.attributes.get("source") in ("auto", "ocr") else None
        if auto_conf is None and zone.attributes.get("source") == "auto_index":
            # Auto Zone Index (spec 18/28): a NORMAL/high-confidence entry
            # displays with its own real tag color - IndexPE/SE/TE's own
            # existing visualization, unchanged (spec: "Use the existing
            # zone visualization system... Do not introduce arbitrary new
            # colors") - only a LOW-confidence one gets the existing
            # dashed/bucket-colored auto-zone highlight, so the user's eye
            # is drawn to exactly the entries worth double-checking (spec:
            # "For low-confidence zones, highlight them in the UI").
            idx_conf = zone.attributes.get("confidence")
            if idx_conf is not None and az_confidence.bucket(idx_conf, self.app.settings.get(
                    "auto_zone_thresholds")) == "low":
                auto_conf = idx_conf
        if zone.zone_id in self.app.overlapping_zone_ids:
            color = "#FF0000"
            width = 4 if is_selected else 3
            rect = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width,
                                                 fill=color, stipple="gray12")
        elif zone.attributes.get("needs_review") and not zone.attributes.get("manual_override"):
            # Auto Tag decision flagged NEEDS REVIEW (auto_zoning/auto_tag_engine.py)
            color = "#C2185B"
            width = 3 if is_selected else 2
            rect = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width, dash=(3, 2),
                                                 fill=color, stipple="gray12")
        elif auto_conf is not None:
            # Auto Zone confidence highlight (zone.attributes["source"] in
            # ("auto","ocr"), set by auto_zoning.hierarchy_builder.create_page
            # - never touches zone.bbox/tag, purely a transient-looking but
            # PERSISTED display marker so an auto- or OCR-created zone stays
            # visually distinct from a manually drawn one until the user
            # edits it) - dashed outline,
            # colored by confidence bucket (auto_zoning.confidence.bucket),
            # distinct from the solid overlap-red so both states never look
            # alike; overlap red still takes priority above (same precedence
            # pattern as the merged-zone gold color).
            bucket = az_confidence.bucket(auto_conf, self.app.settings.get("auto_zone_thresholds"))
            color = AUTO_ZONE_COLORS[bucket]
            width = 3 if is_selected else 2
            rect = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width, dash=(5, 2))
        else:
            color = self._zone_color(zone)
            width = 2 if is_selected else 1
            # Show Zone Borders OFF (spec 22-24): the rectangle item is
            # ALWAYS created (never state="hidden") so click-to-select/
            # resize-handle hit-testing never changes - only its outline
            # color blends into the canvas background, making it visually
            # invisible without losing any interactivity.
            show_borders = self.app.settings.get("show_zone_borders", True)
            outline_color = color if show_borders else self.palette["canvas_bg"]
            rect = self.canvas.create_rectangle(x0, y0, x1, y1, outline=outline_color, width=width)
        # Multi-zone selection highlight (Ctrl+A / Ctrl+click) - purely
        # ADDITIVE on top of whichever branch above already drew this
        # zone's own rect, never replacing the overlap-red/auto-
        # confidence/normal precedence those branches already establish.
        # Only shown once 2+ zones are actually selected together, so a
        # single Ctrl+click (or the plain single-selection path, which
        # keeps selected_zone_ids at size <=1) never looks different from
        # today's existing single-selection appearance.
        if len(self.selected_zone_ids) > 1 and zone.zone_id in self.selected_zone_ids:
            self.canvas.itemconfig(rect, dash=(4, 2))
        label_text = zone.tag if not zone.attributes.get("list_type") else f"{zone.tag}:{zone.attributes['list_type']}"
        # Reading Order comes FIRST, immediately before the tag name
        # ("[RO:14] p", not "p [RO:14]") - and is omitted entirely (no
        # fake placeholder like "[RO:-]") for a zone that doesn't currently
        # hold a Reading Order slot (a Horizontal-Split parent, a zone
        # consumed by an earlier merge - see ZoneManager.
        # _counts_in_reading_order), rather than displaying a number that
        # doesn't mean anything. Show Reading Order OFF (spec 24) omits
        # just this "[RO:N] " prefix, independent of the Show Tag Labels
        # toggle below (which hides the WHOLE label, RO prefix included).
        show_ro = self.app.settings.get("show_reading_order", True)
        if show_ro and isinstance(zone.serial, int):
            label_text = f"[RO:{zone.serial}] {label_text}"
        if auto_conf is not None:
            label_text += f" (auto {round(auto_conf)}%)"
        if zone.attributes.get("needs_review"):
            label_text += " REVIEW"
        if zone.attributes.get("locked"):
            label_text = "\U0001F512 " + label_text
        label = self.canvas.create_text(x0 + 4, y0 + 3, anchor="nw", text=label_text,
                                         fill=color, font=("Segoe UI", 8, "bold"))
        # Compact badge background behind the RO/tag label (spec 66.10 -
        # "compact numbered badges") - readable over any page content,
        # regardless of what's underneath in the PDF. Drawn AFTER the text
        # so its bbox is known, then lowered behind it (never behind the
        # zone rect itself, so the badge always sits above the fill/border).
        lx0, ly0, lx1, ly1 = self.canvas.bbox(label)
        badge = self.canvas.create_rectangle(lx0 - 2, ly0 - 1, lx1 + 2, ly1 + 1,
                                              fill=self.palette["surface"], outline=color, width=1)
        self.canvas.tag_lower(badge, label)
        # Hover feedback (spec 66.9/66.4) - a per-item <Enter>/<Leave> tag_bind
        # is a native, cheap Tk canvas feature (no per-pixel motion tracking
        # needed) - purely a border-width bump, never touching bbox/geometry.
        if not is_selected:
            self.canvas.tag_bind(rect, "<Enter>", lambda e, r=rect, w=width: self.canvas.itemconfigure(r, width=w + 1))
            self.canvas.tag_bind(rect, "<Leave>", lambda e, r=rect, w=width: self.canvas.itemconfigure(r, width=w))
        # Tag-only label at the zone's own RIGHT/END edge (distinct from the
        # top-left tag+RO label above) - spec: "SINGLE CLICK / DOUBLE CLICK
        # BEHAVIOR" section 7/8 ("A single click anywhere inside the zone
        # boundary should select the zone. This includes... zone label...
        # Double-clicking the zone label must also open the existing zone
        # popup"). Deliberately has NO item-level tag_bind of its own:
        # _hit_test_zone (below) already treats this label's own bbox as
        # part of the zone's clickable footprint (it can sit slightly
        # OUTSIDE the rect's own bounds), so the canvas-WIDGET-level
        # <ButtonPress-1>/<Double-Button-1> handlers (_on_press/
        # _on_double_click) already select/open-popup correctly for a
        # click here. An EARLIER version of this fix added a parallel
        # item-level tag_bind here too - confirmed via a real application-
        # level test to double-fire alongside the widget-level handler
        # (canvas item-level "break" does not reliably block the
        # separately-registered widget-level binding for the same
        # physical click - a real Tk quirk), opening the popup TWICE per
        # double-click. One hit-test, one set of handlers, per spec 37/38
        # ("Do not duplicate popup implementation... Ensure selection is
        # not lost between <Button-1> and <Double-Button-1>").
        end_text = f"[{zone.tag}]"
        ex, ey, eanchor = self._end_label_xy(x0, y0, x1, y1, end_text)
        end_label = self.canvas.create_text(ex, ey, anchor=eanchor, text=end_text,
                                             fill=color, font=("Segoe UI", 8, "bold"))
        self.canvas.tag_bind(rect, "<Button-1>", lambda e, zid=zone.zone_id: self._select_zone(zid))
        # Show Tag Labels OFF (spec 22/23/33): the label/badge/end-label
        # items are still CREATED (so self.zone_items keeps its stable
        # 3-tuple shape - live drag/resize code elsewhere calls
        # canvas.coords() on indices [1]/[2] unconditionally and would
        # error on a missing item) but set to state="hidden" - invisible
        # AND excluded from hit-testing, which is fine here since only the
        # zone RECTANGLE (never these text items) needs to keep receiving
        # clicks; the rectangle/selection/resize handles are completely
        # untouched by this toggle.
        if not self.app.settings.get("show_tag_labels", True):
            self.canvas.itemconfigure(label, state="hidden")
            self.canvas.itemconfigure(badge, state="hidden")
            self.canvas.itemconfigure(end_label, state="hidden")
        elif not is_selected:
            # Label clutter fix (spec: "excessive overlapping labels" on a
            # dense page - e.g. a two-column Index with 30+ small, tightly
            # stacked zones, where every zone's own end-edge label lands
            # right next to its neighbor's top-left label): the top-left
            # "[RO:N] tag" label above ALREADY conveys full zone identity
            # on its own and stays visible regardless (spec: "Do NOT
            # remove zone information") - the separate end-edge "[tag]"
            # label (a click-to-change-tag shortcut) is redundant noise on
            # every OTHER zone simultaneously, so it's shown only for the
            # currently SELECTED zone, where its quick-access role
            # actually matters (spec: "the currently selected zone may
            # show [RO:9] indexsecondary but other zones should have less
            # intrusive labels"). Still fully created either way (same
            # zone_items 3-tuple shape live drag/resize code depends on)
            # - only its visibility changes, and only when Show Tag Labels
            # is ON in the first place (the branch above already handles
            # the OFF case for every label uniformly).
            self.canvas.itemconfigure(end_label, state="hidden")
        self.zone_items[zone.zone_id] = (rect, label, end_label)

    def _end_label_xy(self, x0, y0, x1, y1, text):
        """Position for the small "[tag]" end-label: just OUTSIDE the right
        edge (anchor="w"), vertically centered on the zone, unless that
        would run past the rendered page's own right edge, in which case
        it's placed just INSIDE instead (anchor="e") so it stays visible -
        a cheap character-count width estimate, not an actual canvas
        measurement, since this is also reused for the live drag/resize
        preview (see _on_drag/_update_resize) where per-frame precision
        isn't required - only the final, post-release redraw() (which
        always goes through here) needs to be exactly right."""
        y_mid = (y0 + y1) / 2
        content_w = (self.base_image.width * self.app.zoom) if self.base_image else self.canvas.winfo_width()
        est_width = 7 * len(text) + 8
        if x1 + 4 + est_width <= content_w:
            return x1 + 4, y_mid, "w"
        return x1 - 4, y_mid, "e"

    def _open_zone_popup(self, zone_id):
        """spec: "SINGLE CLICK / DOUBLE CLICK BEHAVIOR" - "Reuse the
        existing zone properties/edit dialog... Do not duplicate popup
        implementation." THE one place a double-click (zone body via
        _on_double_click, or the end-of-zone tag label via its own
        tag_bind above) opens the existing "Change Tag" UI (ZoneInfoDialog,
        via App.on_zone_edit_requested - the same combobox the left Tags
        panel's own buttons are built from). Selects first (spec section
        2: "The zone must become selected. Then open the EXISTING zone
        popup/edit dialog"), then opens - never the reverse order. Returns
        "break" so an item-level tag_bind caller (the end label) can stop
        this same double-click event from ALSO reaching the canvas-wide
        <Double-Button-1> handler and firing a second, duplicate popup."""
        self._select_zone(zone_id)
        self.app.on_zone_edit_requested(zone_id)
        return "break"

    # ---------------- coordinate conversion ----------------
    def _current_rotation(self):
        return self.app.page_rotations.get(self.app.current_page, 0)

    def pdf_to_screen(self, bbox):
        """Native PDF-space bbox -> screen/canvas pixel coords, honoring
        the CURRENT page's rotation (see module-level _rotate_bbox) before
        applying the existing DPI/zoom scale - unrotated pages (rot=0, the
        overwhelming majority of the time) take the exact same fast path
        as before this feature existed."""
        scale = (self.viewer_dpi() / 72.0) * self.app.zoom
        rot = self._current_rotation()
        if rot and self.app.pdf_document:
            pw, ph = self.app.pdf_document.page_size(self.app.current_page)
            bbox = _rotate_bbox(bbox, pw, ph, rot)
        x0, y0, x1, y1 = bbox
        return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)

    def screen_to_pdf(self, coords):
        """Inverse of pdf_to_screen: screen/canvas pixel coords -> native
        PDF-space bbox, un-rotating by the CURRENT page's rotation. Every
        zone bbox that ends up stored (drawing a new zone, moving,
        resizing) always passes through here, so it is always native-space
        regardless of what rotation the user is currently viewing at."""
        scale = (self.viewer_dpi() / 72.0) * self.app.zoom
        x0, y0, x1, y1 = coords
        bbox = [x0 / scale, y0 / scale, x1 / scale, y1 / scale]
        rot = self._current_rotation()
        if rot and self.app.pdf_document:
            pw, ph = self.app.pdf_document.page_size(self.app.current_page)
            bbox = _unrotate_bbox(bbox, pw, ph, rot)
        return bbox

    def canvas_point(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    # ---------------- selection & handles ----------------
    def redraw_selection_only(self):
        """Redraw zone overlays without rerendering the PDF page image."""
        if self.base_image is None:
            return
        for items in list(self.zone_items.values()):
            for item in items:
                try:
                    self.canvas.delete(item)
                except Exception:
                    pass
        for item in list(self.handle_items.values()):
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        self.zone_items = {}
        self.handle_items = {}
        self._draw_all_zones()
        self._draw_region_splits()
        smart_az = getattr(self.app, "smart_az", None)
        if smart_az is not None:
            smart_az.draw_debug_overlays(self)

    def _select_zone(self, zone_id):
        if self.mode in ("split", "region_split_h", "region_split_v"):
            return
        self.selected_zone_id = zone_id
        # A plain (non-Ctrl) click collapses any existing multi-selection
        # down to just this one zone - standard selection convention, and
        # what lets redraw_selection_only() below correctly clear every
        # OTHER zone's own multi-select highlight (it redraws every
        # zone's overlay fresh, not just this one's).
        self.selected_zone_ids = {zone_id}
        self.app.on_zone_selected(zone_id)
        self.redraw_selection_only()

    def deselect(self):
        self.selected_zone_id = None
        self.selected_zone_ids = set()
        self.app.on_zone_selected(None)
        self.redraw_selection_only()

    def select_all_zones_on_page(self):
        """Ctrl+A (spec: "Zoning canvas focus: Ctrl+A = select all
        zones"). Selects every zone on the CURRENT page - matching this
        app's own existing page-scoped convention elsewhere (the
        Properties panel/Zone Navigator already operate per-page, not
        book-wide), rather than inventing a new whole-book selection
        mode with no existing precedent. Returns "break" so this
        canvas-level binding is never re-interpreted by a lower-priority
        default Tk binding."""
        if not self.app.pdf_document:
            return "break"
        page = self.app.current_page
        zone_ids = {z.zone_id for z in self.app.zone_manager.zones_on_page(page)}
        self.selected_zone_ids = zone_ids
        if zone_ids and self.selected_zone_id not in zone_ids:
            self.selected_zone_id = next(iter(zone_ids))
            self.app.on_zone_selected(self.selected_zone_id)
        elif not zone_ids:
            self.selected_zone_id = None
            self.app.on_zone_selected(None)
        self.redraw_selection_only()
        return "break"

    def _on_ctrl_click(self, event):
        """Ctrl+click toggles one zone's membership in the multi-
        selection - independent of _on_press's own drag/move/draw logic
        (Tk dispatches the more specific <Control-Button-1> binding
        instead of the plain <ButtonPress-1> one for a Ctrl-held click,
        so both never fire for the same click)."""
        if self.mode in ("split", "region_split_h", "region_split_v", "drawing", "resizing"):
            return
        x, y = self.canvas_point(event)
        clicked_zone = self._hit_test_zone(x, y)
        if not clicked_zone:
            return
        if clicked_zone in self.selected_zone_ids:
            self.selected_zone_ids.discard(clicked_zone)
            if self.selected_zone_id == clicked_zone:
                self.selected_zone_id = next(iter(self.selected_zone_ids), None)
                self.app.on_zone_selected(self.selected_zone_id)
        else:
            self.selected_zone_ids.add(clicked_zone)
            self.selected_zone_id = clicked_zone
            self.app.on_zone_selected(clicked_zone)
        self.redraw_selection_only()

    def _handle_positions(self, x0, y0, x1, y1):
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return {
            "nw": (x0, y0), "n": (mx, y0), "ne": (x1, y0), "e": (x1, my),
            "se": (x1, y1), "s": (mx, y1), "sw": (x0, y1), "w": (x0, my),
        }

    def _draw_handles(self, zone_id):
        zone = self.app.zone_manager.zones.get(zone_id)
        if not zone:
            return
        x0, y0, x1, y1 = self.pdf_to_screen(zone.bbox)
        for name, (hx, hy) in self._handle_positions(x0, y0, x1, y1).items():
            s = HANDLE_SIZE / 2
            item = self.canvas.create_rectangle(hx - s, hy - s, hx + s, hy + s,
                                                 fill="white", outline="black")
            self.canvas.tag_bind(item, "<ButtonPress-1>",
                                  lambda e, n=name: self._start_resize(n))
            self.handle_items[name] = item

    # ---------------- split-line hit testing ----------------
    def _find_boundary_near(self, screen_x, screen_y, tol=6):
        if not self.split_state:
            return None
        scale = (self.viewer_dpi() / 72.0) * self.app.zoom
        coord = screen_x if self.split_state.get("axis") == "v" else screen_y
        for i, pdf_val in enumerate(self.split_state["boundaries"]):
            if abs(pdf_val * scale - coord) <= tol:
                return i
        return None

    # ---------------- generic manual region-split guides (any tag) ----------------
    def start_region_split_mode(self, zone_id, axis):
        """Enters manual Region Split guide-editing mode for ANY zone,
        regardless of tag (Ctrl+Shift+R for axis="row"/horizontal,
        Ctrl+Shift+C for axis="col"/vertical) - mirrors Horizontal
        Split's own add/click-to-delete/drag-to-move interaction (see the
        "split" branches of _on_motion/_on_press/_on_drag/_on_release
        below) but guides are PERSISTED DIRECTLY onto the zone's own
        attributes ("horizontal_splits"/"vertical_splits", PDF-space
        coordinates) on every add/move/delete via _persist_region_splits,
        rather than only materializing on a separate confirm step - they
        are permanent per-zone split metadata (spec: "They are NOT
        separate XML zones... must be saved in the project"), never a
        one-time split-into-new-zones action the way Horizontal Split's
        own boundaries are. core.xml_generator.split_zone_into_regions
        consumes them at XML-generation time to produce one element per
        region for every tag except table (whose splits instead form a
        row x column grid inside one <table-wrap>, see _zone_table) - the
        SAME generic mechanism regardless of the zone's own tag."""
        zone = self.app.zone_manager.zones.get(zone_id)
        if not zone:
            return
        self.mode = "region_split_h" if axis == "row" else "region_split_v"
        self.selected_zone_id = zone_id
        key = "horizontal_splits" if axis == "row" else "vertical_splits"
        existing = list(zone.attributes.get(key, []))
        self.region_split_state = {
            "zone_id": zone_id, "axis": axis, "values": existing, "guide_item": None,
            "committed_items": [], "dragging_index": None, "drag_moved": False,
            "selected_index": None,
        }
        self.canvas.config(cursor="crosshair")
        self.redraw()
        label = "Horizontal" if axis == "row" else "Vertical"
        orientation = "horizontal" if axis == "row" else "vertical"
        # Real, reported confusion this message now heads off directly:
        # there is NO separate confirm/apply step for Region Split (see
        # this method's own docstring - every line is persisted to the
        # zone's own attributes the instant it's added/moved/deleted).
        # An operator coming from the OLDER Horizontal Split feature
        # (which DOES need Enter to confirm, since it creates whole new
        # zones) reasonably expected the same "confirm" step here and
        # couldn't find one - because none exists to find.
        self.app.set_status(f"{label} Split Mode ({zone.tag}) - click inside the zone to add a {orientation} split "
                             f"line (saved immediately, no confirm needed). Click a line to select it (Delete key "
                             f"removes it), drag to move it. Esc when finished, Ctrl+Shift+X to clear all guides.")

    def exit_region_split_mode(self):
        if self.region_split_state and self.region_split_state.get("guide_item"):
            self.canvas.delete(self.region_split_state["guide_item"])
        self.region_split_state = None
        self.mode = "idle"
        self.canvas.config(cursor="")
        self.redraw()

    def clear_region_splits(self):
        st = self.region_split_state
        if not st:
            return
        st["values"] = []
        self._persist_region_splits()
        for item in st["committed_items"]:
            self.canvas.delete(item)
        st["committed_items"] = []
        if st.get("guide_item"):
            self.canvas.delete(st["guide_item"])
            st["guide_item"] = None
        self.app.set_status("Cleared all split guides for this zone")

    def delete_selected_region_split(self):
        """Removes whichever guide is currently SELECTED (highlighted red -
        set by a plain click-no-drag on it, see _on_release below), bound
        to the global <Delete> key while in Row/Column Split Mode. Distinct
        from dragging/clicking-to-add: this is the explicit "Delete:
        Delete Selected Split" shortcut. No-op if nothing is selected."""
        st = self.region_split_state
        if not st or st.get("selected_index") is None:
            return
        idx = st["selected_index"]
        st["values"].pop(idx)
        st["selected_index"] = None
        self._persist_region_splits()
        self.redraw()
        label = "horizontal" if st["axis"] == "row" else "vertical"
        self.app.set_status(f"Deleted selected {label} split guide  |  {len(st['values'])} guide(s) remaining")

    def _persist_region_splits(self):
        """Writes the CURRENT working guide list back onto the zone's own
        attributes - same established pattern as hyphen_keep_boundaries
        (gui/dialogs.py HyphenReviewDialog): round-trips through project
        save/load automatically, no core/project_manager.py changes
        needed. Works identically for any tag - "horizontal_splits"/
        "vertical_splits" are generic zone attributes, not table-specific."""
        st = self.region_split_state
        if not st:
            return
        zone = self.app.zone_manager.zones.get(st["zone_id"])
        if not zone:
            return
        key = "horizontal_splits" if st["axis"] == "row" else "vertical_splits"
        zone.attributes[key] = sorted(st["values"])

    def _draw_region_splits(self):
        """Renders every currently-committed guide line for the active
        region_split_state, if any - called from redraw() (line ~80)
        alongside _draw_all_zones so guides reappear correctly after
        ANY full canvas rebuild (e.g. triggered by an unrelated
        on_zones_changed elsewhere), not just when guide mode is first
        entered."""
        st = self.region_split_state
        if not st:
            return
        zone = self.app.zone_manager.zones.get(st["zone_id"])
        if not zone:
            return
        zx0, zy0, zx1, zy1 = self.pdf_to_screen(zone.bbox)
        color = "cyan" if st["axis"] == "row" else "magenta"
        selected_index = st.get("selected_index")
        st["committed_items"] = []
        for i, v in enumerate(st["values"]):
            is_selected = i == selected_index
            line_color = "red" if is_selected else color
            line_width = 3 if is_selected else 2
            if st["axis"] == "row":
                sx0, sy, sx1, _ = self.pdf_to_screen([zone.bbox[0], v, zone.bbox[2], v])
                item = self.canvas.create_line(sx0, sy, sx1, sy, fill=line_color, width=line_width)
            else:
                sx, sy0, _, sy1 = self.pdf_to_screen([v, zone.bbox[1], v, zone.bbox[3]])
                item = self.canvas.create_line(sx, sy0, sx, sy1, fill=line_color, width=line_width)
            st["committed_items"].append(item)

    def _find_region_split_near(self, x, y, tol=6):
        st = self.region_split_state
        if not st:
            return None
        scale = (self.viewer_dpi() / 72.0) * self.app.zoom
        coord = y if st["axis"] == "row" else x
        for i, v in enumerate(st["values"]):
            if abs(v * scale - coord) <= tol:
                return i
        return None

    def _get_split_zone(self):
        """Safely return the zone referenced by the current split state.

        A confirmed split can replace/remove the original zone while Tk is
        still delivering queued mouse events. Using .get() here prevents a
        stale zone_id from raising KeyError during those events.
        """
        if not self.split_state:
            return None
        zone_id = self.split_state.get("zone_id")
        if not zone_id:
            return None
        return self.app.zone_manager.zones.get(zone_id)

    def _get_region_split_zone(self):
        """Safely return the zone referenced by region_split_state."""
        if not self.region_split_state:
            return None
        zone_id = self.region_split_state.get("zone_id")
        if not zone_id:
            return None
        return self.app.zone_manager.zones.get(zone_id)

    # ---------------- mouse handlers ----------------
    def _on_motion(self, event):
        if self.mode == "split" and self.split_state and self.split_state.get("dragging_index") is None:
            x, y = self.canvas_point(event)
            zone = self._get_split_zone()
            if zone is None:
                self.split_state = None
                self.mode = "idle"
                self.canvas.config(cursor="")
                self.redraw()
                return
            zx0, zy0, zx1, zy1 = self.pdf_to_screen(zone.bbox)
            if self.split_state.get("guide_item"):
                self.canvas.delete(self.split_state["guide_item"])
            if self.split_state.get("axis") == "v":
                x = max(zx0, min(zx1, x))  # never preview a boundary outside the zone
                self.split_state["guide_item"] = self.canvas.create_line(x, zy0, x, zy1, fill="yellow", width=2, dash=(4, 2))
                pdf_val = self.screen_to_pdf((x, zy0, x, zy1))[0]
                self.app.set_status(f"Split position: x={pdf_val:.0f}  |  {len(self.split_state['boundaries'])} boundary(ies)")
            else:
                y = max(zy0, min(zy1, y))  # never preview a boundary outside the zone
                self.split_state["guide_item"] = self.canvas.create_line(zx0, y, zx1, y, fill="yellow", width=2, dash=(4, 2))
                pdf_val = self.screen_to_pdf((zx0, y, zx1, y))[1]
                self.app.set_status(f"Split position: y={pdf_val:.0f}  |  {len(self.split_state['boundaries'])} boundary(ies)")
        elif self.mode in ("region_split_h", "region_split_v") and self.region_split_state \
                and self.region_split_state.get("dragging_index") is None:
            x, y = self.canvas_point(event)
            st = self.region_split_state
            zone = self._get_region_split_zone()
            if zone is None:
                self.region_split_state = None
                self.mode = "idle"
                self.canvas.config(cursor="")
                self.redraw()
                return
            zx0, zy0, zx1, zy1 = self.pdf_to_screen(zone.bbox)
            if st.get("guide_item"):
                self.canvas.delete(st["guide_item"])
            if st["axis"] == "row":
                y = max(zy0, min(zy1, y))
                st["guide_item"] = self.canvas.create_line(zx0, y, zx1, y, fill="yellow", width=2, dash=(4, 2))
                pdf_val = self.screen_to_pdf((zx0, y, zx1, y))[1]
            else:
                x = max(zx0, min(zx1, x))
                st["guide_item"] = self.canvas.create_line(x, zy0, x, zy1, fill="yellow", width=2, dash=(4, 2))
                pdf_val = self.screen_to_pdf((x, zy0, x, zy1))[0]
            label = "Row" if st["axis"] == "row" else "Column"
            self.app.set_status(f"{label} split position: {pdf_val:.0f}  |  {len(st['values'])} guide(s)")

    def _hit_test_zone(self, x, y):
        """Returns the SMALLEST-area zone whose rect contains (x, y), or None.
        Smallest-wins (not last-drawn-wins) so hit-testing is independent of
        creation order - matches the same "smallest containing zone" rule
        used for auto-parenting, so clicking a small child inside a larger
        parent reliably hits the child regardless of which was drawn first.

        spec: "SINGLE CLICK / DOUBLE CLICK BEHAVIOR" section 7 ("A single
        click anywhere inside the zone boundary should select the zone.
        This includes: zone border, zone label, zone interior, zone text
        area.") - the zone's own end-edge "[tag]" label can sit slightly
        OUTSIDE the rect's own bounds (drawn at the zone's right/end edge),
        so a SECOND pass checks each zone's end_label bbox too, only for
        points the rect pass didn't already resolve. This is also what
        makes the item-level tag_bind on that label redundant-but-
        harmless rather than load-bearing: canvas item-level "break" does
        NOT reliably stop the separately-registered canvas-WIDGET-level
        <ButtonPress-1>/<Double-Button-1> bindings this method backs
        (confirmed directly - a real Tk canvas quirk, not a Python bug) -
        so correctness here comes from hit-testing the label's real
        position, never from relying on event-propagation being blocked."""
        best_id, best_area = None, None
        for zid, (rect, label, end_label) in self.zone_items.items():
            bx0, by0, bx1, by1 = self.canvas.coords(rect)
            # Bounds are rounded before comparing: a mouse click is always
            # reported at an integer canvas pixel, but a zone's drawn rect
            # can sit at a fractional canvas coordinate (any non-integer
            # zoom/DPI scale), so an integer click exactly on the visually-
            # rendered border pixel could otherwise fall a fraction of a
            # pixel outside the raw float bounds and be missed. Rounding
            # matches what the eye actually sees Tk render for that pixel.
            if round(bx0) <= x <= round(bx1) and round(by0) <= y <= round(by1):
                a = (bx1 - bx0) * (by1 - by0)
                if best_area is None or a < best_area:
                    best_area, best_id = a, zid
        if best_id is not None:
            return best_id
        for zid, (rect, label, end_label) in self.zone_items.items():
            bbox = self.canvas.bbox(end_label)   # None when hidden (state="hidden") - never matches
            if bbox and bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                return zid
        return None

    def _on_press(self, event):
        x, y = self.canvas_point(event)
        if self.mode == "split":
            # If the press lands on an existing committed line, remember which
            # one - _on_drag will move it live, _on_release decides (based on
            # whether it actually moved) between "delete" (plain click) and
            # "reposition" (dragged). A press on empty space inside the zone
            # adds a new boundary on release, as before.
            if self.split_state:
                self.split_state["dragging_index"] = self._find_boundary_near(x, y)
                self.split_state["drag_moved"] = False
            return
        if self.mode in ("region_split_h", "region_split_v"):
            # Same click-vs-drag disambiguation as Horizontal Split above:
            # _on_drag sets "drag_moved" only if the press actually landed
            # on an existing guide AND the mouse moved before release.
            if self.region_split_state:
                self.region_split_state["dragging_index"] = self._find_region_split_near(x, y)
                self.region_split_state["drag_moved"] = False
            return
        if self.mode == "resizing":
            # A resize handle's own tag_bind already ran (it fires alongside
            # this canvas-wide handler for the same click, since Tk dispatches
            # both item-level and widget-level bindings) and set mode to
            # "resizing" with its own self.drag shape. A corner/edge handle
            # sits exactly on the selected zone's own boundary, so without
            # this guard the zone-body hit-test below would match the
            # selected zone and immediately overwrite mode back to "moving",
            # silently turning every resize-handle drag into a zone move
            # instead - this was why dragging the handles never resized
            # anything. Same fix pattern as the split-mode handle guard.
            return
        clicked_zone = self._hit_test_zone(x, y)
        if self.app.active_tag:
            # A selected tag means "create this tag" when drawing. Only allow
            # moving the already-selected zone when its tag matches the active
            # tag. This is required for Figure -> Caption: after Figure is
            # created it may remain selected, but selecting Caption and drawing
            # over/inside that Figure must create a real Caption zone instead
            # of moving the Figure.
            same_tag_as_selected = (
                clicked_zone
                and clicked_zone == self.selected_zone_id
                and self.app.zone_manager.zones[clicked_zone].tag == self.app.active_tag[0]
            )
            if same_tag_as_selected:
                self.mode = "moving"
                self.drag = {"start": (x, y), "orig_bbox": list(self.app.zone_manager.zones[clicked_zone].bbox)}
            else:
                self.mode = "drawing"
                self.drag = {"start": (x, y), "rect": self.canvas.create_rectangle(
                    x, y, x, y, outline=self._tag_color(self.app.active_tag[0]), width=2, dash=(3, 2))}
            return
        if clicked_zone and clicked_zone == self.selected_zone_id:
            self.mode = "moving"
            self.drag = {"start": (x, y), "orig_bbox": list(self.app.zone_manager.zones[clicked_zone].bbox)}
        elif clicked_zone:
            self._select_zone(clicked_zone)
        else:
            self.deselect()

    def _on_drag(self, event):
        self._update_auto_scroll(event.y)
        x, y = self.canvas_point(event)
        self._apply_drag_at(x, y)

    def _apply_drag_at(self, x, y):
        """The live-update half of _on_drag, factored out so the edge
        auto-scroll tick (_auto_scroll_step) can re-run the exact same
        drawing/resizing/moving update using a freshly recomputed
        cursor-in-canvas-space position after each programmatic scroll -
        without this, the in-progress zone rectangle would stop tracking
        the cursor the moment auto-scroll starts moving the page underneath
        it while the mouse itself stays physically still (spec: "the
        current zone rectangle must remain attached to the cursor
        correctly")."""
        if self.mode == "split" and self.split_state and self.split_state.get("dragging_index") is not None:
            self.split_state["drag_moved"] = True
            zone = self._get_split_zone()
            if zone is None:
                self.split_state = None
                self.mode = "idle"
                self.canvas.config(cursor="")
                self.redraw()
                return
            zx0, zy0, zx1, zy1 = self.pdf_to_screen(zone.bbox)
            idx = self.split_state["dragging_index"]
            if idx >= len(self.split_state.get("committed_items", [])):
                self.split_state = None
                self.mode = "idle"
                self.canvas.config(cursor="")
                self.redraw()
                return
            if self.split_state.get("axis") == "v":
                x = max(zx0, min(zx1, x))
                self.canvas.coords(self.split_state["committed_items"][idx], x, zy0, x, zy1)
            else:
                y = max(zy0, min(zy1, y))
                self.canvas.coords(self.split_state["committed_items"][idx], zx0, y, zx1, y)
        elif self.mode in ("region_split_h", "region_split_v") and self.region_split_state \
                and self.region_split_state.get("dragging_index") is not None:
            st = self.region_split_state
            st["drag_moved"] = True
            zone = self._get_region_split_zone()
            if zone is None:
                self.region_split_state = None
                self.mode = "idle"
                self.canvas.config(cursor="")
                self.redraw()
                return
            zx0, zy0, zx1, zy1 = self.pdf_to_screen(zone.bbox)
            idx = st["dragging_index"]
            if idx >= len(st.get("committed_items", [])):
                self.region_split_state = None
                self.mode = "idle"
                self.canvas.config(cursor="")
                self.redraw()
                return
            if st["axis"] == "row":
                y = max(zy0, min(zy1, y))
                self.canvas.coords(st["committed_items"][idx], zx0, y, zx1, y)
            else:
                x = max(zx0, min(zx1, x))
                self.canvas.coords(st["committed_items"][idx], x, zy0, x, zy1)
        elif self.mode == "drawing":
            sx, sy = self.drag["start"]
            self.canvas.coords(self.drag["rect"], sx, sy, x, y)
        elif self.mode == "moving" and self.selected_zone_id:
            sx, sy = self.drag["start"]
            dx, dy = x - sx, y - sy
            zone = self.app.zone_manager.zones[self.selected_zone_id]
            x0, y0, x1, y1 = self.pdf_to_screen(self.drag["orig_bbox"])
            nx0, ny0, nx1, ny1 = x0 + dx, y0 + dy, x1 + dx, y1 + dy
            self.canvas.coords(self.zone_items[self.selected_zone_id][0], nx0, ny0, nx1, ny1)
            self.canvas.coords(self.zone_items[self.selected_zone_id][1], nx0 + 3, ny0 + 2)
            end_text = f"[{zone.tag}]"
            ex, ey, _eanchor = self._end_label_xy(nx0, ny0, nx1, ny1, end_text)
            self.canvas.coords(self.zone_items[self.selected_zone_id][2], ex, ey)
            self._reposition_handles(nx0, ny0, nx1, ny1)
        elif self.mode == "resizing" and self.selected_zone_id:
            self._update_resize(x, y)

    # ---------------- edge auto-scroll while dragging ----------------
    def _edge_scroll_amount(self, widget_y):
        """widget_y: cursor position relative to the canvas widget itself
        (NOT canvas/scroll space - the edge band is a fixed screen-space
        strip regardless of current scroll offset). Returns a signed
        "units" delta for yview_scroll (negative = scroll up/toward top,
        positive = scroll down), 0 outside the activation band. Magnitude
        grows the closer the cursor is to the actual edge (spec: "scrolling
        speed increases as the cursor gets closer to the edge"), never
        during idle mode - only while actually drawing/resizing/moving."""
        height = self.canvas.winfo_height()
        if widget_y < AUTO_SCROLL_EDGE_PX:
            depth = AUTO_SCROLL_EDGE_PX - max(0, widget_y)
            sign = -1
        elif widget_y > height - AUTO_SCROLL_EDGE_PX:
            depth = widget_y - (height - AUTO_SCROLL_EDGE_PX)
            sign = 1
        else:
            return 0
        frac = max(0.0, min(1.0, depth / AUTO_SCROLL_EDGE_PX))
        units = AUTO_SCROLL_MIN_UNITS + round(frac * (AUTO_SCROLL_MAX_UNITS - AUTO_SCROLL_MIN_UNITS))
        return sign * units

    def _update_auto_scroll(self, widget_y):
        """Called on every real mouse-drag event - starts the repeating
        auto-scroll tick the first time the cursor enters the edge band
        during an active drag, lets it keep running (widget_y is re-sampled
        fresh on each tick, see _auto_scroll_step) as long as the cursor
        stays there, and stops it the moment the cursor moves back away
        from the edge (spec: "scrolling stops when the cursor moves away
        from the edge") - _on_release separately guarantees it also stops
        the instant the button is released, even if the cursor is still
        sitting inside the edge band at that moment."""
        if self.mode not in ("drawing", "resizing", "moving"):
            self._stop_auto_scroll()
            return
        if self._edge_scroll_amount(widget_y) == 0:
            self._stop_auto_scroll()
            return
        if self._auto_scroll_job is None:
            self._auto_scroll_step()

    def _auto_scroll_step(self):
        """One tick of the auto-scroll loop, rescheduling itself via
        after() while conditions still hold - this is what lets scrolling
        continue smoothly while the mouse itself stays physically stationary
        at the edge (a real <B1-Motion> event only fires when the mouse
        actually moves, which the auto-scroll case explicitly does NOT
        require). Re-reads the CURRENT pointer position fresh each tick via
        winfo_pointerxy (never a stale captured event) - the correct thing
        to poll here, since nothing else generates a fresh Tk event while
        the mouse is idle."""
        if self.mode not in ("drawing", "resizing", "moving"):
            self._auto_scroll_job = None
            return
        widget_y = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
        widget_x = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
        amount = self._edge_scroll_amount(widget_y)
        if amount == 0:
            self._auto_scroll_job = None
            return
        self.canvas.yview_scroll(amount, "units")
        # Re-derive the cursor's CANVAS-space (scroll-independent) position
        # after the scroll and feed it through the same update path a real
        # drag event uses, so the in-progress rectangle/resize/move keeps
        # extending toward the cursor as the page moves underneath it.
        cx, cy = self.canvas.canvasx(widget_x), self.canvas.canvasy(widget_y)
        self._apply_drag_at(cx, cy)
        self._auto_scroll_job = self.after(AUTO_SCROLL_INTERVAL_MS, self._auto_scroll_step)

    def _stop_auto_scroll(self):
        if self._auto_scroll_job is not None:
            self.after_cancel(self._auto_scroll_job)
            self._auto_scroll_job = None

    def _on_release(self, event):
        # Unconditional and first: auto-scroll must stop the instant the
        # button is released, regardless of mode or where the cursor
        # currently sits (spec: "scrolling must not continue after mouse
        # release") - every mode branch below still runs exactly as before.
        self._stop_auto_scroll()
        x, y = self.canvas_point(event)
        if self.mode == "split":
            if self.split_state:
                zone = self._get_split_zone()
                if zone is None:
                    self.split_state = None
                    self.mode = "idle"
                    self.canvas.config(cursor="")
                    self.redraw()
                    return
                x0, y0, x1, y1 = self.pdf_to_screen(zone.bbox)
                is_vertical = self.split_state.get("axis") == "v"
                x = max(x0, min(x1, x))
                y = max(y0, min(y1, y))  # never allow a boundary outside the zone
                dragging_index = self.split_state.get("dragging_index")
                if dragging_index is not None:
                    if self.split_state.get("drag_moved"):
                        # dragged an existing line to a new position
                        if is_vertical:
                            pdf_val = self.screen_to_pdf((x, y0, x, y1))[0]
                            self.canvas.coords(self.split_state["committed_items"][dragging_index], x, y0, x, y1)
                        else:
                            pdf_val = self.screen_to_pdf((x0, y, x1, y))[1]
                            self.canvas.coords(self.split_state["committed_items"][dragging_index], x0, y, x1, y)
                        self.split_state["boundaries"][dragging_index] = pdf_val
                    else:
                        # plain click on an existing line with no drag - delete it
                        self.canvas.delete(self.split_state["committed_items"].pop(dragging_index))
                        self.split_state["boundaries"].pop(dragging_index)
                    self.split_state["dragging_index"] = None
                    self.split_state["drag_moved"] = False
                elif is_vertical and x0 <= x <= x1:
                    pdf_val = self.screen_to_pdf((x, y0, x, y1))[0]
                    self.split_state["boundaries"].append(pdf_val)
                    line = self.canvas.create_line(x, y0, x, y1, fill="lime", width=2)
                    self.split_state.setdefault("committed_items", []).append(line)
                elif not is_vertical and y0 <= y <= y1:
                    pdf_val = self.screen_to_pdf((x0, y, x1, y))[1]
                    self.split_state["boundaries"].append(pdf_val)
                    line = self.canvas.create_line(x0, y, x1, y, fill="lime", width=2)
                    self.split_state.setdefault("committed_items", []).append(line)
            return
        if self.mode in ("region_split_h", "region_split_v"):
            st = self.region_split_state
            if st:
                zone = self._get_region_split_zone()
                if zone is None:
                    self.region_split_state = None
                    self.mode = "idle"
                    self.canvas.config(cursor="")
                    self.redraw()
                    return
                zx0, zy0, zx1, zy1 = self.pdf_to_screen(zone.bbox)
                dragging_index = st.get("dragging_index")
                if dragging_index is not None:
                    if st.get("drag_moved"):
                        # dragged an existing guide to a new position
                        if st["axis"] == "row":
                            cy = max(zy0, min(zy1, y))
                            pdf_val = self.screen_to_pdf((zx0, cy, zx1, cy))[1]
                        else:
                            cx = max(zx0, min(zx1, x))
                            pdf_val = self.screen_to_pdf((cx, zy0, cx, zy1))[0]
                        st["values"][dragging_index] = pdf_val
                        st["selected_index"] = dragging_index
                        self._persist_region_splits()
                        self.redraw()
                    else:
                        # plain click on an existing guide with no drag -
                        # SELECT it (highlighted red); Delete key removes
                        # the selected guide via delete_selected_region_split().
                        st["selected_index"] = dragging_index
                        self.redraw()
                    st["dragging_index"] = None
                    st["drag_moved"] = False
                elif st["axis"] == "row" and zy0 <= y <= zy1:
                    pdf_val = self.screen_to_pdf((zx0, y, zx1, y))[1]
                    st["values"].append(pdf_val)
                    st["selected_index"] = None
                    self._persist_region_splits()
                    self.redraw()
                elif st["axis"] == "col" and zx0 <= x <= zx1:
                    pdf_val = self.screen_to_pdf((x, zy0, x, zy1))[0]
                    st["values"].append(pdf_val)
                    st["selected_index"] = None
                    self._persist_region_splits()
                    self.redraw()
            return
        if self.mode == "drawing":
            sx, sy = self.drag["start"]
            self.canvas.delete(self.drag["rect"])
            if abs(x - sx) > 3 and abs(y - sy) > 3:
                bbox = self._clamp_bbox(self.screen_to_pdf((min(sx, x), min(sy, y), max(sx, x), max(sy, y))))
                tag, attrs = self.app.active_tag
                zone = self.app.zone_manager.add_zone(self.app.current_page, tag, bbox, attributes=attrs)
                self.app.on_zone_created(zone.zone_id)
        elif self.mode == "moving" and self.selected_zone_id:
            sx, sy = self.drag["start"]
            dx, dy = x - sx, y - sy
            scale = (self.viewer_dpi() / 72.0) * self.app.zoom
            ox0, oy0, ox1, oy1 = self.drag["orig_bbox"]
            new_bbox = [ox0 + dx / scale, oy0 + dy / scale, ox1 + dx / scale, oy1 + dy / scale]
            new_bbox = self._clamp_move(new_bbox)
            self.app.zone_manager.update_bbox(self.selected_zone_id, new_bbox)
            self.app.on_zones_changed()
        elif self.mode == "resizing" and self.selected_zone_id:
            self._finish_resize()
        self.mode = "idle"
        self.drag = {}

    def _on_wheel(self, event):
        units = self._normalize_wheel_units(event.delta)
        if units:
            self.canvas.yview_scroll(units, "units")
        debug_log.log("SCROLL", f"widget=PDFViewer.canvas event=<MouseWheel> delta={event.delta} "
                                 f"widget_xy=({event.x},{event.y}) action=yview_scroll units={units} handled=True")

    @staticmethod
    def _normalize_wheel_units(delta: int) -> int:
        return normalize_wheel_units(delta)

    def _on_wheel_h(self, event):
        self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
        debug_log.log("SCROLL", f"widget=PDFViewer.canvas event=<Shift-MouseWheel> delta={event.delta} "
                                 f"widget_xy=({event.x},{event.y}) action=xview_scroll handled=True")

    def _on_ctrl_wheel(self, event):
        if event.delta > 0:
            self.app.zoom_in()
        else:
            self.app.zoom_out()
        debug_log.log("SCROLL", f"widget=PDFViewer.canvas event=<Control-MouseWheel> delta={event.delta} "
                                 f"widget_xy=({event.x},{event.y}) action=zoom handled=True")

    def _on_touchpad_scroll(self, dx: int, dy: int):
        """Tk 9's <TouchpadScroll> (see theme.bind_touchpad_scroll) - dx/dy
        are already-unpacked, high-resolution signed deltas on the SAME
        notch scale <MouseWheel>'s delta uses (dy == +/-120 for a "full
        notch" equivalent), just delivered at much finer granularity - a
        continuous two-finger swipe fires many small, closely-spaced
        samples rather than a handful of discrete notches. Scrolling a
        full SCROLL_UNIT_PX-sized unit on EVERY one of those samples
        (the previous behavior) discarded each sample's own small
        magnitude entirely and moved a full unit regardless - for a fast
        gesture firing dozens of samples per second, that compounds into
        exactly the "way too fast" jumpiness the wheel-side fix above
        doesn't otherwise touch. _accumulate_touchpad_units instead
        accumulates the FRACTIONAL scroll amount across samples (spec:
        "accumulated movement remains smooth") and only actually scrolls
        once it crosses a whole-unit threshold - a slow, careful gesture
        stays smooth and small; a fast one still adds up correctly over
        many samples, capped per-call by the same MAX_WHEEL_UNITS_PER_EVENT
        clamp _on_wheel uses. Horizontal (dx) handling is UNCHANGED - the
        spec's own complaint is specifically about vertical over-scrolling,
        and dx is an independent value with no fractional-amplification
        issue of its own (touchpad horizontal swipes were never reported
        as too fast)."""
        if dy:
            units = self._accumulate_touchpad_units(dy)
            if units:
                self.canvas.yview_scroll(units, "units")
        if dx:
            self.canvas.xview_scroll(1 if dx > 0 else -1, "units")
        debug_log.log("SCROLL", f"widget=PDFViewer.canvas event=<TouchpadScroll> dx={dx} dy={dy} "
                                 f"action=yview/xview_scroll handled=True")

    def _accumulate_touchpad_units(self, dy: int) -> int:
        self._touchpad_accum_y, units = accumulate_scroll_units(self._touchpad_accum_y, dy)
        return units

    def _on_double_click(self, event):
        x, y = self.canvas_point(event)
        zid = self._hit_test_zone(x, y)
        if zid:
            self._open_zone_popup(zid)

    def _on_right_click(self, event):
        x, y = self.canvas_point(event)
        zid = self._hit_test_zone(x, y)
        if zid:
            self._select_zone(zid)
            self.app.show_zone_context_menu(event, zid)
        else:
            # Empty canvas (no zone under the cursor) - spec: "PDF Viewer
            # right-click menu -> Auto Zone -> Paragraph/Bibliography".
            # Never touches the zone-specific menu above.
            self.app.show_empty_canvas_context_menu(event)

    # ---------------- resize handles ----------------
    def _page_size(self):
        if not self.app.pdf_document:
            return None
        return self.app.pdf_document.page_size(self.app.current_page)

    def _clamp_bbox(self, bbox):
        """Enforces the minimum zone size (MIN_ZONE_SIZE, in PDF-space units -
        so it holds at every zoom level) and keeps the bbox within the page."""
        x0, y0, x1, y1 = bbox
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        page_size = self._page_size()
        if page_size:
            pw, ph = page_size
            x0, x1 = max(0.0, min(x0, pw)), max(0.0, min(x1, pw))
            y0, y1 = max(0.0, min(y0, ph)), max(0.0, min(y1, ph))
        if x1 - x0 < MIN_ZONE_SIZE:
            x1 = x0 + MIN_ZONE_SIZE
            if page_size and x1 > page_size[0]:
                x1 = page_size[0]
                x0 = max(0.0, x1 - MIN_ZONE_SIZE)
        if y1 - y0 < MIN_ZONE_SIZE:
            y1 = y0 + MIN_ZONE_SIZE
            if page_size and y1 > page_size[1]:
                y1 = page_size[1]
                y0 = max(0.0, y1 - MIN_ZONE_SIZE)
        return [x0, y0, x1, y1]

    def _clamp_move(self, bbox):
        """Keeps a moved (not resized) zone's width/height fixed but shifts it
        back inside the page if the drag would push it off the edge."""
        x0, y0, x1, y1 = bbox
        page_size = self._page_size()
        if not page_size:
            return bbox
        pw, ph = page_size
        w, h = x1 - x0, y1 - y0
        if x0 < 0:
            x0, x1 = 0.0, w
        if x1 > pw:
            x1, x0 = pw, pw - w
        if y0 < 0:
            y0, y1 = 0.0, h
        if y1 > ph:
            y1, y0 = ph, ph - h
        return [x0, y0, x1, y1]

    def _start_resize(self, handle_name):
        if not self.selected_zone_id:
            return
        self.mode = "resizing"
        zone = self.app.zone_manager.zones[self.selected_zone_id]
        self.drag = {"handle": handle_name, "orig_bbox": list(zone.bbox)}

    def _update_resize(self, x, y):
        zone_id = self.selected_zone_id
        handle = self.drag["handle"]
        x0, y0, x1, y1 = self.pdf_to_screen(self.drag["orig_bbox"])
        if "n" in handle:
            y0 = y
        if "s" in handle:
            y1 = y
        if "w" in handle:
            x0 = x
        if "e" in handle:
            x1 = x
        clamped = self._clamp_bbox(self.screen_to_pdf((x0, y0, x1, y1)))
        x0, y0, x1, y1 = self.pdf_to_screen(clamped)
        self.canvas.coords(self.zone_items[zone_id][0], x0, y0, x1, y1)
        self.canvas.coords(self.zone_items[zone_id][1], x0 + 3, y0 + 2)
        end_text = f"[{self.app.zone_manager.zones[zone_id].tag}]"
        ex, ey, _eanchor = self._end_label_xy(x0, y0, x1, y1, end_text)
        self.canvas.coords(self.zone_items[zone_id][2], ex, ey)
        self._reposition_handles(x0, y0, x1, y1)

    def _reposition_handles(self, x0, y0, x1, y1):
        for name, (hx, hy) in self._handle_positions(x0, y0, x1, y1).items():
            if name in self.handle_items:
                s = HANDLE_SIZE / 2
                self.canvas.coords(self.handle_items[name], hx - s, hy - s, hx + s, hy + s)

    def _finish_resize(self):
        zone_id = self.selected_zone_id
        rect = self.zone_items[zone_id][0]
        coords = self.canvas.coords(rect)
        old_bbox = self.drag.get("orig_bbox")
        new_bbox = self._clamp_bbox(self.screen_to_pdf(coords))
        from core import debug_log
        debug_log.log("RESIZE", f"zone={zone_id} handle={self.drag.get('handle')}",
                       f"old_bbox={[round(v, 1) for v in old_bbox] if old_bbox else None}",
                       f"new_bbox={[round(v, 1) for v in new_bbox]}",
                       f"zoom={self.app.zoom} canvas_coord={[round(v, 1) for v in coords]} "
                       f"pdf_coord={[round(v, 1) for v in new_bbox]}")
        self.app.zone_manager.update_bbox(zone_id, new_bbox)
        self.app.on_zones_changed()

    # ---------------- horizontal / vertical split ----------------
    def start_split(self, zone_id, axis="h"):
        """Enters Split mode for the toolbar's "Horizontal Split" button -
        axis="h" (default, top/bottom, the original behavior) or "v"
        (left/right) - see toggle_split_axis for switching axis mid-mode
        without a second button. On Confirm, zone_manager.split_zone
        REPLACES the original zone's own XML output with its new split
        children (the parent is kept only as split-history metadata -
        see xml_generator._gen_zone_multi's flatten logic); works for any
        tag, not just p."""
        self.mode = "split"
        self.selected_zone_id = zone_id
        self.split_state = {"zone_id": zone_id, "axis": axis, "boundaries": [], "guide_item": None,
                             "committed_items": [], "dragging_index": None, "drag_moved": False}
        self.canvas.config(cursor="crosshair")
        self.redraw()
        self._set_split_status()

    def _set_split_status(self):
        if not self.split_state:
            return
        label = "Vertical" if self.split_state.get("axis") == "v" else "Horizontal"
        self.app.set_status(f"{label} Split: click to add a boundary, click an existing line to delete it, "
                             f"drag a line to move it, Enter to confirm, Esc to cancel, "
                             f"Ctrl+Shift+V to toggle horizontal/vertical")

    def toggle_split_axis(self):
        """Ctrl+Shift+V while Split mode is active - switches the CURRENT
        split between horizontal (top/bottom) and vertical (left/right)
        without a second toolbar button (spec: keep the existing toolbar
        exactly as-is). Any boundaries already drawn for the previous
        axis are cleared first - a Y boundary and an X boundary are not
        interchangeable, so mixing them would be meaningless."""
        if not self.split_state:
            return
        for item in self.split_state.get("committed_items", []):
            self.canvas.delete(item)
        if self.split_state.get("guide_item"):
            self.canvas.delete(self.split_state["guide_item"])
        self.split_state["axis"] = "v" if self.split_state.get("axis") == "h" else "h"
        self.split_state["boundaries"] = []
        self.split_state["committed_items"] = []
        self.split_state["guide_item"] = None
        self.split_state["dragging_index"] = None
        self.split_state["drag_moved"] = False
        self._set_split_status()

    def cancel_split(self):
        if self.split_state and self.split_state.get("guide_item"):
            self.canvas.delete(self.split_state["guide_item"])
        self.split_state = None
        self.mode = "idle"
        self.canvas.config(cursor="")
        self.redraw()

    def confirm_split(self, child_tag=None):
        if not self.split_state:
            return []
        zone_id = self.split_state["zone_id"]
        axis = self.split_state.get("axis", "h")
        boundaries = sorted(self.split_state["boundaries"])
        split_parent = self.app.zone_manager.zones.get(zone_id)
        split_page = split_parent.page if split_parent is not None else None
        created = self.app.zone_manager.split_zone(zone_id, boundaries, child_tag=child_tag, axis=axis)
        self.split_state = None
        self.mode = "idle"
        self.canvas.config(cursor="")
        # Full canonical, column-aware recompute (spec: "EPUBForge - Fix
        # Canonical Reading Order for Multi-Column, Split and Merged
        # Zones" - "When splitting a zone, first inherit the parent's
        # column identity for all children, then recalculate the complete
        # page Reading Order"). The split pieces inherit the parent's own
        # bbox region (see ZoneManager.split_zone), so column detection
        # naturally places them in the parent's own column; a PRIOR
        # revision skipped this recompute entirely (relying only on
        # split_zone's own serial-seeding) - which is exactly what this
        # spec supersedes, since seeding alone never re-derives column
        # membership if OTHER zones were added/moved since the split
        # target's own position was last correct.
        # Only the split zone's own page is recomputed (pieces never leave
        # their parent's page) - same result, without re-ordering every page.
        self.app.on_zones_changed(pages=[split_page] if split_page is not None else None)
        return created

    def clear_last_split_boundary(self):
        if self.split_state and self.split_state["boundaries"]:
            self.split_state["boundaries"].pop()
            if self.split_state.get("committed_items"):
                item = self.split_state["committed_items"].pop()
                self.canvas.delete(item)
