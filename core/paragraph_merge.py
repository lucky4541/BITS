"""Collapses runs of top-level Paragraph (and other text-bearing) zones that
are logically ONE unit into a single merged-paragraph tree node, from TWO
independent sources:

1. Automatic: a page/column boundary marked ONLY by non-content boundary
   zones - Page Number markers and/or footnote/endnote references - must
   never silently split one logical element into two (spec: "A footnote
   appearing between two fragments of a paragraph must not automatically
   terminate the paragraph"; "PageNum MUST NEVER participate in text
   merging"). Restricted to same-tag pairs from _AUTO_CONTINUATION_TAGS
   (currently "p" and "reference" - a Bibliography Reference that continues
   onto the next page, spec Pattern 16, is the same shape of problem as a
   wrapped paragraph), a same-column (X-tolerance) heuristic, and a genuine
   PAGE transition. Two evidence tiers, deliberately NOT treated as equally
   strong (spec: "explicit structural relationship > physical proximity",
   and Priority 5 text continuity is only ever SUPPORTING evidence):
     - Page-Number-flanked: the higher-confidence case - a printed page
       number is essentially never anything OTHER than a page/column
       boundary marker, so structural adjacency (same tag, same column,
       real page break) is sufficient evidence on its own, same as before
       this module's footnote handling existed.
     - Footnote-only gap (no page-number zone at all between them): a
       WEAKER structural signal on its own (nothing rules out two
       genuinely independent same-tag zones just happening to share a
       column across a page break - very common in a single-column book
       layout), so this branch additionally REQUIRES text-continuity
       evidence (_looks_like_continuation: the earlier zone's text doesn't
       end with sentence-ending punctuation, or the later zone's text
       starts lowercase) before firing - see spec Priority 4 (structural/
       reading-order continuity) combined with Priority 5 (text
       continuity) as supporting evidence, never tag-adjacency alone
       (spec section 5: "Two consecutive <p> zones do NOT automatically
       mean they are the same paragraph").

   page_marker_tags/footnote_flow_tags are PROFILE-DRIVEN (the exact same
   sets core.zone_manager.ZoneManager.merge_with_previous already uses for
   the identical purpose - see gui/main_window.py's _merge_skip_tags),
   never a hardcoded literal tag name. This matters: a hardcoded
   zone.tag == "pagenumber" check here would silently never fire for
   CUPEPUB, whose own PageNum zone is tagged "pagenum" (a different
   string, confirmed directly via core.profile_manager.get_profile
   ("CUPEPUB")["page_marker_tags"] == ["pagenum"]) - meaning the ENTIRE
   automatic continuation feature was silently dead for CUPEPUB documents
   before this parameter existed, despite being built and documented as a
   CUPEPUB-relevant feature. Every caller that passes neither argument
   (nothing outside gui/main_window.py's own generation call sites, which
   now thread the active profile's own tag sets through) sees the exact
   same default behavior ({"pagenumber"} / no footnote tags) as before
   this generalization.

2. Explicit: the user's "Merge Previous" action, recorded as
   zone.attributes["merged_with_previous"] / ["merge_target"] on the LATER
   zone (core/zone_manager.py ZoneManager.merge_with_previous). This is a
   LOGICAL merge only - neither zone's bbox, parent_id, children, page, or
   tag is ever touched here or anywhere in this module; both zones remain
   fully independent physical zones in ZoneManager's data model. The merge
   is AUTHORITATIVE (spec: "explicit structural relationship > physical
   proximity" - this IS that explicit relationship, spec section 2's
   "SPLIT-PARENT identity" concept, implemented via merge_target rather
   than a separate parallel field): it applies regardless of intervening
   page-number (or other) zones, differing bboxes/pages/columns/X-
   positions, or (per the existing application's tag rules) differing
   tags - the target is found by its declared merge_target identity, not
   by stream adjacency, so any number of non-content boundary zones may
   sit between the two merged zones in the reading-order stream without
   breaking the link.

Both produce the same {"type": "merged_p", "zone_ids": [...]} node consumed
by hierarchy.py/xml_generator.py, so downstream handling is identical either
way. A Page Number zone consumed by either kind of merge IS included in the
chain's zone_ids, in its correct relative position - but ONLY as its own
inline pagebreak marker, NEVER as merged text (spec 9: "PageNum MUST NEVER
participate in text merging... it must remain: <span role=doc-pagebreak/>").
xml_generator.py's _merged_chain_text and epub_xml_generator.py's
_gen_merged_text_zone both special-case a page-marker zone_id found INSIDE
a chain into its own inline marker element (never a text run) - this was
previously dead code in xml_generator.py (paragraph_merge.py never actually
included a page-marker id in zone_ids at the time it was written) and
entirely unhandled in epub_xml_generator.py; a real, confirmed bug this
generalization surfaced (a PageNum consumed by a continuation merge was
being silently DISCARDED - its pagebreak span never emitted at all, a real
EPUB page-navigation/accessibility loss - only reachable, and therefore
only actually observed, once the CUPEPUB page_marker_tags fix in this same
module made the automatic merge fire for CUPEPUB in the first place). A
Page Number zone NOT consumed by any merge still passes through the stream
unchanged, exactly as before, so it can still generate its own marker
independently. A footnote/endnote zone is NEVER absorbed into a merge
chain either way (it has its own real, independently rendered content,
addressed elsewhere via its own ref/target - spec 7/8: "The footnote is an
independent logical element/reference") - it always gets its own single-
zone chain entry, it just no longer BLOCKS or gets mistaken for the
"previous zone" when the real content zones around it are actually
continuing one another.
"""
import re

