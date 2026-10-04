"""Viewers of the PDF <-> XHTML QC workspace. All of them read the SAME
unified mapping (core.qc.mapping.DocumentMapping); none computes its own.

    PdfView        - rendered PDF page, word / image / page-number overlays
                     coloured by mapping state, click-to-map
    RenderedView   - XHTML rendered as rich text (headings, paragraphs,
                     inline images, page-marker badges, MISSING notes),
                     words coloured by mapping state, click-to-map
    SourceView     - the XHTML source of the current split (editable), line
                     highlighting, click-to-map
    OverlayView    - PDF page image blended with the XHTML rendering of the
                     same page (opacity / blink / difference / edges)
Only the current page / split is ever rendered (lazy - large books)."""
import io
import os
import tkinter as tk
from bisect import bisect_right
from tkinter import ttk

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageTk

from core.qc.mapping import MATCH, MISSING, EXTRA, MODIFIED, MOVED, DUPLICATE, UNCERTAIN, STATE_LABELS

DEFAULT_COLORS = {MATCH: "#2E7D32", MISSING: "#D32F2F", EXTRA: "#D32F2F", MODIFIED: "#F9A825", MOVED: "#1565C0",
                  DUPLICATE: "#EF6C00", UNCERTAIN: "#7B1FA2"}
LIGHT = {MATCH: "#E3F3E4", MISSING: "#FBD5D5", EXTRA: "#FBD5D5", MODIFIED: "#FFF2B3", MOVED: "#D6E6FA",
         DUPLICATE: "#FFE0C2", UNCERTAIN: "#EBD9F2"}


