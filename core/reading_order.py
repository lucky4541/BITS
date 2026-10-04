"""Reading Order is zone.serial, a PER-PAGE continuous 1..N sequence, owned
and renumbered exclusively by core.zone_manager.ZoneManager.
renumber_reading_order()/set_reading_order(). Each page's own top-level
zones are numbered independently - page 2 always restarts at 1, it never
continues page 1's count.

The PHYSICAL PAGE LAYOUT - column membership, then top-to-bottom position
within a column - is the single source of truth for automatic Reading
Order (spec: "EPUBForge - Fix Canonical Reading Order for Multi-Column,
Split and Merged Zones": "Reading Order must be based on the actual page
layout and column sequence, NOT zone creation order... Do NOT use
creation order as Reading Order"). recompute_page_order()/
recompute_all_pages() below - THE one canonical Reading Order engine,
used everywhere, never duplicated - runs automatically after every
completed zoning operation (create, split, merge, delete, move, resize)
via gui/main_window.py's App.on_zones_changed(), and also backs the
explicit "Recalculate Reading Order" menu action for a project whose
saved order predates this engine.

An intermediate revision of this module instead treated whatever
relative order zones already held (creation order) as untouchable,
skipping the geometric recompute for create/split/merge/delete - which
solved one failure mode (a full recompute mis-detecting a brand-new
column that only had 1-2 samples yet) but reintroduced a different one:
a user who zones the right column first and only later goes back to add
more to the left column needs the left column's zones to end up first
regardless of which was drawn first, which pure creation-order can never
achieve. That revision is superseded - core.column_detector's own
column-detection algorithm has since been hardened directly against the
sparse-column/full-width/unequal-width failure modes that originally
motivated avoiding automatic recomputation (see that module's own
docstrings for the specific bugs and fixes), so running it automatically
is safe again. The explicit manual reorder controls (Set Reading Order /
Move Up / Move Down) and a pure tag-only change (gui/dialogs.py's
ZoneInfoDialog._apply) still pass recompute_reading_order=False to
on_zones_changed() - a manual override must never be immediately
overwritten by the very refresh it triggers, and a tag has no bearing on
physical position at all.

The rest of this module's job is unchanged: expose whatever order zone.
serial currently holds to the rest of the pipeline (hierarchy.py,
xml_generator.py, the GUI) in the shapes they need. This file still never
assigns zone.serial directly as a final value outside of
recompute_page_order() below (which - like split_zone/duplicate_zone
elsewhere - only ever SEEDS relative order via zone.serial, then delegates
the actual clean 1..N compaction to ZoneManager.renumber_reading_order(),
the single writer of normalized serial values).

The full DOCUMENT order (needed for XML generation and for Merge Previous's
cross-page-boundary detection) is: PDF pages in page-number order, and
within each page, that page's own zone.serial order - see
flatten_document_order(). This is different from a single flat sort by
serial value, since serial is only unique WITHIN a page.

zone.column is still stored on Zone (kept for backward-compatible project
files) and still propagated from parent to child by _propagate_column, but
it is purely a legacy/display field now - nothing in this file or downstream
uses it to decide order.

This file never assigns or mutates zone.parent_id / zone.children - those
relationships are owned exclusively by ZoneManager; this module only reads
them to expose reading order."""


