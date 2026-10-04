"""Top-level Auto Zone orchestrator: given a PDF (already open in the live
ZoneManager) and a reference LayoutTemplate, proposes zones for every page
that has NO existing zones yet - a page with even one manual zone is
skipped entirely (the single rule that guarantees "no duplicate zones" and
"never modifies existing manual zoning" without needing any merge/dedup
logic). Reuses zone_manager.add_zone for actual creation (ID generation,
undo, containment auto-parenting), core.validation for the overlap count,
and hierarchy_builder/reading_order_engine for orchestration - this module
contains no PDF-parsing or scoring logic of its own."""
from dataclasses import dataclass

from core import column_detector
from auto_zoning import pdf_block_detector, list_detector, paragraph_auto_zone
from auto_zoning import tag_predictor, confidence as confidence_mod
from auto_zoning import hierarchy_builder, reading_order_engine, overlap_detector
from auto_zoning.hierarchy_builder import PredictedZone
from auto_zoning.layout_template import compute_features
from core.column_detector import Band

LIST_DETECTION_CONFIDENCE = 92.0
# An embedded image found by pdf_block_detector.detect_images IS a figure
# region almost by definition in this app's domain - unlike text tags,
# there's no ambiguity to resolve via template scoring, so this is a fixed
# confidence (same pattern as LIST_DETECTION_CONFIDENCE) rather than a
# tag_predictor call.
IMAGE_DETECTION_CONFIDENCE = 90.0
CAPTION_MAX_GAP = 24.0    # max vertical gap (pt) below an image for a text block to count as its caption


@dataclass
class AutoZoneResult:
    zones_created: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    overlaps: int = 0
    pages_skipped: int = 0
    pages_processed: int = 0


def _predict_block(block, page_width, page_height, column_bounds, body_size, template):
    features = compute_features(
        bbox=block.bbox, page_width=page_width, page_height=page_height, column_bounds=column_bounds,
        text=block.text, font_size=block.font_size, bold=block.bold, italic=block.italic,
        body_font_size=body_size, list_marker=None)
    tag, conf = tag_predictor.predict_tag(features, template)
    return PredictedZone(bbox=block.bbox, tag=tag, confidence=conf)


def _claim_caption(image_bbox, blocks):
    """Looks for the first `blocks` entry directly below `image_bbox` (small
    gap, horizontally overlapping) - the figure's caption, if any. Removes
    it from `blocks` so it is never ALSO emitted as an ordinary paragraph
    candidate. Returns (caption_block_or_None, figure_bbox)."""
    for b in blocks:
        gap = b.bbox[1] - image_bbox[3]
        x_overlap = min(b.bbox[2], image_bbox[2]) - max(b.bbox[0], image_bbox[0])
        if 0 <= gap <= CAPTION_MAX_GAP and x_overlap > 0:
            blocks.remove(b)
            fig_bbox = (min(image_bbox[0], b.bbox[0]), image_bbox[1],
                        max(image_bbox[2], b.bbox[2]), b.bbox[3])
            return b, fig_bbox
    return None, image_bbox


def _list_run_to_predicted_zone(run):
    """Maps a list_detector.ListRun to a PredictedZone tree - this engine's
    own pre-existing tag convention (always "list" + a list_type attribute,
    regardless of marker kind) is left completely unchanged; only nesting
    support is added here (spec: "EPUBForge - Complete Semantic Auto-Zone"
    Part T). Recursive: an item's own nested sub-list (list_detector.
    ListItemSpan.children) maps through this SAME function and becomes that
    list-item's own PredictedZone.children - hierarchy_builder.create_page's
    _create_zone_tree already creates however-deep a PredictedZone tree
    unconditionally, so no change is needed there for nesting to work."""
    item_zones = [PredictedZone(bbox=item.bbox, tag="list-item", confidence=LIST_DETECTION_CONFIDENCE,
                                 children=[_list_run_to_predicted_zone(child) for child in item.children])
                  for item in run.items]
    return PredictedZone(bbox=run.bbox, tag="list", attributes={"list_type": run.list_type},
                          confidence=LIST_DETECTION_CONFIDENCE, children=item_zones)


def _figure_predicted_zone(image_bbox, caption_block):
    children = [PredictedZone(bbox=image_bbox, tag="graphic", confidence=IMAGE_DETECTION_CONFIDENCE)]
    fig_bbox = image_bbox
    if caption_block is not None:
        children.append(PredictedZone(bbox=caption_block.bbox, tag="caption", confidence=IMAGE_DETECTION_CONFIDENCE))
        fig_bbox = (min(image_bbox[0], caption_block.bbox[0]), image_bbox[1],
                    max(image_bbox[2], caption_block.bbox[2]), caption_block.bbox[3])
    return PredictedZone(bbox=fig_bbox, tag="figure", confidence=IMAGE_DETECTION_CONFIDENCE, children=children)


