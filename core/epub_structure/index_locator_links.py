"""Step (spec: "FIX INDEX PAGE-LOCATOR DETECTION - DOM/SEMANTIC AWARE").

The forbidden algorithm is "number found inside index entry = page
locator". This module classifies every numeric token inside a real
<li epub:type="index-entry"> by its DOM ROLE before ever considering it
a candidate INDEX_PAGE_LOCATOR / INDEX_PAGE_RANGE - a page-registry match
alone is never enough (spec 3/13).

DOM-aware scope, deliberately conservative rather than exhaustive:
  - Text that is a DIRECT child of the <li> itself - i.e. `li.text` and
    the `.tail` of each of its direct child elements - is the entry's own
    FLOW text, the only place a locator candidate can ever be found.
  - Any number whose OWN text lives INSIDE an inline child element
    (<sup>/<sub>/<i>/<b>/<em>/<strong>/<span>, or an existing <a>) is
    never scanned for locator candidacy at all - spec 3/4 single out
    <sup>/<sub> by name as "content by default" (C<sup>60</sup>,
    CO<sub>2</sub>), and every one of the spec's own chemical/
    mathematical examples (section 17) is expressed via <sup>/<sub>
    markup; this module does not attempt independent chemical/
    mathematical/formula parsing beyond that structural signal.
  - Within the FLOW text of one run, a locator candidate is only ever the
    TRAILING comma/"and"-separated list of bare integers/ranges anchored
    at the very END of that run (spec 6/7: "TERM [SUBTERM] LOCATOR(S)",
    never "last number = page" blindly) - a run with no such trailing
    region (e.g. "C" before a <sup>, or "... nanotubes" with no digits at
    the end) yields zero candidates.
  - A bracketed/parenthesised number ("(1.1)", "[60]") can never satisfy
    the trailing-region anchor (its own closing ")"/"]" is not among the
    allowed trailing whitespace/punctuation), so a reference-style
    citation is naturally never mistaken for a locator without a separate
    REFERENCE_NUMBER classifier - disclosed here rather than duplicated.
  - "see"/"see also" immediately before a numeric run is left for REVIEW,
    never auto-linked (spec 19).
  - An existing <a> (including an already-explicit
    epub:type="index-locator") is NEVER re-classified or unwrapped here -
    core.epub_structure.link_resolver already repairs/validates ANY
    existing href generically; this module only counts, after that pass
    has run, which index-locator anchors are valid/auto-fixed/broken (see
    classify_existing_locators)."""
import posixpath
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.xhtml_parser import EPUB_NS, XHTML_NS, epub_type, local_name

_EPUB_TYPE_ATTR = f"{{{EPUB_NS}}}type"
_SUP_SUB = ("sup", "sub")
_OPAQUE_INLINE = ("sup", "sub", "i", "b", "em", "strong", "span", "a")

_LOCATOR_ITEM_RE = re.compile(r"\d+(?:[-–—]\d+)?")
_RANGE_INNER_RE = re.compile(r"(\d+)([-–—])(\d+)")
_TRAILING_ALLOWED_RE = re.compile(r"[\s.,;]*")
_GAP_RE = re.compile(r"^\s*(?:,|and)\s*$", re.IGNORECASE)
_SEE_RE = re.compile(r"\bsee(?:\s+also)?\s*$", re.IGNORECASE)


@dataclass
class LinkResult:
    candidates_detected: int = 0
    confirmed_locators: int = 0
    ranges: int = 0
    abbreviated_ranges: int = 0
    created: int = 0
    already_valid: int = 0
    auto_fixed: int = 0
    broken: int = 0
    review: list = field(default_factory=list)   # (doc_path, text, reason)
    superscript_ignored: int = 0
    subscript_ignored: int = 0
    chemical_ignored: int = 0        # subsumed by superscript/subscript - see module docstring
    mathematical_ignored: int = 0    # subsumed by superscript/subscript - see module docstring
    reference_ignored: int = 0       # naturally excluded by the trailing-region anchor - see module docstring
    other_content_ignored: int = 0
    new_anchors: set = field(default_factory=set, repr=False)   # internal - anchors THIS run created, not report data


def _page_index(registry) -> dict:
    """label -> (doc, Pagebreak) book-wide - a label duplicated across the
    book is intentionally excluded (ambiguous, never guessed)."""
    counts = {}
    by_label = {}
    for doc in registry.documents:
        for pb in doc.pagebreaks:
            if not pb.label or not pb.id:
                continue
            counts[pb.label] = counts.get(pb.label, 0) + 1
            by_label[pb.label] = (doc, pb)
    return {label: entry for label, entry in by_label.items() if counts[label] == 1}