def _order_key(zone, zone_manager=None):
    """The one and only sort key for Reading Order anywhere in the project:
    the zone's own manually-maintained serial (see Zone.serial /
    ZoneManager.renumber_reading_order), tie-broken by created_order.

    A zone that doesn't hold its own Reading Order (serial is None - a
    Horizontal Split parent, or a zone Merge-Previous-consumed into an
    earlier one; see ZoneManager._counts_in_reading_order) still needs to
    sort in its correct RELATIVE position among its top-level siblings for
    XML generation: it's still the zone that physically appears in that slot
    of flatten_document_order (its split pieces are its CHILDREN, not
    top-level themselves - they're reached via containment recursion
    instead, see xml_generator._gen_zone_multi). So its effective position
    is derived HERE, at sort time, from its associated zones - a split
    parent inherits the smallest serial among its own children; a
    merge-consumed zone inherits its merge target's serial - WITHOUT ever
    writing that derived value back into zone.serial itself, which must
    stay a clean integer or None (see Zone.serial's docstring: fractional
    values must never appear in reading_order)."""
    if zone.serial is not None:
        return (0, zone.serial, zone.created_order)
    if zone_manager is not None:
        children = [zone_manager.zones[cid] for cid in zone.children if cid in zone_manager.zones]
        counted_kids = [c for c in children if c.serial is not None]
        if counted_kids:
            return (0, min(c.serial for c in counted_kids), zone.created_order)
        if zone.attributes.get("merged_with_previous"):
            target = zone_manager.zones.get(zone.attributes.get("merge_target"))
            if target is not None and target.page != zone.page:
                # CROSS-PAGE continuation (spec: "READING ORDER IS PAGE/
                # PHYSICAL POSITION INFORMATION. MERGE TARGET IS CONTENT/
                # RELATIONSHIP INFORMATION"). The target's serial is a
                # position on a DIFFERENT page - page 137's "10" says nothing
                # about where the continuation sits on page 138. Borrowing
                # it sorted the continuation AFTER every following zone on
                # its own page (e.g. after endnotes 11 and 12). The
                # continuation keeps its own page-local physical slot
                # instead; which zone it continues is resolved by identity
                # (merge_target) downstream, never by position.
                return (0, _physical_slot(zone, zone_manager), zone.created_order)
            if target is not None and target.serial is not None:
                return (0, target.serial, zone.created_order)
    return (1, 10 ** 9, zone.created_order)  # no derivable position - sort last, deterministically


def _physical_slot(zone, zone_manager) -> float:
    """Page-local sort position for a zone with no Reading Order slot of
    its own (a merge-consumed cross-page continuation): half-way between
    the last slot-holding zone on the same page that physically PRECEDES
    it and the next one. "Precedes" = ends above its top edge, or sits in
    an earlier column (entirely to its left and starting above its
    bottom) - the same top-to-bottom / column-first convention the
    automatic Reading Order engine uses. Only ever a sort key - never
    written to zone.serial (which must stay a clean int or None)."""
    x0, y0, x1, y1 = zone.bbox
    tol = 2.0
    before = 0
    for other in zone_manager.zones.values():
        if other is zone or other.page != zone.page or other.serial is None:
            continue
        ox0, oy0, ox1, oy1 = other.bbox
        above = oy1 <= y0 + tol
        earlier_column = ox1 <= x0 + tol and oy0 < y1
        if above or earlier_column:
            before = max(before, other.serial)
    return before + 0.5


def flatten_document_order(zone_manager) -> list:
    """THE canonical "give me every top-level zone in true document order"
    function - processes PAGES IN PDF PAGE ORDER, and within each page uses
    that page's own zone.serial order (which restarts at 1 on every page).
    NEVER a single flat sort by raw serial value across pages, since serial
    is only unique within one page - a page-2 zone with serial=1 must still
    sort after every page-1 zone. hierarchy.build_document_tree and
    ZoneManager._find_previous_in_reading_order both call this directly
    rather than each re-deriving their own flattening/sorting logic, so
    there is exactly one algorithm."""
    # One pass to group top-level zones by page (dict insertion order keeps
    # the same per-page zone sequence the old per-page rescans produced, so
    # the stable sort below gives identical results). Rescanning EVERY zone
    # once per page was O(pages x zones) and ran several times per edit.
    by_page = {}
    for z in zone_manager.zones.values():
        if z.parent_id is None:
            by_page.setdefault(z.page, []).append(z)
    result = []
    for page in sorted(by_page):
        top_level = by_page[page]
        top_level.sort(key=lambda z: _order_key(z, zone_manager))
        result.extend(z.zone_id for z in top_level)
    return result