from core import debug_log
from core.constants import NON_FLOW_TAGS

X_TOLERANCE = 20.0

# Tags eligible for the AUTOMATIC continuation heuristic below (never for
# the explicit Merge Previous path, which applies to any tag - see
# merge_top_level_stream's docstring). Both zones in a candidate pair must
# share the SAME tag from this set; a "p" is never auto-merged with a
# "reference" or vice versa.
_AUTO_CONTINUATION_TAGS = {"p", "reference"}

from core import lang as _lang  # noqa: E402

_SENTENCE_END_RE = re.compile(_lang.SENTENCE_END_CLASS + _lang.CLOSERS_CLASS + r'*\s*$')


class _StartsLower:
    """Unicode-aware: lower-case start in any cased script (é, ж, α ...)."""
    @staticmethod
    def match(text):
        return _lang.starts_lowercase(text)


_STARTS_LOWERCASE_RE = _StartsLower()
_FOOTNOTE_MARKER_START_RE = re.compile(r'^\s*(\d+|[*†‡§¶]+)[.)\s]')


def _compatible(zone_a, zone_b):
    """Same-column heuristic for AUTOMATIC continuation only - the only
    geometric signal available to tell "this is the same paragraph
    continuing" apart from "this is a new, coincidentally-adjacent
    paragraph". Never applied to an explicit Merge Previous, which is
    authoritative regardless of position."""
    return abs(zone_a.bbox[0] - zone_b.bbox[0]) <= X_TOLERANCE


def _requires_text_evidence(tag: str) -> bool:
    """"p" (ordinary flowing prose): structural adjacency (same tag, same
    column, a genuine page transition, a PageNum actually present between
    them) is already reliable evidence on its own - a full page essentially
    never happens to end AND the next one start a coincidentally-adjacent
    new paragraph in the same column often enough to matter, and this has
    been true in practice for this heuristic since before this module's
    footnote-handling even existed.

    "reference" (bibliography/citation entries) is a fundamentally
    different case, confirmed by a real false-positive: consecutive
    INDEPENDENT entries in a reference list routinely share tag + column +
    page-adjacency (that is simply how a reference list is laid out - the
    physical structure that "same tag, same column, real page break" is
    supposed to indicate looks IDENTICAL whether the last entry on a page
    continues onto the next page or a brand new entry just happens to
    start there). Text evidence is therefore REQUIRED here even for the
    otherwise-stronger page-number-flanked case, not just as an extra
    safety net for the weaker footnote-only gap."""
    return tag == "reference"


def _looks_like_reference_continuation(next_text: str) -> bool:
    """Narrower, stronger positive signal than _looks_like_continuation,
    used ONLY for "reference"-tagged automatic continuation (see
    _requires_text_evidence) - a NEW bibliography/reference entry is
    essentially NEVER lowercase-started (an author surname or reference
    number is always capitalized), so this is a much more reliable signal
    than "the previous entry lacks terminal punctuation", which proves
    nothing for citation text (a real citation routinely ends mid-clause -
    an incomplete title, a trailing "and of the" - for reasons entirely
    unrelated to whether the NEXT zone continues it; spec's own confirmed
    example: "Cattell, R. B. ... and of the" followed by an unrelated
    "Bem, Sandra Lapses. 1983. ..." must never be merged just because the
    first entry happens to lack a period)."""
    next_text = (next_text or "").strip()
    return bool(next_text) and bool(_STARTS_LOWERCASE_RE.match(next_text))


def _looks_like_continuation(prev_text: str, next_text: str) -> bool:
    """Text-continuity evidence (spec Priority 5) for "p" - required by
    EVERY automatic-continuation tier (see should_continue's own
    docstring for why the page-number-flanked tier used to skip this
    check, and why that was a confirmed false-positive source). True when
    the earlier fragment's text does NOT end with sentence-ending
    punctuation (a complete sentence in real prose almost always does; an
    interrupted one almost never does) OR the later fragment's text
    starts with a lowercase letter (a new sentence/paragraph in English
    prose is essentially always capitalized) - either signal alone is
    real evidence of an incomplete sentence spanning the gap, not a
    coincidence."""
    prev_text, next_text = (prev_text or "").strip(), (next_text or "").strip()
    if not prev_text or not next_text:
        return False
    return not _SENTENCE_END_RE.search(prev_text) or bool(_STARTS_LOWERCASE_RE.match(next_text))