def _resolve_abbreviated_end(start_num: str, end_num: str) -> str:
    """"45-7" -> end resolves to "47"; "145-7" -> "147"; a full-length end
    ("154-155") is returned unchanged - never re-interpreted."""
    if len(end_num) >= len(start_num):
        return end_num
    prefix_len = len(start_num) - len(end_num)
    return start_num[:prefix_len] + end_num


def _find_trailing_locator_region(text: str):
    """Returns (region_start, [item_match, ...]) for the TRAILING comma/
    "and"-separated list of bare integer/range items in `text`, or None if
    `text` has no such region. The region must run to the very end of
    `text` (only whitespace/.,; allowed after the last item, spec 7) - a
    number not anchored at the end of its own flow-text run is never a
    locator candidate at all."""
    items = list(_LOCATOR_ITEM_RE.finditer(text))
    if not items:
        return None
    if not _TRAILING_ALLOWED_RE.fullmatch(text[items[-1].end():]):
        return None
    kept = [items[-1]]
    for m in reversed(items[:-1]):
        gap = text[m.end():kept[0].start()]
        if _GAP_RE.match(gap):
            kept.insert(0, m)
        else:
            break
    return kept[0].start(), kept


def _resolve_items(items, text: str, doc_path: str, by_label: dict, result: LinkResult):
    """Returns a list of (start, end, target_doc_path, target_id) atomic
    spans - one per real page number (a range contributes two, its start
    and its resolved end, with the dash between them left as plain text).
    Every item is judged independently (spec: prefer REVIEW over a wrong
    link, never all-or-nothing for the whole trailing region)."""
    spans = []
    for m in items:
        result.candidates_detected += 1
        range_m = _RANGE_INNER_RE.match(text, m.start())
        if range_m:
            start_num, dash, end_raw = range_m.group(1), range_m.group(2), range_m.group(3)
            end_num = _resolve_abbreviated_end(start_num, end_raw)
            start_entry = by_label.get(start_num)
            end_entry = by_label.get(end_num)
            if start_entry is None or end_entry is None:
                result.review.append((doc_path, m.group(0), "range endpoint has no matching pagebreak (or is ambiguous)"))
                continue
            spans.append((range_m.start(1), range_m.end(1), start_entry[0].path, start_entry[1].id))
            spans.append((range_m.start(3), range_m.end(3), end_entry[0].path, end_entry[1].id))
            result.ranges += 1
            if end_raw != end_num:
                result.abbreviated_ranges += 1
        else:
            entry = by_label.get(m.group(0))
            if entry is None:
                result.review.append((doc_path, m.group(0), "no pagebreak with this label exists anywhere in the book (or is ambiguous)"))
                continue
            spans.append((m.start(), m.end(), entry[0].path, entry[1].id))
            result.confirmed_locators += 1
    return spans


def _apply_spans(el, doc_path: str, spans, new_anchors: set):
    text = el.text
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        anchor.set(_EPUB_TYPE_ATTR, "index-locator")
        rel_href = ("" if target_doc_path == doc_path else posixpath.basename(target_doc_path)) + f"#{target_id}"
        anchor.set("href", rel_href)
        anchor.text = text[start:end]
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
        new_anchors.add(anchor)
    el.text = leading
    for offset, anchor in enumerate(nodes):
        el.insert(offset, anchor)


def _apply_tail_spans(el, child, doc_path: str, spans, new_anchors: set):
    text = child.tail
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        anchor.set(_EPUB_TYPE_ATTR, "index-locator")
        rel_href = ("" if target_doc_path == doc_path else posixpath.basename(target_doc_path)) + f"#{target_id}"
        anchor.set("href", rel_href)
        anchor.text = text[start:end]
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
        new_anchors.add(anchor)
    child.tail = leading
    insert_at = el.index(child) + 1
    for offset, anchor in enumerate(nodes):
        el.insert(insert_at + offset, anchor)


def _is_index_entry(el) -> bool:
    return local_name(el.tag) == "li" and epub_type(el) == "index-entry"


