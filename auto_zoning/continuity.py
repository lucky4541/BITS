"""Cross-page CONTINUATION detection for Auto Zone.

A paragraph (or a reference entry / endnote) that runs over a page break
is zoned as two zones - the last one on page N and the first one on page
N+1. This module decides, from several independent signals, whether the
second zone continues the first, and if so records it as an ordinary
Merge Previous (ZoneManager.merge_with_previous): the zone turns yellow,
"Unmerge" undoes it, and XHTML generation combines the two only because
the merge is recorded on the zone (generation itself never guesses).

The decision is a weighted evidence score (log-odds style: every signal
adds or subtracts, the sum is turned into a 0..1 confidence) instead of a
single rule, and every decision keeps its reasons:

  paragraph flow (p / blockquote / list text ...)
    + previous text ends without sentence-final punctuation      (+2.0)
    + previous text ends with a line-break hyphen                (+2.5)
    + next text starts with a lowercase letter                   (+2.0)
    + next zone's first line is NOT indented, in a book whose paragraphs
      ARE indented                                                (+1.0)
    - next zone's first line carries the paragraph indent        (-2.0)
    + previous zone's last line runs to the full measure          (+0.8)
    - previous zone's last line is short (paragraph ended)       (-1.5)
  reference / note flow (ref_n, ref_d, en, fn ...)
    - next text starts with an entry / note number               (-4.0)
    - next text starts like a new author-date entry "Surname, X" (-2.0)
    + next text starts with a lowercase letter                   (+2.5)
    + next zone's first line sits at the hanging-indent position (+1.5)
    + previous text ends without final punctuation               (+1.0)
  always required: same tag, same column (left edges within tolerance).

Merged when the confidence reaches MERGE_CONFIDENCE.
"""
import math
import re
from dataclasses import dataclass, field

MERGE_CONFIDENCE = 0.80
X_TOLERANCE = 24.0
_END_PUNCT_RE = re.compile(r"[.!?…][\"'’”)\]]*(\s*\d{1,3})?\s*$")
_HYPHEN_END_RE = re.compile(r"[A-Za-z]-\s*$")
_LOWER_START_RE = re.compile(r"^[\s\"'‘“(\[]*[a-z]")
_ENTRY_NUM_RE = re.compile(r"^\s*(\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)]?\s|[*†‡§¶]\s)")
_AUTHOR_START_RE = re.compile(r"^\s*[A-Z][\w'’\-]+(?:\s[A-Z][\w'’\-]+)?,\s+[A-Z]")
_HEADING_TAG_RE = re.compile(r"^(h\d|.*head(ing)?|title|.*title)$", re.IGNORECASE)


@dataclass
class ContinuationDecision:
    prev_id: str
    zone_id: str
    confidence: float
    merge: bool
    reasons: list = field(default_factory=list)


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _is_reference_flow(tag: str, footnote_flow_tags=()) -> bool:
    t = (tag or "").lower()
    if t in {x.lower() for x in (footnote_flow_tags or ())}:
        return True
    return t.startswith(("ref_", "ref", "bib")) or t in ("en", "fn", "endnote", "footnote")


def _zone_lines(pdf, zone, cache):
    """PDF text lines inside the zone (x0, y0, x1, y1), top to bottom -
    measured from visible words (section_context.page_text_lines)."""
    from auto_zoning.section_context import page_text_lines
    key = zone.page
    if key not in cache:
        try:
            cache[key] = [ln[:4] for ln in page_text_lines(pdf.get_page(zone.page))]
        except Exception:
            cache[key] = []
    x0, y0, x1, y1 = zone.bbox
    pad = 6.0
    inside = [r for r in cache[key] if r[1] >= y0 - 2 and r[3] <= y1 + 2 and r[0] >= x0 - pad and r[2] <= x1 + pad]
    return sorted(inside, key=lambda r: (r[1], r[0]))


