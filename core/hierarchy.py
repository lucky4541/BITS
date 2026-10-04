"""Builds the document section tree from the reading-order-sorted stream of
top-level zones (across all pages).

THREE nesting mechanisms combine here:
  1. Geometric containment (resolved earlier by ZoneManager into zone.parent_id /
     zone.children) - used for Figure->Label/Caption/Graphic, Caption->Title,
     List->List-item, Boxed-text->content, nested lists, etc.
  2. Heading-based sectioning (resolved here) - a heading zone never geometrically
     contains the paragraphs/figures/lists that logically belong to it, so section
     membership is inferred purely from reading order: a new heading of the same
     level closes the previous section, a higher-level heading closes all lower
     open sections, a lower-level heading nests inside the current one.
  3. Boxed Text Start/End boundary markers (resolved here, same stack as #2) -
     an alternative to drawing one big geometric Boxed Text zone: mark where a
     boxed-text begins and ends in reading order, and everything in between
     (at that stack depth) becomes its children - lets boxed-text content span
     an irregular region without needing one bounding rectangle up front.
     Supports nesting for free via the stack. Headings opened inside an open
     boxed-text nest inside it and don't close it; only its own matching End
     marker does (an unmatched Start/End is reported, never crashes).

Top-level zones (zone.parent_id is None) are exactly the "section stream";
anything with a parent_id is already handled by mechanism (1) and is reached
later via zone.children when the XML generator recurses.
"""
from core.constants import heading_level, TEXT_MERGE_TAGS, BACK_MATTER_TAGS
from core import reading_order, paragraph_merge, debug_log


def finalize_order(zone_manager, doc_tree: dict):
    """Assigns `level` (TRUE hierarchy depth: 0 for a top-level node, +1 per
    level of nesting in the ASSEMBLED tree) to every zone, by walking
    doc_tree itself.

    Deliberately distinct from heading disp-level (h1..h6): a heading's
    *disp-level* is fixed by its own tag (h1 -> disp-level 1) regardless of
    nesting, whereas hierarchy `level` is purely "how many ancestors does
    this node have in the final tree" - e.g. an h3 directly nested under an
    h1 (h2 skipped) still gets hierarchy level 1, not 2, because it has
    exactly one ancestor node.

    Reading Order (zone.serial) is NEVER touched here - it is a persistent,
    manually-maintained GLOBAL sequence owned by ZoneManager (see
    ZoneManager.normalize_reading_order/set_reading_order), completely
    independent of hierarchy nesting depth. Calling this repeatedly (e.g. on
    every Zone Hierarchy refresh) never disturbs the user's manual Reading
    Order - hierarchy and reading order are separate concepts by design.
    """
    def visit(node, depth):
        if node["type"] == "sec":
            # A heading merged via Merge Previous carries ALL its constituent
            # zones (see hierarchy.build_document_tree) - every one of them
            # gets a level, not just the first.
            for zid in node.get("merged_zone_ids") or [node["zone_id"]]:
                zone_manager.zones[zid].level = depth
            for child in node["children"]:
                visit(child, depth + 1)
        elif node["type"] == "boxed-text":
            zone_manager.zones[node["start_zone_id"]].level = depth
            if node.get("end_zone_id"):
                zone_manager.zones[node["end_zone_id"]].level = depth
            for child in node["children"]:
                visit(child, depth + 1)
        elif node["type"] == "merged_p":
            for zid in node["zone_ids"]:
                zone_manager.zones[zid].level = depth
        else:
            visit_zone(zone_manager.zones[node["zone_id"]], depth)

    def visit_zone(zone, depth):
        zone.level = depth
        for cid in reading_order.sort_children_ids(zone_manager, zone.zone_id):
            visit_zone(zone_manager.zones[cid], depth + 1)

    for node in doc_tree["children"]:
        visit(node, 0)


_DEFAULT_CONTAINER_MARKERS = (("boxed-text-start", "boxed-text-end"),)