def _build_flow(li):
    """Returns (flow_text, segments) - segments is a list of
    (seg_start, seg_end, kind, ref, is_candidate_source) tuples,
    contiguous and covering the whole of flow_text.

    Confirmed against a real production index (9780521554466's own
    back-matter index): a page RANGE's own dash/en-dash is routinely
    italicised as its own element - "166<i>—</i>167" - splitting one
    logical range across TWO physical DOM text nodes (li.text and the
    <i>'s own .tail). A plain <sup>/<sub>/<a> child's own text is a hard
    content boundary (never scanned, flow continues via its .tail only);
    any OTHER inline wrapper (<i>/<b>/<em>/<strong>/<span>) is treated as
    a transparent BRIDGE - its own flattened text is included in the flow
    so a dash-only wrapper like this correctly keeps a range as ONE
    logical expression, but a bridge segment is marked as not itself a
    valid link target (is_candidate_source=False) so an actual digit
    living inside such a wrapper - no real-world evidence of this exists -
    can still never be spliced into a new anchor."""
    parts = []
    segments = []
    pos = 0

    def add(text, kind, ref, is_candidate_source):
        nonlocal pos
        if not text:
            return
        start = pos
        parts.append(text)
        pos += len(text)
        segments.append((start, pos, kind, ref, is_candidate_source))

    add(li.text, "text", None, True)
    for child in list(li):
        cln = local_name(child.tag)
        if cln not in _SUP_SUB and cln != "a":
            add("".join(child.itertext()), "bridge", child, False)
        add(child.tail, "tail", child, True)

    return "".join(parts), segments


def _segment_for(segments, global_pos):
    for seg_start, seg_end, kind, ref, is_candidate in segments:
        if seg_start <= global_pos < seg_end:
            return seg_start, seg_end, kind, ref, is_candidate
    return None


def apply_index_locator_links(registry) -> LinkResult:
    """THE single entry point for CREATING new index-locator links. Never
    touches an existing <a> (of any kind) or descends into any <sup>/
    <sub> child's own text - see module docstring for the exact scope."""
    result = LinkResult()
    by_label = _page_index(registry)

    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        for li in doc.tree.iter():
            if not _is_index_entry(li):
                continue

            for child in li:
                cln = local_name(child.tag)
                if cln in _SUP_SUB:
                    digit_count = len(_LOCATOR_ITEM_RE.findall("".join(child.itertext())))
                    if cln == "sup":
                        result.superscript_ignored += digit_count
                    else:
                        result.subscript_ignored += digit_count

            flow_text, segments = _build_flow(li)
            region = _find_trailing_locator_region(flow_text)
            if region is None:
                continue
            region_start, items = region
            if _SEE_RE.search(flow_text[:region_start]):
                result.candidates_detected += len(items)
                result.review.append((doc.path, flow_text[region_start:],
                                       "'see'/'see also' reference - not auto-linked as a page locator"))
                continue

            spans = _resolve_items(items, flow_text, doc.path, by_label, result)
            if not spans:
                continue

            by_segment = {}   # (kind, ref) -> [(local_start, local_end, target_doc_path, target_id), ...]
            for g_start, g_end, target_doc_path, target_id in spans:
                seg = _segment_for(segments, g_start)
                if seg is None or g_end > seg[1] or not seg[4]:
                    continue   # defensive only - a genuine digit run never spans a bridge boundary in practice
                seg_start, _seg_end, kind, ref, _ = seg
                by_segment.setdefault((kind, ref), []).append(
                    (g_start - seg_start, g_end - seg_start, target_doc_path, target_id))

            for (kind, ref), local_spans in by_segment.items():
                local_spans.sort(key=lambda s: s[0])
                if kind == "text":
                    _apply_spans(li, doc.path, local_spans, result.new_anchors)
                else:
                    _apply_tail_spans(li, ref, doc.path, local_spans, result.new_anchors)
                result.created += len(local_spans)
                changed_here = True

        if changed_here:
            doc.dirty = True
    return result


def classify_existing_locators(registry, link_resolution, result: LinkResult) -> None:
    """Second pass, run AFTER core.epub_structure.link_resolver has
    already validated/repaired every href in the book (spec 12: preserve
    and validate an existing explicit index-locator, never re-derive its
    own repair logic here). Fills in result.already_valid/auto_fixed/
    broken by cross-referencing the SAME live elements link_resolver's own
    LinkResolution already classified - never a second, divergent repair
    pass.

    Skips every anchor in result.new_anchors - an index-locator this SAME
    run just created (it also carries epub:type="index-locator", so
    without this exclusion a freshly-created link would double-count
    itself as "already valid" on top of already being counted in
    result.created)."""
    fixed_elements = {issue.element for issue in link_resolution.fixed}
    review_elements = {issue.element for issue in link_resolution.review}
    for doc in registry.documents:
        if doc.tree is None:
            continue
        for el in doc.tree.iter():
            if el in result.new_anchors:
                continue
            if local_name(el.tag) != "a" or epub_type(el) != "index-locator":
                continue
            if el in review_elements:
                result.broken += 1
            elif el in fixed_elements:
                result.auto_fixed += 1
            else:
                result.already_valid += 1
