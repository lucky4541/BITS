"""Tag suggestions for a zone: "which tag is this?", ranked, with reasons.

Three independent kinds of evidence are combined (noisy-or per tag button):

1. STYLE LEARNING - the typographic signature of the zone (font family,
   size, colour, bold / italic, all-caps, bullet glyph) compared with the
   zones already tagged in this project. Tag one blue 11.5 pt bold heading
   as "Heading 2" and every zone with that signature is suggested as
   Heading 2 - in any language, for any book design.
2. CONTENT RULES (any language, core.lang) - figure / table labels
   ("Figura 1.2", "Таблица 3", "图1-1") -> caption, chapter labels
   ("Capítulo 1", "第3章") -> chapter title, structural headings
   ("REFERENCIAS", "参考文献", "Index") -> their part titles, bullets,
   numbered references, page numbers, ISBN, copyright ...
3. THE AUTO TAG ENGINE - its decision for the block at the zone's place
   (optional: pass `engine_decide(page) -> [TagDecision]`).

`suggest(zone)` returns up to k Suggestion(label, tag, attrs, score 0..1,
reasons); `similar_zones(zone)` lists the zones with the same style
signature (for "apply to all similar")."""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core import lang

_BULLETS = set("•●○◦▪▫■□◆◇♦‣⁃–—-*·►▶✓✔")
_ISBN_RE = re.compile(r"\bISBN\b|97[89][\s\-]?\d", re.I)
_COPY_RE = re.compile(r"©|\(c\)\s*\d{4}|copyright|derechos reservados|todos los derechos|alle rechte|tous droits",
                      re.I)
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_NUM_ENTRY_RE = re.compile(r"^\s*(\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)])\s+\S")
_PAGE_NUM_RE = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7})\s*$")
_ALPHA_ITEM_RE = re.compile(r"^\s*[a-z][.)]\s+\S")
_ROMAN_ITEM_RE = re.compile(r"^\s*[ivx]{1,5}[.)]\s+\S")
_TAG_RE = re.compile(r"<[^>]+>")

# heading kind (core.lang) -> part_type of the profile's heading buttons
_HEADING_PART = {"references": "bibliography", "notes": "notes", "index": "index", "contents": "toc",
                 "preface": "preface", "foreword": "foreword", "introduction": "introduction", "ack": "ack",
                 "glossary": "glossary", "dedication": "dedication"}


@dataclass
class Suggestion:
    label: str
    tag: str
    attrs: dict
    score: float
    reasons: list = field(default_factory=list)

    @property
    def percent(self):
        return int(round(self.score * 100))