def continuation_confidence(prev_text: str, next_text: str) -> float:
    """A real, evidence-based 0.0-1.0 confidence score for whether
    next_text is a genuine continuation of prev_text (spec: "EPUBForge -
    URGENT ZONING FIX - PREVIOUS-PARAGRAPH MERGE PROBLEM", section 30:
    "Calculate merge_confidence using... text continuity, punctuation...
    Do not automatically merge low-confidence candidates"). This is the
    SAME text-continuity signal _looks_like_continuation already uses for
    the automatic engine, exposed here as a public, numeric, reusable
    score for gui/main_window.py's manual "Merge Previous" confirmation
    dialog (spec section 11: "display a confirmation if confidence is
    low... Confidence: 94%") - never a second, separately-maintained
    heuristic. Tag compatibility itself is a separate, harder precondition
    checked before this is ever called (core.zone_manager.ZoneManager.
    merge_with_previous already refuses an incompatible tag outright) -
    this score is purely about whether the TEXT actually continues."""
    prev_text, next_text = (prev_text or "").strip(), (next_text or "").strip()
    if not prev_text or not next_text:
        return 0.5  # nothing to evaluate either way - neither confirms nor denies
    ends_incomplete = not _SENTENCE_END_RE.search(prev_text)
    starts_lowercase = bool(_STARTS_LOWERCASE_RE.match(next_text))
    if ends_incomplete and starts_lowercase:
        return 0.98
    if ends_incomplete or starts_lowercase:
        return 0.82
    return 0.30


def _looks_like_footnote_continuation(next_text: str) -> bool:
    """Footnote-continuation-specific positive signal (spec sections 23/
    24/56 - "Patterns of Con-" on one page continuing as "tention,
    Rodriguez..." on the next must remain footnote 1, never become a new
    footnote 5). A genuine NEW footnote almost always opens with its own
    number/symbol marker (spec's own example: "2. ..."); a continuation
    fragment of the PREVIOUS footnote, carried across a page break, never
    does - it picks up mid-word/mid-sentence instead.

    Deliberately does NOT also consult the prior fragment's own ending
    punctuation the way _looks_like_continuation does for prose: footnote
    text routinely ends without terminal punctuation even for a complete,
    independent entry (a trailing citation, page number, or bare URL),
    which would otherwise falsely suggest continuation for a perfectly
    ordinary NEW footnote sitting right after it - the same false-positive
    risk _requires_text_evidence's own docstring documents for
    "reference" lists, which footnote lists share (consecutive entries
    routinely look structurally identical whether one continues the other
    or not)."""
    next_text = (next_text or "").strip()
    return bool(next_text) and not _FOOTNOTE_MARKER_START_RE.match(next_text)