def _process_page(zone_manager, pdf_document, page_num, template, thresholds) -> tuple:
    """Returns (created_top_level_zone_ids, confidence_counts) for one page.
    Does NOT touch reading order itself - see order_page, called by the
    caller once every zone_id for the page is known."""
    page = pdf_document.get_page(page_num)
    page_width, page_height = pdf_document.page_size(page_num)
    lines = pdf_block_detector.detect_lines(page)
    images = pdf_block_detector.detect_images(page)
    counts = {"high": 0, "medium": 0, "low": 0}
    if not lines and not images:
        return [], counts
    body_size = pdf_block_detector.page_body_font_size(lines) if lines else 0.0
    bands = column_detector.detect_bands(page_width, page_height, lines) if lines else \
        [Band(y0=0.0, y1=page_height, columns=1, column_bounds=[(0.0, page_width)])]

    remaining_images = list(images)
    predicted = []   # [PredictedZone, ...] in final page/band/column creation order
    for band in bands:
        # Each column of this band is processed independently, top-to-
        # bottom within the column, left column fully before the next -
        # the spec's "read the first column completely before moving to
        # the next column" rule. A full-width band always has exactly one
        # "column" (band.columns == 1).
        for col_idx in range(band.columns):
            col_bounds = band.column_bounds[col_idx]
            col_lines = [li for li in band.items
                         if column_detector.column_index_for_bbox(band, li.bbox) == col_idx]

            list_runs = list_detector.detect_list_runs(col_lines) if col_lines else []
            consumed = set()
            for run in list_runs:
                for item in run.items:
                    for li in item.lines:
                        consumed.add(id(li))
            remaining_lines = [li for li in col_lines if id(li) not in consumed]
            # paragraph_auto_zone.group_lines_into_paragraphs (spec: "EPUBForge
            # - Complete Semantic Auto-Zone" - "ONE PDF TEXT LINE != ONE
            # PARAGRAPH") - see auto_zoning/page_analyzer.py's own identical
            # swap for the full rationale: pdf_block_detector.group_lines_
            # into_blocks groups purely on font-style continuity + gap size,
            # with no indentation/paragraph-spacing signal, which is what
            # produced the reported "one zone per PDF text line" (or,
            # conversely, one bloated multi-paragraph zone) Auto Zone output.
            # Returns the SAME BlockInfo container _predict_block below
            # already expects - a drop-in swap, not a new data shape.
            blocks = paragraph_auto_zone.group_lines_into_paragraphs(remaining_lines) if remaining_lines else []

            # An image whose CENTER falls inside this band/column claims any
            # caption block directly below it from THIS column's own blocks
            # (see _claim_caption) before those blocks are otherwise turned
            # into ordinary paragraph/heading candidates.
            col_images = [img for img in remaining_images
                          if band.y0 - 2 <= (img[1] + img[3]) / 2 <= band.y1 + 2
                          and column_detector.column_index_for_bbox(band, img) == col_idx]
            figure_pzs = []
            for img in col_images:
                remaining_images.remove(img)
                caption_block, _fig_bbox = _claim_caption(img, blocks)
                figure_pzs.append(_figure_predicted_zone(img, caption_block))

            # Merge list runs, figures, and paragraph/heading blocks back
            # into ONE top-to-bottom sequence for this column, by each
            # entry's own top Y - a list region interrupted by ordinary
            # paragraphs (list_detector already breaks the run there)
            # naturally ends up as a separate "list" PredictedZone here
            # too, never merged into one.
            entries = []
            for run in list_runs:
                entries.append((run.bbox[1], _list_run_to_predicted_zone(run)))
            for fig_pz in figure_pzs:
                entries.append((fig_pz.bbox[1], fig_pz))
            for block in blocks:
                entries.append((block.bbox[1], _predict_block(block, page_width, page_height,
                                                                col_bounds, body_size, template)))
            entries.sort(key=lambda t: t[0])
            predicted.extend(pz for _, pz in entries)

    # Any image that never matched a band/column (e.g. it straddles the
    # gutter, or the page has no text lines at all so `bands` is a single
    # synthetic full-page band with no lines to claim a caption from) is
    # still placed - appended at the end rather than silently dropped, since
    # there is no more reliable position to insert it at among the other
    # already-ordered entries.
    for img in remaining_images:
        predicted.append(_figure_predicted_zone(img, None))

    created_ids = hierarchy_builder.create_page(zone_manager, page_num, predicted)
    for pz in predicted:
        counts[confidence_mod.bucket(pz.confidence, thresholds)] += 1
        for child in pz.children:
            counts[confidence_mod.bucket(child.confidence, thresholds)] += 1
    reading_order_engine.order_page(zone_manager, page_num, created_ids)
    return created_ids, counts


def run_auto_zone(pdf_document, zone_manager, template, settings=None) -> AutoZoneResult:
    """The single entry point the GUI (App.auto_zone) calls. `template`:
    a merged layout_template.LayoutTemplate (see auto_zoning.layout_template.
    merge_templates) built from every currently-loaded reference project.
    `settings`: the live project's own settings dict (for
    auto_zone_thresholds) - never required, defaults apply if omitted."""
    settings = settings or {}
    thresholds = settings.get("auto_zone_thresholds") or confidence_mod.DEFAULT_THRESHOLDS
    result = AutoZoneResult()
    for page_num in range(1, pdf_document.page_count + 1):
        if zone_manager.zones_on_page(page_num):
            result.pages_skipped += 1
            continue
        before = len(zone_manager.zones)
        _created_ids, counts = _process_page(zone_manager, pdf_document, page_num, template, thresholds)
        result.pages_processed += 1
        result.zones_created += len(zone_manager.zones) - before
        result.high += counts["high"]
        result.medium += counts["medium"]
        result.low += counts["low"]
    result.overlaps = len(overlap_detector.check(zone_manager))
    return result