def build_document_tree(zone_manager, page_order: dict, extra_container_markers=None,
                          page_marker_tags=None, footnote_flow_tags=None, non_flow_tags=None):
    """Returns (root, boundary_issues). boundary_issues lists any unmatched
    Start/End container marker as human-readable strings - never raises; an
    unclosed group simply stays open through the rest of the document, so
    generation always produces *something* rather than crashing.

    `page_order` is accepted for signature compatibility with existing
    callers, but the actual top-level stream comes from
    reading_order.flatten_document_order() - PDF pages in page-number order,
    each page's own zones in that page's own Reading Order within (Reading
    Order is per-page: zone.serial restarts at 1 on every page, see
    core/zone_manager.py ZoneManager.renumber_reading_order), never a single
    flat sort by raw serial value across pages.

    extra_container_markers: optional iterable of (start_tag, end_tag)
    pairs, checked ALONGSIDE the XML profile's own hardcoded
    ("boxed-text-start", "boxed-text-end") pair (always active, exactly as
    before this parameter existed - every existing caller passes nothing
    and sees zero behavior change). CUPEPUB's Box_Start/Box_End (fnames
    "box_start"/"box_end" - see core/cup_config.py's box_container_tags)
    reuses this SAME stack-based mechanism instead of a second one: reading
    -order-driven, nests for free, and reports an unmatched Start or End as
    a boundary issue rather than crashing - exactly what CUPEPUB's own Box
    validation needs. Matching is scoped PER PAIR (an "X_end" only closes
    the nearest open "X_start" frame of the SAME configured pair), so two
    different marker vocabularies can appear interleaved/nested without
    one's End accidentally closing the other's Start.

    page_marker_tags/footnote_flow_tags/non_flow_tags: forwarded straight
    through to paragraph_merge.merge_top_level_stream (see its own
    docstring) - the active profile's own tag sets, so a page-marker tag
    other than the literal "pagenumber" (e.g. CUPEPUB's "pagenum") is
    recognized correctly. Every existing caller that doesn't pass these
    keeps the exact same default behavior as before this parameter
    existed."""
    container_markers = tuple(_DEFAULT_CONTAINER_MARKERS) + tuple(extra_container_markers or ())
    start_to_pair = {start: (start, end) for start, end in container_markers}
    end_to_pair = {end: (start, end) for start, end in container_markers}

    # logical_document_order == flatten_document_order, except that a
    # content-split parent whose pieces take part in a Merge Previous is
    # replaced by those pieces, so the merge is resolved on the zones that
    # actually carry the text (see its own docstring).
    flat = reading_order.logical_document_order(zone_manager)
    # Cross-page paragraph continuations (page-number-flanked and footnote-
    # flanked - see paragraph_merge.py) are resolved on the flat, already-
    # ordered, whole-document stream (merging can span a page boundary)
    # BEFORE heading-based sectioning is applied.
    stream = paragraph_merge.merge_top_level_stream(zone_manager, flat, page_marker_tags, footnote_flow_tags,
                                                     non_flow_tags)

    root = {"type": "root", "children": []}
    stack = [(0, root, "root")]  # (heading-level-or-None, node, kind)
    issues = []

    def close_for_back_matter():
        """Pops every open section/boxed-text frame back to the document
        root - a Bibliography or standalone Reference zone is document-level
        back matter, never content that belongs nested inside whatever
        section happens to still be open from an earlier heading (spec: "if
        a previous section/body is open, close it before <back>"). Called
        instead of the normal non-heading placement whenever the zone about
        to be placed has a tag in BACK_MATTER_TAGS, so it always lands as a
        genuine TOP-LEVEL doc_tree child - xml_generator.py's <body>/<back>
        split only ever looks at top-level children, so this is what makes
        that split actually see it there instead of buried inside a <sec>."""
        del stack[1:]

    def push_single_zone(zid):
        """Places one zone at the current stack position, same logic as the
        non-chain branch below - factored out so a merge chain whose tag
        isn't text-combinable (see TEXT_MERGE_TAGS, or the active profile's
        own footnote_flow_tags - a footnote/endnote chain IS combinable,
        just via its own dedicated renderer rather than TEXT_MERGE_TAGS'
        generic one, see core.epub_xml_generator._gen_merged_footnote_zone)
        can fall back to placing each of its zones individually, still
        flagged/colored as merged but not flattened into one XML element."""
        zone = zone_manager.zones[zid]
        lvl = heading_level(zone.tag)
        if lvl > 0:
            while len(stack) > 1 and stack[-1][2] == "sec" and stack[-1][0] >= lvl:
                stack.pop()
            node = {"type": "sec", "level": lvl, "zone_id": zid, "children": []}
            stack[-1][1]["children"].append(node)
            stack.append((lvl, node, "sec"))
        else:
            if zone.tag in BACK_MATTER_TAGS:
                close_for_back_matter()
            stack[-1][1]["children"].append({"type": "zone", "zone_id": zid})

    for item in stream:
        if isinstance(item, dict):  # a Merge-Previous (or page-number-flanked) chain
            zone_ids = item["zone_ids"]
            first_tag = zone_manager.zones[zone_ids[0]].tag
            if first_tag not in TEXT_MERGE_TAGS and first_tag not in (footnote_flow_tags or ()):
                # No sensible "combined text" for this tag (figure, graphic,
                # equation, list, boxed-text, title-group, ...) - place each
                # zone in the chain normally/independently; the merge still
                # applies for display (yellow) and the data model, just not
                # for XML structure.
                for zid in zone_ids:
                    if zone_manager.zones[zid].tag != "pagenumber":
                        push_single_zone(zid)
                continue
            first_lvl = heading_level(first_tag)
            if first_lvl > 0:
                # A merged HEADING chain (e.g. Heading 2 + Heading 2 via Merge
                # Previous) must still open a real <sec> - not be flattened
                # into a bare element - just with combined title text from
                # every zone in the chain. See xml_generator._gen_sec.
                while len(stack) > 1 and stack[-1][2] == "sec" and stack[-1][0] >= first_lvl:
                    stack.pop()
                node = {"type": "sec", "level": first_lvl, "zone_id": zone_ids[0],
                        "merged_zone_ids": zone_ids, "children": []}
                stack[-1][1]["children"].append(node)
                stack.append((first_lvl, node, "sec"))
            else:
                if first_tag in BACK_MATTER_TAGS:
                    close_for_back_matter()
                stack[-1][1]["children"].append({"type": "merged_p", "zone_ids": zone_ids})
            continue
        zid = item
        zone = zone_manager.zones[zid]

        if zone.tag in start_to_pair:
            pair = start_to_pair[zone.tag]
            node = {"type": "boxed-text", "start_zone_id": zid, "end_zone_id": None, "children": [],
                    "marker_pair": pair}
            stack[-1][1]["children"].append(node)
            stack.append((None, node, ("boxed-text", pair)))
            continue

        if zone.tag in end_to_pair:
            pair = end_to_pair[zone.tag]
            idx = None
            for i in range(len(stack) - 1, 0, -1):
                kind = stack[i][2]
                if isinstance(kind, tuple) and kind[0] == "boxed-text" and kind[1] == pair:
                    idx = i
                    break
            if idx is None:
                issues.append(f"{pair[1]} on page {zone.page} ({zid}) has no matching {pair[0]}")
            else:
                stack[idx][1]["end_zone_id"] = zid
                del stack[idx:]  # closes it and anything nested inside it
            continue

        lvl = heading_level(zone.tag)
        if lvl > 0:
            # Only pop "sec" frames by level comparison - a heading appearing
            # while a boxed-text is open nests INSIDE it instead of closing it
            # (only the boxed-text's own End marker does that).
            while len(stack) > 1 and stack[-1][2] == "sec" and stack[-1][0] >= lvl:
                stack.pop()
            node = {"type": "sec", "level": lvl, "zone_id": zid, "children": []}
            stack[-1][1]["children"].append(node)
            stack.append((lvl, node, "sec"))
        else:
            if zone.tag in BACK_MATTER_TAGS:
                close_for_back_matter()
            node = {"type": "zone", "zone_id": zid}
            stack[-1][1]["children"].append(node)

    for level, node, kind in stack[1:]:
        if isinstance(kind, tuple) and kind[0] == "boxed-text":
            start_tag, end_tag = kind[1]
            start_zone = zone_manager.zones[node["start_zone_id"]]
            issues.append(f"{start_tag} on page {start_zone.page} ({node['start_zone_id']}) "
                           f"has no matching {end_tag}")

    return root, issues


