"""Document SECTION context for Auto Zone: which parts of the book are a
references / bibliography list and which are endnotes.

A reference entry or an endnote looks like an ordinary paragraph when it is
judged on its own page in isolation (same body font, full-measure lines),
so the classifier alone tags them as paragraphs. What actually identifies
them is the SECTION they sit in - the "References" / "Works Cited" /
"Notes" heading above them, possibly several pages earlier. This module
scans the whole PDF text once (fast: text layer only, cached), finds those
section headings and where each section ends, and then lets Auto Zone

  * tag the section heading as the references / endnotes heading role, and
  * tag every text block inside the section as a reference entry
    (numbered or author-date, from its own first characters) or an endnote.

Nothing is document-specific: heading words come from SECTION_WORDS, a
heading is recognised by its own typography relative to the document's body
text (bigger, or bold, short, on its own line), and a section ends at the
next heading of the same or higher rank (a smaller sub-heading inside the
notes - "Chapter 1" - does not end it).
"""
import re
from dataclasses import dataclass, field

REFERENCES, ENDNOTES = "references", "endnotes"

SECTION_WORDS = {
    REFERENCES: ("references", "reference list", "bibliography", "select bibliography", "selected bibliography",
                 "works cited", "works consulted", "literature cited", "sources", "further reading",
                 "sources and further reading", "bibliographical notes"),
    ENDNOTES: ("notes", "endnotes", "end notes", "chapter notes"),
}
_LEAD_NUM_RE = re.compile(r"^\s*(?:\d+|[ivxlcdm]+)[.:\s]+", re.IGNORECASE)
_TRAIL_RE = re.compile(r"[\s.:]+$")
_NOTES_TO_RE = re.compile(r"^notes\s+(to|for)\s+(the\s+)?(chapter|part|chapters|introduction|conclusion|text|volume)\b",
                          re.IGNORECASE)
ENTRY_NUM_RE = re.compile(r"^\s*(\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)]?\s)")
HEADER_BAND = 0.07
BLANK_PAGE_RE = re.compile(r"^\W*this\s+page\s+(is\s+)?(intentionally|deliberately)\s+(left\s+)?blank\W*$",
                           re.IGNORECASE)
RUNNING_BAND = 0.1


def section_kind(text: str):
    """REFERENCES / ENDNOTES when `text` is such a section heading."""
    t = _TRAIL_RE.sub("", _LEAD_NUM_RE.sub("", (text or "").strip())).lower()
    t = re.sub(r"\s+", " ", t)
    if not t or len(t) > 60:
        return None
    for kind, words in SECTION_WORDS.items():
        if t in words:
            return kind
    if _NOTES_TO_RE.match(t):
        return ENDNOTES
    return None


@dataclass
class HeadingMark:
    page: int
    y0: float
    y1: float
    size: float
    text: str
    kind: str = None          # REFERENCES / ENDNOTES / None (any other heading)


@dataclass
class SectionMap:
    headings: list = field(default_factory=list)       # HeadingMark, document order
    body_size: float = 10.0

    def to_dict(self):
        return {"body_size": self.body_size,
                "headings": [[h.page, h.y0, h.y1, h.size, h.text, h.kind] for h in self.headings]}

    @classmethod
    def from_dict(cls, d):
        return cls(body_size=d.get("body_size", 10.0),
                   headings=[HeadingMark(*row) for row in d.get("headings", [])])

    def region_at(self, page: int, y: float):
        """Kind of the section that position (page, y) belongs to."""
        current = None
        for h in self.headings:
            if (h.page, h.y0) > (page, y):
                break
            if h.kind:
                current = h
            elif current is not None and h.size >= current.size - 0.6:
                current = None          # same-or-higher rank heading ends the section
        return current.kind if current is not None else None

    def heading_kind_at(self, page: int, bbox):
        """REFERENCES / ENDNOTES when a section heading lies inside bbox."""
        for h in self.headings:
            if h.page == page and h.kind and h.y0 >= bbox[1] - 2 and h.y1 <= bbox[3] + 2:
                return h.kind
        return None

    @property
    def has_sections(self):
        return any(h.kind for h in self.headings)


