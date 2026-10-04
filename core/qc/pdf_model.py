"""PDF side of the unified QC mapping model.

Per physical page (cached on disk per page, keyed by PDF fingerprint):
  * body words in READING ORDER with their boxes - reusing the Zoning
    module's own layout analysis (auto_zoning.layout_engine glyph lines,
    core.column_detector bands/columns, paragraph_auto_zone paragraphs) so
    QC reads a page exactly the way zoning does; line-end hyphenation is
    joined for comparison while both word boxes are kept for highlighting;
  * running headers / footers and the printed page number (header or
    footer, decorated forms such as "- 12 -" or "Page 12", Arabic or Roman),
    resolved document-wide with a numbering model (offset per numbering
    system, front-matter Roman -> body Arabic transitions, unnumbered pages
    inferred from their neighbours);
  * images: placement box, pixel size, aspect, SHA-1 of the image data,
    64-bit average + difference perceptual hashes, caption block and the
    reading-order position (number of body words before it).
Scanned pages without a text layer use the Zoning module's cached OCR when
available.
"""
import hashlib
import io
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field

from core.qc import textnorm

CACHE_VERSION = "qc-pdf-2"
BAND = 0.085               # header / footer band (fraction of page height)
_ARABIC_RE = re.compile(r"^\d{1,4}$")
_ROMAN_RE = re.compile(r"^[ivxlcdm]{1,8}$", re.IGNORECASE)
_DECOR_RE = re.compile(r"^(?:page|p\.|pg\.?|seite|página|pagina)?\s*[-–—~·•|\[(]*\s*([0-9]{1,4}|[ivxlcdmIVXLCDM]{1,8})"
                       r"\s*[-–—~·•|\])]*$", re.IGNORECASE)
_CAPTION_RE = re.compile(r"^\s*([A-Za-zÀ-ɏ]{2,}\.?)\s*(\d+[A-Za-z]?([.\-:]\d+)*|[IVXLC]+)\b")


@dataclass
class PdfWord:
    text: str
    page: int
    bbox: tuple
    para: int = 0                  # global paragraph number
    line: int = 0                  # global line number
    extra_bboxes: list = field(default_factory=list)   # second half of a hyphen-joined word

    @property
    def key(self):
        return textnorm.key(self.text)


@dataclass
class PdfImage:
    page: int
    index: int                     # per page
    bbox: tuple
    width: int = 0
    height: int = 0
    sha1: str = ""
    ahash: int = 0
    dhash: int = 0
    thumb: str = ""
    caption: str = ""
    caption_bbox: tuple = None
    word_pos: int = 0              # global body-word index the image precedes

    @property
    def aspect(self):
        w, h = self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]
        return (w / h) if h else 0.0

    @property
    def uid(self):
        return f"p{self.page}-img{self.index}"


@dataclass
class PdfPage:
    number: int
    width: float
    height: float
    word_start: int = 0
    word_end: int = 0
    printed: str = None            # printed page label as found / inferred
    printed_found: str = None      # label literally found on the page (None = inferred / none)
    printed_confidence: float = 0.0
    printed_bbox: tuple = None
    running_text: list = field(default_factory=list)
    has_text_layer: bool = True
    images: list = field(default_factory=list)   # PdfImage