def lighten(hex_color, amount=0.82):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (int(c + (255 - c) * amount) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


# =================================================================== PDF
class PdfView(tk.Frame):
    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        self.page = 1
        self.zoom = 1.3
        self.fit = None          # None | "width" | "page"
        self._img = None
        self._scale = 1.0
        self.focus_bboxes = []
        bar = tk.Frame(self)
        bar.pack(fill=tk.X)
        self.title = tk.Label(bar, text="PDF", font=("Segoe UI", 9, "bold"), anchor="w")
        self.title.pack(side=tk.LEFT, padx=4)
        self.canvas = tk.Canvas(self, bg="#7f8792", highlightthickness=0)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        xs.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Configure>", lambda e: self.fit and self.render())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)

    # ------------------------------------------------------------ render
    def show(self, page, focus_bboxes=None):
        s = self.ws.session
        if s is None or s.pdf is None:
            return
        self.page = max(1, min(page, len(s.pdf.pages)))
        self.focus_bboxes = list(focus_bboxes or [])
        self.render()

    def render(self):
        s = self.ws.session
        if s is None or s.pdf is None:
            return
        pg = s.pdf.page(self.page)
        cw, ch = max(200, self.canvas.winfo_width()), max(200, self.canvas.winfo_height())
        if self.fit == "width":
            self.zoom = (cw - 20) / pg.width
        elif self.fit == "page":
            self.zoom = min((cw - 20) / pg.width, (ch - 20) / pg.height)
        img = s.pdf.render(self.page, self.zoom)
        self._scale = self.zoom
        self._img = ImageTk.PhotoImage(img)
        c = self.canvas
        c.delete("all")
        c.create_image(10, 10, image=self._img, anchor="nw")
        c.configure(scrollregion=(0, 0, img.width + 20, img.height + 20))
        self._overlays(pg)
        m = s.mapping
        pm = m.pages[self.page - 1] if m else None
        printed = f"   printed page {pg.printed}" if pg.printed else "   (unnumbered)"
        conf = f"   mapping {pm.confidence:.0%}" if pm else ""
        self.title.config(text=f"PDF page {self.page}/{len(s.pdf.pages)}{printed}{conf}")
        if self.focus_bboxes:
            self._scroll_to(self.focus_bboxes[0])

    def to_canvas(self, bbox):
        z = self._scale
        return (10 + bbox[0] * z, 10 + bbox[1] * z, 10 + bbox[2] * z, 10 + bbox[3] * z)

    def _overlays(self, pg):
        s, ws = self.ws.session, self.ws
        m = s.mapping
        c = self.canvas
        if m is None:
            return
        colors = ws.colors
        if ws.show_highlights.get():
            for i in range(pg.word_start, pg.word_end):
                state = m.p_state[i]
                if state == MATCH and not ws.show_matched.get():
                    continue
                w = s.pdf.words[i]
                for bb in [w.bbox] + list(w.extra_bboxes):
                    x0, y0, x1, y1 = self.to_canvas(bb)
                    if state == MATCH:
                        c.create_line(x0, y1 + 1, x1, y1 + 1, fill=colors[MATCH], width=2)
                    else:
                        c.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1, outline=colors[state], width=2,
                                           fill=colors[state], stipple="gray25")
        # images
        states = dict(m.pages[self.page - 1].images)
        for img in pg.images:
            state = states.get(img.uid, UNCERTAIN)
            x0, y0, x1, y1 = self.to_canvas(img.bbox)
            c.create_rectangle(x0, y0, x1, y1, outline=colors[state], width=4, tags=("img", img.uid))
            c.create_text(x0 + 4, y0 + 4, anchor="nw", text=f"IMAGE: {STATE_LABELS[state]}",
                          fill=colors[state], font=("Segoe UI", 9, "bold"))
        # printed page number
        pm = m.pages[self.page - 1]
        if pg.printed:
            state = {MATCH: MATCH, MISSING: MISSING, MODIFIED: MODIFIED}.get(pm.marker_state, UNCERTAIN)
            txt = {MATCH: f"Page {pg.printed}: marker matched", MISSING: f"MISSING PAGE MARKER: {pg.printed}",
                   MODIFIED: f"WRONG/MISPLACED PAGE MARKER: {pg.printed}"}.get(pm.marker_state,
                                                                           f"Page {pg.printed}")
            if pg.printed_bbox:
                x0, y0, x1, y1 = self.to_canvas(pg.printed_bbox)
                c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline=colors[state], width=3)
                c.create_text(x1 + 8, (y0 + y1) / 2, anchor="w", text=txt, fill=colors[state],
                              font=("Segoe UI", 9, "bold"))
            else:
                c.create_text(14, 14, anchor="nw", text=txt + " (inferred)", fill=colors[state],
                              font=("Segoe UI", 9, "bold"))
        for bb in self.focus_bboxes:
            if bb:
                x0, y0, x1, y1 = self.to_canvas(bb)
                c.create_rectangle(x0 - 4, y0 - 4, x1 + 4, y1 + 4, outline="#000000", width=3, dash=(4, 2))

    def _scroll_to(self, bbox):
        x0, y0, x1, y1 = self.to_canvas(bbox)
        sr = self.canvas.bbox("all")
        if not sr:
            return
        h = sr[3] - sr[1]
        vh = self.canvas.winfo_height()
        self.canvas.yview_moveto(max(0.0, (y0 - vh / 3) / max(h, 1)))

    def _wheel(self, event):
        delta = -1 if (getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0) else 1
        first, last = self.canvas.yview()
        if delta > 0 and last >= 0.999 and self.ws.sync_scroll.get():
            self.ws.goto_pdf_page(self.page + 1)
            self.canvas.yview_moveto(0)
            return
        if delta < 0 and first <= 0.001 and self.ws.sync_scroll.get():
            self.ws.goto_pdf_page(self.page - 1)
            self.canvas.yview_moveto(1)
            return
        self.canvas.yview_scroll(delta * 3, "units")

    def _click(self, event):
        s = self.ws.session
        if s is None or s.mapping is None:
            return
        x = (self.canvas.canvasx(event.x) - 10) / self._scale
        y = (self.canvas.canvasy(event.y) - 10) / self._scale
        pg = s.pdf.page(self.page)
        for img in pg.images:
            if img.bbox[0] <= x <= img.bbox[2] and img.bbox[1] <= y <= img.bbox[3]:
                self.ws.show_pdf_image(img)
                return
        split, j = s.mapping.location_for_pdf(self.page, x, y)
        self.ws.goto_xhtml_word(j, from_pdf=True)


