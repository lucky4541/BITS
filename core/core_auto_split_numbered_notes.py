"""EPUBForge Auto Split Based on Numbers detector.
Analyzes only the selected zone and returns horizontal PDF-Y boundaries.
"""
import re
from dataclasses import dataclass

NUMBERED_RE = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,4})(?:[.)])?|\((\d{1,4})\))(?=\s|$)")
PAGE_ONLY_RE = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,8})(?:\s*[-–—]\s*(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,8}))?\s*$")
TAG_RE = re.compile(r"<[^>]+>")

@dataclass
class Detection:
    boundaries: list
    heading_boundary: float | None
    page_boundary: float | None
    first_note_number: int | None
    note_count: int
    heading_detected: bool
    page_detected: bool
    warnings: list


def plain(s):
    return TAG_RE.sub("", s or "").strip()


def number_at_start(s):
    m = NUMBERED_RE.match(plain(s))
    return int(next(g for g in m.groups() if g is not None)) if m else None


def page_only(s):
    return bool(PAGE_ONLY_RE.fullmatch(plain(s)))


def uppercase(s):
    s = plain(s)
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= .82


def detect(page, zone_bbox, text_extractor, heading_max_fraction=.24, page_bottom_fraction=.075):
    x0, y0, x1, y1 = map(float, zone_bbox)
    lines = []
    for lb, formatted in text_extractor.extract_lines(page, zone_bbox):
        text = plain(formatted)
        if text:
            lines.append((tuple(lb), text))
    lines.sort(key=lambda x: (x[0][1], x[0][0]))
    if not lines:
        return Detection([], None, None, None, 0, False, False, ["No text lines found in selected zone."])

    page_h = float(getattr(page.rect, "height", y1) or y1)
    numbered = []
    for i, (bbox, text) in enumerate(lines):
        n = number_at_start(text)
        if n is not None and not (page_only(text) and bbox[3] >= page_h * (1 - page_bottom_fraction)):
            numbered.append((i, n))
    if not numbered:
        return Detection([], None, None, None, 0, False, False,
                         ["No numbered note starts detected. Supported forms: 1., 1), 1, [1], (1)."])

    first_i, first_num = numbered[0]
    pre = lines[:first_i]
    heading_lines = [li for li in pre if li[0][1] <= y0 + (y1-y0) * heading_max_fraction]
    heading = False
    heading_boundary = None
    if heading_lines:
        joined = " ".join(t for _, t in heading_lines).strip()
        if uppercase(joined) or (len(heading_lines) <= 3 and len(joined) <= 180):
            heading = True
            heading_boundary = lines[first_i][0][1]

    folios = [li for li in lines if page_only(li[1]) and li[0][3] >= page_h * (1-page_bottom_fraction)]
    folio = min(folios, key=lambda li: li[0][1]) if folios else None
    page_boundary = folio[0][1] if folio else None

    valid_notes = [(i,n) for i,n in numbered if page_boundary is None or lines[i][0][1] < page_boundary-0.5]
    boundaries = []
    if heading and heading_boundary is not None:
        boundaries.append(heading_boundary)
    boundaries.extend(lines[i][0][1] for i,_ in valid_notes[1:])
    if page_boundary is not None and y0 < page_boundary < y1:
        boundaries.append(page_boundary)

    clean=[]
    for y in sorted(boundaries):
        if y0 < y < y1 and (not clean or abs(y-clean[-1]) > 1.0):
            clean.append(y)
    warnings=[]
    if page_boundary is not None:
        warnings.append("Bottom page number detected.")
    return Detection(clean, heading_boundary, page_boundary, first_num, len(valid_notes), heading, folio is not None, warnings)