def pdf_fingerprint(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def image_hashes(pil_image):
    """(average hash, difference hash) - 64-bit perceptual hashes."""
    g = pil_image.convert("L")
    a = g.resize((8, 8))
    px = list(a.get_flattened_data() if hasattr(a, "get_flattened_data") else a.getdata())
    avg = sum(px) / 64.0
    ah = 0
    for i, v in enumerate(px):
        if v >= avg:
            ah |= 1 << i
    d = g.resize((9, 8))
    px = list(d.get_flattened_data() if hasattr(d, "get_flattened_data") else d.getdata())
    dh = 0
    bit = 0
    for y in range(8):
        for x in range(8):
            if px[y * 9 + x] < px[y * 9 + x + 1]:
                dh |= 1 << bit
            bit += 1
    return ah, dh


def thumbnail(pil_image) -> str:
    """16x16 RGB thumbnail as hex (768 bytes) - colour + layout signature."""
    return pil_image.convert("RGB").resize((16, 16)).tobytes().hex()


def visual_similarity(a, b) -> float:
    """0..1 similarity of two images described by objects with ahash, dhash
    and thumb: perceptual hashes (structure) + thumbnail colour distance.
    Hashes alone confuse very different pictures on similar backgrounds."""
    hs = hash_similarity(a.ahash, a.dhash, b.ahash, b.dhash)
    ta, tb = getattr(a, "thumb", ""), getattr(b, "thumb", "")
    if not ta or not tb or len(ta) != len(tb):
        return hs
    ba, bb = bytes.fromhex(ta), bytes.fromhex(tb)
    mad = sum(abs(x - y) for x, y in zip(ba, bb)) / (len(ba) * 255.0)
    ts = max(0.0, 1.0 - mad * 4.0)          # mean colour difference of 25% -> 0
    return 0.4 * hs + 0.6 * ts


def hash_similarity(a1, d1, a2, d2) -> float:
    if not (a1 or d1) or not (a2 or d2):
        return 0.0
    da = bin((a1 ^ a2) & ((1 << 64) - 1)).count("1")
    dd = bin((d1 ^ d2) & ((1 << 64) - 1)).count("1")
    return 1.0 - (da + dd) / 128.0


def _roman_value(s: str) -> int:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = vals.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


def _to_roman(n: int, lower=True) -> str:
    table = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
             (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for v, s in table:
        while n >= v:
            out += s
            n -= v
    return out if lower else out.upper()


def _page_number_candidate(text: str):
    t = " ".join((text or "").split())
    m = _DECOR_RE.match(t)
    if not m:
        return None
    label = m.group(1)
    if _ARABIC_RE.match(label):
        return label
    if _ROMAN_RE.match(label) and _to_roman(_roman_value(label), label.islower()) == label:
        return label
    return None


def _signature(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+|\b[ivxlcdm]+\b", "#", (text or "").lower())).strip()


class PdfModel:
    def __init__(self, path: str, cache_dir: str = None):
        import fitz
        self.path = path
        self.doc = fitz.open(path)
        self.fingerprint = pdf_fingerprint(path)
        self.cache_dir = os.path.join(cache_dir, self.fingerprint) if cache_dir else None
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
        self.pages = []
        self.words = []
        self.images = []
        self._raw = {}

    # ---------------------------------------------------------- build
    def build(self, progress=None, cancel=None):
        n = self.doc.page_count
        raw_pages = []
        for i in range(n):
            if cancel and cancel():
                break
            raw_pages.append(self._page_raw(i + 1))
            if progress:
                progress("PDF pages", i + 1, n)
        self._assemble(raw_pages)
        return self

    def _page_raw(self, pno: int) -> dict:
        path = os.path.join(self.cache_dir, f"p{pno:05d}.json") if self.cache_dir else None
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("v") == CACHE_VERSION:
                    return data
            except (OSError, ValueError):
                pass
        data = self._analyse_page(pno)
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError:
                pass
        return data

    def _analyse_page(self, pno: int) -> dict:
        from auto_zoning import layout_engine, paragraph_auto_zone
        from core import column_detector
        page = self.doc[pno - 1]
        w, h = page.rect.width, page.rect.height
        lines = layout_engine.extract_page_lines(page, with_decorations=False)
        has_text = bool(lines)
        raw_words = [(x0, y0, x1, y1, t) for (x0, y0, x1, y1, t, *_r) in page.get_text("words")]
        if not lines:
            lines, raw_words = self._ocr_lines(pno)
        top = [li for li in lines if li.bbox[3] <= h * BAND]
        bottom = [li for li in lines if li.bbox[1] >= h * (1 - BAND)]
        band_lines = top + bottom
        band_ids = {id(li) for li in band_lines}
        body = [li for li in lines if id(li) not in band_ids]
        # reading order: bands -> columns -> paragraphs -> lines
        paragraphs = []
        if body:
            for band in column_detector.detect_bands(w, h, body):
                for c in range(band.columns):
                    col = [li for li in band.items if column_detector.column_index_for_bbox(band, li.bbox) == c]
                    if not col:
                        continue
                    for blk in paragraph_auto_zone.group_lines_into_paragraphs(col):
                        paragraphs.append(sorted(blk.lines, key=lambda li: (li.bbox[1], li.bbox[0])))
        out_paras = []
        for para in paragraphs:
            plines = []
            for li in para:
                ws = [rw for rw in raw_words if li.bbox[0] - 1 <= (rw[0] + rw[2]) / 2 <= li.bbox[2] + 1
                      and li.bbox[1] - 1 <= (rw[1] + rw[3]) / 2 <= li.bbox[3] + 1]
                ws.sort(key=lambda rw: rw[0])
                if ws:
                    plines.append([[round(v, 2) for v in rw[:4]] + [rw[4]] for rw in ws])
            if plines:
                out_paras.append(plines)
        bands = []
        for li in band_lines:
            ws = sorted((rw for rw in raw_words if li.bbox[0] - 1 <= (rw[0] + rw[2]) / 2 <= li.bbox[2] + 1
                         and li.bbox[1] - 1 <= (rw[1] + rw[3]) / 2 <= li.bbox[3] + 1), key=lambda rw: rw[0])
            bands.append({"text": li.text, "bbox": [round(v, 2) for v in li.bbox],
                          "where": "top" if any(li is t for t in top) else "bottom",
                          "words": [[round(v, 2) for v in rw[:4]] + [rw[4]] for rw in ws]})
        images = self._page_images(page, pno)
        return {"v": CACHE_VERSION, "w": w, "h": h, "paras": out_paras, "bands": bands,
                "images": images, "text": has_text}

    def _ocr_lines(self, pno):
        """Lines/words of a scanned page from the Zoning module's OCR cache."""
        try:
            from core.pdf_loader import PDFDocument
            from core.ocr import ocr_service
            from auto_zoning import layout_engine
            res = ocr_service.get_cached_ocr_for_page(PDFDocument(self.path), pno)
            if res is None:
                return [], []
            lines = layout_engine.ocr_page_lines(res)
            words = []
            for li in lines:
                toks = li.text.split()
                if not toks:
                    continue
                step = (li.bbox[2] - li.bbox[0]) / len(toks)
                for k, t in enumerate(toks):
                    words.append((li.bbox[0] + k * step, li.bbox[1], li.bbox[0] + (k + 1) * step, li.bbox[3], t))
            return lines, words
        except Exception:
            return [], []

    def _page_images(self, page, pno) -> list:
        from PIL import Image
        out = []
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:
            infos = []
        pw, ph = page.rect.width, page.rect.height
        for k, info in enumerate(infos):
            bbox = info.get("bbox")
            if not bbox or (bbox[2] - bbox[0]) < 4 or (bbox[3] - bbox[1]) < 4:
                continue
            if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 0.85 * pw * ph:
                continue  # scanned page background, not a figure
            pil = None
            data = b""
            xref = info.get("xref") or 0
            if xref:
                try:
                    ext = self.doc.extract_image(xref)
                    data = ext.get("image", b"")
                    pil = Image.open(io.BytesIO(data))
                except Exception:
                    pil = None
            if pil is None:
                try:
                    import fitz
                    pix = page.get_pixmap(clip=fitz.Rect(bbox), dpi=72)
                    data = pix.tobytes("png")
                    pil = Image.open(io.BytesIO(data))
                except Exception:
                    continue
            ah, dh = image_hashes(pil)
            out.append({"index": len(out), "bbox": [round(v, 2) for v in bbox], "width": pil.width,
                        "height": pil.height, "sha1": hashlib.sha1(data).hexdigest(), "ahash": ah, "dhash": dh,
                        "thumb": thumbnail(pil)})
        return out

    # ------------------------------------------------------- assembling
    def _assemble(self, raw_pages):
        # running heads: repeated signatures in the bands
        sig_count = Counter()
        for rp in raw_pages:
            for b in rp["bands"]:
                sig_count[_signature(b["text"])] += 1
        n = max(1, len(raw_pages))
        repeated = {s for s, c in sig_count.items() if c >= max(2, int(0.2 * n)) and s not in ("", "#")}

        para_no = line_no = 0
        candidates = {}
        for pno, rp in enumerate(raw_pages, start=1):
            page = PdfPage(number=pno, width=rp["w"], height=rp["h"], has_text_layer=rp.get("text", True))
            page.word_start = len(self.words)
            body_bands = {"top": [], "bottom": []}
            for b in rp["bands"]:
                cand = _page_number_candidate(b["text"])
                if cand:
                    candidates.setdefault(pno, []).append((cand, tuple(b["bbox"])))
                elif _signature(b["text"]) in repeated:
                    page.running_text.append(b["text"])
                elif b.get("words"):
                    # not a page number, not repeated: real content that
                    # happens to sit in the margin band - keep it as text
                    body_bands[b["where"]].append([b["words"]])
            pending = None
            for para in body_bands["top"] + rp["paras"] + body_bands["bottom"]:
                para_no += 1
                for line in para:
                    line_no += 1
                    for x0, y0, x1, y1, t in line:
                        if pending is not None:
                            # line-end hyphenation: join for comparison
                            pending.text = pending.text[:-1] + t
                            pending.extra_bboxes.append((x0, y0, x1, y1))
                            pending = None
                            continue
                        word = PdfWord(text=t, page=pno, bbox=(x0, y0, x1, y1), para=para_no, line=line_no)
                        self.words.append(word)
                    last = self.words[-1] if self.words and self.words[-1].page == pno else None
                    if last is not None and last.text.endswith("-") and len(last.text) > 2 and \
                            last.text[-2].isalpha() and line is not para[-1]:
                        nxt = para[para.index(line) + 1] if para.index(line) + 1 < len(para) else None
                        if nxt and nxt[0][4][:1].islower():
                            pending = last
            page.word_end = len(self.words)
            for im in rp["images"]:
                img = PdfImage(page=pno, index=im["index"], bbox=tuple(im["bbox"]), width=im["width"],
                               height=im["height"], sha1=im["sha1"], ahash=im["ahash"], dhash=im["dhash"],
                               thumb=im.get("thumb", ""))
                img.word_pos = self._word_pos_for_box(page, img.bbox)
                img.caption, img.caption_bbox = self._caption_for(page, img.bbox)
                page.images.append(img)
                self.images.append(img)
            self.pages.append(page)
        self._resolve_page_numbers(candidates)

    def _word_pos_for_box(self, page, bbox) -> int:
        """Index of the first body word that comes after the box in reading
        order (same column: overlapping horizontally, starting below it)."""
        for i in range(page.word_start, page.word_end):
            w = self.words[i]
            same_col = min(w.bbox[2], bbox[2]) - max(w.bbox[0], bbox[0]) > -20
            if same_col and w.bbox[1] >= bbox[3] - 2:
                return i
            if w.bbox[0] > bbox[2] and w.bbox[1] > bbox[1] - 2:
                # a later column: the image belongs before it
                return i
        return page.word_end

    def _caption_for(self, page, bbox):
        best = None
        paras = {}
        for i in range(page.word_start, page.word_end):
            paras.setdefault(self.words[i].para, []).append(self.words[i])
        for ws in paras.values():
            x0 = min(w.bbox[0] for w in ws)
            y0 = min(w.bbox[1] for w in ws)
            x1 = max(w.bbox[2] for w in ws)
            y1 = max(w.bbox[3] for w in ws)
            overlap = min(x1, bbox[2]) - max(x0, bbox[0])
            if overlap <= 0:
                continue
            gap_below = y0 - bbox[3]
            gap_above = bbox[1] - y1
            text = " ".join(w.text for w in ws)
            labelled = bool(_CAPTION_RE.match(text))
            for gap in (gap_below, gap_above):
                if 0 <= gap <= 30 and (labelled or len(text) < 200):
                    score = gap - (20 if labelled else 0)
                    if best is None or score < best[0]:
                        best = (score, text, (x0, y0, x1, y1))
        return (best[1], best[2]) if best else ("", None)

    def _resolve_page_numbers(self, candidates):
        """Document-wide numbering model: per numbering system the dominant
        offset (printed value - physical page) is learnt from the pages that
        carry a number; candidates agreeing with it are confirmed, pages
        without one are inferred inside the numbered range, conflicting
        candidates keep low confidence."""
        arabic, roman = Counter(), Counter()
        for pno, cands in candidates.items():
            for label, _bb in cands:
                if label.isdigit():
                    arabic[int(label) - pno] += 1
                else:
                    roman[_roman_value(label) - pno] += 1
        a_off = arabic.most_common(1)[0][0] if arabic else None
        r_off = roman.most_common(1)[0][0] if roman else None
        r_lower = True
        for cands in candidates.values():
            for label, _bb in cands:
                if not label.isdigit():
                    r_lower = label.islower()
        a_pages = [p for p, cs in candidates.items() for lb, _ in cs if lb.isdigit() and int(lb) - p == a_off]
        r_pages = [p for p, cs in candidates.items() for lb, _ in cs if not lb.isdigit() and
                   _roman_value(lb) - p == r_off]
        a_first = min(a_pages) if a_pages else None
        a_last = max(a_pages) if a_pages else None
        r_first = min(r_pages) if r_pages else None
        r_last = max(r_pages) if r_pages else None
        for page in self.pages:
            cands = candidates.get(page.number, [])
            confirmed = None
            for label, bb in cands:
                if label.isdigit() and a_off is not None and int(label) - page.number == a_off:
                    confirmed = (label, bb)
                elif not label.isdigit() and r_off is not None and _roman_value(label) - page.number == r_off:
                    confirmed = (label, bb)
            if confirmed:
                page.printed, page.printed_found, page.printed_bbox = confirmed[0], confirmed[0], confirmed[1]
                page.printed_confidence = 0.97 if (arabic if confirmed[0].isdigit() else roman).most_common(1)[0][1] >= 2 \
                    else 0.8
                continue
            if cands:
                label, bb = cands[0]
                page.printed, page.printed_found, page.printed_bbox = label, label, bb
                page.printed_confidence = 0.4   # disagrees with the document's numbering model
                continue
            # unnumbered page: infer inside a numbered range
            if a_first is not None and a_first <= page.number <= a_last + 1 and page.number + a_off > 0:
                page.printed = str(page.number + a_off)
                page.printed_confidence = 0.75
            elif r_first is not None and r_first <= page.number <= r_last + 1 and page.number + r_off > 0:
                page.printed = _to_roman(page.number + r_off, r_lower)
                page.printed_confidence = 0.7
            elif a_first is not None and page.number < a_first and page.number + a_off > 0 and \
                    (r_last is None or page.number > r_last):
                # chapter opener directly before the first numbered page
                page.printed = str(page.number + a_off)
                page.printed_confidence = 0.6

    # ------------------------------------------------------------ query
    def page(self, pno) -> PdfPage:
        return self.pages[pno - 1]

    def page_of_word(self, idx) -> int:
        return self.words[idx].page if 0 <= idx < len(self.words) else None

    def render(self, pno: int, zoom: float = 1.0):
        """PIL image of a page (lazy; never cached for whole books)."""
        import fitz
        from PIL import Image
        pix = self.doc[pno - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def image_pil(self, img: PdfImage, dpi: int = 110):
        import fitz
        from PIL import Image
        pix = self.doc[img.page - 1].get_pixmap(clip=fitz.Rect(img.bbox), dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def search(self, text: str) -> list:
        """[(page, word_index)] of words whose key starts the searched phrase."""
        keys = [textnorm.key(t) for t in textnorm.words(text)]
        if not keys:
            return []
        out = []
        for i in range(len(self.words) - len(keys) + 1):
            if all(self.words[i + k].key == keys[k] for k in range(len(keys))):
                out.append((self.words[i].page, i))
        return out