# ============================================================= rendered
class RenderedView(tk.Frame):
    """XHTML rendered as rich text, one split at a time."""

    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        self.split = None
        self._offsets = []       # sorted absolute char offsets of words
        self._word_at = []       # word index for each offset
        self._images = []
        self._pos = [0]
        self.text = tk.Text(self, wrap="word", font=("Georgia", 11), padx=16, pady=10, cursor="arrow",
                            spacing1=2, spacing3=6)
        ys = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.text.configure(yscrollcommand=lambda a, b: (ys.set(a, b), self._on_scroll()))
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.bind("<Button-1>", self._click)
        self.text.bind("<Key>", lambda e: "break" if e.keysym not in ("Up", "Down", "Prior", "Next", "Home", "End")
                       and not (e.state & 0x4) else None)
        self._scroll_job = None
        self._suspend_sync = False

    def _yview(self, *args):
        self.text.yview(*args)

    def configure_tags(self):
        t = self.text
        colors = self.ws.colors
        t.tag_configure("h1", font=("Georgia", 18, "bold"), spacing1=12, spacing3=8)
        t.tag_configure("h2", font=("Georgia", 15, "bold"), spacing1=10, spacing3=6)
        t.tag_configure("h3", font=("Georgia", 13, "bold"), spacing1=8, spacing3=4)
        t.tag_configure("caption", font=("Georgia", 10, "italic"), lmargin1=30, lmargin2=30)
        t.tag_configure("quote", lmargin1=40, lmargin2=40, rmargin=40)
        t.tag_configure("li", lmargin1=24, lmargin2=36)
        for st, col in colors.items():
            show = self.ws.show_highlights.get() and (st != MATCH or self.ws.show_matched.get())
            t.tag_configure(f"st_{st}", background=(lighten(col) if show else ""))
        for st, col in colors.items():
            t.tag_configure(f"badge_{st}", foreground="white", background=col, font=("Segoe UI", 8, "bold"))
        t.tag_configure("missing_note", foreground=colors[MISSING], font=("Segoe UI", 8, "bold"),
                        background=lighten(colors[MISSING], 0.9))
        t.tag_configure("focus", borderwidth=2, relief="solid", background="#FFF59D")
        t.tag_raise("focus")
        t.tag_configure("img_label", foreground="#555", font=("Segoe UI", 8))

    def show_split(self, split, force=False):
        if split == self.split and not force:
            return
        s = self.ws.session
        self.split = split
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._offsets, self._word_at, self._images = [], [], []
        self.configure_tags()
        if s is None or split is None:
            return
        x, m = s.xhtml, s.mapping
        w0, w1, b0, b1 = x.split_ranges.get(split, (0, 0, 0, 0))
        markers = [(mk.word_pos, k, mk) for k, mk in enumerate(x.markers) if mk.split == split]
        images = [(im.word_pos, k, im) for k, im in enumerate(x.images) if im.split == split]
        missing = []
        if m is not None:
            missing = [(d.x_start, d) for d in m.differences if d.kind == "MISSING_TEXT" and d.split == split]
        events = sorted([(p, 0, k, "marker", o) for p, k, o in markers] +
                        [(p, 1, k, "image", o) for p, k, o in images] +
                        [(p, 2, id(o), "missing", o) for p, o in missing], key=lambda e: (e[0], e[1]))
        ev_i = 0
        pos = self._pos = [0]

        def put(text, tags=()):
            t.insert("end", text, tags)
            pos[0] += len(text)

        def flush_events(upto):
            nonlocal ev_i
            while ev_i < len(events) and events[ev_i][0] <= upto:
                _p, _o, k, kind, obj = events[ev_i]
                ev_i += 1
                if kind == "marker":
                    st = (m.marker_state.get(k) if m else None) or UNCERTAIN
                    put(f" ⎘ page {obj.label} ", (f"badge_{st}", f"marker_{k}"))
                    put(" ")
                elif kind == "image":
                    self._insert_image(obj, k, put)
                else:
                    put(f" ⚠ MISSING FROM XHTML: “{obj.pdf_text[:120]}” ", ("missing_note",))
                    put(" ")
        for blk in x.blocks[b0:b1]:
            if blk.word_end <= blk.word_start:
                continue
            flush_events(blk.word_start - 1)
            tag = blk.tag if blk.tag in ("h1", "h2", "h3") else ("h3" if blk.tag in ("h4", "h5", "h6") else
                  "caption" if blk.tag in ("figcaption", "caption") else "quote" if blk.tag == "blockquote" else
                  "li" if blk.tag in ("li", "dd", "dt") else "")
            block_tags = (tag,) if tag else ()
            for j in range(blk.word_start, blk.word_end):
                flush_events(j)
                w = x.words[j]
                st = m.x_state[j] if m else MATCH
                self._offsets.append(pos[0])
                self._word_at.append(j)
                put(w.text, block_tags + (f"st_{st}",))
                put(" ", block_tags)
            put("\n", block_tags)
        flush_events(10 ** 12)
        t.configure(state="disabled")

    def _insert_image(self, im, k, put):
        s = self.ws.session
        m = s.mapping
        st = UNCERTAIN
        if m:
            for pair in m.image_pairs:
                if pair.get("xhtml_index") == k:
                    st = pair["state"]
        try:
            pil = Image.open(s.mgr.abspath(im.file)) if im.exists else None
        except Exception:
            pil = None
        put("\n")
        if pil is not None:
            pil = ImageOps.contain(pil.convert("RGB"), (260, 200))
            pil = ImageOps.expand(pil, border=4, fill=self.ws.colors[st])
            photo = ImageTk.PhotoImage(pil)
            self._images.append(photo)
            self.text.image_create("end", image=photo, padx=6, pady=4)
            self.text.tag_add(f"image_{k}", "end-2c", "end-1c")
            self._pos[0] += 1    # the embedded image occupies one index
        put(f"  [image {os.path.basename(im.src)} - {STATE_LABELS[st]}]\n", ("img_label", f"image_{k}"))
        self.text.tag_bind(f"image_{k}", "<Button-1>", lambda e, kk=k: self.ws.show_xhtml_image(kk))

    def index_of_word(self, j):
        if j in self._word_at:
            off = self._offsets[self._word_at.index(j)]
            return f"1.0+{off}c"
        return None

    def word_at_index(self, index):
        off = int(self.text.count("1.0", index, "chars")[0]) if self.text.count("1.0", index, "chars") else 0
        k = bisect_right(self._offsets, off) - 1
        return self._word_at[k] if 0 <= k < len(self._word_at) else None

    def focus_words(self, j1, j2=None):
        t = self.text
        t.tag_remove("focus", "1.0", "end")
        a = self.index_of_word(j1)
        if a is None:
            return
        b = self.index_of_word(j2 - 1) if j2 and j2 > j1 else None
        end = f"{b} wordend" if b else f"{a} wordend"
        t.tag_add("focus", a, end)
        self._suspend_sync = True
        t.see(a)
        self.after(300, lambda: setattr(self, "_suspend_sync", False))

    def top_word(self):
        try:
            return self.word_at_index(self.text.index("@0,0"))
        except tk.TclError:
            return None

    def _on_scroll(self):
        if self._suspend_sync or not self.ws.sync_scroll.get():
            return
        if self._scroll_job:
            self.after_cancel(self._scroll_job)
        self._scroll_job = self.after(250, self._sync_from_scroll)

    def _sync_from_scroll(self):
        self._scroll_job = None
        j = self.top_word()
        if j is not None:
            self.ws.sync_pdf_to_word(j)

    def _click(self, event):
        idx = self.text.index(f"@{event.x},{event.y}")
        for tag in self.text.tag_names(idx):
            if tag.startswith("marker_"):
                self.ws.show_marker(int(tag.split("_")[1]))
                return "break"
        j = self.word_at_index(idx)
        if j is not None and j >= 0:
            self.ws.goto_pdf_from_word(j)
        return "break"


