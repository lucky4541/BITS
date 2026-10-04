"""Automatic numbered-notes split detector.

Detects a heading, numbered note starts and an optional bottom folio inside
ONE selected zone.  It returns horizontal Y boundaries for the existing
ZoneManager.split_zone() pipeline; it does not mutate zones.
"""
from dataclasses import dataclass, field
import re

NUMBER_RE = re.compile(
    r"^\s*(?P<marker>"
    r"(?:\[\s*\d{1,4}\s*\]|\(\s*\d{1,4}\s*\)|\d{1,4}[.)])"
    r"|(?:\d{1,4})(?=\s+)"
    r")\s*(?P<body>.*)$"
)
PAGE_ONLY_RE = re.compile(
    r"^\s*(?:\d{1,4}|[ivxlcdm]{1,12})\s*[.]?\s*$", re.I
)

@dataclass
class DetectedLine:
    bbox: list
    text: str
    marker: str = ""
    body: str = ""
    kind: str = "other"

@dataclass
class SplitDetection:
    boundaries: list = field(default_factory=list)
    heading_bbox: list | None = None
    # Unnumbered running text between the heading (if any) and the first
    # numbered note: the continuation of the previous page's last note.
    continuation_bbox: list | None = None
    note_lines: list = field(default_factory=list)
    page_number_bbox: list | None = None
    page_number_text: str | None = None
    warnings: list = field(default_factory=list)