def _merge_involved_ids(zone_manager) -> set:
    """Every zone that takes part in an explicit Merge Previous relationship,
    either as the continuing zone or as its target."""
    ids = set()
    for z in zone_manager.zones.values():
        if z.attributes.get("merged_with_previous") and z.attributes.get("merge_target"):
            ids.add(z.zone_id)
            ids.add(z.attributes["merge_target"])
    return ids


def logical_document_order(zone_manager) -> list:
    """flatten_document_order(), with one refinement used by generation
    (core/hierarchy.py): a content/leaf split parent whose PIECES take part
    in a Merge Previous relationship is replaced, in place, by those leaf
    pieces (recursively, in their own reading order).

    Why: a split parent never emits its own text - its pieces do (see
    ZoneManager._is_same_tag_split_source). When a piece is merged (with a
    sibling piece - Split -> Merge; or with a zone on the previous page -
    a cross-page continuation that happens to be the first piece of an
    auto-split notes block), the merge must be resolved at the level of the
    zones that actually carry the text. Resolving it on the parent instead
    either ignored the merge (a merged piece still emitted on its own) or
    duplicated it (the target piece rendered once inside the merged chain
    and again via the parent's split-piece recursion).

    Split parents with no merge involvement are left exactly as before -
    both generators already expand them into their pieces at the same
    position, so their output is unchanged."""
    involved = _merge_involved_ids(zone_manager)
    flat = flatten_document_order(zone_manager)
    if not involved:
        return flat
    result = []
    for zid in flat:
        leaves = zone_manager.content_split_leaves(zid) if hasattr(zone_manager, "content_split_leaves") else None
        if leaves and (len(leaves) > 1 or leaves[0].zone_id != zid) and \
                any(leaf.zone_id in involved for leaf in leaves):
            result.extend(leaf.zone_id for leaf in leaves)
        else:
            result.append(zid)
    return result


def sort_children_ids(zone_manager, parent_id):
    """A zone's direct children, in Reading Order (by their own serial) -
    children always share their parent's page, so per-page scoping never
    matters here; this is simply the same ordering concept applied to one
    parent's children list. This is what keeps a nested structure
    (list -> list-item -> list -> list-item, list-item) as one coherent
    sequence inside its parent, rather than being pulled apart or promoted
    to page-level siblings - nesting depth and Reading Order are completely
    independent concepts (see module docstring)."""
    parent = zone_manager.zones[parent_id]
    children = [zone_manager.zones[cid] for cid in parent.children if cid in zone_manager.zones]
    children.sort(key=lambda z: _order_key(z, zone_manager))
    return [z.zone_id for z in children]


def _propagate_column(zone_manager, zid):
    """Legacy: inherits the parent's display-only `column` value down to
    descendants. Kept only for backward-compatible project files / any UI
    still reading zone.column - has no effect on ordering."""
    z = zone_manager.zones[zid]
    for cid in sort_children_ids(zone_manager, zid):
        zone_manager.zones[cid].column = z.column
        _propagate_column(zone_manager, cid)


def compute_page_order(zone_manager) -> dict:
    """Returns {page: [top-level zone_ids]} - each page's own top-level
    zones in that page's own Reading Order (see flatten_document_order,
    which this simply partitions by page - already page-major, so no
    re-sorting happens here)."""
    page_order = {}
    flat = flatten_document_order(zone_manager)  # computed once, used twice
    for zid in flat:
        zone = zone_manager.zones[zid]
        page_order.setdefault(zone.page, []).append(zid)
    for zid in flat:
        _propagate_column(zone_manager, zid)
    return page_order


def compute_reading_order(zone_manager) -> dict:
    """Convenience wrapper: page order + level finalization in one call.
    Prefer this unless you need the intermediate doc_tree yourself."""
    from core import hierarchy  # local import: hierarchy imports this module too
    page_order = compute_page_order(zone_manager)
    doc_tree, _boundary_issues = hierarchy.build_document_tree(zone_manager, page_order)
    hierarchy.finalize_order(zone_manager, doc_tree)
    return page_order