# ------------------------------------------------------------------- style
def zone_style(pdf_document, zone) -> dict:
    """Typographic signature of the text inside the zone (from the PDF's
    own characters; empty for an image-only / scanned zone)."""
    try:
        from core.text_extractor import _get_rawdict
        raw = _get_rawdict(pdf_document.get_page(zone.page))
    except Exception:  # noqa: BLE001
        return {}
    x0, y0, x1, y1 = zone.bbox
    fonts, faces, sizes, colors = Counter(), Counter(), Counter(), Counter()
    bold = italic = upper = letters = total = 0
    first_chars, lines = [], 0
    line_x = []
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            lb = ln.get("bbox")
            if not lb:
                continue
            cy = (lb[1] + lb[3]) / 2
            if not (y0 - 1 <= cy <= y1 + 1) or lb[2] < x0 - 1 or lb[0] > x1 + 1:
                continue
            got_first = False
            for sp in ln.get("spans", []):
                fname = sp.get("font", "")
                for ch in sp.get("chars", []):
                    c = ch.get("c", "")
                    cb = ch.get("bbox") or lb
                    cx = (cb[0] + cb[2]) / 2
                    if not (x0 - 1 <= cx <= x1 + 1) or not c.strip():
                        continue
                    if not got_first:
                        first_chars.append((c, sp.get("color", 0)))
                        line_x.append(cb[0])
                        got_first = True
                    total += 1
                    if c in _BULLETS:
                        continue
                    fonts[_family(fname)] += 1
                    faces[fname.split("+", 1)[-1]] += 1
                    sizes[round(sp.get("size", 0) * 2) / 2] += 1
                    colors[sp.get("color", 0)] += 1
                    flags = sp.get("flags", 0)
                    low = fname.lower()
                    if flags & 16 or "bold" in low or "black" in low or "heavy" in low or "semibold" in low \
                            or "demi" in low:
                        bold += 1
                    if flags & 2 or "italic" in low or "oblique" in low or "-it" in low:
                        italic += 1
                    if c.isalpha():
                        letters += 1
                        upper += c.isupper()
            if got_first:
                lines += 1
    if not total:
        return {"face": "", "font": "", "size": 0, "color": None, "bold": False, "italic": False, "caps": False,
                "bullet": "", "bullet_color": None, "lines": 0, "indent": 0, "empty": True,
                **_graphics_context(pdf_document, zone)}
    n = max(1, sum(fonts.values()))
    first_char, first_color = first_chars[0] if first_chars else ("", 0)
    style = {
        "face": faces.most_common(1)[0][0] if faces else "",
        "font": fonts.most_common(1)[0][0] if fonts else "",
        "size": sizes.most_common(1)[0][0] if sizes else 0,
        "color": colors.most_common(1)[0][0] if colors else 0,
        "bold": bold / n >= 0.6,
        "italic": italic / n >= 0.6,
        "caps": letters >= 3 and upper / max(1, letters) >= 0.85,
        "bullet": first_char if first_char in _BULLETS else "",
        "bullet_color": first_color if first_char in _BULLETS else None,
        "lines": lines,
        "indent": round(min(line_x) - zone.bbox[0]) if line_x else 0,
        "empty": False,
    }
    style.update(_graphics_context(pdf_document, zone))
    return style


_GRAPHICS_CACHE = {}


def _page_graphics(pdf_document, page_no):
    """(images, horizontal rules, other drawings) of a page, cached."""
    key = (getattr(pdf_document, "path", id(pdf_document)), page_no)
    if key in _GRAPHICS_CACHE:
        return _GRAPHICS_CACHE[key]
    images, rules, drawings = [], [], []
    try:
        page = pdf_document.get_page(page_no)
        W, H = page.rect.width, page.rect.height
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                images.append((r.x0, r.y0, r.x1, r.y1))
        for d in page.get_drawings():
            r = d["rect"]
            if r.width > 0.9 * W or r.height > 0.9 * H:
                continue                                   # page backgrounds / frames
            if r.height < 2.5 and r.width > 20:
                rules.append((r.x0, r.y0, r.x1, r.y1))
            elif r.width * r.height > 30:
                drawings.append((r.x0, r.y0, r.x1, r.y1))
    except Exception:  # noqa: BLE001
        pass
    if len(_GRAPHICS_CACHE) > 64:
        _GRAPHICS_CACHE.clear()
    _GRAPHICS_CACHE[key] = (images, rules, drawings)
    return _GRAPHICS_CACHE[key]


def _graphics_context(pdf_document, zone):
    images, rules, drawings = _page_graphics(pdf_document, zone.page)
    x0, y0, x1, y1 = zone.bbox
    area = max(1.0, (x1 - x0) * (y1 - y0))
    cover = sum(_inter(zone.bbox, g) for g in images + drawings) / area
    near = any(_inter((x0 - 25, y0 - 25, x1 + 25, y1 + 25), g) > 0 and _inter(zone.bbox, g) < 0.5 * area
               for g in images + drawings)
    inner_rules = sum(1 for r in rules if r[1] >= y0 - 2 and r[3] <= y1 + 2 and r[0] >= x0 - 4 and r[2] <= x1 + 4)
    return {"graphic_cover": round(min(1.0, cover), 2), "near_graphic": near, "ruled": inner_rules >= 2}


