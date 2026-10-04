"""Side-by-side / overlay PDF viewer (spec sections 47/50 of the original
Fidelity Compare spec, extended by "ZONETOOL - ADVANCED PDF VIEWER
PERFORMANCE" and "EPUBForge - PAGE-WISE SIDE-BY-SIDE COMPARISON VIEWER").
Original PDF on the left, Converted/Generated PDF on the right, each its
own scrollable canvas. Selecting a difference (from difference_panel.py)
navigates both sides to the relevant page and highlights the affected
bounding box on each. READ-ONLY: only ever calls core.pdf_loader.
PDFDocument's existing render methods - never writes to either PDF.

Performance (spec: "ADVANCED PDF VIEWER PERFORMANCE" sections 11/12/28/37/
38): the viewer can be handed BEFORE comparison finishes (load_documents
accepts an empty/naive page_groups list and update_page_groups() upgrades
it later without resetting whatever page the user is currently looking
at - "the user must be able to start browsing before comparison
finishes" is the single most important requirement in that spec). Every
rendered PAGE (before any highlight is drawn) is cached by (document
identity, page number, dpi) in a small LRU dict - revisiting an
already-rendered page, or changing which difference is highlighted on
the SAME page, never re-rasterizes the PDF (core.fidelity_compare.
highlight_engine.draw_boxes draws onto a cached base image).

Overlay mode blends the two currently-shown pages at an adjustable
opacity (0-100%) via PIL. Side-by-side (page-level sync via an explicit
"Synchronize Pages" checkbox, using the REAL content-based page
correspondence from page_aligner - never a blind identical-offset
assumption) is the default view, matching the "PAGE-WISE SIDE-BY-SIDE"
spec's own section 1."""
import tkinter as tk
from collections import OrderedDict
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from core.fidelity_compare import highlight_engine, page_aligner
from core.pdf_loader import PDFDocument
from gui import theme
from gui.pdf_viewer import SCROLL_UNIT_PX, accumulate_scroll_units, normalize_wheel_units

BASE_DPI = 96
MIN_ZOOM, MAX_ZOOM, ZOOM_STEP = 0.5, 3.0, 1.2
# Generous enough to hold the current page + several pages of back-and-forth
# navigation history on BOTH sides at once (spec section 11: "Current page,
# +-2 nearby pages") without ever growing unbounded (spec section 25/26:
# "Never load all rendered pages into RAM simultaneously").
PAGE_CACHE_MAX = 24