# ---------- automatic geometry/column-based recalculation ----------
# Everything below is the one new automatic-Reading-Order mechanism (see
# module docstring). It answers a DIFFERENT question than the rest of this
# file: not "what order do the zones currently hold" but "what order SHOULD
# they hold, given where they are on the page right now."

def _recompute_candidates(zone_manager, page: int) -> list:
    """Every zone on `page` that holds (or, after this call, WILL hold) its
    own top-level Reading Order slot - reuses ZoneManager.
    _counts_in_reading_order UNCHANGED, so a Horizontal-Split parent, a
    Merge-Previous-consumed zone, and any geometric CHILD (a list-item, a
    figure's label/caption/graphic) are excluded exactly as they always
    were; a Page Number zone is NOT excluded here (see module docstring
    point 10 in the feature spec this implements: existing page-number
    handling - i.e. counting normally but being skipped over specifically
    by ZoneManager._find_previous_in_reading_order's merge-chain walk - is
    preserved unchanged, not replaced)."""
    return [z for z in zone_manager.zones_on_page(page) if zone_manager._counts_in_reading_order(z)]


def _column_aware_order(zone_manager, page: int, candidates: list):
    """The actual column-aware geometry calculation, factored out so both
    recompute_page_order() (writes it as the page's new final order) and
    insert_zone_preserving_order() (only reads WHERE one specific zone
    landed, see below) share the one algorithm - never two competing
    implementations. Returns (ordered_zones, debug_rows) - debug_rows is
    [(zone, column_index_within_its_band), ...] in final order, for
    recompute_page_order's own debug logging.

    No-op-returning ([], []) if the zone_manager has no pdf_document
    attached (e.g. a bare unit-test ZoneManager with no page geometry
    available) or there are no candidates."""
    from core import column_detector
    if zone_manager.pdf_document is None or not candidates:
        return [], []
    page_width, page_height = zone_manager.pdf_document.page_size(page)
    candidates = sorted(candidates, key=lambda z: (z.bbox[1], z.bbox[0]))  # top-to-bottom, ties left-to-right - detect_bands' own input contract
    bands = column_detector.detect_bands(page_width, page_height, candidates)
    ordered = []
    debug_rows = []
    for band in bands:
        for col_idx in range(band.columns):
            col_zones = [z for z in band.items if column_detector.column_index_for_bbox(band, z.bbox) == col_idx]
            col_zones.sort(key=lambda z: (z.bbox[1], z.bbox[0]))
            ordered.extend(col_zones)
            debug_rows.extend((z, col_idx) for z in col_zones)
    return ordered, debug_rows


