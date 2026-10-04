"""Single-page, NO-REFERENCE-REQUIRED structural analysis - the engine
behind the "Auto Analyse" feature (gui/main_window.py's App.auto_analyse),
a separate capability from the existing reference-template-driven "Auto
Zone" (auto_zoning/auto_zone_engine.py). Deliberately reuses as much of
that existing package as already fits: pdf_block_detector (line/font
extraction, paragraph-block grouping), core.column_detector (band/column
geometry), list_detector (marker-run detection), and
auto_zone_engine._claim_caption/_figure_predicted_zone (figure+caption
matching, already template-independent). The only genuinely new pieces are
page_number_detector (header/footer exclusion), table_detector (table
region geometry), and heading_detector (self-relative font-size ranking) -
see those modules for the actual classification logic.

analyze_page() is PURE/READ-ONLY: it returns a tree of PredictedZone
objects (the exact same dataclass hierarchy_builder.create_page already
knows how to turn into real zones) plus summary counts, and never touches
a ZoneManager - gui/main_window.py shows the counts in a preview dialog
before anything is actually created."""
from dataclasses import dataclass, field

from core import column_detector
from auto_zoning import (pdf_block_detector, list_detector, page_number_detector, table_detector,
                          heading_detector, paragraph_auto_zone)
from auto_zoning.hierarchy_builder import PredictedZone
from auto_zoning.auto_zone_engine import _claim_caption, _figure_predicted_zone

AUTO_ANALYSE_CONFIDENCE = 95.0
TABLE_CAPTION_MAX_GAP = 24.0   # max vertical gap (pt) above a table for a text block to count as its caption


@dataclass
class PageAnalysisResult:
    predicted_zones: list = field(default_factory=list)   # top-level PredictedZone, in final reading order
    counts: dict = field(default_factory=dict)              # tag -> count, counted recursively through children


def _count_tags(pz, counts):
    counts[pz.tag] = counts.get(pz.tag, 0) + 1
    for child in pz.children:
        _count_tags(child, counts)


def _list_run_to_predicted(run):
    """Maps a list_detector.ListRun to a PredictedZone tree. A "simple"
    run (bullet glyphs, per xml_generator.py's own _LIST_MARKER_PATTERNS -
    the same character class the manual "List Bullet" tag's marker-
    stripping already expects) becomes a "list-bullet" CONTAINER with real
    "list-item" children - using the children-aware _build_bullet_list path
    (core/xml_generator.py) so real per-item text is generated, never one
    flattened item. number/alpha-upper/alpha-lower become tag "list" with
    the matching list_type attribute, identical to what the existing Auto
    Zone engine already produces for those - both are pre-existing,
    already-tested container conventions; nothing new in xml_generator.py
    is required for either.

    Recursive: an item with its own nested sub-list (list_detector.
    ListItemSpan.children, spec Part T - "must preserve nesting") gets that
    child ListRun mapped THROUGH THIS SAME FUNCTION and attached as the
    list-item zone's own PredictedZone.children - hierarchy_builder.
    create_page's own _create_zone_tree already recursively creates
    however-deep a PredictedZone tree unconditionally, so nesting to any
    depth needs no change there at all."""
    item_zones = [PredictedZone(bbox=item.bbox, tag="list-item", confidence=AUTO_ANALYSE_CONFIDENCE,
                                 children=[_list_run_to_predicted(child) for child in item.children])
                  for item in run.items]
    if run.list_type == "simple":
        return PredictedZone(bbox=run.bbox, tag="list-bullet", confidence=AUTO_ANALYSE_CONFIDENCE,
                              children=item_zones)
    return PredictedZone(bbox=run.bbox, tag="list", attributes={"list_type": run.list_type},
                          confidence=AUTO_ANALYSE_CONFIDENCE, children=item_zones)


def _classify_lines(lines, region_width, region_height):
    """Runs the shared band/column -> list-run -> block -> heading/
    paragraph pipeline over `lines` (already page-number/header/footer/
    table-caption-excluded, scoped either to the whole page or to one
    table's own interior) and returns a FLAT list of PredictedZone in
    final reading order: band order, then column order within a band
    (read the first column completely before the next - never a single
    global Y-sort across columns), then top-to-bottom within a column."""
    if not lines:
        return []
    bands = column_detector.detect_bands(region_width, region_height, lines)
    body_size = pdf_block_detector.page_body_font_size(lines)
    ordered = []
    for band in bands:
        for col_idx in range(band.columns):
            col_lines = [li for li in band.items
                         if column_detector.column_index_for_bbox(band, li.bbox) == col_idx]
            if not col_lines:
                continue
            list_runs = list_detector.detect_list_runs(col_lines)
            consumed = {id(li) for run in list_runs for item in run.items for li in item.lines}
            remaining = [li for li in col_lines if id(li) not in consumed]
            # paragraph_auto_zone.group_lines_into_paragraphs (spec: "EPUBForge -
            # Complete Semantic Auto-Zone" - "ONE PDF TEXT LINE != ONE PARAGRAPH"),
            # NOT pdf_block_detector.group_lines_into_blocks: that function groups
            # purely on font-style continuity + gap size, with NO indentation/
            # paragraph-spacing check at all, so several consecutive same-style
            # body paragraphs separated only by an ordinary line-wrap-sized gap
            # collapse into ONE giant block/zone there - confirmed the real
            # cause of Auto Analyse creating one bloated paragraph (or, sharing
            # the exact same underlying gap-only signal in the opposite
            # direction on some inputs, one zone per line) instead of correct
            # per-paragraph zones. group_lines_into_paragraphs returns the
            # SAME generic BlockInfo container (drop-in compatible with
            # heading_detector.classify_blocks below, which only reads a
            # block's own .font_size/.bold/.text/.bbox - nothing style-
            # grouping-specific), so a genuine heading/title (already visually
            # isolated by extra whitespace or its own distinct indentation)
            # still forms its own single-line paragraph block here and is
            # still correctly promoted to h1..h6 immediately afterward -
            # never any less precise than before, only correctly scoped to
            # real paragraph boundaries for ordinary body text.
            blocks = paragraph_auto_zone.group_lines_into_paragraphs(remaining) if remaining else []
            classified = heading_detector.classify_blocks(blocks, body_size) if blocks else []

            col_entries = [(run.bbox[1], _list_run_to_predicted(run)) for run in list_runs]
            col_entries.extend((block.bbox[1], PredictedZone(bbox=block.bbox, tag=tag,
                                                               confidence=AUTO_ANALYSE_CONFIDENCE))
                                for block, tag in classified)
            col_entries.sort(key=lambda t: t[0])   # top-to-bottom WITHIN this one column only
            ordered.extend(pz for _, pz in col_entries)
    return ordered