def should_continue(prev_zone, zone, pending_pagenumbers, saw_footnote_since_content) -> tuple:
    """DISABLED (spec: "URGENT FIX - ZONE-BOUNDARY PRESERVATION IN EPUB
    GENERATION" - "SOURCE ZONE BOUNDARY > TEXT SIMILARITY... > PAGE
    ADJACENCY... ONE SOURCE ZONE = ONE OUTPUT ELEMENT unless an explicit
    existing logical relationship proves otherwise... remove auto merge
    feature... page break mergings need to [be a] manual merge"). This
    function used to automatically combine two independent top-level
    zones into one XHTML element purely from inferred evidence (same
    tag, same column, a page transition, and text-continuity heuristics
    such as "the earlier zone doesn't end with terminal punctuation" or
    "the later zone starts lowercase") - real evidence, but still a
    GUESS, never something the zoning data itself records. Confirmed as
    a real, reproducible over-merge on real production data: two
    genuinely independent top-level zones on two different pages, with
    NO merged_with_previous/merge_target attribute anywhere in the saved
    project JSON, were silently combined into one <p> at generation time
    purely because this heuristic's text-continuity check fired.

    The EXPLICIT mechanism (zone.attributes["merged_with_previous"] /
    ["merge_target"], set only by a real, deliberate "Merge Previous"
    user action - see merge_top_level_stream's own branch for it) is a
    completely separate code path from this function and is NOT affected
    by this change: an explicit merge the user actually made, anywhere
    in the document including across a page break, still works exactly
    as before. Only the INFERRED, no-user-action-recorded continuation
    this function used to decide is disabled - every zone that would
    have relied on it now stays its own independent top-level element,
    per the user's own explicit "keep separate unless explicit existing
    metadata proves otherwise" rule. continuation_confidence() (the
    GUI's own manual "Merge Previous" confirmation-dialog score) is
    untouched - it is a separate, explicitly user-facing figure. shown
    only when the user has ALREADY chosen to merge, never something that
    merges on its own.

    Returns (should_merge: bool, reason: str_or_None) - reason is a
    short, human-readable explanation for the positive case (used both
    for debug_log and, potentially, a future diagnostic/debug view -
    spec 17/18), None for a negative decision.

    prev_zone may be None (no prior content chain yet - never merges).
    pending_pagenumbers/saw_footnote_since_content describe what kind of
    boundary (if any) sits between the two zones in the reading-order
    stream, as tracked by merge_top_level_stream's own loop.

    Evidence tiers (spec: "explicit structural relationship > physical
    proximity"; never tag-adjacency alone):
      1. Structural eligibility (always required): same tag, both zones
         genuinely eligible for this heuristic (_AUTO_CONTINUATION_TAGS),
         a REAL page transition, same column.
      2. Boundary evidence: was there an actual PageNum (stronger) or only
         a footnote (weaker) between them?
      3. Text-continuity evidence: REQUIRED for EVERY tier, including the
         page-number-flanked one (spec: "URGENT ZONING FIX - PREVIOUS-
         PARAGRAPH MERGE PROBLEM" - "Two consecutive <p> zones do NOT
         automatically mean they are the same paragraph... Do NOT merge
         merely because... same page[-boundary shape]"). An earlier
         version of this function treated a page-number sitting between
         two same-tag/same-column zones as sufficient evidence ON ITS
         OWN for "p" - which is a confirmed real false-positive source:
         virtually EVERY page boundary in a real book has exactly this
         shape (a page number between the last paragraph of one page and
         the first of the next), so that rule silently merged the vast
         majority of genuinely independent, unrelated paragraphs that
         merely happen to straddle a page break. Structural eligibility
         (tier 1) plus a stronger boundary marker (tier 2) narrow WHICH
         pairs are even considered; text continuity (tier 3) is what
         actually decides whether THIS pair is the same paragraph -
         required for "reference" always (a confirmed real false
         positive otherwise - consecutive INDEPENDENT bibliography
         entries routinely share tag+column+page-adjacency purely from
         list layout, so structural evidence alone proves nothing
         there); required for "p" via the general text-continuity check
         (_looks_like_continuation) in every tier."""
    return False, None
    # Unreachable: kept below, unmodified, as a record of the exact
    # evidence tiers this heuristic used to apply - never deleted outright
    # so the automatic behavior's own prior design stays fully visible and
    # trivially restorable if ever wanted again.
    structurally_eligible = (
        prev_zone is not None
        and zone.tag in _AUTO_CONTINUATION_TAGS
        and prev_zone.tag == zone.tag
        and prev_zone.page != zone.page
        and _compatible(prev_zone, zone)
    )
    if not structurally_eligible:
        return False, None

    needs_text_evidence = _requires_text_evidence(zone.tag)
    if pending_pagenumbers:
        if needs_text_evidence:
            if _looks_like_reference_continuation(zone.text):
                return True, "page-number-flanked continuation (new entry does not start capitalized)"
            return False, None
        if _looks_like_continuation(prev_zone.text, zone.text):
            return True, "page-number-flanked continuation (text-continuity confirmed)"
        return False, None

    if saw_footnote_since_content:
        if needs_text_evidence:
            if _looks_like_reference_continuation(zone.text):
                return True, "footnote-flanked continuation (new entry does not start capitalized)"
            return False, None
        if _looks_like_continuation(prev_zone.text, zone.text):
            return True, "footnote-flanked continuation (text-continuity confirmed)"
        return False, None

    # Spec: "EPUBForge - Paragraph Continuation + Split/Merge + Reading
    # Order Fix" - a BARE page transition with no PageNum/footnote zone at
    # all sitting between the two candidates (neither stronger boundary-
    # evidence tier above fired) can still be a genuine continuation - many
    # real documents don't zone an explicit PageNum marker on every single
    # page. This is the WEAKEST evidence tier, so it requires the SAME
    # text-continuity evidence the footnote-flanked "p" case above already
    # demands - never structural eligibility (same tag/column/page
    # transition) alone, and never text evidence alone without it; both
    # must independently agree (spec: "Do NOT rely on one signal alone...
    # Do NOT blindly merge based only on text ending or page boundary" -
    # satisfied here since structural eligibility was already required
    # above before this point is ever reached at all).
    if needs_text_evidence:
        if _looks_like_reference_continuation(zone.text):
            return True, "bare page-boundary continuation (new entry does not start capitalized)"
        return False, None
    if _looks_like_continuation(prev_zone.text, zone.text):
        return True, "bare page-boundary continuation (text-continuity confirmed)"
    return False, None