def recompute_page_order(zone_manager, page: int):
    """Fully automatic Reading Order recalculation for one page, from the
    CURRENT bbox of every zone that counts (see _recompute_candidates) -
    page number, then top Y, then left X, with column detection so a
    multi-column page reads one column completely before the next (reuses
    core.column_detector - the SAME primitive auto_zoning/auto_zone_engine.py
    already uses to lay out brand-new candidate zones, not a second/
    competing column-detection algorithm).

    Like every other reading-order-writing operation in this project
    (ZoneManager.split_zone, duplicate_zone, auto_zoning's own
    reading_order_engine), this only SEEDS the desired relative order via
    zone.serial and then delegates the actual clean 1..N compaction to
    ZoneManager.renumber_reading_order() - never a second, parallel
    normalization step.

    This is a FULL, page-wide re-derivation from scratch - appropriate
    after move/resize/delete/split/merge (see module docstring), and for
    the explicit "Recalculate Reading Order" action, but NOT after
    creating a brand-new zone (see insert_zone_preserving_order instead,
    which this function shares its column-aware core with via
    _column_aware_order).

    EXCEPT for a zone the user has manually pinned (attributes["manual_ro"]
    - see ZoneManager.set_reading_order, the only writer of that flag,
    covering both Move Up/Down and the Set Reading Order dialog) - spec:
    "EPUBForge - Paragraph Continuation + Split/Merge + Reading Order Fix"
    - "Do NOT immediately overwrite a manual reading-order change with an
    automatic geometry recalculation... Automatic recalculation should
    occur only where the existing application's normal automatic-order
    workflow explicitly requires it." A pinned zone keeps its CURRENT
    RELATIVE rank among this page's other zones (not a frozen absolute
    serial number - it can still shift up/down if zones are added/removed
    elsewhere on the page, matching ordinary "insertion" semantics); every
    OTHER (non-pinned) zone on the page is still fully, freshly
    recalculated from geometry exactly as before - this is a slot-filling
    merge of the two orderings, never a page-wide exemption from
    recompute just because ONE zone happens to be pinned."""
    from core import debug_log
    candidates = _recompute_candidates(zone_manager, page)
    if not candidates:
        return
    current_order = sorted(candidates, key=lambda z: _order_key(z, zone_manager))
    manual_ids = {z.zone_id for z in current_order if z.attributes.get("manual_ro")}
    auto_candidates = [z for z in current_order if z.zone_id not in manual_ids]
    if auto_candidates:
        ordered_auto, debug_rows = _column_aware_order(zone_manager, page, auto_candidates)
        if not ordered_auto:
            return  # no pdf_document attached (e.g. a bare unit-test ZoneManager) - no-op, exactly as before
    else:
        ordered_auto, debug_rows = [], []
    auto_iter = iter(ordered_auto)
    ordered = [z if z.zone_id in manual_ids else next(auto_iter) for z in current_order]
    for i, zone in enumerate(ordered, start=1):
        zone.serial = i
    zone_manager.renumber_reading_order(page)
    # Debug visibility into the column-aware calculation itself (spec:
    # "ZoneTool - URGENT FIX - MULTI-COLUMN + SPLIT ZONE READING ORDER",
    # section 19) - only built/logged when Debug Logging is on (Settings),
    # since debug_rows' own construction above is otherwise free (already
    # a byproduct of the ordering loop, not extra work).
    if debug_log.is_enabled():
        lines = [f"page {page}: {len(ordered)} counting zone(s)"]
        for zone, col_idx in debug_rows:
            lines.append(f"  RO {zone.serial} -> {zone.zone_id} -> {zone.tag} -> col={col_idx} "
                         f"x={round(zone.bbox[0], 1)} y={round(zone.bbox[1], 1)}")
        debug_log.log("READING_ORDER", *lines)


# NOTE: an earlier version of this file had insert_zone_preserving_order()
# here - a column-aware INSERTION for brand-new zones (an improvement over
# a full recompute, but still letting geometry decide WHERE a new zone
# lands). Removed per spec "EPUBForge - FIX READING ORDER USING CANONICAL
# ZONE SEQUENCE": geometry must never decide order for an ordinary
# interactive zone creation, not even just its insertion point - see
# gui/main_window.py's App.on_zone_created(), which now relies entirely on
# ZoneManager.add_zone()'s own existing "new zone lands at the end of its
# page's current sequence" behavior, with no reading_order module
# involvement at all.


def recompute_all_pages(zone_manager):
    """Calls recompute_page_order for every page that currently has any
    zones. This is what App.on_zones_changed() calls (see gui/main_window.py)
    after every completed zoning operation - recomputing every page rather
    than trying to track exactly which page(s) a given operation touched is
    a deliberate simplification: it costs no more than the auto-save that
    already happens on every on_zones_changed() call (same O(all zones)
    order of work), and it is trivially, unconditionally correct regardless
    of how many pages an operation (or an undo/redo snapshot restore, which
    can in principle span pages) actually affected."""
    if zone_manager.pdf_document is None:
        return
    for page in {z.page for z in zone_manager.zones.values()}:
        recompute_page_order(zone_manager, page)