class SynchronizedPdfViewer(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        palette = theme.current.palette
        self.original_doc = None
        self.converted_doc = None
        self.page_groups = []
        self._orig_page = 1
        self._conv_page = 1
        self._mode = "side_by_side"  # side_by_side / overlay / difference_only
        self._opacity = tk.DoubleVar(value=50.0)
        self._zoom = 1.0
        self._sync_pages = tk.BooleanVar(value=True)  # spec: side-by-side is the default, synchronized view
        self._orig_photo = None
        self._conv_photo = None
        self._overlay_photo = None
        # (id(doc), page_number, dpi) -> PIL.Image, the UNHIGHLIGHTED base
        # render only - see draw_boxes() in highlight_engine for why
        # highlights are drawn fresh onto a COPY every time instead of
        # being part of the cache key (spec section 38).
        self._page_cache: "OrderedDict" = OrderedDict()
        self._touchpad_accum = {"left": 0.0, "right": 0.0}

        controls = tk.Frame(self, bg=palette["app_bg"])
        controls.pack(fill=tk.X)
        self.orig_page_var = tk.StringVar(value="1")
        self.conv_page_var = tk.StringVar(value="1")
        self._build_page_nav(controls, "ORIGINAL PDF", self.orig_page_var, "original", palette)
        ttk.Separator(controls, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        self._build_page_nav(controls, "GENERATED PDF", self.conv_page_var, "converted", palette)

        mode_frame = tk.Frame(self, bg=palette["app_bg"])
        mode_frame.pack(fill=tk.X, pady=(4, 0))
        for label, mode in (("Side by Side", "side_by_side"), ("Overlay", "overlay"),
                            ("Difference Only", "difference_only")):
            tk.Button(mode_frame, text=label, command=lambda m=mode: self._set_mode(m)).pack(side=tk.LEFT, padx=2)
        tk.Checkbutton(mode_frame, text="Synchronize Pages", variable=self._sync_pages,
                        bg=palette["app_bg"], fg=palette["text"], selectcolor=palette["surface"]
                        ).pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(mode_frame, text="Opacity:", bg=palette["app_bg"], fg=palette["text"]).pack(side=tk.LEFT, padx=(12, 2))
        self.opacity_scale = tk.Scale(mode_frame, from_=0, to=100, orient=tk.HORIZONTAL, variable=self._opacity,
                                       command=lambda _v: self._render(), length=140)
        self.opacity_scale.pack(side=tk.LEFT)

        zoom_frame = tk.Frame(self, bg=palette["app_bg"])
        zoom_frame.pack(fill=tk.X, pady=(4, 0))
        tk.Button(zoom_frame, text="Zoom -", command=lambda: self._adjust_zoom(1 / ZOOM_STEP)).pack(side=tk.LEFT)
        self.zoom_label = tk.Label(zoom_frame, text="100%", bg=palette["app_bg"], fg=palette["text"], width=6)
        self.zoom_label.pack(side=tk.LEFT, padx=4)
        tk.Button(zoom_frame, text="Zoom +", command=lambda: self._adjust_zoom(ZOOM_STEP)).pack(side=tk.LEFT)
        tk.Button(zoom_frame, text="100%", command=lambda: self._set_zoom(1.0)).pack(side=tk.LEFT, padx=(8, 0))

        self._canvas_frame = tk.Frame(self, bg=palette["app_bg"])
        self._canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.left_canvas, self._left_vsb = self._build_canvas(self._canvas_frame, "left")
        self.right_canvas, self._right_vsb = self._build_canvas(self._canvas_frame, "right")

        self._current_highlight = None  # (BBox_or_None, BBox_or_None, severity)

    # ---------------- widget construction ----------------
    def _build_page_nav(self, parent, title, page_var, side, palette):
        col = tk.Frame(parent, bg=palette["app_bg"])
        col.pack(side=tk.LEFT, padx=4)
        tk.Label(col, text=title, bg=palette["app_bg"], fg=palette["text"],
                 font=theme.FONT_SMALL_BOLD).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(col, text="<", width=2, command=lambda: self._nudge_page(side, -1)).pack(side=tk.LEFT)
        entry = tk.Entry(col, textvariable=page_var, width=5, justify="center")
        entry.pack(side=tk.LEFT, padx=2)
        entry.bind("<Return>", lambda e: self._go_to_page(side, page_var.get()))
        count_label = tk.Label(col, text="/ -", bg=palette["app_bg"], fg=palette["text_muted"])
        count_label.pack(side=tk.LEFT)
        setattr(self, f"_{side}_count_label", count_label)
        tk.Button(col, text=">", width=2, command=lambda: self._nudge_page(side, 1)).pack(side=tk.LEFT, padx=(2, 0))

    def _build_canvas(self, parent, side):
        palette = theme.current.palette
        frame = tk.Frame(parent, bg=palette["app_bg"])
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(frame, bg="#808080", highlightthickness=0, yscrollincrement=SCROLL_UNIT_PX)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=canvas.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # Scrolling (spec: "PDF VIEWER PERFORMANCE" section 4 / "PAGE-WISE
        # SIDE-BY-SIDE" section 4/40 - mouse wheel, laptop touchpad,
        # scrollbar, keyboard, ALL required, independently per viewer, no
        # physical mouse required). Reuses the SAME centralized scroll-
        # speed helpers gui/pdf_viewer.py's own PDF canvas already
        # established, never a second/differently-tuned implementation.
        canvas.bind("<MouseWheel>", lambda e, c=canvas: self._on_wheel(c, e))
        canvas.bind("<Button-4>", lambda e, c=canvas: c.yview_scroll(-3, "units"))
        canvas.bind("<Button-5>", lambda e, c=canvas: c.yview_scroll(3, "units"))
        theme.bind_touchpad_scroll(canvas, lambda dx, dy, s=side: self._on_touchpad_scroll(s, dx, dy))
        # Mouse wheel / touchpad scrolling above deliberately does NOT
        # require keyboard focus at all (spec 40: "If the mouse pointer is
        # over the left viewer, scroll left viewer" - Tk already routes
        # wheel events to whatever's under the cursor, focus or not).
        # Keyboard scrolling (PageUp/Down/arrows/Home/End below) only
        # reaches whichever canvas the user actually clicked - a plain
        # tk.Canvas does NOT grab keyboard focus on click by default, so
        # that's requested explicitly here (click-to-focus, an intentional
        # user action). Deliberately NOT focus-follows-mouse-on-<Enter>:
        # that would silently steal keyboard focus away from another
        # widget (e.g. the difference list, mid F7-navigation) the instant
        # the pointer happens to rest over a viewer canvas - confirmed as a
        # real, reproducible bug during this feature's own test
        # development.
        canvas.bind("<Button-1>", lambda e, c=canvas: c.focus_set(), add="+")
        canvas.bind("<Prior>", lambda e, c=canvas: (c.yview_scroll(-1, "pages"), "break")[1])
        canvas.bind("<Next>", lambda e, c=canvas: (c.yview_scroll(1, "pages"), "break")[1])
        canvas.bind("<Up>", lambda e, c=canvas: (c.yview_scroll(-2, "units"), "break")[1])
        canvas.bind("<Down>", lambda e, c=canvas: (c.yview_scroll(2, "units"), "break")[1])
        canvas.bind("<Home>", lambda e, c=canvas: (c.yview_moveto(0), "break")[1])
        canvas.bind("<End>", lambda e, c=canvas: (c.yview_moveto(1), "break")[1])
        # Ctrl+PageUp/PageDown = turn to the actual previous/next PAGE
        # (spec section 41 - distinct from plain PageUp/PageDown, which
        # only scroll within the current page image).
        canvas.bind("<Control-Prior>", lambda e, s=side: (self._nudge_page(s, -1), "break")[1])
        canvas.bind("<Control-Next>", lambda e, s=side: (self._nudge_page(s, 1), "break")[1])
        canvas.bind("<Control-plus>", lambda e: (self._adjust_zoom(ZOOM_STEP), "break")[1])
        canvas.bind("<Control-minus>", lambda e: (self._adjust_zoom(1 / ZOOM_STEP), "break")[1])
        canvas.bind("<Control-0>", lambda e: (self._set_zoom(1.0), "break")[1])
        return canvas, vsb

    # ---------------- scrolling ----------------
    def _on_wheel(self, canvas, event):
        units = normalize_wheel_units(event.delta)
        if units:
            canvas.yview_scroll(units, "units")

    def _on_touchpad_scroll(self, side, dx, dy):
        canvas = self.left_canvas if side == "left" else self.right_canvas
        if dy:
            self._touchpad_accum[side], units = accumulate_scroll_units(self._touchpad_accum[side], dy)
            if units:
                canvas.yview_scroll(units, "units")
        if dx:
            canvas.xview_scroll(1 if dx > 0 else -1, "units")

    # ---------------- zoom ----------------
    def _dpi(self) -> int:
        return max(30, int(BASE_DPI * self._zoom))

    def _adjust_zoom(self, factor):
        self._set_zoom(self._zoom * factor)

    def _set_zoom(self, value):
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, value))
        self.zoom_label.configure(text=f"{round(self._zoom * 100)}%")
        self._render()

    # ---------------- page cache (spec sections 11/12/28/37/38) ----------------
    def _get_base_image(self, doc, page_num: int) -> Image.Image:
        dpi = self._dpi()
        key = (id(doc), page_num, dpi)
        cached = self._page_cache.get(key)
        if cached is not None:
            self._page_cache.move_to_end(key)
            return cached
        img = doc.render_page_image(page_num, dpi=dpi).convert("RGB")
        self._page_cache[key] = img
        if len(self._page_cache) > PAGE_CACHE_MAX:
            self._page_cache.popitem(last=False)
        return img

    # ---------------- document loading ----------------
    def load_documents(self, original_pdf_path: str, converted_pdf_path: str, page_groups: list = None):
        """Spec: "ADVANCED PDF VIEWER PERFORMANCE" section 32 - "The user
        can start browsing before comparison finishes" - the caller may
        (and, per that spec, SHOULD) call this the moment both PDF paths
        are known, passing page_groups=None/[] before any comparison has
        actually run; update_page_groups() below upgrades the mapping
        later without disturbing whatever the user is already looking at."""
        if self.original_doc is not None:
            self.original_doc.close()
        if self.converted_doc is not None:
            self.converted_doc.close()
        self.original_doc = PDFDocument(original_pdf_path)
        self.converted_doc = PDFDocument(converted_pdf_path)
        self.page_groups = page_groups or []
        self._page_cache.clear()
        self._orig_page = 1
        self._conv_page = 1
        self._current_highlight = None
        self._render()

    def update_page_groups(self, page_groups: list):
        """Called once the background comparison finishes and a real,
        content-based page correspondence is available - never resets
        _orig_page/_conv_page or clears the render cache, since neither
        document nor zoom/page changed."""
        self.page_groups = page_groups or []

    # ---------------- navigation ----------------
    def _go_to_page(self, side, raw_value):
        """Spec: "PAGE-WISE SIDE-BY-SIDE" section 2/19 - "[Go to Page]"
        for each PDF, never requiring the user to click Next N times."""
        try:
            page = int(raw_value)
        except (TypeError, ValueError):
            return
        doc = self.original_doc if side == "original" else self.converted_doc
        if not doc:
            return
        page = max(1, min(doc.page_count, page))
        self._set_page(side, page)

    def _nudge_page(self, side, delta):
        doc = self.original_doc if side == "original" else self.converted_doc
        if not doc:
            return
        current = self._orig_page if side == "original" else self._conv_page
        self._set_page(side, max(1, min(doc.page_count, current + delta)))

    def _set_page(self, side, page):
        if side == "original":
            self._orig_page = page
        else:
            self._conv_page = page
        self._current_highlight = None
        if self._sync_pages.get():
            self._sync_other_side(side)
        self._render()

    def _sync_other_side(self, moved_side):
        """Content-based synchronized navigation (spec: "PAGE-WISE
        SIDE-BY-SIDE" section 5/6 - "because page sizes and pagination may
        differ, synchronization must use CONTENT POSITION rather than
        blindly using identical scroll offsets"). Uses the SAME page_
        aligner.page_number_for() mapping show_difference() already
        relies on - never a second, separately-invented correspondence
        mechanism. A page with no known mapping (still-running/naive
        comparison) is left alone rather than guessed at."""
        if not self.page_groups:
            return
        if moved_side == "original":
            mapped = page_aligner.page_number_for(self.page_groups, original_page=self._orig_page)
            if mapped and self.converted_doc:
                self._conv_page = max(1, min(self.converted_doc.page_count, mapped[0]))
        else:
            mapped = page_aligner.page_number_for(self.page_groups, converted_page=self._conv_page)
            if mapped and self.original_doc:
                self._orig_page = max(1, min(self.original_doc.page_count, mapped[0]))

    def _set_mode(self, mode):
        self._mode = mode
        self._render()

    def show_difference(self, diff):
        """Navigates both sides to the difference's own page(s) and
        highlights its bounding box(es) - spec section 47/49/73."""
        if diff.original_page and self.original_doc:
            self._orig_page = diff.original_page
        if diff.converted_page and self.converted_doc:
            self._conv_page = diff.converted_page
        elif diff.original_page and self.page_groups and self.converted_doc:
            mapped = page_aligner.page_number_for(self.page_groups, original_page=diff.original_page)
            if mapped:
                self._conv_page = mapped[0]
        self._current_highlight = (diff.original_bbox, diff.converted_bbox, diff.severity)
        self._render()

    def _boxes_for(self, side_bbox):
        if side_bbox is None:
            return []
        from core.fidelity_compare.document_model import BBox
        sev = self._current_highlight[2] if self._current_highlight else "MEDIUM"
        return [(BBox(side_bbox.x0, side_bbox.y0, side_bbox.x1, side_bbox.y1), sev)]

    @staticmethod
    def _qa_color(severity):
        """
        QA visual convention:
          GREEN  = confirmed match / informational
          RED    = missing, added, mismatch, error, fatal
          YELLOW = modified / changed / review
        The existing Difference record is the source of truth; this only
        controls the visual overlay color.
        """
        s = str(severity or "").upper()
        if any(k in s for k in ("MISSING", "ERROR", "FATAL", "UNMATCH", "ADDED", "EXTRA")):
            return (220, 38, 38, 105), (185, 28, 28, 255)
        if any(k in s for k in ("MODIF", "CHANGED", "REVIEW", "MEDIUM", "WARNING")):
            return (245, 158, 11, 105), (194, 120, 0, 255)
        return (34, 197, 94, 95), (22, 140, 60, 255)

    def _draw_qa_boxes(self, image, boxes, dpi):
        if not boxes:
            return image
        out = image.convert("RGBA").copy()
        draw = ImageDraw.Draw(out, "RGBA")
        scale = dpi / 72.0
        for bbox, severity in boxes:
            x0 = bbox.x0 * scale
            y0 = bbox.y0 * scale
            x1 = bbox.x1 * scale
            y1 = bbox.y1 * scale
            fill, outline = self._qa_color(severity)
            draw.rectangle((x0, y0, x1, y1), fill=fill, outline=outline, width=max(2, int(2 * scale / 1.5)))
        return out.convert("RGB")

    # ---------------- rendering ----------------
    def _render(self):
        orig_total = self.original_doc.page_count if self.original_doc else None
        conv_total = self.converted_doc.page_count if self.converted_doc else None
        self.orig_page_var.set(str(self._orig_page if self.original_doc else 1))
        self.conv_page_var.set(str(self._conv_page if self.converted_doc else 1))
        self._original_count_label.configure(text=f"/ {orig_total}" if orig_total else "/ -")
        self._converted_count_label.configure(text=f"/ {conv_total}" if conv_total else "/ -")
        if not self.original_doc or not self.converted_doc:
            return
        dpi = self._dpi()
        orig_boxes = self._boxes_for(self._current_highlight[0]) if self._current_highlight else []
        conv_boxes = self._boxes_for(self._current_highlight[1]) if self._current_highlight else []
        orig_base = self._get_base_image(self.original_doc, self._orig_page)
        conv_base = self._get_base_image(self.converted_doc, self._conv_page)
        orig_img = self._draw_qa_boxes(orig_base, orig_boxes, dpi) if orig_boxes else orig_base
        conv_img = self._draw_qa_boxes(conv_base, conv_boxes, dpi) if conv_boxes else conv_base

        if self._mode == "side_by_side":
            self.right_canvas.master.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._draw(self.left_canvas, orig_img, "_orig_photo")
            self._draw(self.right_canvas, conv_img, "_conv_photo")
        else:
            self.right_canvas.master.pack_forget()
            if self._mode == "overlay":
                blended = self._blend(orig_img, conv_img, self._opacity.get() / 100.0)
                self._draw(self.left_canvas, blended, "_overlay_photo")
            else:  # difference_only - show only the converted side's highlighted regions
                self._draw(self.left_canvas, conv_img, "_overlay_photo")

    def _blend(self, img_a: Image.Image, img_b: Image.Image, alpha: float) -> Image.Image:
        size = (max(img_a.width, img_b.width), max(img_a.height, img_b.height))
        a = img_a.resize(size)
        b = img_b.resize(size)
        return Image.blend(a.convert("RGB"), b.convert("RGB"), alpha)

    def _draw(self, canvas: tk.Canvas, img: Image.Image, attr_name: str):
        photo = ImageTk.PhotoImage(img)
        setattr(self, attr_name, photo)  # keep a reference - Tk drops the image otherwise
        canvas.delete("all")
        canvas.configure(scrollregion=(0, 0, img.width, img.height))
        canvas.create_image(0, 0, anchor="nw", image=photo)