def merge_top_level_stream(zone_manager, ordered_zone_ids, page_marker_tags=None, footnote_flow_tags=None,
                            non_flow_tags=None):
    """ordered_zone_ids: flat, already reading-order-sorted top-level zone ids
    spanning the whole document (all pages, in final order). Returns a new
    list where mergeable chains are collapsed into {"type": "merged_p",
    "zone_ids": [...]} dicts (zone_ids always in document reading order,
    never including a Page Number id); every other zone passes through
    unchanged as its original zone_id string.

    page_marker_tags (default {"pagenumber"}) / footnote_flow_tags (default
    none) / non_flow_tags (default core.constants.NON_FLOW_TAGS) - the
    active profile's own sets, the SAME ones core.zone_manager.ZoneManager.
    merge_with_previous already takes for the identical purpose. See this
    module's own docstring for why a hardcoded literal tag name here would
    silently break CUPEPUB.

    A non_flow_tags zone (image/figure/equation - spec: "An image,
    advertisement, figure, or other object between two text pieces must
    not automatically break the logical continuation") gets its own
    single-zone chain entry, exactly where it sits in the stream - it is
    NEVER merged as text and NEVER deleted - but unlike every other zone
    it does NOT become the new "last content chain" a later fragment's
    continuation is judged against, so last_content_chain/last_footnote_
    zone_id keep pointing at whatever real content preceded it.

    Footnote/endnote continuation (spec sections 23/24/56): a footnote-
    tagged zone is ALSO checked against the immediately preceding
    footnote-tagged zone for a genuine cross-page continuation (the same
    "Con-"/"tention" shape of problem paragraph_merge already solves for
    "p"), using ONLY the footnote-specific text-evidence signal (see
    _looks_like_footnote_continuation) - structural adjacency ALONE is
    never sufficient here (footnote lists share the same "consecutive
    entries look structurally identical" ambiguity as reference lists),
    so footnote 2/3/4 immediately following a genuine continuation are
    correctly left as independent footnotes, never absorbed.

    The explicit Merge Previous flag works for ANY tag (Merge Previous is a
    generic zoning operation, not paragraph-specific) - hierarchy.py routes a
    merged HEADING chain into normal section-building (combined title text),
    and xml_generator.py restricts actual text-combination to genuinely
    text-bearing tags, leaving image/container tags as separate elements
    even when flagged merged (there's no sensible "combined text" for e.g.
    two Equation zones) - this module never filters by tag for an explicit
    merge; that decision belongs downstream. The automatic continuation
    heuristic stays restricted to _AUTO_CONTINUATION_TAGS ("p"/"reference"),
    as before - that one's a heuristic, not a user action.

    Merge target resolution is identity-based, not stream-adjacency-based:
    a zone's merge_target is looked up by zone_id among the chains already
    built from everything BEFORE it in ordered_zone_ids (so the target is
    guaranteed to occur earlier in reading order, never later - an ordering
    violation is simply an unresolvable reference, handled the same as any
    other invalid target). This is what lets an intervening Page Number (or
    several, or a page/column boundary) sit between the two merged zones
    without breaking the link, and what lets a chain grow transitively
    (C merges into B, B already merged into A -> one chain [A, B, C], never
    two overlapping chains with duplicated zone ids).

    An explicit merge whose target cannot be resolved (deleted zone, typo,
    or a reference to something that hasn't occurred yet in reading order)
    never crashes and never silently falls back to guessing a DIFFERENT
    merge partner via the automatic heuristic - the zone simply stays
    unmerged, and the invalid reference is reported through debug_log if
    enabled."""
    page_marker_tags = set(page_marker_tags) if page_marker_tags is not None else {"pagenumber"}
    footnote_flow_tags = set(footnote_flow_tags) if footnote_flow_tags else set()
    non_flow_tags = set(non_flow_tags) if non_flow_tags is not None else set(NON_FLOW_TAGS)

    zones = zone_manager.zones
    result = []                 # list of {"zone_ids": [...]} chain dicts, in stream order
    chain_of = {}                # content zone_id -> the chain dict (from `result`, OR still pending) it belongs to
    pending_pagenumbers = []     # page-marker zone ids buffered since the last content zone - ALSO
    # used (unchanged) as should_continue's own evidence-tier signal below - never repurposed for that.
    pending_boundary_ids = []    # ALL non-content zone ids buffered since the last content zone -
    # pagenumbers AND footnotes/endnotes together, in their TRUE relative stream order (spec: "do not
    # move footnotes into the wrong paragraph" - a footnote sitting between two continuing paragraph
    # fragments must end up EMBEDDED in the resulting chain at its own correct relative position, the
    # exact same way a PageNum marker already does, never left as a dangling sibling AFTER the whole
    # merged paragraph - a real, confirmed bug: the footnote's own <sup><a> reference marker used to
    # always get its own independent chain entry immediately, regardless of whether the paragraph
    # around it went on to merge). A footnote's own chain object (in chain_of) is used, never
    # duplicated, when it needs flushing/splicing - see flush_pending_boundary()/the splice sites below.
    last_content_chain_by_tag = {}  # tag -> the chain of the most recent zone of THAT tag - keyed
    # per-tag (spec section 22: "MULTIPLE INDEPENDENT CHAINS" - A1->A2 and B1->B2 running in
    # parallel, e.g. "p" and "reference" interleaved on the same pages, must never cross-wire into
    # A1->B2/B1->A2). A single shared "last content chain" variable (the original design) breaks the
    # MOMENT two different auto-continuation-eligible tags alternate: zone B1 becomes "the previous
    # zone" for A2's own continuation check purely because it happened to be processed more recently,
    # even though it's a completely different logical flow - confirmed as a real bug via a dedicated
    # test (tests/test_global_merge_continuation_engine.py) before this per-tag keying existed.
    # Deliberately excludes footnote/endnote and non_flow_tags zones - a footnote zone never itself
    # becomes "the previous zone" a later paragraph fragment might continue (spec 8: a footnote must
    # not terminate/redirect a paragraph's continuation), and a non_flow_tags zone (image/figure/
    # equation) likewise never becomes "the previous zone" for anything.
    last_footnote_zone_id = None  # the most recent footnote/endnote zone id - independent tracking
    # for footnote-to-footnote continuation (see the footnote branch below), never confused with
    # last_content_chain (a footnote never counts as "content" for a paragraph's own continuation).
    saw_footnote_since_content = False

    def flush_pending_pagenumbers():
        # Kept as a separate, named entry point (still used at end-of-stream
        # below) but now just delegates - pagenumbers and footnotes flush
        # together, in their true relative order, via flush_pending_boundary().
        flush_pending_boundary()

    def flush_pending_boundary():
        nonlocal pending_pagenumbers, pending_boundary_ids
        seen_chain_object_ids = set()
        for bid in pending_boundary_ids:
            if zones[bid].tag in page_marker_tags:
                result.append({"zone_ids": [bid]})
            else:
                chain = chain_of[bid]
                if id(chain) in seen_chain_object_ids:
                    continue  # a footnote-continuation chain has >1 zid mapping to the SAME chain object
                seen_chain_object_ids.add(id(chain))
                result.append(chain)
        pending_pagenumbers = []
        pending_boundary_ids = []

    def start_new_chain(zid):
        flush_pending_boundary()
        chain = {"zone_ids": [zid]}
        result.append(chain)
        chain_of[zid] = chain
        return chain

    def splice_pending_boundary(target_chain):
        """Embeds every currently-pending pagenumber/footnote zid into
        target_chain, in their true relative order, BEFORE the zid that
        triggered this merge is itself appended (shared by both the
        explicit-merge and auto-continuation branches below - never a
        second, divergent splice implementation). Re-points chain_of for
        each spliced footnote to target_chain, so a LATER footnote-to-
        footnote continuation correctly keeps extending the chain the
        footnote now actually lives in, never an orphaned original."""
        nonlocal pending_pagenumbers, pending_boundary_ids
        target_chain["zone_ids"].extend(pending_boundary_ids)
        for bid in pending_boundary_ids:
            if zones[bid].tag not in page_marker_tags:
                chain_of[bid] = target_chain
        pending_pagenumbers = []
        pending_boundary_ids = []

    for zid in ordered_zone_ids:
        zone = zones[zid]

        if zone.tag in page_marker_tags:
            pending_pagenumbers.append(zid)
            pending_boundary_ids.append(zid)
            continue

        if zone.tag in non_flow_tags:
            # An image/figure/equation carries no reading-flow text of its
            # own (spec: "An image...between two text pieces must not
            # automatically break the logical continuation") - it still
            # gets its own independent chain entry, exactly where it sits
            # in the stream (never merged, never deleted), but it must
            # NEVER become the new "previous zone" a later fragment's
            # continuation is judged against - last_content_chain and
            # last_footnote_zone_id are both deliberately left untouched,
            # still pointing at whatever real content/footnote preceded it.
            start_new_chain(zid)
            continue

        if zone.tag in footnote_flow_tags:
            # An EXPLICIT Merge Previous on a footnote/endnote zone (spec:
            # "use existing merge/continuation metadata" - the strongest,
            # most authoritative continuation signal there is, an actual
            # human decision) must take priority over the automatic
            # footnote-to-footnote text-continuity heuristic below, which
            # is necessarily weaker. Confirmed real gap: this branch used
            # to unconditionally `continue` for every footnote/endnote
            # zone BEFORE the general explicit-merge check further down
            # ever ran, making an explicit merge on such a zone
            # unreachable regardless of merged_with_previous/merge_target.
            if zone.attributes.get("merged_with_previous"):
                target_id = zone.attributes.get("merge_target")
                target_chain = chain_of.get(target_id) if target_id else None
                if target_chain is not None:
                    # Ordinary case: the target is itself a plain top-level
                    # footnote/endnote zone already in this stream. Never
                    # call splice_pending_boundary here (unlike the general
                    # explicit-merge branch below): a footnote/endnote
                    # chain is ALWAYS still sitting in pending_boundary_ids
                    # itself (see the buffering comment above - it's never
                    # immediately flushed to `result`), so target_chain IS
                    # one of the very items splice_pending_boundary would
                    # try to fold INTO itself, self-referentially
                    # duplicating its own zone_id. Appending directly is
                    # both correct and sufficient - target_chain is the
                    # same mutable object already destined for `result`
                    # (via the pending flush) or already inside it,
                    # exactly like the automatic footnote-to-footnote
                    # continuation branch just below, which never splices
                    # either.
                    target_chain["zone_ids"].append(zid)
                    chain_of[zid] = target_chain
                    last_footnote_zone_id = zid
                    saw_footnote_since_content = True
                    continue
                target_zone = zones.get(target_id) if target_id else None
                if target_zone is not None and target_zone.parent_id:
                    # The merge target is a SPLIT-PIECE CHILD of a top-
                    # level split-parent (a real, confirmed shape: an OCR
                    # block auto-split into one piece per endnote, where
                    # the LAST piece is the true continuation target). A
                    # split piece is never independently present in
                    # ordered_zone_ids/chain_of - core.reading_order.
                    # flatten_document_order deliberately excludes it (see
                    # _order_key's own docstring: "its split pieces are
                    # its CHILDREN... reached via containment recursion
                    # instead") - so there is no top-level chain object to
                    # splice into here. core.epub_xml_generator's own per-
                    # zone generation (_gen_footnote_zone's reverse merge-
                    # target lookup, _note_continuation_chain) is what
                    # actually appends this zone's text onto the split
                    # piece's own generated content; this zone must simply
                    # be OMITTED from the top-level stream entirely here,
                    # so it is never ALSO emitted as its own independent,
                    # duplicate entry.
                    debug_log.log(
                        "MERGE",
                        f"{zid}: merge_target {target_id!r} is a split-piece child of "
                        f"{target_zone.parent_id!r} - deferred to per-zone generation, not "
                        "emitted as its own top-level stream entry",
                    )
                    last_footnote_zone_id = zid
                    saw_footnote_since_content = True
                    continue
                debug_log.log(
                    "MERGE",
                    f"{zid}: merge_target {target_id!r} not found before it in reading order "
                    f"(deleted zone, or occurs later) - ignoring, zone stays unmerged",
                )
                chain = {"zone_ids": [zid]}
                chain_of[zid] = chain
                pending_boundary_ids.append(zid)
                last_footnote_zone_id = zid
                saw_footnote_since_content = True
                continue

            # Footnote-to-footnote continuation (spec sections 23/24/56):
            # only the FIRST footnote-tagged zone after a genuine page
            # transition is ever a continuation candidate, and only on
            # strong footnote-specific text evidence (never structural
            # adjacency alone - see _looks_like_footnote_continuation's own
            # docstring on why). A footnote immediately following on the
            # SAME page (fn2/fn3/fn4 right after a just-continued fn1) is
            # never eligible here since prev_footnote.page == zone.page.
            prev_footnote = zones.get(last_footnote_zone_id) if last_footnote_zone_id else None
            # Only ever between two LEAF note zones. A zone with children
            # (a split parent - e.g. a whole page of endnotes auto-split by
            # number - or any container) is not one note: its cached
            # zone.text is the WHOLE block's text, so a block that merely
            # starts with a continuation fragment ("white': The Clothing
            # ...") looked like a continuation and the ENTIRE block - every
            # following endnote on that page included - was absorbed into
            # the previous page's last note (confirmed: notes 11 and 12
            # nested inside note 10's <li>). Such a continuation must be
            # an explicit Merge Previous on the piece that really
            # continues the note.
            if (prev_footnote is not None and prev_footnote.tag == zone.tag
                    and not zone.children and not prev_footnote.children
                    and prev_footnote.page != zone.page
                    and _compatible(prev_footnote, zone)
                    and _looks_like_footnote_continuation(zone.text)
                    and last_footnote_zone_id in chain_of):
                # A split-piece child can be remembered as the previous
                # footnote zone, but it is intentionally NOT represented in
                # the top-level chain_of map (its continuation is handled by
                # per-zone generation).  Never index chain_of blindly here.
                # If there is no top-level chain, this zone starts its own
                # independent chain instead of raising KeyError.
                chain = chain_of[last_footnote_zone_id]
                chain["zone_ids"].append(zid)
                chain_of[zid] = chain
                last_footnote_zone_id = zid
                saw_footnote_since_content = True
                debug_log.log("MERGE", f"footnote continuation: {zid} -> {chain['zone_ids'][0]}")
                continue
            # A footnote/endnote always has its own real, independently
            # rendered content - never silently absorbed AS TEXT into a
            # surrounding paragraph's merge chain, and it never counts as
            # "the previous zone" for a PARAGRAPH's continuation purposes on
            # either side of it (last_content_chain is left untouched) - but
            # unlike a moment ago, it is NOT committed to `result` yet: it is
            # buffered in pending_boundary_ids (own chain created now, so
            # footnote-to-footnote continuation above can still find/extend
            # it via chain_of), exactly like a PageNum marker, so that if the
            # paragraph around it turns out to keep continuing across this
            # footnote, the footnote's own reference marker gets correctly
            # embedded INSIDE the resulting merged paragraph at this exact
            # relative position - instead of always becoming its own
            # independent chain entry immediately, which left it stranded as
            # a dangling sibling AFTER the whole merged paragraph (a real,
            # confirmed bug this buffering fixes). If no merge ends up
            # happening, flush_pending_boundary()/start_new_chain's own call
            # to it commits this chain to `result` unchanged, in the same
            # relative position it always occupied - today's exact behavior.
            chain = {"zone_ids": [zid]}
            chain_of[zid] = chain
            pending_boundary_ids.append(zid)
            last_footnote_zone_id = zid
            saw_footnote_since_content = True
            continue

        if zone.attributes.get("merged_with_previous"):
            target_id = zone.attributes.get("merge_target")
            target_chain = chain_of.get(target_id) if target_id else None
            if target_chain is not None:
                # Any PageNum marker buffered since the target's own last
                # zone is spliced INLINE into the chain, in its correct
                # relative position, BEFORE zid itself - never discarded
                # (spec 9: PageNum must remain its own <span role=
                # doc-pagebreak> marker even when the surrounding text
                # continues across it) and never contributing its own
                # value as merged TEXT (xml_generator.py's
                # _merged_chain_text / epub_xml_generator.py's
                # _gen_merged_text_zone both special-case a page-marker
                # zone_id found INSIDE a chain into its own inline marker
                # element, never plain text). A pending footnote reference
                # marker is spliced in exactly the same way, at its own
                # correct relative position (spec: never leave it stranded
                # after the whole merged paragraph).
                splice_pending_boundary(target_chain)
                target_chain["zone_ids"].append(zid)
                chain_of[zid] = target_chain
                last_content_chain_by_tag[zone.tag] = target_chain
                saw_footnote_since_content = False
                continue
            debug_log.log(
                "MERGE",
                f"{zid}: merge_target {target_id!r} not found before it in reading order "
                f"(deleted zone, or occurs later) - ignoring, zone stays unmerged",
            )
            # An explicit merge that failed to resolve must NOT fall back to
            # the automatic continuation heuristic below and silently merge
            # with some different, unintended zone instead.
            last_content_chain_by_tag[zone.tag] = start_new_chain(zid)
            saw_footnote_since_content = False
            continue

        # Automatic continuation - see should_continue() for the full
        # decision (spec 3: "create a robust decision:
        # should_continue(previous_zone, current_zone, context)" - never
        # "if current.tag == previous.tag: merge()"). Looked up PER TAG
        # (see last_content_chain_by_tag's own docstring above) - never a
        # single shared "whatever content zone came most recently"
        # variable, which would let an unrelated different-tag zone
        # silently become "the previous zone" for this one.
        prev_chain = last_content_chain_by_tag.get(zone.tag)
        prev_zone = zones[prev_chain["zone_ids"][-1]] if prev_chain is not None else None
        auto_merge, reason = should_continue(prev_zone, zone, pending_pagenumbers, saw_footnote_since_content)

        if auto_merge:
            # Splice any buffered PageNum marker AND/OR footnote reference
            # marker inline, in position, before zid - same reasoning as
            # the explicit-merge branch above (never discarded, never
            # contributes text as part of the merged paragraph string).
            splice_pending_boundary(prev_chain)
            prev_chain["zone_ids"].append(zid)
            chain_of[zid] = prev_chain
            saw_footnote_since_content = False
            debug_log.log("MERGE", f"auto-continuation: {zid} -> {prev_chain['zone_ids'][0]} ({reason})")
            continue

        last_content_chain_by_tag[zone.tag] = start_new_chain(zid)
        saw_footnote_since_content = False

    flush_pending_pagenumbers()

    return [
        {"type": "merged_p", "zone_ids": chain["zone_ids"]} if len(chain["zone_ids"]) > 1
        else chain["zone_ids"][0]
        for chain in result
    ]