def _page_lines(page):
    """[(text, size, bold, y0, y1, x0, x1)] for every text line of a fitz page."""
    out = []
    try:
        d = page.get_text("dict")
    except Exception:
        return out
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in line["spans"]).strip()
            chars = sum(len(s["text"].strip()) for s in spans) or 1
            size = max(s.get("size", 0) for s in spans)
            bold = sum(len(s["text"].strip()) for s in spans
                       if (s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower()) / chars >= 0.6
            x0, y0, x1, y1 = line["bbox"]
            out.append((text, size, bold, y0, y1, x0, x1))
    return out


def build_section_map(pdf_document, page_count: int = None) -> SectionMap:
    n = page_count or pdf_document.page_count
    pages = []
    sizes = {}
    for p in range(1, n + 1):
        try:
            page = pdf_document.get_page(p)
        except Exception:
            pages.append((p, 0, []))
            continue
        lines = _page_lines(page)
        pages.append((p, page.rect.height, lines))
        for text, size, _b, *_ in lines:
            key = round(size * 2) / 2
            sizes[key] = sizes.get(key, 0) + len(text)
    body = max(sizes.items(), key=lambda kv: kv[1])[0] if sizes else 10.0
    smap = SectionMap(body_size=body)
    for p, height, lines in pages:
        for k, (text, size, bold, y0, y1, x0, x1) in enumerate(lines):
            if height and y1 < HEADER_BAND * height:
                continue                                    # running head band
            words = len(text.split())
            if words == 0 or words > 12 or len(text) > 90:
                continue
            bigger = size >= body * 1.12
            kind = section_kind(text)
            if not (bigger or (bold and words <= 6) or (kind and (bold or bigger or text.isupper()))):
                continue
            if not kind and not bigger:
                continue                                    # a bold run-in phrase is not a section boundary
            smap.headings.append(HeadingMark(p, y0, y1, size, text, kind))
    return smap


# ------------------------------------------------------------- apply
def adjust_roles(layout, role_map: dict, smap: SectionMap, page: int, candidate_cls):
    """Returns a NEW role map in which blocks inside a references /
    endnotes section get the matching role on top (the cached map is never
    mutated). candidate_cls is semantic_classifier.RoleCandidate."""
    if smap is None or not smap.has_sections:
        return role_map
    out = {}
    for b in layout.blocks:
        cands = list(role_map.get(id(b), []))
        out[id(b)] = cands
        if b.kind in ("page_number", "running", "figure", "table"):
            continue
        if BLANK_PAGE_RE.match(re.sub(r"<[^>]+>", "", b.text or "").strip()):
            out[id(b)] = [candidate_cls("blank_page_notice", 0.97, ["'this page intentionally left blank'"])]
            continue
        hk = smap.heading_kind_at(page, b.bbox)
        if hk:
            role = "reference_heading" if hk == REFERENCES else "endnote_heading"
            cands.insert(0, candidate_cls(role, 0.96, [f"'{(b.text or '').strip()[:40]}' opens the {hk} section"]))
            continue
        region = smap.region_at(page, (b.bbox[1] + b.bbox[3]) / 2)
        if region is None:
            continue
        height = getattr(layout, "height", 0) or 0
        if _running_like(b, height):
            # "128 Notes to page 3": a running head whose text changes on every page - never zoned
            out[id(b)] = [candidate_cls("running_header", 0.95, ["numbered running head in the top margin"])]
            continue
        if _group_heading_like(b):
            # a group heading inside the section - "Introduction", "1 'Jewels of Women'" in
            # book-end notes grouped by chapter (numbering restarts under each one)
            role = "endnote_heading" if region == ENDNOTES else "reference_heading"
            cands.insert(0, candidate_cls(role, 0.94, [f"bold group heading inside the {region} section"]))
            continue
        top = cands[0] if cands else None
        if top is not None and top.role.startswith("heading_") and top.score >= 0.6 and \
                b.features.get("n_lines", 9) <= 2:
            continue                                         # a sub-heading inside the section stays a heading
        if region == REFERENCES:
            numbered = bool(ENTRY_NUM_RE.match(b.text or ""))
            role = "reference_numbered" if numbered else "reference_author_date"
            ev = ["inside the references section", "starts with an entry number" if numbered else "author-date entry"]
        else:
            role = "endnote"
            ev = ["inside the notes section"]
        cands.insert(0, candidate_cls(role, 0.93, ev))
    return out


# ------------------------------------------------------ entry splitting
def page_text_lines(pdf_page):
    """[(x0, y0, x1, y1, text)] for every text line of a fitz page, measured
    from its visible WORDS (a leading space glyph - common before a
    right-aligned note number - never shifts the line's left edge), with
    pieces that share a baseline joined into one line."""
    try:
        words = pdf_page.get_text("words")
    except Exception:
        return []
    groups = {}
    for x0, y0, x1, y1, w, bno, lno, _wno in words:
        groups.setdefault((bno, lno), []).append((x0, y0, x1, y1, w))
    lines = []
    for ws in groups.values():
        ws.sort(key=lambda t: t[0])
        lines.append([min(t[0] for t in ws), min(t[1] for t in ws), max(t[2] for t in ws), max(t[3] for t in ws),
                      " ".join(t[4] for t in ws)])
    lines.sort(key=lambda ln: (ln[1], ln[0]))
    rows = []
    for ln in lines:
        prev = rows[-1] if rows else None
        if prev is not None:
            overlap = min(prev[3], ln[3]) - max(prev[1], ln[1])
            if overlap > 0.5 * min(prev[3] - prev[1], ln[3] - ln[1]):
                left, right = (prev, ln) if prev[0] <= ln[0] else (ln, prev)
                rows[-1] = [min(prev[0], ln[0]), min(prev[1], ln[1]), max(prev[2], ln[2]), max(prev[3], ln[3]),
                            left[4] + " " + right[4]]
                continue
        rows.append(ln)
    return [tuple(r) for r in rows]


_TERMINAL_RE = re.compile(r"[.!?)\]\"'’”]\s*$")


def _entry_boundaries(lines, body):
    """Indices of lines (>0) that start a new reference entry / note.

    Hanging layout (number / first line out, turn-over lines in): the TEXT
    COLUMN is the rightmost left-edge cluster within 3 body sizes of the
    leftmost line; a line that sticks out to the left of it opens a new
    entry (right-aligned numbers "4" / "10" may sit at different x, all of
    them left of the text column). A line AT the text column never does,
    even when it starts with a number ("1898 in a letter...").
    Flush / first-line-indent layout: a line starting with an entry number
    opens a new entry when the line before it ended the previous entry."""
    tol = 0.3 * body
    xs = [ln[0] for ln in lines]
    minx = min(xs)
    clusters = sorted({round(x / tol) * tol for x in xs if x <= minx + 3 * body})
    textx = clusters[-1] if clusters else minx
    hanging = textx > minx + tol and any(x <= textx - tol for x in xs)
    gaps = [lines[i][1] - lines[i - 1][3] for i in range(1, len(lines))]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 0
    measure = max(ln[2] for ln in lines) - minx
    cuts = []
    for i in range(1, len(lines)):
        x, text = lines[i][0], lines[i][4]
        prev = lines[i - 1]
        gap = lines[i][1] - prev[3]
        if hanging:
            if x <= textx - tol:
                cuts.append(i)                  # outdented: a new entry
                continue
        elif ENTRY_NUM_RE.match(text) and (_TERMINAL_RE.search(prev[4]) or
                                           (measure > 0 and (prev[2] - minx) / measure < 0.85)):
            cuts.append(i)
            continue
        if gaps and gap > max(typical * 1.6, typical + 0.35 * body):
            cuts.append(i)                      # extra space between entries
    return cuts


def _group_heading_like(b):
    f = b.features
    return f.get("n_lines", 9) <= 2 and f.get("words", 99) <= 14 and f.get("bold_ratio", 0) >= 0.6


def _running_like(b, height):
    return bool(height) and b.bbox[3] < RUNNING_BAND * height and b.features.get("n_lines", 1) <= 1 and \
        bool(re.search(r"\d", b.text or ""))


def split_section_entries(layout, role_map: dict, smap: SectionMap, page: int, page_lines: list, block_cls):
    """Reference lists and notes are usually set with a HANGING indent
    (number / first line out, turn-over lines in) - the opposite of body
    paragraphs - so the paragraph grouper both keeps several entries in one
    block AND starts a new block at every indented turn-over line. Inside a
    references / endnotes section, each run of consecutive text blocks is
    therefore re-segmented from its PDF lines into exactly one block per
    entry (_entry_boundaries). Group headings, running heads and section
    headings end a run and are kept as they are. Returns (layout_copy,
    role_map); the cached layout is never mutated."""
    import copy
    if smap is None or not smap.has_sections or not page_lines:
        return layout, role_map
    height = getattr(layout, "height", 0) or 0
    pad = 0.6 * smap.body_size

    def eligible(b):
        return (b.kind in ("text", "list_item")
                and smap.region_at(page, (b.bbox[1] + b.bbox[3]) / 2) is not None
                and not smap.heading_kind_at(page, b.bbox)
                and not _running_like(b, height) and not _group_heading_like(b))

    def lines_of(b):
        return [ln for ln in page_lines if ln[1] >= b.bbox[1] - 2 and ln[3] <= b.bbox[3] + 2
                and ln[0] >= b.bbox[0] - pad and ln[2] <= b.bbox[2] + pad]

    runs = []                                    # ("keep", block) | ("run", [blocks])
    for b in layout.blocks:
        if not eligible(b):
            runs.append(("keep", b))
        elif runs and runs[-1][0] == "run" and runs[-1][1][-1].column == b.column:
            runs[-1][1].append(b)
        else:
            runs.append(("run", [b]))
    new_blocks, new_map, changed = [], dict(role_map), False
    for kind, item in runs:
        if kind == "keep":
            new_blocks.append(item)
            continue
        run = item
        seen, lines = set(), []
        for b in run:
            for ln in lines_of(b):
                if ln not in seen:
                    seen.add(ln)
                    lines.append(ln)
        lines.sort(key=lambda ln: (ln[1], ln[0]))
        if not lines:
            new_blocks.extend(run)
            continue
        cuts = _entry_boundaries(lines, smap.body_size) if len(lines) >= 2 else []
        parts = [lines[a:z] for a, z in zip([0] + cuts, cuts + [len(lines)])]
        if len(parts) == len(run) and all(
                abs(b.bbox[1] - min(ln[1] for ln in part)) < 1.5 for b, part in zip(run, parts)):
            new_blocks.extend(run)                 # the grouper already got these entries right
            continue
        changed = True
        base = run[0]
        for part in parts:
            bbox = (min(ln[0] for ln in part), min(ln[1] for ln in part),
                    max(ln[2] for ln in part), max(ln[3] for ln in part))
            nb = block_cls(kind="text", bbox=bbox, features=dict(base.features, n_lines=len(part),
                                                                 words=sum(len(ln[4].split()) for ln in part),
                                                                 split_from_entries=True),
                           column=base.column, band=base.band, order=base.order, region=base.region,
                           stored_text="\n".join(ln[4] for ln in part))
            new_blocks.append(nb)
            new_map[id(nb)] = [copy.copy(c) for c in role_map.get(id(base), [])]
    if not changed:
        return layout, role_map
    out = copy.copy(layout)
    out.blocks = new_blocks
    return out, new_map