# =============================================================== source
class SourceView(tk.Frame):
    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        self.split = None
        bar = tk.Frame(self)
        bar.pack(fill=tk.X)
        self.label = tk.Label(bar, text="", anchor="w", font=("Segoe UI", 8))
        self.label.pack(side=tk.LEFT, padx=4)
        tk.Button(bar, text="Save source edit", command=self._save).pack(side=tk.RIGHT, padx=2)
        tk.Button(bar, text="Revert", command=lambda: self.show_split(self.split, force=True)).pack(side=tk.RIGHT)
        self.text = tk.Text(self, wrap="none", font=("Consolas", 9), undo=True)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        xs.pack(side=tk.BOTTOM, fill=tk.X)
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.tag_configure("line", background="#FFF59D")
        self.text.bind("<ButtonRelease-1>", self._click)

    def show_split(self, split, force=False):
        if split == self.split and not force:
            return
        self.split = split
        self.text.delete("1.0", "end")
        s = self.ws.session
        if s is None or not split:
            return
        try:
            with open(s.mgr.abspath(split), "r", encoding="utf-8") as f:
                self.text.insert("1.0", f.read())
        except OSError as e:
            self.text.insert("1.0", str(e))
        self.text.edit_reset()
        self.label.config(text=split)

    def show_line(self, line):
        self.text.tag_remove("line", "1.0", "end")
        if not line:
            return
        self.text.tag_add("line", f"{line}.0", f"{line}.end")
        self.text.see(f"{line}.0")

    def _click(self, _event):
        line = int(self.text.index("insert").split(".")[0])
        self.ws.goto_from_source_line(self.split, line)

    def _save(self):
        s = self.ws.session
        if s is None or not self.split:
            return
        data = self.text.get("1.0", "end-1c")
        from lxml import etree
        try:
            etree.fromstring(data.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            from tkinter import messagebox
            messagebox.showerror("Source edit", f"Not well-formed XML - not saved:\n{e}", parent=self)
            return
        rel = self.split

        def write(mgr):
            with open(mgr.abspath(rel), "w", encoding="utf-8") as f:
                f.write(data)
            mgr._docs.pop(rel, None)
            return []
        s.mgr.edit(f"Edit source of {os.path.basename(rel)}", write, [rel])
        self.ws.after_change(f"Saved source edit of {os.path.basename(rel)}", reanalyse=True)


# ============================================================== overlay
class OverlayView(tk.Frame):
    """PDF page image vs the XHTML rendering of the same content."""

    def __init__(self, parent, ws):
        super().__init__(parent)
        self.ws = ws
        bar = tk.Frame(self)
        bar.pack(fill=tk.X)
        self.mode = tk.StringVar(value="blend")
        for val, lbl in (("blend", "Opacity"), ("blink", "Blink"), ("diff", "Difference"), ("edges", "Edges")):
            ttk.Radiobutton(bar, text=lbl, value=val, variable=self.mode, command=self.render).pack(side=tk.LEFT)
        tk.Label(bar, text="  PDF").pack(side=tk.LEFT)
        self.alpha = tk.Scale(bar, from_=0, to=100, orient="horizontal", length=160, showvalue=True,
                              command=lambda v: self.render())
        self.alpha.set(50)
        self.alpha.pack(side=tk.LEFT)
        tk.Label(bar, text="XHTML").pack(side=tk.LEFT)
        self.info = tk.Label(bar, text="", anchor="e", font=("Segoe UI", 8))
        self.info.pack(side=tk.RIGHT, padx=4)
        self.canvas = tk.Canvas(self, bg="#7f8792", highlightthickness=0)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=ys.set)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._photo = None
        self._blink_job = None
        self._blink_state = False
        self._cache = {}

    def xhtml_page_image(self, page_no, size):
        """MuPDF's own HTML layout engine (fitz.Story) renders the XHTML
        blocks mapped to this PDF page at the PDF page size."""
        key = (page_no, size, self.ws.session.mgr.root, len(self.ws.session.differences))
        if key in self._cache:
            return self._cache[key]
        import fitz
        from lxml import etree
        s = self.ws.session
        m = s.mapping
        pm = m.pages[page_no - 1]
        x = s.xhtml
        if pm.x_start is None:
            return None
        blocks = []
        seen = set()
        for blk in x.blocks:
            if blk.word_end <= blk.word_start or blk.word_end <= pm.x_start or blk.word_start >= (pm.x_end or 0):
                continue
            if blk.tag in ("body", "section", "div", "article"):
                continue
            el = s.mgr.doc(blk.split).xpath(blk.xpath)
            if not el or blk.xpath in seen:
                continue
            seen.add(blk.xpath)
            blocks.append(etree.tostring(el[0], encoding="unicode"))
        for im in x.images:
            if pm.x_start <= im.word_pos <= (pm.x_end or 0):
                blocks.insert(0, f'<p><img src="{s.mgr.abspath(im.file)}" style="max-width:100%"/></p>')
        html = "<html><body>" + "".join(blocks) + "</body></html>"
        pg = s.pdf.page(page_no)
        story = fitz.Story(html=html, user_css="body{font-family:serif;font-size:10pt} p{margin:0 0 4pt 0}")
        rect = fitz.Rect(0, 0, pg.width, pg.height)
        out = io.BytesIO()
        w = fitz.DocumentWriter(out)
        dev = w.begin_page(rect)
        story.place(rect + (54, 60, -54, -40))
        story.draw(dev)
        w.end_page()
        w.close()
        doc = fitz.open("pdf", out.getvalue())
        z = size[0] / pg.width
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self._cache[key] = img
        return img

    def render(self):
        s = self.ws.session
        if s is None or s.mapping is None:
            return
        page = self.ws.pdf_view.page
        zoom = 1.3
        pdf_img = s.pdf.render(page, zoom)
        try:
            x_img = self.xhtml_page_image(page, pdf_img.size)
        except Exception as e:  # noqa: BLE001
            self.info.config(text=f"XHTML rendering failed: {e}")
            x_img = None
        if x_img is None:
            x_img = Image.new("RGB", pdf_img.size, "white")
        x_img = x_img.resize(pdf_img.size)
        mode = self.mode.get()
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None
        if mode == "blend":
            out = Image.blend(pdf_img, x_img, 1 - self.alpha.get() / 100.0)
            self.info.config(text=f"page {page}: {self.alpha.get()}% PDF / {100 - self.alpha.get()}% XHTML")
        elif mode == "diff":
            out = ImageOps.invert(ImageChops.difference(pdf_img, x_img))
            self.info.config(text="dark = different pixels")
        elif mode == "edges":
            pe = ImageOps.grayscale(pdf_img).filter(ImageFilter.FIND_EDGES)
            xe = ImageOps.grayscale(x_img).filter(ImageFilter.FIND_EDGES)
            out = Image.merge("RGB", (ImageOps.invert(xe), ImageOps.invert(Image.blend(pe, xe, 0.5)),
                                      ImageOps.invert(pe)))
            self.info.config(text="red = PDF only, blue = XHTML only, dark = both")
        else:
            self._blink_state = not self._blink_state
            out = pdf_img if self._blink_state else x_img
            self.info.config(text=f"BLINK - showing {'PDF' if self._blink_state else 'XHTML'}")
            self._blink_job = self.after(600, self.render)
        self._photo = ImageTk.PhotoImage(out)
        self.canvas.delete("all")
        self.canvas.create_image(10, 10, image=self._photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, out.width + 20, out.height + 20))

    def stop(self):
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None
