"""Paragraph-aware layer for the Verification window (Pass 3): a "logical
paragraph" can legitimately span MULTIPLE zones (via the existing Merge
Previous mechanism - core.zone_manager.ZoneManager.merge_with_previous),
and zone boundaries are NOT paragraph boundaries. This module does not
invent a new grouping algorithm - it calls the exact two functions
core.hierarchy.build_document_tree already uses at real XHTML-generation
time (reading_order.flatten_document_order + paragraph_merge.
merge_top_level_stream), so a paragraph id assigned here always matches
the SAME grouping generation will actually use. It also does not
resurrect automatic geometry-based paragraph-boundary detection -
paragraph_merge.should_continue is deliberately disabled in this
codebase (a prior decision documented in that module); find_merge_
candidate_issues below only ever SUGGESTS a reviewable issue using the
same text-only evidence (continuation_confidence) the manual Merge
Previous confirmation dialog already uses, never merges automatically.

core.verification.span_model's own Span concept is unchanged by this
module - a Span is still scoped to one zone's own extracted text; this
module composes multiple zones' own Span lists together, in document
order, purely for paragraph-aware display/navigation/edit-routing. No
new data is persisted - everything here is computed on demand from
existing zone/attribute state, so the project file format is untouched."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core import reading_order, paragraph_merge
from core.verification import role_detector, span_model as sm

# A zone's own text vocabulary key that means "flowing continuation text,
# not a separate block" - matches core.epub_xml_generator.py's own
# "NESTED ZONE / INLINE ZONE SUPPORT" scoping ("Scoped to 'p' only: every
# OTHER tag with real structural children (list, title-group, boxed-
# text, ...) keeps its EXISTING, unchanged behavior"). Reused here
# verbatim rather than re-deriving a second tag rule.
_INLINE_SPLICE_TAG = "p"


@dataclass
class ParagraphSegment:
    zone_id: str
    start: int   # offset into the paragraph's own joined plain text
    end: int
    editable: bool  # False for a spliced page-number/footnote placeholder


@dataclass
class ParagraphIndex:
    zone_to_paragraph: Dict[str, str] = field(default_factory=dict)
    paragraph_zone_ids: Dict[str, List[str]] = field(default_factory=dict)
    paragraph_order: List[str] = field(default_factory=list)

    def paragraph_of(self, zone_id: str) -> Optional[str]:
        return self.zone_to_paragraph.get(zone_id)

    def is_multi_zone(self, paragraph_id: str) -> bool:
        return len(self.paragraph_zone_ids.get(paragraph_id, [])) > 1


def leaf_descendant_ids(zone_manager, zone_id: str) -> List[str]:
    """Recursively resolves zone_id down to its ordered LEAF zone ids (a
    zone with no children is its own single leaf). Confirmed real-data
    bug this exists for: a "p" zone can have several geometrically-
    nested CHILD zones whose combined bboxes exactly tile the parent's
    own (much larger) bbox - core.epub_xml_generator.py's own "NESTED
    ZONE / INLINE ZONE SUPPORT" already splices these children INLINE
    into the parent's own generated text at generation time. Extracting
    the PARENT zone's own text directly (its bbox spans everything) is
    technically accurate to what's there, but it silently swallows every
    child zone's own individual identity - exactly what made verification
    impossible for a 28-zone front-matter page (one giant unstructured
    blob instead of 28 individually addressable blocks)."""
    zone = zone_manager.zones.get(zone_id)
    if zone is None or not zone.children:
        return [zone_id]
    result = []
    for cid in reading_order.sort_children_ids(zone_manager, zone_id):
        result.extend(leaf_descendant_ids(zone_manager, cid))
    return result


def _paragraph_units_for_zone(zone_manager, zone_id: str) -> List[List[str]]:
    """Recursively resolves ONE stream entry into the list of
    independently-addressable "paragraph units" verification should
    show. A zone with no children is already its own leaf - one
    single-member unit, unchanged from pre-Pass-3 behavior. A "p" zone
    WITH children is generation's own "inline-splice" convention (see
    leaf_descendant_ids) - ONE joined, multi-segment paragraph unit, its
    children individually addressable/navigable within it. Any OTHER
    tag with children (list, boxed-text, title-group, table, ...) is a
    genuine STRUCTURAL container at generation time - each child becomes
    its own INDEPENDENT element there (never spliced text), so each
    becomes its own independent paragraph unit here too (recursing, in
    case a child is itself a "p"-with-children or another container)."""
    zone = zone_manager.zones.get(zone_id)
    if zone is None or not zone.children:
        return [[zone_id]]
    if zone.tag == _INLINE_SPLICE_TAG:
        return [leaf_descendant_ids(zone_manager, zone_id)]
    units = []
    for cid in reading_order.sort_children_ids(zone_manager, zone_id):
        units.extend(_paragraph_units_for_zone(zone_manager, cid))
    return units


def _expand_merged_chain(zone_manager, zone_ids: List[str]) -> List[str]:
    """For an EXPLICIT Merge Previous chain of top-level zones: splices
    any member's own "p"+children nested runs inline at that member's
    position (matching generation's own splicing - see
    leaf_descendant_ids), producing ONE ordered leaf-zone list for the
    whole chain. A chain member is never expanded into SEPARATE
    paragraphs here - it was already linked into this one chain by an
    explicit human action (Merge Previous), which always wins over the
    nested-children default."""
    expanded = []
    for zid in zone_ids:
        zone = zone_manager.zones.get(zid)
        if zone is not None and zone.children and zone.tag == _INLINE_SPLICE_TAG:
            expanded.extend(leaf_descendant_ids(zone_manager, zid))
        else:
            expanded.append(zid)
    return expanded


def build_paragraph_index(zone_manager, page_marker_tags=None, footnote_flow_tags=None,
                           non_flow_tags=None) -> ParagraphIndex:
    """Only covers TOP-LEVEL zones and their own descendants
    (flatten_document_order's own top-level scope, matching
    build_document_tree, plus this module's own nested-children
    expansion above) - a zone belonging to some OTHER top-level zone's
    own subtree is covered as part of that ancestor's expansion; callers
    treat a zone genuinely absent from the index (e.g. one dropped by an
    unresolved/invalid parent reference) as "its own single-zone
    paragraph", exactly today's existing single-zone fallback, never an
    error."""
    flat = reading_order.flatten_document_order(zone_manager)
    stream = paragraph_merge.merge_top_level_stream(zone_manager, flat, page_marker_tags,
                                                     footnote_flow_tags, non_flow_tags)
    index = ParagraphIndex()
    counter = 1
    for node in stream:
        if isinstance(node, dict):
            units = [_expand_merged_chain(zone_manager, node["zone_ids"])]
        else:
            units = _paragraph_units_for_zone(zone_manager, node)
        for unit_zone_ids in units:
            pid = f"P{counter:03d}"
            counter += 1
            index.paragraph_zone_ids[pid] = unit_zone_ids
            index.paragraph_order.append(pid)
            for zid in unit_zone_ids:
                index.zone_to_paragraph[zid] = pid
    return index


def _is_boundary_only(zone) -> bool:
    """A page-number or footnote/endnote zone spliced INSIDE a text
    chain (core.paragraph_merge splices these in at their true relative
    position rather than dropping them) - never itself a text-bearing
    continuation member. Reuses role_detector's own tag/cup_name checks
    directly rather than re-deriving a second tag-set test."""
    return role_detector.is_page_number_zone(zone) or role_detector.note_zone_kind(zone) is not None


def _boundary_placeholder_text(zone) -> str:
    """UI-only marker text for a spliced boundary zone inside a joined
    paragraph view - never fed to spelling/content/OCR comparison (it
    exists only within the Span list this function's caller builds for
    on-screen display), matching the app's existing rule that zone/role
    labels are never verification content."""
    if role_detector.is_page_number_zone(zone):
        return f"⟦page {(zone.text or '').strip() or '?'}⟧"
    return "⟦note⟧"


def paragraph_plain_spans(zone_manager, pdf_document, paragraph_id: str, index: ParagraphIndex):
    """Builds the concatenated Span list for one paragraph's ENTIRE chain,
    joining each text-bearing member zone's own extracted formatted text
    with that (later) zone's own merge_join attribute - the exact same
    field/value core.epub_xml_generator._gen_merged_text_zone already
    joins with at generation time, so this preview and the real XHTML
    output are provably built from the same join, not two divergent
    implementations. A member from an EXPLICIT merge chain always has
    this attribute set (merge_with_previous always writes it, either
    " " or ""), so its own real stored value is used, never the
    fallback below. A member that only exists here because it's a
    nested-children sibling (see _paragraph_units_for_zone) never has
    this attribute at all - those default to "\\n" (each sibling on its
    own line - spec: "If source zoning/metadata says two blocks are
    separate paragraphs: render them separately", never silently
    concatenated into one run-on blob), NOT a plain space. A spliced
    page-number/footnote zone becomes a non-editable placeholder segment
    instead of joined text (see _boundary_placeholder_text). Returns
    (spans, segments) where segments is a ParagraphSegment list in the
    same order, offsets matching the concatenated plain text of
    `spans`."""
    from core.text_extractor import extract_zone_formatted_text

    zone_ids = index.paragraph_zone_ids.get(paragraph_id, [])
    spans: List[sm.Span] = []
    segments: List[ParagraphSegment] = []
    pos = 0
    seen_text_member = False
    for zid in zone_ids:
        zone = zone_manager.zones.get(zid)
        if zone is None:
            continue
        if _is_boundary_only(zone):
            placeholder = _boundary_placeholder_text(zone)
            spans.append(sm.Span(placeholder))
            segments.append(ParagraphSegment(zid, pos, pos + len(placeholder), editable=False))
            pos += len(placeholder)
            continue
        if seen_text_member:
            join = zone.attributes.get("merge_join", "\n")
            if join:
                spans.append(sm.Span(join))
                pos += len(join)
        seen_text_member = True
        start = pos
        page = pdf_document.get_page(zone.page)
        tagged = extract_zone_formatted_text(page, zone)
        for span in sm.parse_tagged_text(tagged):
            spans.append(span)
            pos += len(span.text)
        segments.append(ParagraphSegment(zid, start, pos, editable=True))
    return spans, segments


def segment_for_offset(segments: List[ParagraphSegment], offset: int) -> Optional[ParagraphSegment]:
    for seg in segments:
        if seg.start <= offset < seg.end:
            return seg
    if segments and offset == segments[-1].end:
        return segments[-1]
    return None


def segment_spanning_range(segments: List[ParagraphSegment], start: int, end: int) -> Optional[ParagraphSegment]:
    """Returns the ONE segment that fully contains [start, end), or None
    if the range crosses a zone-segment seam (spans 0-1 segments only,
    never 2+) - callers use None to refuse an edit/style-apply that would
    otherwise straddle two different zones' own independently-stored
    text, per this pass's own explicit scope boundary (no full multi-
    zone contiguous editor)."""
    seg = segment_for_offset(segments, start)
    if seg is None or end > seg.end:
        return None
    return seg


def segments_intersecting_range(segments: List[ParagraphSegment], start: int, end: int):
    """Returns [(segment, local_start, local_end), ...] for every
    EDITABLE segment [start, end) actually overlaps, each with the
    overlap clipped to that segment's own zone-local offsets - used for
    STYLE APPLICATION, which (unlike a raw text edit) can safely span
    multiple underlying zones: applying "italic" to each affected zone's
    own sub-range independently produces the exact same combined visual
    result as one operation would, with no ambiguity about which zone's
    text changed (a real text edit has no such safe per-zone split,
    which is why segment_spanning_range above still refuses one - this
    function is deliberately NOT used for text edits). A non-editable
    placeholder segment (spliced page-number/footnote marker) inside the
    range is silently skipped, never styled and never a reason to refuse
    the rest of the selection."""
    result = []
    for seg in segments:
        if not seg.editable:
            continue
        overlap_start, overlap_end = max(start, seg.start), min(end, seg.end)
        if overlap_start < overlap_end:
            result.append((seg, overlap_start - seg.start, overlap_end - seg.start))
    return result


_MERGE_CANDIDATE_THRESHOLD = 0.8  # same territory as the manual Merge Previous dialog's own confidence display


def find_merge_candidate_issues(pdf_document, zone_manager, page_marker_tags=None,
                                  footnote_flow_tags=None, non_flow_tags=None):
    """Suggests (NEVER applies) a likely-missed paragraph continuation
    between two adjacent, same-tag, not-already-linked top-level zones -
    completes the item Pass 2 deferred. Uses ONLY the same text-only
    evidence (paragraph_merge.continuation_confidence) the manual Merge
    Previous confirmation dialog already uses; never geometry, never
    auto-merges (paragraph_merge.should_continue is deliberately disabled
    in this codebase - see that module's own docstring). Returns a list
    of core.verification.verification_models.VerificationIssue (STRUCTURE,
    REVIEW status - never AUTO_FIX_AVAILABLE, since merging is a
    structural decision the operator must confirm via the existing
    Merge Zones quick action, not a one-click text fix)."""
    from core.text_extractor import extract_zone_formatted_text, strip_tags_to_plain
    from core.verification import verification_models as vm

    flat = reading_order.flatten_document_order(zone_manager)
    issues = []
    for i in range(1, len(flat)):
        zid = flat[i]
        zone = zone_manager.zones[zid]
        prev_id = flat[i - 1]
        prev_zone = zone_manager.zones[prev_id]
        if zone.attributes.get("merged_with_previous"):
            continue  # already linked - nothing to suggest
        if zone.tag != prev_zone.tag:
            continue
        if _is_boundary_only(zone) or _is_boundary_only(prev_zone):
            continue
        prev_text = strip_tags_to_plain(extract_zone_formatted_text(pdf_document.get_page(prev_zone.page), prev_zone))
        cur_text = strip_tags_to_plain(extract_zone_formatted_text(pdf_document.get_page(zone.page), zone))
        score = paragraph_merge.continuation_confidence(prev_text, cur_text)
        if score < _MERGE_CANDIDATE_THRESHOLD:
            continue
        issue = vm.VerificationIssue(
            id=f"merge-candidate-{prev_id}-{zid}",
            issue_type=vm.STRUCTURE,
            severity="MEDIUM",
            status=vm.REVIEW,
            page=zone.page,
            zone_id=zid,
            source_text=cur_text,
            extracted_text=cur_text,
            confidence="HIGH" if score >= 0.95 else "MEDIUM",
            confidence_score=score,
            source_evidence="text continuity (paragraph_merge.continuation_confidence)",
            explanation=f'Zone {prev_id} ends and zone {zid} begins in a way that looks like one continuing '
                        f'paragraph, but they are not currently linked via Merge Previous.',
        )
        # The GUI's own merge quick action re-derives "the previous zone"
        # itself via ZoneManager.merge_with_previous's existing reading-
        # order lookup when the operator clicks Merge - no need to stash
        # prev_id on the issue (it also wouldn't survive to_dict/from_dict
        # round-tripping, since VerificationIssue enumerates its own
        # fields explicitly - this issue type deliberately carries none
        # of its own beyond what VerificationIssue already defines).
        issues.append(issue)
    return issues