def validate_document_tree(zone_manager, doc_tree: dict) -> list:
    """Read-only consistency check over the ASSEMBLED document tree returned
    by build_document_tree() - distinct in scope from
    ZoneManager.validate_hierarchy(), which checks the raw physical
    parent_id/children graph. This instead walks the actual tree structure
    XML generation consumes, and verifies every zone_id referenced by a tree
    node (i) exists and (ii) appears in the tree exactly once. A zone_id
    appearing twice would mean the same zone producing two top-level XML
    elements - exactly the "duplicate structural node" failure this file
    must never produce (see module docstring point 1: geometric children,
    i.e. anything with parent_id set, must never also get a second top-level
    node here). Never modifies doc_tree or any zone - only reports; returns
    a list of human-readable issue strings (empty = clean)."""
    issues = []
    seen = {}  # zone_id -> path of first occurrence, for a useful message

    def note(zid, where):
        if zid not in zone_manager.zones:
            issues.append(f"{where}: references nonexistent zone {zid!r}")
            return
        if zid in seen:
            issues.append(f"{where}: zone {zid!r} already appears at "
                           f"{seen[zid]!r} (duplicate structural node)")
        else:
            seen[zid] = where

    def walk(node, path):
        ntype = node.get("type")
        if ntype == "root":
            for i, child in enumerate(node["children"]):
                walk(child, f"root[{i}]")
        elif ntype == "sec":
            for zid in node.get("merged_zone_ids") or [node["zone_id"]]:
                note(zid, f"{path}/sec")
            for i, child in enumerate(node["children"]):
                walk(child, f"{path}/sec[{i}]")
        elif ntype == "boxed-text":
            note(node["start_zone_id"], f"{path}/boxed-text-start")
            if node.get("end_zone_id"):
                note(node["end_zone_id"], f"{path}/boxed-text-end")
            for i, child in enumerate(node["children"]):
                walk(child, f"{path}/boxed-text[{i}]")
        elif ntype == "merged_p":
            for zid in node["zone_ids"]:
                note(zid, f"{path}/merged_p")
        elif ntype == "zone":
            note(node["zone_id"], path)

    walk(doc_tree, "root")
    return issues