def _inter(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def _family(font_name: str) -> str:
    name = (font_name or "").split("+", 1)[-1]
    return re.split(r"[-,]", name, 1)[0]


def style_key(style: dict, loose=False, visual=False):
    if not style or style.get("empty"):
        return None
    if visual:
        return (style.get("face") or style["font"], style["size"], style["color"], style["bold"], style["italic"],
                style["caps"], style["bullet"], style["bullet_color"])
    if loose:
        return (style["font"], style["size"], style["bold"], bool(style["bullet"]), style.get("near_graphic"))
    return (style.get("face") or style["font"], style["size"], style["color"], style["bold"], style["italic"],
            style["caps"], style["bullet"], style["bullet_color"], style.get("near_graphic"), style.get("ruled"))


def describe_style(style: dict) -> str:
    if not style:
        return "no text"
    bits = [f"{style['font']} {style['size']:g}pt"]
    if style["bold"]:
        bits.append("bold")
    if style["italic"]:
        bits.append("italic")
    if style["caps"]:
        bits.append("caps")
    if style["color"] not in (0, None):
        bits.append(f"#{style['color']:06x}")
    if style["bullet"]:
        bits.append(f"bullet {style['bullet']}")
    return " ".join(bits)


# --------------------------------------------------------------- suggester
class TagSuggester:
    def __init__(self, pdf_document, zone_manager, tag_buttons, engine_decide=None, language=None):
        self.pdf = pdf_document
        self.zm = zone_manager
        self.buttons = list(tag_buttons)            # [(label, tag, attrs)]
        self.engine_decide = engine_decide
        self.language = language
        self._styles = {}                           # zone_id -> style (cache)
        self._learned = None

    # ---- profile button lookup
    def _button(self, label=None, tag=None, part_type=None, **attrs):
        for lab, t, a in self.buttons:
            if label and lab == label:
                return lab, t, a
        for lab, t, a in self.buttons:
            if part_type and a.get("part_type") == part_type:
                return lab, t, a
        for lab, t, a in self.buttons:
            if tag and t == tag and all(a.get(k) == v for k, v in attrs.items()):
                return lab, t, a
        return None

    def label_of(self, zone):
        best, best_n = None, -1
        for lab, t, a in self.buttons:
            if t != zone.tag:
                continue
            a2 = {k: v for k, v in a.items() if k != "tag_label"}
            if zone.attributes.get("tag_label") == lab:
                return lab
            if all(zone.attributes.get(k) == v for k, v in a2.items()) and len(a2) > best_n:
                best, best_n = lab, len(a2)
        return best

    # ---- styles
    def style(self, zone):
        key = (zone.zone_id, tuple(round(v, 1) for v in zone.bbox), zone.page)
        if key not in self._styles:
            self._styles[key] = zone_style(self.pdf, zone)
        return self._styles[key]

    def _trusted(self, zone):
        """Zones whose tag a person chose or confirmed (not raw engine output)."""
        a = zone.attributes
        return not a.get("auto_engine") or a.get("manual_override") or a.get("reviewed") or a.get("locked")

    def learn(self, refresh=False):
        """style key -> Counter(label) over the trusted zones of the project."""
        if self._learned is not None and not refresh:
            return self._learned
        exact, loose = defaultdict(Counter), defaultdict(Counter)
        for z in self.zm.zones.values():
            if not self._trusted(z):
                continue
            lab = self.label_of(z)
            st = self.style(z)
            if not lab or not st or st.get("empty"):
                continue
            exact[style_key(st)][lab] += 1
            loose[style_key(st, loose=True)][lab] += 1
        self._learned = (exact, loose)
        return self._learned

    def invalidate(self):
        self._learned = None

    # ---- main
    def suggest(self, zone, k=3, exclude_current=False):
        votes = defaultdict(list)                   # label -> [(score, reason)]
        text = _TAG_RE.sub("", zone.text or "").strip()
        st = self.style(zone)
        self._style_votes(zone, st, votes)
        self._content_votes(zone, text, st, votes)
        self._layout_votes(zone, text, st, votes)
        self._engine_votes(zone, votes)
        out = []
        for lab, items in votes.items():
            btn = self._button(label=lab)
            if not btn:
                continue
            score = _combine(items)
            reasons = [r for s, r in sorted(items, key=lambda x: -x[0]) if r]
            out.append(Suggestion(btn[0], btn[1], {kk: vv for kk, vv in btn[2].items()}, score, reasons))
        out.sort(key=lambda s: -s.score)
        if exclude_current:
            cur = self.label_of(zone)
            out = [s for s in out if s.label != cur]
        return out[:k]

    def _style_votes(self, zone, st, votes):
        if not st or st.get("empty"):
            return
        exact, loose = self.learn()
        own = self.label_of(zone) if self._trusted(zone) else None
        for counts, weight, what in ((exact.get(style_key(st)), 0.92, "same style"),
                                     (loose.get(style_key(st, loose=True)), 0.7, "same font and size")):
            if not counts:
                continue
            counts = Counter(counts)
            if own:
                counts[own] -= 1                   # do not learn from the zone itself
            total = sum(v for v in counts.values() if v > 0)
            for lab, n in counts.items():
                if n <= 0:
                    continue
                share = n / total
                conf = weight * share * min(1.0, 0.55 + 0.15 * n)
                votes[lab].append((conf, f"{what} as {n} zone(s) you tagged {lab} - {describe_style(st)}"))

    def _content_votes(self, zone, text, st, votes):
        if not text:
            return
        first_line = text.split("\n", 1)[0]
        words = text.split()
        # figure / table labels in any language
        if lang.match_label(text, "figure"):
            b = self._button(label="Caption") or self._button(tag="caption")
            if b:
                votes[b[0]].append((0.85, f"starts with a figure label \"{lang.match_label(text, 'figure')[0]}\""))
        if lang.match_label(text, "table"):
            b = self._button(label="Table Caption") or self._button(tag="table-caption")
            if b:
                votes[b[0]].append((0.85, f"starts with a table label \"{lang.match_label(text, 'table')[0]}\""))
        # chapter / part labels
        for cat, pt in (("chapter", "chapter"), ("part", "part"), ("appendix", "appendix")):
            m = lang.match_label(text, cat)
            if not m:
                continue
            big = st and st.get("size", 0) >= 12
            if m[1] and len(words) <= 16:
                b = self._button(part_type=pt)
                if b:
                    votes[b[0]].append((0.75 if big else 0.55, f"starts with a {cat} label \"{m[0]}\""))
            elif not m[1]:
                b = self._button(label="Chapter Number / Label") or self._button(tag="label")
                if b:
                    votes[b[0]].append((0.8, f"a {cat} number on its own (\"{m[0]}\")"))
        # structural headings (References, Index, Contents, Preface ...)
        kind = lang.heading_kind(first_line) if len(words) <= 8 else None
        if kind in _HEADING_PART:
            b = self._button(part_type=_HEADING_PART[kind])
            if b:
                votes[b[0]].append((0.85, f"\"{first_line}\" is a {kind} heading"))
        elif kind == "abstract":
            b = self._button(label="Abstract") or self._button(label="Chapter Abstract")
            if b:
                votes[b[0]].append((0.7, "abstract heading"))
        elif kind == "keywords":
            b = self._button(label="Keywords")
            if b:
                votes[b[0]].append((0.75, "keywords heading"))
        # page number
        if _PAGE_NUM_RE.match(text):
            page_h = self._page_height(zone.page)
            in_margin = page_h and (zone.bbox[1] < 0.14 * page_h or zone.bbox[3] > 0.86 * page_h)
            b = self._button(label="Page Number") or self._button(tag="pagenumber")
            if b:
                votes[b[0]].append((0.9 if in_margin else 0.5, "a number alone" + (" in the page margin"
                                                                                   if in_margin else "")))
        # bullets / numbered items / references
        if st and st.get("bullet") or (text[:1] in _BULLETS and len(text) > 2):
            b = self._button(label="List Bullet") or self._button(tag="list-bullet")
            if b:
                votes[b[0]].append((0.8, f"starts with a bullet \"{(st or {}).get('bullet') or text[:1]}\""))
        if _NUM_ENTRY_RE.match(text):
            if _YEAR_RE.search(text) and (len(text) > 40):
                b = self._button(label="Reference") or self._button(tag="reference")
                if b:
                    votes[b[0]].append((0.7, "numbered entry with a year (a reference)"))
            else:
                b = self._button(label="List Number") or self._button(tag="list")
                if b:
                    votes[b[0]].append((0.55, "numbered item"))
        elif _ALPHA_ITEM_RE.match(text):
            b = self._button(label="List Lower Alpha")
            if b:
                votes[b[0]].append((0.5, "lettered item"))
        elif _YEAR_RE.search(text) and re.match(r"^\s*" + lang.UPPER + r"[^\s,]+,\s*" + lang.UPPER, text) \
                and len(text) > 40:
            b = self._button(label="Reference")
            if b:
                votes[b[0]].append((0.55, "author, initials ... year (a reference)"))
        # front-matter metadata
        if _ISBN_RE.search(text) and len(text) < 120:
            b = self._button(label="ISBN")
            if b:
                votes[b[0]].append((0.85, "ISBN"))
        if _COPY_RE.search(text) and len(text) < 400:
            b = self._button(label="Copyright")
            if b:
                votes[b[0]].append((0.7, "copyright statement"))
        # prose: a long, multi-line block of body type is a paragraph
        if st and st.get("lines", 0) >= 3 and len(words) >= 25 and not st.get("bullet"):
            b = self._button(label="Paragraph") or self._button(tag="p")
            if b:
                votes[b[0]].append((0.45, f"{st['lines']} lines of running text"))

    def _layout_votes(self, zone, text, st, votes):
        """Votes that need no learning: graphics, body text, headings."""
        if not st:
            return
        if st.get("graphic_cover", 0) >= 0.4 and len(text.split()) <= 40:
            b = self._button(label="Figure") or self._button(tag="figure")
            if b:
                votes[b[0]].append((0.85, f"covers an image / drawing ({st['graphic_cover']:.0%})"))
        if st.get("empty"):
            return
        if st.get("ruled") and st.get("lines", 0) >= 2:
            b = self._button(label="Table") or self._button(tag="table")
            if b:
                votes[b[0]].append((0.55, "text between ruling lines"))
        body = self.body_size()
        size = st.get("size", 0)
        words = len(text.split())
        if body and abs(size - body) <= 0.6 and not st.get("bold") and not st.get("bullet"):
            b = self._button(label="Paragraph") or self._button(tag="p")
            if b:
                votes[b[0]].append((0.45, f"body text size ({size:g}pt)"))
        if body and size >= body + 0.9 and words <= 18 and (st.get("bold") or st.get("caps") or size >= body * 1.3):
            for lab, conf, why in self._heading_guess(size, st):
                votes[lab].append((conf, why))

    def _heading_guess(self, size, st):
        """Heading level for an unseen heading style: placed by size among
        the headings already tagged in this project."""
        ladder = self._heading_ladder()
        heads = [(lab, t, a) for lab, t, a in self.buttons if t in ("h2", "h3", "h4", "h5", "h6")
                 or (t == "h1" and not a.get("part_type"))]
        if not heads:
            return []
        if not ladder:
            lab = heads[0][0]
            return [(lab, 0.4, f"larger / bold short line ({size:g}pt)")]
        # ladder: [(size, label)] largest first
        lower = [lab for s, lab in ladder if s <= size + 0.25]
        upper = [lab for s, lab in ladder if s > size + 0.25]
        out = []
        if lower:
            out.append((lower[0], 0.45, f"heading-like line, {size:g}pt - same or smaller headings are {lower[0]}"))
        if upper:
            idx = next((i for i, h in enumerate(heads) if h[0] == upper[-1]), None)
            if idx is not None and idx + 1 < len(heads):
                out.append((heads[idx + 1][0], 0.35, f"heading-like line, smaller than {upper[-1]}"))
        return out

    def _heading_ladder(self):
        exact, _loose = self.learn()
        sizes = defaultdict(Counter)
        for key, counts in exact.items():
            for lab, n in counts.items():
                if lab.startswith("Heading") or lab in ("Chapter Title", "Part Title"):
                    sizes[lab][key[1]] += n
        ladder = [(c.most_common(1)[0][0], lab) for lab, c in sizes.items()]
        return sorted(ladder, key=lambda x: -x[0])

    def body_size(self):
        if getattr(self, "_body", None) is None:
            sizes = Counter()
            try:
                from core.text_extractor import _get_rawdict
                n = self.pdf.page_count
                for p in sorted({1, max(1, n // 3), max(1, n // 2), max(1, 2 * n // 3), n}):
                    for b in _get_rawdict(self.pdf.get_page(p)).get("blocks", []):
                        for ln in b.get("lines", []):
                            for sp in ln.get("spans", []):
                                sizes[round(sp.get("size", 0) * 2) / 2] += len(sp.get("chars", []))
            except Exception:  # noqa: BLE001
                pass
            self._body = sizes.most_common(1)[0][0] if sizes else 0
        return self._body

    def _engine_votes(self, zone, votes):
        if not self.engine_decide:
            return
        try:
            decisions = self.engine_decide(zone.page) or []
        except Exception:  # noqa: BLE001
            return
        best, best_iou = None, 0.0
        for d in decisions:
            if not getattr(d, "option", None):
                continue
            iou = _iou(zone.bbox, d.block.bbox)
            if iou > best_iou:
                best, best_iou = d, iou
        if best is None or best_iou < 0.3:
            return
        lab = getattr(best.option, "label", None)
        conf = (best.confidence or 0) / 100.0 * min(1.0, 0.4 + best_iou)
        if lab:
            votes[lab].append((0.75 * conf, f"Auto Tag engine: {best.role} ({best.confidence:.0f}%)"))
        for alt in (best.alternatives or [])[:2]:
            alab, role, sc = alt[0], alt[1], alt[2]
            if alab and alab != lab:
                votes[alab].append((0.4 * float(sc), f"Auto Tag alternative: {role}"))

    def _page_height(self, page):
        try:
            return self.pdf.get_page(page).rect.height
        except Exception:  # noqa: BLE001
            return 0

    # ---- bulk
    def learned_retags(self, min_score=0.8):
        """[(zone, Suggestion)] for every automatic (untrusted) zone that has
        exactly the style of zones you tagged / corrected (same face, size,
        colour, weight, case, bullet and layout context) and a different tag -
        "retag the rest the way I tagged these".

        Confidence here is the agreement of your own tags for that style
        (one correction is enough when nothing contradicts it), not the
        ranking score of suggest()."""
        exact, _loose = self.learn()
        out = []
        for z in self.zm.zones.values():
            if self._trusted(z) or z.locked:
                continue
            st = self.style(z)
            if not st or st.get("empty"):
                continue
            counts = exact.get(style_key(st))
            if not counts:
                continue
            counts = Counter(counts)
            total = sum(counts.values())
            lab, n = counts.most_common(1)[0]
            share = n / total
            score = share * min(1.0, 0.8 + 0.07 * n)
            if score < min_score or lab == self.label_of(z):
                continue
            btn = self._button(label=lab)
            if btn:
                reason = f"same style as {n} zone(s) you tagged {lab} - {describe_style(st)}"
                out.append((z, Suggestion(btn[0], btn[1], dict(btn[2]), score, [reason])))
        return out

    def similar_zones(self, zone, include_same_tag=False):
        """Zones that LOOK the same (font face, size, colour, weight, case,
        bullet) - layout context such as a nearby figure is not part of it."""
        st = self.style(zone)
        key = style_key(st, visual=True)
        if key is None:
            return []
        cur = self.label_of(zone)
        out = []
        for z in self.zm.zones.values():
            if z.zone_id == zone.zone_id or z.locked:
                continue
            if style_key(self.style(z), visual=True) != key:
                continue
            if not include_same_tag and self.label_of(z) == cur:
                continue
            out.append(z)
        return out


def _combine(items):
    """Noisy-or of independent evidence scores."""
    p = 1.0
    for sc, _r in items:
        p *= (1.0 - max(0.0, min(0.99, sc)))
    return 1.0 - p


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0