def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def _uppercase_ratio(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(c.isupper() for c in letters) / len(letters)

def _parse_numbered(text):
    """Parse a possible note-start marker.

    Important: a leading parenthesized number is NOT automatically a note
    start. Bibliographic references commonly contain continuation lines such
    as ``(1970), 77-95.``. Those must remain attached to the preceding note.
    """
    cleaned = _clean(text)
    m = NUMBER_RE.match(cleaned)
    if not m or not m.group("body").strip():
        return None

    marker = m.group("marker")
    body = m.group("body").strip()
    digits = re.search(r"\d+", marker)
    if not digits:
        return None
    number = int(digits.group())
    strong = bool(re.search(r"[\[\]()\.)]", marker))

    # A four-digit year in a parenthesized/bracketed citation, especially when
    # followed by punctuation, is normally a continuation of the preceding
    # bibliographic note rather than a new note.  Example from the reported
    # failure: ``(1970), 77-95.``
    if strong and 1500 <= number <= 2099:
        if marker.lstrip().startswith(("(", "[")) and re.match(r"^[,.;:)]", body):
            return None

    # A bare four-digit year followed by prose is also not a reliable note
    # marker. Bare note numbering is handled later only when repeated markers
    # provide enough evidence.
    if not strong and 1500 <= number <= 2099:
        return None

    return number, body, strong

_CONTINUATION_START_RE = re.compile(r"""^[\s"'\u2018\u2019\u201c\u201d(\[]*[a-z]""")


def _looks_like_continuation_start(text):
    """A line that opens with a lowercase word (optionally after quote/
    bracket punctuation - e.g. ``white”: The Clothing of ...``) picks
    up a sentence begun on the previous page. It is never a heading, and a
    notes block that STARTS with such text begins with the continuation of
    the previous page's last note."""
    return bool(_CONTINUATION_START_RE.match(_clean(text)))


def _looks_like_heading(text):
    s = _clean(text)
    if not s:
        return False
    if _looks_like_continuation_start(s):
        return False
    parsed = _parse_numbered(s)
    if parsed:
        s = parsed[1]
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", s)
    if len(words) < 2:
        return False
    upper = _uppercase_ratio(s)
    # All-caps headings and short title-like headings are common in notes.
    titleish = sum(w[0].isupper() for w in words) / len(words)
    return upper >= 0.72 or (titleish >= 0.80 and len(words) <= 14)

def _heading_prefix(lines, zone_height):
    """Return physical heading lines immediately above the first real note."""
    if not lines:
        return []
    first_note = None
    for i, line in enumerate(lines):
        parsed = _parse_numbered(line.text)
        if not parsed:
            continue
        # A numbered all-caps line near the top is more likely a heading than
        # a note (e.g. "1 THE ROOTS OF ..."). Keep scanning for the real note.
        yrel = (line.bbox[1] - lines[0].bbox[1]) / max(zone_height, 1.0)
        if parsed[2] and _looks_like_heading(line.text) and yrel < 0.30:
            continue
        if not parsed[2] and _looks_like_heading(line.text) and yrel < 0.20:
            continue
        first_note = i
        break
    if first_note is None:
        # There may be a heading with no numbered notes; don't manufacture one.
        return []
    candidates = []
    for line in lines[:first_note]:
        if _looks_like_heading(line.text):
            candidates.append(line)
        elif candidates and _clean(line.text):
            # A wrapped heading continuation may not itself be all caps.
            if len(_clean(line.text).split()) <= 12:
                candidates.append(line)
            else:
                break
        elif _clean(line.text):
            # A heading sits at the TOP of the block. Ordinary text before
            # any heading line means the block opens with running text (the
            # continuation of the previous page's last note) - a later
            # title-case line inside that text is not a heading.
            break
    return candidates


def detect(page, zone_bbox, text_extractor):
    raw = text_extractor.extract_lines(page, zone_bbox)
    lines = []
    for bbox, text in raw:
        if not _clean(text):
            continue
        lines.append(DetectedLine(list(bbox), _clean(text)))
    result = SplitDetection()
    if len(lines) < 2:
        result.warnings.append("The selected zone does not contain enough text lines.")
        return result

    y0, y1 = float(zone_bbox[1]), float(zone_bbox[3])
    zone_h = max(1.0, y1 - y0)

    # First identify the bottom printed folio separately.
    for line in reversed(lines):
        rel_bottom = (y1 - line.bbox[3]) / zone_h
        if rel_bottom > 0.86 and PAGE_ONLY_RE.match(line.text):
            result.page_number_bbox = line.bbox
            result.page_number_text = line.text.strip()
            line.kind = "page_number"
            break

    usable = [ln for ln in lines if ln.kind != "page_number"]

    # Detect heading BEFORE accepting numbered note starts.
    heading_lines = _heading_prefix(usable, zone_h)
    if heading_lines:
        result.heading_bbox = [
            min(x.bbox[0] for x in heading_lines),
            min(x.bbox[1] for x in heading_lines),
            max(x.bbox[2] for x in heading_lines),
            max(x.bbox[3] for x in heading_lines),
        ]
        heading_ids = {id(x) for x in heading_lines}
    else:
        heading_ids = set()

    candidates = []
    for line in usable:
        if id(line) in heading_ids:
            continue
        parsed = _parse_numbered(line.text)
        if parsed:
            number, body, strong = parsed
            line.marker = str(number)
            line.body = body
            candidates.append((line, number, strong))

    # Bare "1 text" is accepted only when there is a repeated numbered-note
    # pattern; this avoids splitting ordinary prose/year lines.
    if candidates:
        strong_count = sum(1 for _, _, strong in candidates if strong)
        if strong_count == 0 and len(candidates) < 2:
            candidates = []
        elif strong_count == 0:
            nums = [n for _, n, _ in candidates]
            if len(set(nums)) != len(nums):
                candidates = []

    if not candidates:
        result.warnings.append("No reliable numbered-note starts were detected.")
        return result

    # The first candidate is the first note. Require actual body text and
    # ignore isolated numeric lines that are too close to the zone bottom.
    note_candidates = []
    for line, number, strong in candidates:
        if line.bbox[1] >= y1 - zone_h * 0.18 and PAGE_ONLY_RE.match(line.text):
            continue
        note_candidates.append((line, number, strong))

    if not note_candidates:
        result.warnings.append("No numbered note starts remain after page-number filtering.")
        return result

    result.note_lines = [line for line, _, _ in note_candidates]

    # Leading continuation text (spec: cross-page endnote continuation -
    # "white”: The Clothing of the Sixteenth-Century English Book ..."
    # at the top of page 138, BEFORE "11. Ann Rosalind Jones ..."). It
    # belongs to the previous page's last note, so it must become its OWN
    # piece - previously it was left in the same piece as the first
    # numbered note (no boundary above the first note), so a Merge Previous
    # on that piece pulled note 11 into note 10.
    first_note_top = note_candidates[0][0].bbox[1]
    lead = [ln for ln in usable
            if id(ln) not in heading_ids and ln.bbox[3] <= first_note_top + 1.0 and _clean(ln.text)]
    if lead:
        result.continuation_bbox = [
            min(x.bbox[0] for x in lead), min(x.bbox[1] for x in lead),
            max(x.bbox[2] for x in lead), max(max(x.bbox[3] for x in lead), first_note_top),
        ]

    # A heading is a separate child region. Its lower edge becomes the first
    # split boundary. Subsequent note starts become note boundaries. The folio
    # starts a final page-number child when present.
    boundaries = []
    if result.heading_bbox:
        hb = result.heading_bbox[3]
        if y0 < hb < y1:
            boundaries.append(hb)

    if result.continuation_bbox:
        by = first_note_top
        if y0 < by < y1:
            boundaries.append(by)

    for line, _, _ in note_candidates[1:]:
        by = line.bbox[1]
        if y0 < by < y1:
            boundaries.append(by)

    if result.page_number_bbox:
        py = result.page_number_bbox[1]
        if y0 < py < y1:
            boundaries.append(py)

    # Remove near-duplicates; a split only makes sense for a meaningful
    # vertical separation.
    boundaries = sorted(boundaries)
    filtered = []
    for b in boundaries:
        if not filtered or b - filtered[-1] >= 1.0:
            filtered.append(b)
    result.boundaries = filtered

    if not result.boundaries:
        result.warnings.append("No split boundaries were required.")
    return result
