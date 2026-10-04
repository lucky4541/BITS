"""Pre- and post-Generate-XML validation. Surfaces clear, specific errors/
warnings instead of silently dropping content or crashing mid-generation."""
import re
from collections import defaultdict

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def _bbox_area(bbox) -> float:
    if not bbox or len(bbox) != 4:
        return 0.0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _bbox_overlap_area(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _find_overlapping_pairs(zone_manager, page_marker_tags=None):
    """Pure geometry helper - returns [(zone_a, zone_b), ...] for every
    pair of same-tag, same-page zones whose bboxes overlap almost
    completely (>=90% of the smaller zone's own area). Deliberately
    narrow (same tag, same page, near-total overlap only) so ordinary
    adjacent/touching/partially-overlapping zones are never flagged,
    and direct parent/child pairs are skipped (a same-tag parent/child
    IS still anomalous - this app's own ALLOWED_CHILDREN never nests a
    tag under itself - but a split parent legitimately spans the same
    space as its own split children collectively, so that specific case
    must not be reported as accidental duplicate zoning). THE single
    overlap-detection algorithm in the project - validate()'s own error
    text and find_overlapping_zone_ids()'s UI-highlight set both call
    this instead of each having their own copy.

    page_marker_tags (default {"pagenumber"}) excludes the active
    profile's own page-marker zones from overlap checking (a book
    legitimately draws many same-shaped PageNum zones, one per page - a
    hardcoded literal "pagenumber" here would silently stop excluding
    them for CUPEPUB, whose own page-marker tag is "pagenum" - the exact
    same class of bug already found and fixed in core/paragraph_merge.py's
    automatic continuation heuristic)."""
    page_marker_tags = set(page_marker_tags) if page_marker_tags is not None else {"pagenumber"}
    zones = zone_manager.zones
    by_page = defaultdict(list)
    for zone in zones.values():
        if zone.tag not in page_marker_tags:
            by_page[zone.page].append(zone)
    pairs = []
    for page, zlist in by_page.items():
        for i in range(len(zlist)):
            for j in range(i + 1, len(zlist)):
                a, b = zlist[i], zlist[j]
                if a.tag != b.tag:
                    continue
                if a.parent_id == b.zone_id or b.parent_id == a.zone_id:
                    continue
                smaller = min(_bbox_area(a.bbox), _bbox_area(b.bbox))
                if smaller <= 0:
                    continue
                if _bbox_overlap_area(a.bbox, b.bbox) / smaller >= 0.9:
                    pairs.append((a, b))
    return pairs


def find_overlapping_zone_ids(zone_manager, page_marker_tags=None) -> set:
    """Every zone_id involved in at least one near-total-overlap pair
    (see _find_overlapping_pairs) - e.g. z1-z2 overlapping AND z2-z3
    overlapping yields {z1, z2, z3}, so a whole chain of mutually
    overlapping zones is captured, not just isolated pairs. Used ONLY
    for transient UI highlighting (gui/pdf_viewer.py via
    App.overlapping_zone_ids) - purely a read-only geometry query, never
    written back to any zone, never affects XML generation, reading
    order, or the project data model, and never persisted to the
    project JSON."""
    ids = set()
    for a, b in _find_overlapping_pairs(zone_manager, page_marker_tags):
        ids.add(a.zone_id)
        ids.add(b.zone_id)
    return ids


def validate(zone_manager, page_marker_tags=None, footnote_flow_tags=None, non_flow_tags=None) -> list:
    """Returns a list of human-readable error strings; empty means OK. A
    READ-ONLY check - Generate XML must never silently fix the user's
    zones, only refuse and explain. Covers both structural data-model
    integrity (checked here for years) and the specific invariants that
    keep manual zoning as the sole source of truth for XML generation:
    every active zone has a valid tag/bbox/page, no zone accidentally
    holds a duplicate Reading Order slot, no split-parent zone is
    mistakenly still "active" (see ZoneManager._counts_in_reading_order),
    and no two same-tag leaf zones overlap almost completely (the most
    common real cause of duplicated text: the user zoned the same
    content twice instead of once)."""
    errors = []
    zones = zone_manager.zones

    for zid, zone in zones.items():
        if not zone.tag:
            errors.append(f"Zone {zid} has no tag")
        if _bbox_area(zone.bbox) <= 0:
            errors.append(f"Zone {zid} ({zone.tag}) has a missing or invalid bounding box")
        if not isinstance(zone.page, int) or zone.page < 1:
            errors.append(f"Zone {zid} ({zone.tag}) has an invalid page number: {zone.page!r}")
        if zone.parent_id is not None and zone.parent_id not in zones:
            errors.append(f"Zone {zid} ({zone.tag}) references a parent that no longer exists: {zone.parent_id}")
        if zone.parent_id == zid:
            errors.append(f"Zone {zid} ({zone.tag}) is set as its own parent")

    # Circular parent chains (defensive - ZoneManager.set_parent already
    # blocks creating one, but a hand-edited/older project file could have one).
    for zid in zones:
        seen = set()
        cur = zid
        while cur is not None:
            if cur in seen:
                errors.append(f"Zone {zid} is part of a circular parent chain")
                break
            seen.add(cur)
            z = zones.get(cur)
            cur = z.parent_id if z else None

    # Duplicate zone ids are structurally impossible (dict keys), so not checked.

    # A split parent must never independently hold a Reading Order slot -
    # ZoneManager.renumber_reading_order/_resync_noncounting_serials
    # already guarantees this on every split/merge/delete/project load;
    # this only ever fires for a hand-edited/corrupted project file.
    for zid, zone in zones.items():
        if zone.serial is None or not zone.children:
            continue
        children = [zones[c] for c in zone.children if c in zones]
        if children and all(c.is_split and c.source_zone_id == zid and c.tag == zone.tag for c in children):
            errors.append(f"Zone {zid} ({zone.tag}) is a split parent but still holds Reading Order "
                           f"position {zone.serial} - it must be inactive (split children take its place)")

    # Duplicate Reading Order values among zones that actually count in
    # Reading Order (see ZoneManager._counts_in_reading_order), per page -
    # renumber_reading_order already guarantees uniqueness; a duplicate
    # here means the project file was hand-edited or migrated incorrectly.
    by_page_serial = defaultdict(list)
    for zid, zone in zones.items():
        if zone.serial is not None and zone_manager._counts_in_reading_order(zone):
            by_page_serial[(zone.page, zone.serial)].append(zid)
    for (page, serial), zids in by_page_serial.items():
        if len(zids) > 1:
            errors.append(f"Page {page}: duplicate Reading Order {serial} on zones {', '.join(sorted(zids))}")

    # Overlapping zones of the SAME tag on the same page, almost entirely
    # covering one another - the most common real cause of duplicated
    # text output: the user zoned the same content twice instead of
    # once. See _find_overlapping_pairs for the full geometry rationale -
    # the single overlap-detection algorithm in the project, also reused
    # by find_overlapping_zone_ids() for UI highlighting (gui/pdf_viewer.py),
    # never duplicated.
    for a, b in _find_overlapping_pairs(zone_manager, page_marker_tags):
        errors.append(f"Page {a.page}: zones {a.zone_id} and {b.zone_id} (both {a.tag}) "
                       f"overlap almost completely - likely duplicate zoning of the same content")

    # Cross-page continuation / Merge Previous chain integrity (spec:
    # "validate_logical_structure() - detect orphan split fragments,
    # conflicting parents, broken continuation chains"). merge_with_previous
    # itself already prevents creating an invalid merge_target (missing/
    # wrong-tag/cyclic) through the normal UI, so these only ever fire for
    # a hand-edited or corrupted project file - the same "defensive, not
    # load-bearing for normal use" role the circular-parent-chain check
    # above already plays.
    for zid, zone in zones.items():
        if not zone.attributes.get("merged_with_previous"):
            continue
        target_id = zone.attributes.get("merge_target")
        if not target_id:
            errors.append(f"Zone {zid} ({zone.tag}) is flagged as a continuation but has no merge target "
                           f"(orphaned continuation fragment)")
            continue
        target = zones.get(target_id)
        if target is None:
            errors.append(f"Zone {zid} ({zone.tag}) continues zone {target_id}, which no longer exists "
                           f"(orphaned continuation fragment)")
            continue
        if target.tag != zone.tag:
            errors.append(f"Zone {zid} ({zone.tag}) continues zone {target_id} ({target.tag}) - "
                           f"incompatible tags for a continuation")
        seen = {zid}
        cur_id = target_id
        while cur_id is not None:
            if cur_id in seen:
                errors.append(f"Zone {zid} ({zone.tag}) is part of a circular continuation chain")
                break
            seen.add(cur_id)
            cur_zone = zones.get(cur_id)
            cur_id = (cur_zone.attributes.get("merge_target")
                      if cur_zone and cur_zone.attributes.get("merged_with_previous") else None)

    # Boxed Text Start/End boundary matching - reuses the exact same stack
    # logic Generate XML itself uses (hierarchy.build_document_tree), so what
    # validation reports and what generation would actually do never diverge.
    if zones:
        from core import reading_order, hierarchy
        page_order = reading_order.compute_page_order(zone_manager)
        _, boundary_issues = hierarchy.build_document_tree(
            zone_manager, page_order, page_marker_tags=page_marker_tags, footnote_flow_tags=footnote_flow_tags,
            non_flow_tags=non_flow_tags)
        errors.extend(boundary_issues)

    return errors


def _leaf_content_length(zone_manager) -> int:
    """Sums extracted plain-text length only over LEAF zones (no children) of
    text-bearing tags. Container zones are excluded even though ZoneManager
    also populates their own `.text` preview, because a container's own-bbox
    text typically overlaps its children's bboxes - counting both would
    double-count the same characters and make this check noisy rather than
    useful. This makes the check a conservative lower bound, not an exact
    total, which is enough to catch gross content loss (e.g. an entire List
    or Label silently dropped) without false-positiving on expected small
    reductions from marker-stripping or dehyphenation."""
    total = 0
    for zone in zone_manager.zones.values():
        if zone.children:
            continue
        if zone.tag in ("graphic", "figure", "equation", "pagenumber"):
            continue
        total += len(zone.text or "")
    return total


def find_missing_zones(zone_manager, xml_root_element, min_len: int = 8):
    """Returns zone_ids of leaf content zones whose text doesn't appear to be
    represented anywhere in the serialized XML. Uses a normalized snippet
    match (not exact substring of the raw text) so it tolerates the small,
    expected transformations content legitimately goes through - list-marker
    stripping, dehyphenation, merge joins - without false-flagging those."""
    serialized = _normalize(" ".join(xml_root_element.itertext()))
    missing = []
    for zone in zone_manager.zones.values():
        if zone.children:
            continue
        if zone.tag in ("graphic", "figure", "equation", "pagenumber"):
            continue
        text = _normalize(zone.text)
        if len(text) < min_len:
            continue
        # Skip a few leading characters (a stripped list marker typically
        # only removes 2-4 chars) before taking the snippet, so marker
        # stripping doesn't make an otherwise-present zone look missing.
        start = min(5, max(0, len(text) - min_len))
        snippet = text[start:start + 40] or text[:40]
        if snippet and snippet not in serialized:
            missing.append(zone.zone_id)
    return missing


def check_content_loss(zone_manager, xml_root_element, threshold: float = 0.7):
    """Returns a warning string if the generated XML's total text content
    falls suspiciously short of what was extracted from leaf content zones,
    or None if it looks fine. Non-blocking - meant to be shown as a
    dismissible warning after Generate XML, not a hard gate."""
    extracted_total = _leaf_content_length(zone_manager)
    if extracted_total == 0:
        return None
    serialized_total = sum(len((t or "").strip()) for t in xml_root_element.itertext())
    ratio = serialized_total / extracted_total
    if ratio < threshold:
        missing = find_missing_zones(zone_manager, xml_root_element)
        msg = (f"Generated XML contains substantially less text ({serialized_total} chars) "
               f"than was extracted from zoned content ({extracted_total} chars, "
               f"{ratio:.0%} retained). Some content may have been silently dropped.")
        if missing:
            shown = ", ".join(missing[:15])
            more = f" (+{len(missing) - 15} more)" if len(missing) > 15 else ""
            msg += f"\n\nZones whose text was not found in the output: {shown}{more}"
        return msg
    return None