def score_pair(prev, nxt, pdf=None, paragraph_indent=0.0, body_size=10.0, footnote_flow_tags=(), line_cache=None):
    """ContinuationDecision for zone `nxt` continuing zone `prev`."""
    reasons = []
    prev_text = (prev.text or "").strip()
    next_text = (nxt.text or "").strip()
    if prev.tag != nxt.tag:
        return ContinuationDecision(prev.zone_id, nxt.zone_id, 0.0, False, ["different tags"])
    if not prev_text or not next_text:
        return ContinuationDecision(prev.zone_id, nxt.zone_id, 0.0, False, ["no text to compare"])
    if abs(prev.bbox[0] - nxt.bbox[0]) > X_TOLERANCE:
        return ContinuationDecision(prev.zone_id, nxt.zone_id, 0.0, False, ["different column"])
    cache = line_cache if line_cache is not None else {}
    prev_lines = _zone_lines(pdf, prev, cache) if pdf is not None else []
    next_lines = _zone_lines(pdf, nxt, cache) if pdf is not None else []
    ends_open = not _END_PUNCT_RE.search(prev_text)
    hyphen = bool(_HYPHEN_END_RE.search(prev_text))
    lower = bool(_LOWER_START_RE.match(next_text))
    s = -1.0
    if _is_reference_flow(nxt.tag, footnote_flow_tags):
        # hanging layout: a first line AT the text column (right of where the previous entry's
        # number / first line started) is a turn-over line, even when it begins with a number
        # ("1898 in a letter ...") - only an outdented line can open a new entry
        at_text_column = bool(prev_lines and next_lines and
                              next_lines[0][0] > prev_lines[0][0] + 0.5 * body_size)
        if _ENTRY_NUM_RE.match(next_text) and not at_text_column:
            s -= 4.0
            reasons.append("next starts with an entry/note number (new entry)")
        elif _AUTHOR_START_RE.match(next_text):
            s -= 2.0
            reasons.append("next starts like a new author-date entry")
        if lower:
            s += 2.5
            reasons.append("next starts lowercase")
        if ends_open:
            s += 1.0
            reasons.append("previous entry has no final punctuation")
        if hyphen:
            s += 1.5
            reasons.append("previous ends with a broken word")
        if at_text_column:
            s += 1.5
            reasons.append("next first line sits at the hanging-indent position")
    else:
        if hyphen:
            s += 2.5
            reasons.append("previous ends with a broken word")
        elif ends_open:
            s += 2.0
            reasons.append("previous text stops mid-sentence")
        if lower:
            s += 2.0
            reasons.append("next starts lowercase")
        if next_lines:
            indent = next_lines[0][0] - min(r[0] for r in next_lines)
            if paragraph_indent > 0 and len(next_lines) >= 2:
                if indent >= 0.5 * paragraph_indent:
                    s -= 2.0
                    reasons.append(f"next first line indented {indent:.0f}pt (new paragraph)")
                else:
                    s += 1.0
                    reasons.append("next first line not indented where the book indents paragraphs")
        if prev_lines:
            measure = max(r[2] for r in prev_lines) - min(r[0] for r in prev_lines)
            last = prev_lines[-1]
            fill = (last[2] - min(r[0] for r in prev_lines)) / measure if measure > 0 else 1.0
            if len(prev_lines) >= 2:
                if fill >= 0.9:
                    s += 0.8
                    reasons.append("previous last line runs to the full measure")
                elif fill < 0.75:
                    s -= 1.5
                    reasons.append("previous last line is short (paragraph ended)")
    conf = _sigmoid(s)
    return ContinuationDecision(prev.zone_id, nxt.zone_id, round(conf, 3), conf >= MERGE_CONFIDENCE, reasons)


def link_page_boundary(zone_manager, pdf, page: int, profile: dict, paragraph_indent=0.0, body_size=10.0,
                       apply=True, line_cache=None):
    """Checks whether the first content zone of `page` continues the last
    content zone before it (previous page) and records the merge.
    Returns the ContinuationDecision (or None when there is no candidate
    pair). Already-merged zones and protected/manual decisions are left
    alone."""
    page_marker_tags = profile.get("page_marker_tags")
    footnote_flow_tags = profile.get("footnote_flow_tags") or ()
    non_flow_tags = profile.get("non_flow_tags")
    zones = zone_manager.get_logical_top_level_zones(page)
    skip = set(page_marker_tags or ()) | set(non_flow_tags or ())
    flows = {}
    for z in zones:
        if z.tag in skip:
            continue                                   # page numbers / images never start a flow
        flow = "notes" if z.tag in footnote_flow_tags else "main"
        flows.setdefault(flow, z)                      # first zone of each flow on the page
    # a page whose flow opens with a heading starts something new
    flows = {k: z for k, z in flows.items() if not _HEADING_TAG_RE.match(z.tag or "")}
    decisions = []
    for flow, first in flows.items():
        if first.attributes.get("merged_with_previous") or first.attributes.get("continuation_rejected"):
            continue
        prev_id = zone_manager._find_previous_in_reading_order(first.zone_id, page_marker_tags,
                                                               footnote_flow_tags, non_flow_tags)
        prev = zone_manager.zones.get(prev_id) if prev_id else None
        if prev is None or prev.page == first.page:
            continue
        d = score_pair(prev, first, pdf, paragraph_indent, body_size, footnote_flow_tags, line_cache)
        decisions.append(d)
        if d.merge and apply:
            ok, _msg, _ = zone_manager.merge_with_previous(first.zone_id, " ", page_marker_tags,
                                                           footnote_flow_tags, non_flow_tags)
            if ok:
                first.attributes["auto_continuation"] = round(d.confidence * 100)
                first.attributes["auto_continuation_reason"] = "; ".join(d.reasons)
    return decisions