def _table_predicted_zone(table_bbox, all_lines):
    """Builds a "table" PredictedZone with real nested children (list-
    bullet>list-item / list+list-item / p), by re-running _classify_lines
    scoped to just this table's own interior lines and width/height -
    core.table_extractor.analyze_table is NEVER called here; it remains
    exclusively an XML-generation-time concern (unchanged), reading these
    same real child zones back out at Generate XML time exactly like any
    other manually-nested Table > List > List Item structure already
    does (see the previous session round's _match_table_cell_children
    fix)."""
    x0, y0, x1, y1 = table_bbox
    inside = [li for li in all_lines
              if x0 - 2 <= (li.bbox[0] + li.bbox[2]) / 2 <= x1 + 2
              and y0 - 2 <= (li.bbox[1] + li.bbox[3]) / 2 <= y1 + 2]
    children = _classify_lines(inside, x1 - x0, y1 - y0)
    return PredictedZone(bbox=table_bbox, tag="table", confidence=AUTO_ANALYSE_CONFIDENCE,
                          children=children), inside


def analyze_page(pdf_document, page_num: int) -> PageAnalysisResult:
    """THE single entry point Auto Analyse calls. Analyzes ONLY page_num -
    pdf_document.get_page(page_num) is called exactly once, and there is no
    loop over any other page anywhere in this module (unlike
    auto_zone_engine.run_auto_zone, which deliberately IS a whole-document
    loop for its own, different feature)."""
    page = pdf_document.get_page(page_num)
    page_width, page_height = pdf_document.page_size(page_num)
    all_lines = pdf_block_detector.detect_lines(page)

    # Header/footer running text is unconditionally excluded from every
    # other detector below - the ONLY thing ever extracted from that band
    # is, at most, one isolated page-number line.
    pagenumber_line, header_footer_ids = page_number_detector.detect(all_lines, page_width, page_height)
    body_lines = [li for li in all_lines if id(li) not in header_footer_ids]

    # Tables: detect regions, claim an immediately-preceding caption
    # (sibling, not child - see _table_predicted_zone's docstring), then
    # remove every line consumed by a table from the page-level pool so it
    # is never ALSO processed as ordinary page text.
    table_bboxes = table_detector.detect_tables(page, body_lines, page_height)
    remaining = list(body_lines)
    table_entries = []
    for tbbox in table_bboxes:
        cap_candidates = [li for li in remaining
                           if li.bbox[3] <= tbbox[1] and tbbox[1] - li.bbox[3] <= TABLE_CAPTION_MAX_GAP
                           and min(li.bbox[2], tbbox[2]) - max(li.bbox[0], tbbox[0]) > 0]
        cap_line = max(cap_candidates, key=lambda li: li.bbox[3]) if cap_candidates else None

        table_pz, inside_lines = _table_predicted_zone(tbbox, remaining)
        inside_ids = {id(li) for li in inside_lines}
        remaining = [li for li in remaining if id(li) not in inside_ids and li is not cap_line]

        if cap_line is not None:
            table_entries.append((cap_line.bbox[1],
                                   PredictedZone(bbox=cap_line.bbox, tag="table-caption",
                                                 confidence=AUTO_ANALYSE_CONFIDENCE)))
        table_entries.append((tbbox[1], table_pz))

    # Figures: an embedded image claims an immediately-following caption
    # block from the remaining page text, reusing the existing (already
    # template-independent) auto_zone_engine helpers directly rather than
    # a second copy of the same matching logic.
    images = pdf_block_detector.detect_images(page)
    figure_entries = []
    for img in images:
        blocks = pdf_block_detector.group_lines_into_blocks(remaining) if remaining else []
        cap_block, _ = _claim_caption(img, blocks)
        if cap_block is not None:
            remaining = [li for li in remaining if li not in cap_block.lines]
        figure_entries.append((img[1], _figure_predicted_zone(img, cap_block)))

    text_zones = _classify_lines(remaining, page_width, page_height)
    text_entries = [(pz.bbox[1], pz) for pz in text_zones]

    all_entries = table_entries + figure_entries + text_entries
    if pagenumber_line is not None:
        all_entries.append((pagenumber_line.bbox[1], PredictedZone(
            bbox=pagenumber_line.bbox, tag="pagenumber", confidence=AUTO_ANALYSE_CONFIDENCE)))
    all_entries.sort(key=lambda t: t[0])

    predicted_zones = [pz for _, pz in all_entries]
    counts = {}
    for pz in predicted_zones:
        _count_tags(pz, counts)
    return PageAnalysisResult(predicted_zones=predicted_zones, counts=counts)
